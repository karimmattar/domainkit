"""Configuration of the shop sample domain."""

from domainkit.apps import DomainConfig

from .tables import start_mappers


class ShopConfig(DomainConfig):
    """Map the shop's tables."""

    def ready(self) -> None:
        """Map the aggregates."""
        start_mappers()
