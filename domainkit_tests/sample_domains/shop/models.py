"""A product, with one permission beyond the default four."""

from typing import NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot

ProductId = NewType("ProductId", UUID)


class Product(AggregateRoot[ProductId]):
    """Something for sale."""

    name: str

    class Meta:
        """Permissions and names, as Django's ``Meta``."""

        permissions = (("publish_product", "Can publish product"),)


class StockLevel(AggregateRoot[ProductId]):
    """How many of a product are left; only viewed and changed."""

    count: int = 0

    class Meta:
        """Two actions only, and a name of its own."""

        verbose_name = "stock"
        default_permissions = ("change", "view")
