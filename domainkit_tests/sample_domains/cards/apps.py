"""Configuration of the cards sample domain."""

from domainkit.apps import DomainConfig

from .tables import start_mappers


class CardsConfig(DomainConfig):
    """Map the aggregates."""

    def ready(self) -> None:
        """Map the aggregates."""
        start_mappers()
