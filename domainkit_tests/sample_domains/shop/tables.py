"""Tables of the shop domain."""

from sqlalchemy import Column, Integer, String, Table, Uuid

from domainkit.db import map_imperatively, metadata

from .models import Product, StockLevel

products = Table(
    "shop_products",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(100), nullable=False),
    Column("version", Integer, nullable=False),
    info={"domain": "shop"},
)

stock_levels = Table(
    "shop_stock_levels",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("count", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    info={"domain": "shop"},
)


def start_mappers() -> None:
    """Map the aggregates."""
    map_imperatively(Product, products)
    map_imperatively(StockLevel, stock_levels)
