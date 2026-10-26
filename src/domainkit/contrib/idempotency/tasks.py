"""Hourly purge of expired keys (needs ``domainkit.contrib.messaging``)."""

from datetime import timedelta

from domainkit.apps import registry
from domainkit.contrib.messaging import periodic
from domainkit.db import connections

from .operations import purge
from .settings import IdempotencySettings
from .statements import clock_statements


@periodic(every=timedelta(hours=1), name="domainkit.idempotency.purge_expired")
async def purge_expired() -> None:
    """Delete keys past their time to live, on the database's clock.

    Those whose request may have committed become tombstones instead, and
    are deleted once ``TOMBSTONE_SECONDS`` more have passed (FIN-84).
    """
    config = registry.settings(IdempotencySettings)
    async with connections.engine(config.DATABASE).begin() as connection:
        built = clock_statements(connection.dialect.name)
        await purge(connection, built, built.now(), config.TOMBSTONE_SECONDS, None)
