"""The catalog's table, derived from the model."""

from domainkit.db import map_imperatively, table_for

from .models import Item

items = table_for(Item, "catalog_items", domain="catalog")


def start_mappers() -> None:
    """Map ``Item`` onto ``catalog_items`` (idempotent)."""
    map_imperatively(Item, items)
