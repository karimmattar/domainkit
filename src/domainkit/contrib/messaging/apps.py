"""Install ``domainkit.contrib.messaging`` to get the outbox and the worker."""

from collections.abc import Mapping, Sequence
from importlib import import_module
from importlib.util import find_spec
from typing import Any
from weakref import WeakKeyDictionary

from domainkit import lifecycle
from domainkit.apps import DomainConfig, registry
from domainkit.broker import brokers
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.hooks import on_committed, on_stage
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent
from domainkit.lifecycle import Phase

from .dispatch import close_publishers
from .outbox import notify_committed, rows_for, write
from .settings import MessagingSettings

_committing: WeakKeyDictionary[SqlAlchemyUnitOfWork, list[dict[str, Any]]] = (
    WeakKeyDictionary()
)
"""Messages each unit of work wrote, to publish once it commits."""


class MessagingConfig(DomainConfig):
    """Write durable handler calls and deferred tasks with each commit."""

    label = "messaging"
    verbose_name = "Messaging"
    settings_class = MessagingSettings
    requires = ()

    def broker_queues(self) -> tuple[str, Sequence[str]]:
        """``MESSAGING.BROKER``, and every queue the worker's tasks use (``broker``).

        Raises:
            CommandError: ``MESSAGING.BROKER`` names no broker.
        """
        from domainkit.management.base import CommandError

        from . import registry as tasks

        alias = self.get_settings().BROKER
        if alias is None:
            raise CommandError("declare needs MESSAGING.BROKER.")
        return alias, sorted(tasks.queues())

    def ready(self) -> None:
        """Check the home outbox and broker; expose gauges with Prometheus.

        Shutdown (``domainkit.lifecycle``) closes the background publishers
        in its drain phase, in the application and in management commands
        alike, within the time that phase always has (``drain_time``). Each
        commit writes its outbox rows (``on_stage``) and hands them to the
        publisher once it lands (``on_committed``).
        """
        config = self.get_settings()
        router.require_standalone(config.DATABASE, "MESSAGING.DATABASE")
        if config.BROKER is not None:
            brokers.config(config.BROKER)
        lifecycle.register("publishers", close_publishers, phase=Phase.DRAIN)
        on_stage(self.stage_outbox)
        on_committed(self.publish_committed)
        if find_spec("prometheus_client") is not None:
            import_module(f"{self.name}.metrics")

    async def stage_outbox(
        self,
        uow: SqlAlchemyUnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Add outbox rows to the committing transaction."""
        rows = rows_for(events, tasks, uow.events, self.get_settings())
        if not rows:
            return
        session = uow.session
        dialect = session.get_bind().dialect.name
        await write(session, dialect, rows)
        _committing.setdefault(uow, []).extend(rows)

    async def publish_committed(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Hand the messages the unit of work wrote to the background publisher."""
        rows = _committing.pop(uow, [])
        await notify_committed(self.get_settings(), uow.alias, rows)

    async def erase_subject(self, subject: str, reason: str) -> Mapping[str, int]:
        """Blank dead messages about ``subject``; count pending ones (``erasure``)."""
        from .erasure import erase_subject

        return await erase_subject(subject)

    async def export_subject(self, subject: str) -> Mapping[str, object]:
        """The queued and dead messages about ``subject``, with their arguments."""
        from .erasure import export_subject

        found = await export_subject(subject)
        return {"messages": found} if found else {}

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: the outbox commits with each domain's data."""
        return frozenset(router.hosting_targets())

    def get_settings(self) -> MessagingSettings:
        """Return the validated ``MESSAGING`` settings."""
        return registry.settings(MessagingSettings)
