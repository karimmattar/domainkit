"""Configuration of the oddities sample domain."""

from domainkit.apps import DomainConfig

from .tables import start_mappers


class OdditiesConfig(DomainConfig):
    """Map the aggregates."""

    def ready(self) -> None:
        """Map the aggregates."""
        start_mappers()
