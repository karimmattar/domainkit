"""The ``Item`` aggregate."""

from typing import NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot

ItemId = NewType("ItemId", UUID)


class Item(AggregateRoot[ItemId]):
    """Something for sale."""

    name: str
    price: int
