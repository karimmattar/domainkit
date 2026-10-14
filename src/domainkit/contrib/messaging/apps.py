"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

import uuid
from collections.abc import Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import cast
from weakref import WeakKeyDictionary

from domainkit.apps import DomainConfig, registry
from domainkit.broker import brokers
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

from .dispatch import publish_committed
from .outbox import rows_for, write
from .settings import MessagingSettings

_committing: WeakKeyDictionary[UnitOfWork, list[uuid.UUID]] = WeakKeyDictionary()
"""Messages each unit of work wrote, to publish once it commits."""


class MessagingConfig(DomainConfig):
    """Write durable handler calls and deferred tasks with each commit."""

    label = "messaging"
    verbose_name = "Messaging"
    settings_class = MessagingSettings

    def ready(self) -> None:
        """Check the home outbox and broker; expose gauges with Prometheus."""
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "MESSAGING.DATABASE")
        if config.BROKER is not None:
            brokers.config(config.BROKER)
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
        _committing.setdefault(uow, []).extend(row["id"] for row in rows)

    async def committed(self, uow: UnitOfWork) -> None:
        """Publish the messages the unit of work wrote, with a broker."""
        ids = _committing.pop(uow, [])
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        await publish_committed(self.get_settings(), sql_uow.alias, ids)

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: the outbox commits with each domain's data."""
        return frozenset(router.hosting_targets())

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
