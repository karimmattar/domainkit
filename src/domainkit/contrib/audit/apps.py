"""Install ``domainkit.contrib.audit`` to keep an audit trail."""

from collections.abc import Sequence
from importlib import import_module
from typing import Final, cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

from .recorder import event_rows, write
from .settings import AuditSettings

MESSAGING: Final = "domainkit.contrib.messaging"


class AuditConfig(DomainConfig):
    """Record committed domain events in the same transaction."""

    label = "audit"
    verbose_name = "Audit trail"
    settings_class = AuditSettings

    def ready(self) -> None:
        """Check where explicit records go; seal on a schedule with messaging."""
        config = registry.settings(AuditSettings)
        router.require_standalone(config.DATABASE, "AUDIT.DATABASE")
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    async def stage(
        self,
        uow: UnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Add one audit row per committed event (unless excluded)."""
        config = registry.settings(AuditSettings)
        if not config.EVENTS:
            return
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        await write(sql_uow.session, event_rows(events, config))

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: events are audited where they commit."""
        return frozenset(router.hosting_targets())
