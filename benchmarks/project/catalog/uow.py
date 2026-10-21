"""The catalog's repository and unit of work."""

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork

from .models import Item, ItemId


class ItemRepository(SqlAlchemyRepository[Item, ItemId]):
    """Items by id."""

    model = Item


class CatalogUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the items."""

    items: ItemRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Open the items repository on ``session``."""
        self.items = ItemRepository(session)
