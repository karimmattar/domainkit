"""Install ``domainkit.contrib.contenttypes`` (``domainkit.contrib.auth`` needs it)."""

from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import DomainConfig
from domainkit.db.hooks import on_migrated
from domainkit.db.routing import router

from .sync import has_tables, sync_content_types
from .tables import content_types, start_mappers


class ContentTypesConfig(DomainConfig):
    """Map ``ContentType``; create the rows after each ``migrate``."""

    label = "contenttypes"
    verbose_name = "Content types"
    requires = ()

    @property
    def database(self) -> str:
        """The one alias holding the content types (its routing target)."""
        return router.require_standalone(
            router.target_for(self.label), "DATABASE_ROUTING['contenttypes']"
        )

    def ready(self) -> None:
        """Check the routing, map the table, and fill it after ``migrate``."""
        _ = self.database
        start_mappers()
        on_migrated(self.create_content_types)

    async def create_content_types(
        self, connection: AsyncConnection, database: str
    ) -> None:
        """Create rows for models added since the last ``migrate``."""
        if database == self.database and await has_tables(connection, content_types):
            await sync_content_types(connection)
