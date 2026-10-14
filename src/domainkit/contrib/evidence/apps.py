"""Install ``domainkit.contrib.evidence`` to record evidence on a ledger."""

from importlib import import_module

from domainkit.apps import DomainConfig, registry
from domainkit.db.routing import DatabaseRouter
from domainkit.ledger import ledgers

from .settings import EvidenceSettings

MESSAGING = "domainkit.contrib.messaging"


class EvidenceConfig(DomainConfig):
    """Check the evidence ledger; register durable recording with messaging."""

    label = "evidence"
    verbose_name = "Evidence"
    settings_class = EvidenceSettings

    def ready(self) -> None:
        """Fail fast unless ``EVIDENCE.LEDGER`` names a ledger."""
        ledgers.config(registry.settings(EvidenceSettings).LEDGER)
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """None: evidence lives on a ledger, not in a SQL database."""
        return frozenset()
