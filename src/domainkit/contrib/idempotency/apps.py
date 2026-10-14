"""Install ``domainkit.contrib.idempotency`` and add its middleware."""

from importlib import import_module

from domainkit.apps import DomainConfig, registry
from domainkit.db.routing import DatabaseRouter, router

from .settings import IdempotencySettings

MESSAGING = "domainkit.contrib.messaging"


class IdempotencyConfig(DomainConfig):
    """Store replayable responses; purge expired keys when a worker runs."""

    label = "idempotency"
    verbose_name = "Idempotency keys"
    settings_class = IdempotencySettings

    def ready(self) -> None:
        """Check the keys' database; schedule the hourly purge with messaging."""
        config = registry.settings(IdempotencySettings)
        router.require_standalone(config.DATABASE, "IDEMPOTENCY.DATABASE")
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")

    def databases(self, router: DatabaseRouter) -> frozenset[str]:
        """Only ``IDEMPOTENCY.DATABASE``: keys are stored apart from business data."""
        return frozenset({registry.settings(IdempotencySettings).DATABASE})
