"""Writing messages: inside a unit of work, or on their own (``enqueue``)."""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Executable, insert
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.db import connections
from domainkit.ddd import DeferredTask, DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.tracing import trace_headers

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


def _now() -> datetime:
    """Return the current time in UTC."""
    return clock.now()


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
    """Build inserts; rows with a ``dedupe_key`` skip keys that already exist."""
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
    """Insert ``rows`` through an async session or connection."""
    for statement in insert_statements(dialect, rows):
        await executor.execute(statement)


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
