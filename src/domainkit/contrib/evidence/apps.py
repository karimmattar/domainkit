"""Install ``domainkit.contrib.evidence`` to record evidence on a ledger."""

from importlib import import_module

from domainkit.apps import DomainConfig, registry
from domainkit.apps.hooks import on_deployment_warnings
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.routing import DatabaseRouter
from domainkit.exceptions import ImproperlyConfigured
from domainkit.ledger import ledgers

from .settings import EvidenceSettings

MESSAGING = "domainkit.contrib.messaging"
AUDIT = "domainkit.contrib.audit"


class EvidenceConfig(DomainConfig):
    """Check the evidence ledger; register durable recording with messaging."""

    label = "evidence"
    verbose_name = "Evidence"
    settings_class = EvidenceSettings
    requires = ()

    def ready(self) -> None:
        """Fail fast unless ``EVIDENCE.LEDGER`` names a ledger.

        Staging and production also need ``EVIDENCE.SUBJECT_KEY`` (or
        ``SUBJECT_KEYS``): the development key is public, so it would not
        hide subjects.
        """
        config = registry.settings(EvidenceSettings)
        ledgers.config(config.LEDGER)
        if not config.has_subject_key and settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            raise ImproperlyConfigured(
                f"EVIDENCE.SUBJECT_KEY is required in {settings.ENVIRONMENT}: a "
                "random secret of 32 characters or more, kept for as long as "
                "the evidence."
            )
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")
        on_deployment_warnings(self.weak_settings)

    def weak_settings(self) -> list[str]:
        """Evidence on a ledger whose trusted state ends with the process.

        Without ``domainkit.contrib.audit`` nothing keeps the state the
        ledger last proved, so a process that starts after the ledger was
        rolled back trusts it as it is.
        """
        if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS or registry.is_installed(
            AUDIT
        ):
            return []
        warning = (
            "The evidence ledger's trusted state is kept in memory only, so a "
            "process that starts after the ledger was rolled back trusts it: "
            f"install {AUDIT}, which keeps it in AUDIT.DATABASE."
        )
        return [warning]

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """None: evidence lives on a ledger, not in a SQL database."""
        return frozenset()
