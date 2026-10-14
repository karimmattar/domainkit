"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

from collections.abc import Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

from .outbox import rows_for, write
from .settings import MessagingSettings


class MessagingConfig(DomainConfig):
    """Write durable handler calls and deferred tasks with each commit."""

    label = "messaging"
    verbose_name = "Messaging"
    settings_class = MessagingSettings

    def ready(self) -> None:
        """Check the home outbox; expose its gauges when Prometheus is installed."""
        router.require_standalone(self.get_settings().DATABASE, "MESSAGING.DATABASE")
        if find_spec("prometheus_client") is not None:
            import_module(f"{self.name}.metrics")

    async def stage(
        self,
        uow: UnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Add outbox rows to the committing transaction."""
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        rows = rows_for(events, tasks, uow.events, self.get_settings())
        if not rows:
            return
        session = sql_uow.session
        dialect = session.get_bind().dialect.name
        await write(session, dialect, rows)

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: the outbox commits with each domain's data."""
        return frozenset(router.hosting_targets())

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
