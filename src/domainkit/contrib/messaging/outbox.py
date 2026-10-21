"""Writing messages: inside a unit of work, or on their own (``enqueue``).

On PostgreSQL, the transaction that writes messages due at once also
notifies the workers (``NOTIFY domainkit_messages``), so an idle worker
starts them right after the commit instead of on its next poll. The row
stays the record: a notification is only a hint, sent when the transaction
commits and lost harmlessly when no worker listens.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import BindParameter, Executable, func, insert, literal, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import UTCDateTime, connections
from domainkit.ddd import DeferredTask, DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tracing import trace_headers

from . import dbclock
from .registry import (
    CHAIN,
    Chain,
    Task,
    dump_arguments,
    dump_event,
    qualified_name,
    spec_for,
)
from .settings import MessagingSettings
from .tables import DEFAULT_QUEUE, PENDING, messages

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
) -> dict[str, Any]:
    """Build one pending outbox row."""
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
    }


def rows_for(
    events: Sequence[DomainEvent],
    tasks: Sequence[DeferredTask],
    bus: EventBus,
    config: MessagingSettings,
) -> list[dict[str, Any]]:
    """Turn committed events (one row per durable handler) and tasks into rows."""
    rows = [
        message_row(
            kind="event",
            target=qualified_name(handler),
            payload=dump_event(event),
            headers=context_headers(event_type=qualified_name(type(event))),
            max_attempts=config.MAX_ATTEMPTS,
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
            )
        )
    return rows


def insert_statements(dialect: str, rows: Sequence[dict[str, Any]]) -> list[Executable]:
    """Build inserts; rows with a ``dedupe_key`` skip keys that already exist.

    Where the database's clock decides (``dbclock``), a message due at once
    is due by that clock too: a writer whose clock runs ahead does not make
    it wait out the difference.
    """
    if dbclock.uses_database(dialect):
        now = dbclock.now(dialect)
        rows = [
            {**row, "available_at": func.least(_utc(row["available_at"]), now)}
            if row["available_at"] <= row["created_at"]
            else row
            for row in rows
        ]
    plain = [row for row in rows if row["dedupe_key"] is None]
    keyed = [row for row in rows if row["dedupe_key"] is not None]
    statements: list[Executable] = [insert(messages).values(plain)] if plain else []
    if keyed:
        if dialect == "postgresql":
            statement: Any = postgresql.insert(messages)
        elif dialect == "sqlite":
            statement = sqlite.insert(messages)
        else:
            raise ImproperlyConfigured(
                f"Deduplicated messages need PostgreSQL or SQLite, not {dialect}."
            )
        statements.append(
            statement.values(keyed).on_conflict_do_nothing(
                index_elements=["dedupe_key"]
            )
        )
    return statements


async def write(
    executor: AsyncSession | AsyncConnection,
    dialect: str,
    rows: Sequence[dict[str, Any]],
) -> None:
    """Insert ``rows`` through an async session or connection.

    On PostgreSQL, also notify the queues of the rows due at once, when
    ``notifies`` says so; the notification goes out with the commit.
    """
    for statement in insert_statements(dialect, rows):
        await executor.execute(statement)
    if dialect != "postgresql":
        return
    if not notifies(registry.settings(MessagingSettings)):
        return
    due = {row["queue"] for row in rows if row["available_at"] <= row["created_at"]}
    if due:
        notify = (func.pg_notify(CHANNEL, queue) for queue in sorted(due))
        await executor.execute(select(*notify))


async def enqueue(
    function: Task,
    /,
    *,
    delay: timedelta | None = None,
    dedupe_key: str | None = None,
    **kwargs: object,
) -> None:
    """Queue ``function(**kwargs)`` now, in its own transaction.

    Inside a use case prefer ``uow.defer``: the task is then stored only if
    the business change commits.
    """
    config = registry.settings(MessagingSettings)
    task = DeferredTask(function, kwargs, delay, dedupe_key)
    rows = rows_for((), [task], EventBus(), config)
    async with connections.engine(config.DATABASE).begin() as connection:
        await write(connection, connection.dialect.name, rows)
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
    ``publish``), then to the ``watchers``.
    """
    from .dispatch import publish_committed  # Imports this module.

    if publish:
        publish_committed(config, database, [row["id"] for row in rows])
    for watcher in watchers[:]:  # A watcher may remove itself.
        await watcher(database, rows)
