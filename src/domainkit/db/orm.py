"""Shared SQLAlchemy metadata and imperative mapping.

Domains declare ``Table`` objects on ``metadata`` in their infrastructure layer
and map aggregates with ``map_imperatively`` from ``DomainConfig.ready()``.
The domain model itself never imports SQLAlchemy.

A table may name its domain label in ``info={"domain": ...}``; migrations then
ignore it while that domain is not installed (``domainkit.contrib`` apps do).

Child entities live in their own tables, with a foreign key to the
aggregate's, and are mapped as relationships of the aggregate::

    map_imperatively(OrderLine, order_lines)
    map_imperatively(Order, orders, properties={"lines": children(OrderLine)})

``children`` and ``child`` load them with the aggregate and save and delete
them with it. Other aggregates are referenced by id, not by relationship.
"""

from typing import Any, Final

from sqlalchemy import MetaData, Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import RelationshipProperty, registry, relationship

from domainkit.ddd import AggregateRoot

NAMING_CONVENTION: Final = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names, so migrations can alter and drop them."""

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)
"""Every table of every domain; the target of ``makemigrations``."""

mapper_registry: Final = registry(metadata=metadata)


def map_imperatively(class_: type, table: Table, **options: Any) -> None:  # noqa: ANN401 - SQLAlchemy mapper options.
    """Map ``class_`` to ``table`` once; later calls are ignored.

    An aggregate whose table has a ``version`` column gets optimistic locking:
    every UPDATE and DELETE checks and bumps the version.
    """
    if sa_inspect(class_, raiseerr=False) is not None:
        return
    if "version" in table.c and issubclass(class_, AggregateRoot):
        options.setdefault("version_id_col", table.c.version)
    mapper_registry.map_imperatively(class_, table, **options)


def children(entity: type, **options: Any) -> RelationshipProperty[Any]:  # noqa: ANN401 - relationship() options.
    """A relationship to the child entities an aggregate owns (``list[Line]``).

    They load with the aggregate, in one more query for a page of
    aggregates (``selectin``), so reading them never needs lazy I/O. They
    are saved with it, and deleted with it or when removed from the
    collection (``delete-orphan``). ``order_by`` sorts them;
    ``collection_class=set`` suits a ``set[...]`` field.
    """
    options.setdefault("lazy", "selectin")
    options.setdefault("cascade", "all, delete-orphan")
    return relationship(entity, **options)


def child(entity: type, **options: Any) -> RelationshipProperty[Any]:  # noqa: ANN401 - relationship() options.
    """A relationship to the one child entity an aggregate owns, or None."""
    options.setdefault("uselist", False)
    return children(entity, **options)
