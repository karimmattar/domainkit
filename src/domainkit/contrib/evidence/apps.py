"""Install ``domainkit.contrib.evidence`` to record evidence on a ledger."""

from importlib import import_module

from domainkit.apps import DomainConfig, registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.routing import DatabaseRouter
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import ledgers

from .settings import EvidenceSettings

MESSAGING = "domainkit.contrib.messaging"


class EvidenceConfig(DomainConfig):
    """Check the evidence ledger; register durable recording with messaging."""

    label = "evidence"
    verbose_name = "Evidence"
    settings_class = EvidenceSettings

    def ready(self) -> None:
        """Fail fast unless ``EVIDENCE.LEDGER`` names a ledger.

        Staging and production also need ``EVIDENCE.SUBJECT_KEY``: the
        development key is public, so it would not hide subjects.
        """
        config = registry.settings(EvidenceSettings)
        ledgers.config(config.LEDGER)
        if config.SUBJECT_KEY is None and settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            raise ImproperlyConfigured(
                f"EVIDENCE.SUBJECT_KEY is required in {settings.ENVIRONMENT}: a "
                "random secret of 32 characters or more, kept for as long as "
                "the evidence."
            )
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """None: evidence lives on a ledger, not in a SQL database."""
        return frozenset()
