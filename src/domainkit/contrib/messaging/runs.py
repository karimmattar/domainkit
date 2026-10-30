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
  database before it calls the handler at all. A row lives as long as its
  message may run: workers delete it once the message is done or
  cancelled, never while it is pending (held back by its rate, waiting on a
  broker or a paused queue) or dead (an operator may retry it).

So a handler marked ``inbox`` commits its database work once, however often
its message is delivered. What it does outside the database (a mail, a call
to another service) may still happen twice: before its commit, nothing tells
the runs apart.
"""

import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakSet

from sqlalchemy import (
    ColumnElement,
    Delete,
    Exists,
    Insert,
    Select,
    Tuple,
    Update,
    delete,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.exceptions import DomainkitError, ImproperlyConfigured

from . import dbclock
from .tables import DEAD, HELD, PENDING, messages
from .tables import inbox as inbox_table

if TYPE_CHECKING:
    from domainkit.db import SqlAlchemyUnitOfWork


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
        "outbox": run.database,
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


KEPT: Final = (PENDING, HELD, DEAD)
"""The statuses of messages that may still run: their inbox rows stay.

A pending message may be waiting, held back by its rate, on its way
through a broker, or running; a held one waits for an earlier message of
its ordering key; an operator may retry a dead one.
"""


def may_run_again(message_id: ColumnElement[uuid.UUID]) -> Exists:
    """Whether this database's outbox holds ``message_id`` pending or dead."""
    return (
        select(messages.c.id)
        .where(messages.c.id == message_id, messages.c.status.in_(KEPT))
        .exists()
    )


def prune_statement(outbox: str, size: int) -> Delete:
    """The ``DELETE`` of up to ``size`` rows of ``outbox``'s messages that finished.

    For rows here whose message is in this database's outbox (``outbox``
    names it): one statement, which keeps the rows of messages that may
    still run. Rows other workers delete are skipped on PostgreSQL.
    """
    finished = (
        select(inbox_table.c.target, inbox_table.c.message_id)
        .where(
            inbox_table.c.outbox == outbox,
            ~may_run_again(inbox_table.c.message_id),
        )
        .limit(literal(size, literal_execute=True))
        .with_for_update(skip_locked=True)
    )
    return delete(inbox_table).where(_key().in_(finished))


def rows_of(outbox: str, after: tuple[str, uuid.UUID] | None, size: int) -> Select[Any]:
    """Up to ``size`` rows here of ``outbox``'s messages, in key order, after ``after``.

    Their messages are in another database's outbox, which says which
    finished (``unfinished``).
    """
    rows = (
        select(inbox_table.c.target, inbox_table.c.message_id)
        .where(inbox_table.c.outbox == outbox)
        .order_by(inbox_table.c.target, inbox_table.c.message_id)
        .limit(size)
    )
    return rows if after is None else rows.where(_key() > tuple_(*after))


def unfinished(ids: Sequence[uuid.UUID]) -> Select[Any]:
    """The ids, of ``ids``, of messages this database's outbox may still run."""
    return select(messages.c.id).where(
        messages.c.id.in_(ids), messages.c.status.in_(KEPT)
    )


def forget(keys: Sequence[tuple[str, uuid.UUID]]) -> Delete:
    """Delete the rows ``keys``, each ``(target, message_id)``."""
    return delete(inbox_table).where(_key().in_(keys))


def _key() -> Tuple:
    """An inbox row's key, ``(target, message_id)``."""
    return tuple_(inbox_table.c.target, inbox_table.c.message_id)
