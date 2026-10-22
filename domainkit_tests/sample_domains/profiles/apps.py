"""Configuration of the profiles sample domain (listed after the auth app)."""

from domainkit.apps import DomainConfig

from .tables import start_mappers


class ProfilesConfig(DomainConfig):
    """Map the entities onto the profile."""

    def ready(self) -> None:
        """Map the entities onto the profile."""
        start_mappers()
