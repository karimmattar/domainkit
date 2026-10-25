"""Install ``domainkit.contrib.audit`` to keep an audit trail."""

from collections.abc import Sequence
from importlib import import_module
from typing import Final, cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork, map_imperatively
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent, UnitOfWork

from .baseline import check_at_startup, settings_problems
from .models import AuditEntry
from .recorder import event_rows, write
from .settings import AuditSettings
from .tables import audit_log

MESSAGING: Final = "domainkit.contrib.messaging"


class AuditConfig(DomainConfig):
    """Record committed domain events in the same transaction."""

    label = "audit"
    verbose_name = "Audit trail"
    settings_class = AuditSettings

    def ready(self) -> None:
        """Check where explicit records go; seal on a schedule with messaging.

        ``AuditEntry`` is mapped so content types give it its permission.
        """
        config = registry.settings(AuditSettings)
        router.require_standalone(config.DATABASE, "AUDIT.DATABASE")
        map_imperatively(AuditEntry, audit_log)
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    def deployment_warnings(self) -> list[str]:
        """The settings a tamper-evident trail needs, missing in staging and production.

        A ledger, a namespace on it, and a sealing schedule (``baseline``);
        the application checks the ``protect()`` triggers when it starts.
        """
        return settings_problems()

    async def startup(self) -> None:
        """Warn, where deployed, about a trail that is not tamper-evident.

        With ``AUDIT.REQUIRE_BASELINE``, refuse to start instead.
        """
        await check_at_startup()

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
