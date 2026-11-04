"""The catalog domain."""

from domainkit.apps import DomainConfig

from .models import start_mappers


class CatalogConfig(DomainConfig):
    """Map the item onto its table."""

    def ready(self) -> None:
        """Map ``Item``."""
        start_mappers()
