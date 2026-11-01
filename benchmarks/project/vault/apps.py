"""Installation hooks of the vault domain."""

from domainkit.apps import DomainConfig

from .tables import start_mappers


class VaultConfig(DomainConfig):
    """Map the vault's tables."""

    def ready(self) -> None:
        """Map ``Wallet`` and ``Card`` onto their tables."""
        start_mappers()
