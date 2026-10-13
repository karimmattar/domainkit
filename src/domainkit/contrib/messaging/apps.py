"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

from collections.abc import Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork
from domainkit.exceptions import ImproperlyConfigured

from .outbox import rows_for, write
from .settings import MessagingSettings


class MessagingConfig(DomainConfig):
    """Write durable handler calls and deferred tasks with each commit."""

    label = "messaging"
    verbose_name = "Messaging"
    settings_class = MessagingSettings

    def ready(self) -> None:
        """Expose outbox gauges on ``/metrics`` when Prometheus is installed."""
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
        config = self.get_settings()
        rows = rows_for(events, tasks, uow.events, config)
        if not rows:
            return
        if sql_uow.database != config.DATABASE:
            raise ImproperlyConfigured(
                f"The outbox lives in {config.DATABASE!r}; a unit of work on "
                f"{sql_uow.database!r} cannot queue durable work atomically."
            )
        session = sql_uow.session
        dialect = session.get_bind().dialect.name
        await write(session, dialect, rows)

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
