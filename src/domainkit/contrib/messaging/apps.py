"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

from collections.abc import Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import Any, cast
from weakref import WeakKeyDictionary

from domainkit import lifecycle
from domainkit.apps import DomainConfig, registry
from domainkit.broker import brokers
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork
from domainkit.exceptions import ImproperlyConfigured
from domainkit.lifecycle import Phase

from .dispatch import close_publishers
from .outbox import notify_committed, rows_for, write
from .settings import MessagingSettings

_committing: WeakKeyDictionary[UnitOfWork, list[dict[str, Any]]] = WeakKeyDictionary()
"""Messages each unit of work wrote, to publish once it commits."""


class MessagingConfig(DomainConfig):
    """Write durable handler calls and deferred tasks with each commit."""

    label = "messaging"
    verbose_name = "Messaging"
    settings_class = MessagingSettings

    def ready(self) -> None:
        """Check the home outbox and broker; expose gauges with Prometheus.

        Shutdown (``domainkit.lifecycle``) closes the background publishers
        in its drain phase, in the application and in management commands
        alike. It must give a publication under way the time its broker may
        take to confirm it: ``CLOSE_TIMEOUT`` covers the broker's
        ``PUBLISH_TIMEOUT``.
        """
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "MESSAGING.DATABASE")
        lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
        if config.BROKER is not None:
            broker = brokers.config(config.BROKER)
            if config.CLOSE_TIMEOUT < broker.PUBLISH_TIMEOUT:
                raise ImproperlyConfigured(
                    f"MESSAGING.CLOSE_TIMEOUT ({config.CLOSE_TIMEOUT:g}s) must be "
                    f"at least BROKERS[{config.BROKER!r}].PUBLISH_TIMEOUT "
                    f"({broker.PUBLISH_TIMEOUT:g}s), so shutdown lets a "
                    "publication under way finish."
                )
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
        _committing.setdefault(uow, []).extend(rows)

    async def committed(self, uow: UnitOfWork) -> None:
        """Hand the messages the unit of work wrote to the background publisher."""
        rows = _committing.pop(uow, [])
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        await notify_committed(self.get_settings(), sql_uow.alias, rows)

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: the outbox commits with each domain's data."""
        return frozenset(router.hosting_targets())

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
