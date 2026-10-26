"""Install ``domainkit.contrib.audit`` to keep an audit trail."""

from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any, Final, cast

from domainkit.apps import DomainConfig, registry
from domainkit.db import SqlAlchemyUnitOfWork, map_imperatively
from domainkit.db.hooks import on_stage
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent

from .baseline import check_at_startup, settings_problems
from .models import AuditEntry
from .recorder import Outcome, event_rows, record, write
from .settings import AuditSettings
from .tables import audit_log

MESSAGING: Final = "domainkit.contrib.messaging"


class AuditConfig(DomainConfig):
    """Record committed domain events in the same transaction."""

    label = "audit"
    verbose_name = "Audit trail"
    settings_class = AuditSettings
    requires = ()

    def ready(self) -> None:
        """Check where explicit records go; seal on a schedule with messaging.

        ``AuditEntry`` is mapped so content types give it its permission.
        Each commit's events are written with it (``on_stage``).
        """
        config = registry.settings(AuditSettings)
        router.require_standalone(config.DATABASE, "AUDIT.DATABASE")
        map_imperatively(AuditEntry, audit_log)
        on_stage(self.stage_events)
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    async def record_operator_action(
        self, action: str, outcome: str, details: Mapping[str, object]
    ) -> None:
        """Record ``operator.<action>`` (``domainkit.management.operators``)."""
        await record(
            f"operator.{action}",
            Outcome(outcome),
            **cast(dict[str, Any], details),  # Never target, personal, subject.
        )

    async def record_denial(self, details: Mapping[str, object]) -> None:
        """Record ``auth.access`` denied: a principal refused (``http.access``).

        The details are the route's method and template, and the rule's
        class; the actor is the request's.
        """
        await record(
            "auth.access",
            Outcome.DENIED,
            **cast(dict[str, Any], details),  # Never target, personal, subject.
        )

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

    async def stage_events(
        self,
        uow: SqlAlchemyUnitOfWork,
        events: Sequence[DomainEvent],
        tasks: Sequence[DeferredTask],
    ) -> None:
        """Add one audit row per committed event (unless excluded)."""
        config = registry.settings(AuditSettings)
        if not config.EVENTS:
            return
        await write(uow.session, event_rows(events, config))

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Every hosting target: events are audited where they commit."""
        return frozenset(router.hosting_targets())
