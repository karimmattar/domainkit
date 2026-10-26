"""The run a worker makes of a message: its lease fence, and its inbox row.

A worker holds a message under a lease: a lock id, and a time
(``VISIBILITY_TIMEOUT``) after which another worker may claim it again.
Delivery is at least once, so a message may run twice:

- a worker that dies, or loses its database, after the handler committed
  but before it recorded the message as done leaves it to run again once
  the lease ends;
- a handler that outlives its lease (its event loop blocked, its process
  paused, its cancellation ignored) runs on while another worker runs the
  message again.

While a worker runs a message, each unit of work the handler commits joins
the run, in its own transaction (``on_stage``):

- **The fence.** A commit on the database of the message's outbox first
  renews the lease, only while this run still holds it (its lock id). If
  another worker took the message over, the commit raises ``LeaseLost`` and
  rolls back. The message's row stays locked until the commit ends, so no
  worker takes the message over meanwhile.
- **The inbox.** For a handler or task marked ``inbox``, the run's first
  commit also writes ``(target, message id)`` to ``domainkit_inbox``, on
  the database it commits to. If the row is there already, a run of this
  message committed before: the commit raises ``AlreadyHandled`` and rolls
  back, and the worker records the message as done. A run that may repeat
  an earlier one (not its first attempt) looks for the row in the outbox's
  database before it calls the handler at all.

So a handler marked ``inbox`` commits its database work once, however often
its message is delivered. What it does outside the database (a mail, a call
to another service) may still happen twice: before its commit, nothing tells
the runs apart.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakSet

from sqlalchemy import (
    DateTime,
    Delete,
    Insert,
    Update,
    delete,
    func,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.exceptions import DomainkitError, ImproperlyConfigured

from . import dbclock
from .tables import inbox as inbox_table
from .tables import messages

if TYPE_CHECKING:
    from domainkit.db import SqlAlchemyUnitOfWork

    from .settings import MessagingSettings


class LeaseLost(RuntimeError, DomainkitError):
    """Another worker took the message over; this run's commit is refused.

    The run outlived its lease (an event loop blocked, a process paused, a
    cancellation ignored), and another worker claimed the message again.
    Let it propagate: the worker that holds the message now runs it.
    """


class AlreadyHandled(RuntimeError, DomainkitError):
    """The inbox holds this message: an earlier run committed its work.

    Let it propagate: the worker records the message as done.
    """


_inboxed: Final[WeakSet[Callable[..., Any]]] = WeakSet()
"""The functions marked ``inbox`` (methods by their function)."""


def inbox[F: Callable[..., Any]](function: F, /) -> F:
    """Commit a handler's or task's database work once per message.

    Mark a durable event handler, or a task, whose effects are in the
    database::

        @inbox
        async def credit_account(event: PaymentReceived) -> None:
            async with LedgerUnitOfWork() as uow:
                ...
                await uow.commit()

        event_bus.subscribe(PaymentReceived, credit_account, durable=True)

    Each message's run writes ``(target, message id)`` to the inbox in the
    transaction of its first commit; a later run of the same message is
    skipped, or its commit refused (``AlreadyHandled``). Keep the work in
    one unit of work: a run whose first commit landed is skipped whole.
    Order does not matter with ``@task``. In a ``chain``, only the last
    step may be marked: the others hand their result on.
    """
    _inboxed.add(getattr(function, "__func__", function))
    return function


def uses_inbox(function: object) -> bool:
    """Whether ``function`` (or the function of a method) is marked ``inbox``."""
    return getattr(function, "__func__", function) in _inboxed


@dataclass(slots=True, eq=False)
class Run:
    """A worker's run of one message, which the handler's commits join."""

    message_id: uuid.UUID
    target: str
    lock_id: uuid.UUID
    """The lease this run holds the message under."""
    database: str
    """The alias of the outbox that holds the message."""
    lease: float
    """Seconds a renewed lease lasts (``VISIBILITY_TIMEOUT``)."""
    inbox: bool = False
    """Commits write the inbox row (the target is marked ``inbox``)."""
    recorded: bool = False
    """A commit wrote the inbox row: later commits of the run write none."""
    lost: bool = False
    """A commit found the message held by another worker (``LeaseLost``)."""
    handled: bool = False
    """The inbox held the message already (``AlreadyHandled``)."""
    ended: bool = False
    staged: WeakSet["SqlAlchemyUnitOfWork"] = field(default_factory=WeakSet)
    """Units of work that wrote the inbox row and have not committed yet."""


_running: ContextVar[Run | None] = ContextVar("domainkit_message_run", default=None)


@contextmanager
def running(run: Run) -> Iterator[Run]:
    """Make ``run`` the one that commits in the block join; end it after."""
    token = _running.set(run)
    try:
        yield run
    finally:
        run.ended = True  # Tasks the handler started keep the context.
        _running.reset(token)


def current() -> Run | None:
    """The run of the message being processed here, until it ends."""
    run = _running.get()
    return None if run is None or run.ended else run


async def stage(uow: "SqlAlchemyUnitOfWork") -> None:
    """Join ``uow``'s commit to the run of the message being processed, if any.

    On the outbox's database the lease is renewed first; for an ``inbox``
    target, the run's first commit writes its inbox row.

    Raises:
        LeaseLost: another worker holds the message now.
        AlreadyHandled: the inbox holds the message: a run committed before.
    """
    run = current()
    if run is None:
        return
    connection = await uow.session.connection()
    dialect = connection.dialect.name
    if uow.alias == run.database:
        renewed = await connection.execute(renew(run, dialect))
        if not renewed.rowcount:
            run.lost = True
            raise LeaseLost(
                f"Message {run.message_id} ({run.target}) was taken over by "
                "another worker; this run's commit is refused."
            )
    if run.inbox and not run.recorded:
        written = await connection.execute(record(run, dialect))
        if not written.rowcount:
            run.handled = True
            raise AlreadyHandled(
                f"Message {run.message_id} ({run.target}) was handled already."
            )
        run.staged.add(uow)


def committed(uow: "SqlAlchemyUnitOfWork") -> None:
    """Note that ``uow`` committed: if it wrote the run's inbox row, it is kept."""
    run = current()
    if run is not None and uow in run.staged:
        run.recorded = True


def renew(run: Run, dialect: str) -> Update:
    """Extend ``run``'s lease by ``lease`` seconds, if it still holds it."""
    return (
        update(messages)
        .where(messages.c.id == run.message_id, messages.c.lock_id == run.lock_id)
        .values(locked_until=dbclock.at(dialect, run.lease))
    )


def record(run: Run, dialect: str) -> Insert:
    """Write ``run``'s inbox row, unless it is there already (no row changes)."""
    if dialect == "postgresql":
        statement: Any = postgresql.insert(inbox_table)
    elif dialect == "sqlite":
        statement = sqlite.insert(inbox_table)
    else:
        raise ImproperlyConfigured(
            f"The inbox needs PostgreSQL or SQLite, not {dialect}."
        )
    values = {
        "target": run.target,
        "message_id": run.message_id,
        "handled_at": dbclock.now(dialect),
    }
    inserted: Insert = statement.values(values).on_conflict_do_nothing(
        index_elements=[inbox_table.c.target, inbox_table.c.message_id]
    )
    return inserted


async def was_handled(connection: AsyncConnection, run: Run) -> bool:
    """Whether the inbox of ``connection``'s database holds ``run``'s message."""
    found = await connection.scalar(
        select(inbox_table.c.handled_at).where(
            inbox_table.c.target == run.target,
            inbox_table.c.message_id == run.message_id,
        )
    )
    return found is not None


def prune_statement(dialect: str, retention: float, size: int) -> Delete:
    """The ``DELETE`` of up to ``size`` inbox rows older than ``retention`` seconds.

    As the outbox's cleanup does, the retention and the size are written
    into the SQL, so a prepared statement's plan uses ``handled_at``'s index
    (PERF-02), and rows other workers delete are skipped on PostgreSQL.
    """
    cutoff: dbclock.Moment
    if dbclock.uses_database(dialect):
        cutoff = func.statement_timestamp(type_=DateTime(timezone=True)) + literal(
            timedelta(seconds=-retention), literal_execute=True
        )
    else:
        cutoff = dbclock.at(dialect, -retention)
    old = (
        select(inbox_table.c.target, inbox_table.c.message_id)
        .where(inbox_table.c.handled_at < cutoff)
        .limit(literal(size, literal_execute=True))
        .with_for_update(skip_locked=True)
    )
    key = tuple_(inbox_table.c.target, inbox_table.c.message_id)
    return delete(inbox_table).where(key.in_(old))


def redelivery_window(
    config: "MessagingSettings", max_attempts: int, longest_delay: float
) -> float:
    """The longest a message may still run again after a run that committed.

    Each later attempt waits up to ``longest_delay`` (its backoff's cap)
    to come due, holds its lease up to ``VISIBILITY_TIMEOUT``, and, with a
    broker, may wait ``REPUBLISH_AFTER`` for a lost publication to go again.
    """
    republish = config.REPUBLISH_AFTER if config.BROKER is not None else 0.0
    return max_attempts * (config.VISIBILITY_TIMEOUT + longest_delay + republish)
