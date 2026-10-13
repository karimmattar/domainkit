"""Operator queries: queue depth, dead letters, and retries."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from domainkit.db import connections

from .settings import MessagingSettings
from .tables import DEAD, PENDING, messages


async def queue_counts(config: MessagingSettings) -> dict[str, int]:
    """Count messages by status."""
    statement = select(messages.c.status, func.count()).group_by(messages.c.status)
    async with connections.engine(config.DATABASE).connect() as connection:
        rows = (await connection.execute(statement)).all()
    return {status: int(count) for status, count in rows}


async def dead_letters(config: MessagingSettings, limit: int) -> list[Any]:
    """Return the most recent dead messages."""
    statement = (
        select(
            messages.c.id,
            messages.c.kind,
            messages.c.target,
            messages.c.attempts,
            messages.c.last_error,
        )
        .where(messages.c.status == DEAD)
        .order_by(messages.c.finished_at.desc())
        .limit(limit)
    )
    async with connections.engine(config.DATABASE).connect() as connection:
        return list((await connection.execute(statement)).all())


async def retry_dead(config: MessagingSettings, message_id: uuid.UUID | None) -> int:
    """Return dead messages (one, or all) to the queue with fresh attempts."""
    statement = (
        update(messages)
        .where(messages.c.status == DEAD)
        .values(
            status=PENDING, attempts=0, available_at=datetime.now(UTC), finished_at=None
        )
    )
    if message_id is not None:
        statement = statement.where(messages.c.id == message_id)
    async with connections.engine(config.DATABASE).begin() as connection:
        result = await connection.execute(statement)
    return int(result.rowcount)
