"""Install ``domainkit.contrib.idempotency`` and add its middleware."""

from importlib import import_module

from domainkit.apps import DomainConfig, registry

from .settings import IdempotencySettings

MESSAGING = "domainkit.contrib.messaging"


class IdempotencyConfig(DomainConfig):
    """Store replayable responses; purge expired keys when a worker runs."""

    label = "idempotency"
    verbose_name = "Idempotency keys"
    settings_class = IdempotencySettings

    def ready(self) -> None:
        """Schedule the hourly purge when messaging is installed."""
        if registry.is_installed(MESSAGING):
            import_module(f"{self.name}.tasks")
