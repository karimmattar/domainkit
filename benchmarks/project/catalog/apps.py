"""Installation hooks of the catalog domain."""

from domainkit.apps import DomainConfig

from .tables import start_mappers


class CatalogConfig(DomainConfig):
    """Map the catalog's table."""

    def ready(self) -> None:
        """Map ``Item`` onto its table."""
        start_mappers()
