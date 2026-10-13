"""Hourly purge of expired keys (needs ``domainkit.contrib.messaging``)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from domainkit.apps import registry
from domainkit.contrib.messaging import periodic
from domainkit.db import connections

from .settings import IdempotencySettings
from .tables import idempotency_keys


@periodic(every=timedelta(hours=1), name="domainkit.idempotency.purge_expired")
async def purge_expired() -> None:
    """Delete keys past their time to live."""
    config = registry.settings(IdempotencySettings)
    async with connections.engine(config.DATABASE).begin() as connection:
        await connection.execute(
            delete(idempotency_keys).where(
                idempotency_keys.c.expires_at < datetime.now(UTC)
            )
        )
