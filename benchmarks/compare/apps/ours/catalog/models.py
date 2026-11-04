"""The item, its table (the shared one), and its unit of work."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, NewType
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    map_imperatively,
    table_for,
)
from domainkit.ddd import AggregateRoot, Digits, MaxLength, MinLength, Range

ItemId = NewType("ItemId", UUID)


class Item(AggregateRoot[ItemId]):
    """Something for sale."""

    name: Annotated[str, MinLength(1), MaxLength(50)]
    price: Annotated[Decimal, Digits(10, 2), Range(min=0)]
    created_at: datetime


items = table_for(Item, "compare_items", domain="catalog")


class ItemRepository(SqlAlchemyRepository[Item, ItemId]):
    """Items by id."""

    model = Item


class CatalogUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the items."""

    items: ItemRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Open the items repository on ``session``."""
        self.items = ItemRepository(session)


def start_mappers() -> None:
    """Map ``Item`` onto the shared table."""
    map_imperatively(Item, items)
