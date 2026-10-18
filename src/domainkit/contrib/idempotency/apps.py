"""Install ``domainkit.contrib.idempotency`` and add its middleware."""

from collections.abc import Sequence
from importlib import import_module
from typing import cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork, connections
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

from .operations import KeyTakenOver, mark_committed, pin_lease, running_operation
from .settings import IdempotencySettings

MESSAGING = "domainkit.contrib.messaging"


class IdempotencyConfig(DomainConfig):
    """Store replayable responses; mark keys whose changes committed."""

    label = "idempotency"
    verbose_name = "Idempotency keys"
    settings_class = IdempotencySettings

    def ready(self) -> None:
        """Check the keys' database; schedule the hourly purge with messaging."""
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "IDEMPOTENCY.DATABASE")
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    async def stage(
        self,
        uow: UnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Mark the keyed request committed here, or pin its key; refuse a lost key.

        On ``IDEMPOTENCY.DATABASE`` the mark joins this transaction. Elsewhere
        the key is pinned first, in its own transaction, so no retry can take
        it over whether or not the commit lands.
        """
        operation = running_operation()
        if operation is None:
            return
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        database = self.get_settings().DATABASE
        if sql_uow.alias == database:
            connection = await sql_uow.session.connection()
            held = await connection.execute(mark_committed(operation))
        else:
            async with connections.engine(database).begin() as connection:
                held = await connection.execute(pin_lease(operation))
        if not held.rowcount:
            raise KeyTakenOver(
                "This request ran past its Idempotency-Key lease, and a retry "
                "took the key over; its changes were not saved."
            )

    async def committed(self, uow: UnitOfWork) -> None:
        """Note the commit; mark the key now if it went to another database."""
        operation = running_operation()
        if operation is None:
            return
        operation.committed = True
        database = self.get_settings().DATABASE
        if cast(SqlAlchemyUnitOfWork, uow).alias != database:
            async with connections.engine(database).begin() as connection:
                await connection.execute(mark_committed(operation))

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Only ``IDEMPOTENCY.DATABASE``: keys are stored apart from business data."""
        return frozenset({self.get_settings().DATABASE})

    def get_settings(self) -> IdempotencySettings:
        """Return the validated ``IDEMPOTENCY`` settings."""
        return registry.settings(IdempotencySettings)
