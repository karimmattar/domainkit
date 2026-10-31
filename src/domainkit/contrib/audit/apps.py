"""Install ``domainkit.contrib.audit`` to keep an audit trail."""

from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any, Final, cast

from fastapi import FastAPI

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import (
    on_check_application,
    on_deployment_warnings,
    on_startup,
)
from domainkit.db import SqlAlchemyUnitOfWork, map_imperatively
from domainkit.db.hooks import on_migrated, on_stage
from domainkit.db.routing import DatabaseRouter, router
from domainkit.ddd import DeferredTask, DomainEvent
from domainkit.ledger.trust import use_states

from .baseline import check_at_startup, required, settings_problems
from .checkpoints import configured_public_key
from .ledgerstates import DatabaseStateStore
from .models import AuditEntry
from .protection import protect_migrated
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
        Each commit's events are written with it (``on_stage``), and
        ``migrate`` makes the trail append-only on PostgreSQL
        (``protect_migrated``).

        Ledgers keep the state they were last verified at in
        ``AUDIT.DATABASE`` (``ledgerstates``), so every process starts from it.

        Raises:
            ImproperlyConfigured: ``AUDIT.CHECKPOINT_PUBLIC_KEY`` is set but
                is not an Ed25519 public key in PEM, or cryptography is
                missing. The private ``AUDIT.CHECKPOINT_KEY`` is not read
                here: only ``auditprune`` and ``auditverify --export`` sign.
        """
        config = registry.settings(AuditSettings)
        router.require_standalone(config.DATABASE, "AUDIT.DATABASE")
        map_imperatively(AuditEntry, audit_log)
        on_stage(self.stage_events)
        on_migrated(protect_migrated)
        use_states(DatabaseStateStore(config.DATABASE))
        configured_public_key()  # Refuse a key that cannot verify, now.
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")
        on_check_application(self.application_problems)
        on_deployment_warnings(self.weak_settings)
        on_startup(self.start)

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

    def application_problems(self, app: FastAPI) -> list[str]:
        """The settings a tamper-evident trail needs, refused where deployed.

        A ledger, a namespace on it, and a sealing schedule (``baseline``),
        while ``AUDIT.REQUIRE_BASELINE`` holds (the default): staging and
        production refuse to start without them, and ``check`` fails.
        """
        return settings_problems() if required() else []

    def weak_settings(self) -> list[str]:
        """The same settings, as warnings, with ``AUDIT.REQUIRE_BASELINE = False``.

        The application checks the ``protect()`` triggers when it starts.
        """
        return [] if required() else settings_problems()

    async def start(self) -> None:
        """Refuse to start, where deployed, with a trail that is not tamper-evident.

        With ``AUDIT.REQUIRE_BASELINE = False``, warn instead.
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
