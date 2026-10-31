"""Writing messages: inside a unit of work, or on their own (``enqueue``).

On PostgreSQL, the transaction that writes messages due at once also
notifies the workers (``NOTIFY domainkit_messages``), so an idle worker
starts them right after the commit instead of on its next poll. The row
stays the record: a notification is only a hint, sent when the transaction
commits and lost harmlessly when no worker listens.

A message with an ordering key takes its key's turn first, and is written
``held`` when an earlier message of the key still waits (``ordering``).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import BindParameter, Insert, func, insert, literal, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import UTCDateTime, connections
from domainkit.db.hooks import on_stage_transaction, on_transaction_committed
from domainkit.ddd import DeferredTask, DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.reporting import report_error
from domainkit.tracing import trace_headers

from . import dbclock
from .registry import (
    CHAIN,
    Chain,
    Task,
    checked_key,
    dump_arguments,
    dump_event,
    event_key,
    qualified_name,
    spec_for,
    task_key,
)
from .settings import MessagingSettings
from .tables import DEFAULT_QUEUE, PENDING, messages

logger = logging.getLogger(__name__)

CHANNEL: Final = "domainkit_messages"
"""The PostgreSQL notification channel; each payload is a queue's name."""


def _now() -> datetime:
    """Return the current time in UTC."""
    return clock.now()


def _utc(moment: datetime) -> BindParameter[datetime]:
    """``moment`` as a bound value of the outbox's time columns."""
    return literal(moment, UTCDateTime())


def notifies(config: MessagingSettings) -> bool:
    """Report whether commits notify workers (on PostgreSQL) under ``config``.

    ``NOTIFY`` turns it on. With a broker that publishes on commit, the
    broker wakes the workers instead, so nothing is sent.
    """
    return config.NOTIFY and not (
        config.BROKER is not None and config.PUBLISH_ON_COMMIT
    )


type Watcher = Callable[[str, Sequence[Mapping[str, Any]]], Awaitable[None]]
"""Called with each outbox and the rows just committed to it."""

watchers: list[Watcher] = []
"""Called after messages commit (``domainkit.contrib.messaging.testing``)."""


def context_headers(**extra: object) -> dict[str, Any]:
    """Carry the request id, actor, and trace context to the worker.

    The worker logs and audits under the request id and actor, and continues
    the trace of the span that was active (the commit's, in a unit of work).
    """
    request = current_request()
    return {
        "request_id": request.request_id if request else None,
        "actor": request.actor if request else None,
        **trace_headers(),
        **extra,
    }


def message_row(
    *,
    kind: str,
    target: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, Any],
    max_attempts: int,
    queue: str = DEFAULT_QUEUE,
    delay: timedelta | None = None,
    dedupe_key: str | None = None,
    available_at: datetime | None = None,
    priority: int = 0,
    ordering_key: str | None = None,
) -> dict[str, Any]:
    """Build one pending outbox row (``write`` holds a keyed one back if it must)."""
    now = _now()
    return {
        "id": uuid.uuid4(),
        "kind": kind,
        "target": target,
        "queue": queue,
        "priority": priority,
        "payload": dict(payload),
        "headers": dict(headers),
        "status": PENDING,
        "attempts": 0,
        "max_attempts": max_attempts,
        "available_at": available_at or now + (delay or timedelta(0)),
        "created_at": now,
        "dedupe_key": dedupe_key,
        "ordering_key": ordering_key,
    }


def rows_for(
    events: Sequence[DomainEvent],
    tasks: Sequence[DeferredTask],
    bus: EventBus,
    config: MessagingSettings,
) -> list[dict[str, Any]]:
    """Turn committed events (one row per durable handler) and tasks into rows.

    A row's ordering key is the one ``defer`` was given, or the one its task
    or handler declares (``ordered``); a chain's, its first step's.
    """
    rows = [
        message_row(
            kind="event",
            target=qualified_name(handler),
            payload=dump_event(event),
            headers=context_headers(event_type=qualified_name(type(event))),
            max_attempts=config.MAX_ATTEMPTS,
            ordering_key=event_key(handler, event),
        )
        for event in events
        for handler in bus.handlers_for(type(event), durable=True)
    ]
    for deferred in tasks:
        function: object = deferred.function
        rest: list[str] = []
        if isinstance(function, Chain):
            rest = function.keys[1:]
            spec = spec_for(function.steps[0])
        else:
            spec = spec_for(deferred.function)
        key = checked_key(deferred.ordering_key)
        rows.append(
            message_row(
                kind="task",
                target=spec.key,
                payload=dump_arguments(deferred.kwargs),
                headers=context_headers(**({CHAIN: rest} if rest else {})),
                max_attempts=spec.max_attempts or config.MAX_ATTEMPTS,
                queue=spec.queue,
                delay=deferred.delay,
                dedupe_key=deferred.dedupe_key,
                priority=spec.priority,
                ordering_key=key or task_key(spec.function, deferred.kwargs),
            )
        )
    return rows


def _on_database_clock(
    dialect: str, rows: Sequence[dict[str, Any]]
) -> Sequence[dict[str, Any]]:
    """``rows``, those due at once made due by the database's clock where it decides.

    A writer whose clock runs ahead does not make them wait out the
    difference (``dbclock``).
    """
    if not dbclock.uses_database(dialect):
        return rows
    now = dbclock.now(dialect)
    return [
        {**row, "available_at": func.least(_utc(row["available_at"]), now)}
        if row["available_at"] <= row["created_at"]
        else row
        for row in rows
    ]


def _deduplicating(dialect: str) -> Any:  # noqa: ANN401 - the dialect's insert.
    """The insert of ``dialect`` that can skip rows whose ``dedupe_key`` exists."""
    if dialect == "postgresql":
        return postgresql.insert(messages)
    if dialect == "sqlite":
        return sqlite.insert(messages)
    raise ImproperlyConfigured(
        f"Deduplicated messages need PostgreSQL or SQLite, not {dialect}."
    )


def insert_statements(dialect: str, rows: Sequence[dict[str, Any]]) -> list[Insert]:
    """Build inserts of rows without an ordering key (``write`` adds the others).

    Rows with a ``dedupe_key`` skip keys that already exist. Where the
    database's clock decides (``dbclock``), a message due at once is due by
    that clock too.
    """
    rows = _on_database_clock(dialect, rows)
    plain = [row for row in rows if row["dedupe_key"] is None]
    deduped = [row for row in rows if row["dedupe_key"] is not None]
    statements: list[Insert] = [insert(messages).values(plain)] if plain else []
    if deduped:
        statements.append(
            _deduplicating(dialect)
            .values(deduped)
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
    return statements


async def _ordered_statements(
    executor: AsyncSession | AsyncConnection,
    dialect: str,
    rows: Sequence[dict[str, Any]],
) -> list[Insert]:
    """Take the turns of the keys of ``rows``; build one insert per row, in order.

    Each row gets the next sequence of its key, and is held if an earlier
    message of the key still waits, this transaction's included: each
    insert sees the ones before it.
    """
    if not rows:
        return []
    from . import ordering  # Loaded by the first commit of a key (PERF-15).

    sequences = await ordering.take_turns(executor, dialect, ordering.counts_of(rows))
    statements: list[Insert] = []
    for row in _on_database_clock(dialect, rows):
        key = row["ordering_key"]
        values = {
            **row,
            "sequence": sequences[key],
            "status": ordering.first_status(key),
        }
        sequences[key] += 1
        if row["dedupe_key"] is None:
            statements.append(insert(messages).values(values))
        else:
            statements.append(
                _deduplicating(dialect)
                .values(values)
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
            )
    return statements


async def write(
    executor: AsyncSession | AsyncConnection,
    dialect: str,
    rows: Sequence[dict[str, Any]],
) -> None:
    """Insert ``rows`` through an async session or connection.

    Rows without an ordering key go first; then, for those with one, each
    key's turn is taken (``ordering``) and they go one at a time.

    On PostgreSQL, also notify the queues of the rows due at once, when
    ``notifies`` says so; the notification goes out with the commit. Only
    rows the inserts return pending count: one skipped for its
    ``dedupe_key`` (a periodic slot every worker queues, say), or held
    behind an earlier message of its key, wakes no worker.
    """
    statements = insert_statements(
        dialect, [row for row in rows if row.get("ordering_key") is None]
    )
    statements += await _ordered_statements(
        executor, dialect, [row for row in rows if row.get("ordering_key") is not None]
    )
    if dialect != "postgresql" or not notifies(registry.settings(MessagingSettings)):
        for statement in statements:
            await executor.execute(statement)
        return
    inserted: set[uuid.UUID] = set()
    for statement in statements:
        result = await executor.execute(
            statement.returning(messages.c.id, messages.c.status)
        )
        inserted.update(id_ for id_, status in result.all() if status == PENDING)
    due = {
        row["queue"]
        for row in rows
        if row["id"] in inserted and row["available_at"] <= row["created_at"]
    }
    if due:
        notify = (func.pg_notify(CHANNEL, queue) for queue in sorted(due))
        await executor.execute(select(*notify))


async def enqueue(
    function: Task,
    /,
    *,
    delay: timedelta | None = None,
    dedupe_key: str | None = None,
    ordering_key: str | None = None,
    **kwargs: object,
) -> None:
    """Queue ``function(**kwargs)`` now, in its own transaction.

    ``ordering_key`` (or the key the task declares, ``ordered``) runs it
    after the messages of its key committed before, one at a time.

    Inside a use case prefer ``uow.defer``: the task is then stored only if
    the business change commits. Installed domains join the transaction as
    they join a unit of work's (``on_stage_transaction`` and
    ``on_transaction_committed``, ``domainkit.db.hooks``): inside a request with an
    ``Idempotency-Key``, the queued task holds the key as a commit does,
    so a retry does not queue it again.
    """
    config = registry.settings(MessagingSettings)
    task = DeferredTask(function, kwargs, delay, dedupe_key, ordering_key)
    rows = rows_for((), [task], EventBus(), config)
    async with connections.engine(config.DATABASE).begin() as connection:
        await write(connection, connection.dialect.name, rows)
        for staged in on_stage_transaction:
            await staged.call(connection, config.DATABASE)
    for hook in on_transaction_committed:
        try:
            await hook.call(config.DATABASE)
        except Exception as exc:  # The task is queued: report, carry on.
            logger.exception("After-commit work of %r failed", hook.domain)
            report_error(exc, domain=hook.domain)
    await notify_committed(config, config.DATABASE, rows)


async def notify_committed(
    config: MessagingSettings,
    database: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    publish: bool = True,
) -> None:
    """Follow up on ``rows``, just committed to ``database``'s outbox.

    They go to the background publisher (with a broker, unless not
    ``publish``), then to the ``watchers``. Without a broker, the
    dispatcher, and the worker it imports, are never loaded (PERF-15).
    """
    if publish and config.BROKER is not None:
        from .dispatch import publish_committed  # Imports this module.

        publish_committed(config, database, [row["id"] for row in rows])
    for watcher in watchers[:]:  # A watcher may remove itself.
        await watcher(database, rows)
