"""Operator queries: queue depth, lag, listings, retries, and cancelling.

A pending message is *waiting* until a worker starts it, *running* while a
worker holds it (``started_at`` and a live lock), and back to waiting if it
fails and is retried. With a broker, a waiting message may also be
*published*: locked by the relay while it travels to a worker.
"""

import uuid
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    false,
    func,
    not_,
    select,
    update,
)

from domainkit import clock
from domainkit.db import connections

from .settings import MessagingSettings
from .tables import BLANKED, CANCELLED, DEAD, PENDING, messages

__all__ = [
    "blanked_dead_letters",
    "cancel",
    "list_messages",
    "queue_counts",
    "queue_lag",
    "retry_dead",
]

type State = Literal["pending", "locked", "dead"]
"""What ``list_messages`` lists: waiting, locked (running or published), dead."""
STATES: Final = ("pending", "locked", "dead")

LISTED: Final = (
    messages.c.id,
    messages.c.kind,
    messages.c.target,
    messages.c.queue,
    messages.c.priority,
    messages.c.attempts,
    messages.c.max_attempts,
    messages.c.available_at,
    messages.c.locked_until,
    messages.c.started_at,
    messages.c.finished_at,
    messages.c.last_error,
)
"""The columns listings show; payloads and headers stay out."""


def _now() -> datetime:
    """Return the current time in UTC (``domainkit.clock``)."""
    return clock.now()


def locked(now: datetime) -> ColumnElement[bool]:
    """Pending messages a lock holds: running, or published to a broker."""
    return and_(
        messages.c.status == PENDING,
        messages.c.locked_until.is_not(None),
        messages.c.locked_until >= now,
    )


def running(now: datetime) -> ColumnElement[bool]:
    """Pending messages a worker is running now."""
    return and_(locked(now), messages.c.started_at.is_not(None))


def waiting(now: datetime) -> ColumnElement[bool]:
    """Pending messages no worker is running (published ones included)."""
    return and_(messages.c.status == PENDING, not_(running(now)))


async def queue_counts(
    config: MessagingSettings, database: str | None = None
) -> dict[str, int]:
    """Count messages by status in alias ``database`` (default: the home outbox)."""
    statement = select(messages.c.status, func.count()).group_by(messages.c.status)
    async with connections.engine(database or config.DATABASE).connect() as connection:
        rows = (await connection.execute(statement)).all()
    return {status: int(count) for status, count in rows}


async def queue_lag(
    config: MessagingSettings, database: str | None = None
) -> dict[str, float]:
    """Seconds the oldest due message of each queue has waited to start.

    Queues with nothing due are left out. Running messages do not count;
    published ones do, as they wait on the broker.
    """
    now = _now()
    statement = (
        select(messages.c.queue, func.min(messages.c.available_at))
        .where(waiting(now), messages.c.available_at <= now)
        .group_by(messages.c.queue)
    )
    async with connections.engine(database or config.DATABASE).connect() as connection:
        rows = (await connection.execute(statement)).all()
    return {queue: (now - oldest).total_seconds() for queue, oldest in rows}


async def list_messages(
    config: MessagingSettings,
    state: State,
    limit: int,
    database: str | None = None,
    *,
    queue: str | None = None,
) -> list[Any]:
    """List messages in ``state``, most urgent (or, for dead ones, newest) first.

    ``pending`` lists waiting messages in the order workers take them,
    ``locked`` running and published ones, ``dead`` dead letters.
    """
    now = _now()
    statement = select(*LISTED).limit(limit)
    if state == "pending":
        statement = statement.where(not_(locked(now)), messages.c.status == PENDING)
        statement = statement.order_by(
            case((messages.c.available_at <= now, 0), else_=1),  # Due ones first.
            messages.c.priority.desc(),
            messages.c.available_at,
        )
    elif state == "locked":
        statement = statement.where(locked(now)).order_by(messages.c.locked_until)
    else:
        statement = statement.where(messages.c.status == DEAD).order_by(
            messages.c.finished_at.desc()
        )
    if queue is not None:
        statement = statement.where(messages.c.queue == queue)
    async with connections.engine(database or config.DATABASE).connect() as connection:
        return list((await connection.execute(statement)).all())


async def dead_letters(
    config: MessagingSettings, limit: int, database: str | None = None
) -> list[Any]:
    """Return the most recent dead messages."""
    return await list_messages(config, "dead", limit, database)


def blanked() -> ColumnElement[bool]:
    """Messages whose arguments the worker blanked when they died (``sensitive``)."""
    return func.coalesce(messages.c.headers[BLANKED].as_boolean(), false())


async def retry_dead(
    config: MessagingSettings,
    message_id: uuid.UUID | None,
    database: str | None = None,
) -> int:
    """Return dead messages (one, or all) to the queue with fresh attempts.

    Those whose arguments were blanked (``blanked``) stay dead: they have
    nothing left to run with.
    """
    statement = (
        update(messages)
        .where(messages.c.status == DEAD, not_(blanked()))
        .values(
            status=PENDING,
            attempts=0,
            available_at=_now(),
            finished_at=None,
            started_at=None,
        )
    )
    if message_id is not None:
        statement = statement.where(messages.c.id == message_id)
    async with connections.engine(database or config.DATABASE).begin() as connection:
        result = await connection.execute(statement)
    return int(result.rowcount)


async def blanked_dead_letters(
    config: MessagingSettings,
    message_id: uuid.UUID | None,
    database: str | None = None,
) -> int:
    """Count dead messages (one, or all) whose arguments were blanked."""
    statement = (
        select(func.count())
        .select_from(messages)
        .where(messages.c.status == DEAD, blanked())
    )
    if message_id is not None:
        statement = statement.where(messages.c.id == message_id)
    async with connections.engine(database or config.DATABASE).connect() as connection:
        return int(await connection.scalar(statement) or 0)


async def cancel(
    config: MessagingSettings, message_id: uuid.UUID, database: str | None = None
) -> bool:
    """Cancel a pending message that no worker is running; report whether it was.

    One condition picks the row, so a worker that claims it first wins and
    the cancel changes nothing; a cancelled message is never claimed. A
    published one is skipped when its delivery arrives. The rest of a chain
    is never queued. Its arguments are blanked, as a done message's are.
    """
    now = _now()
    statement = (
        update(messages)
        .where(messages.c.id == message_id, waiting(now))
        .values(
            status=CANCELLED,
            finished_at=now,
            locked_until=None,
            lock_id=None,
            last_error="Cancelled by an operator.",
            payload={},
        )
    )
    async with connections.engine(database or config.DATABASE).begin() as connection:
        result = await connection.execute(statement)
    return bool(result.rowcount)


def state_of(row: Any, now: datetime) -> str:  # noqa: ANN401 - a result row.
    """Describe a listed pending message: ``running``, ``published``, ``due``..."""
    if row.locked_until is not None and row.locked_until >= now:
        return "running" if row.started_at is not None else "published"
    return "due" if row.available_at <= now else "scheduled"
