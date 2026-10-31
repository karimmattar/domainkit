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

Any other relationship that leaves ``lazy`` at SQLAlchemy's default
(``"select"``, a load on first access) raises on access instead
(``lazy="raise"``), and so does the reverse side its ``backref`` makes:
a lazy load needs I/O, which an attribute access in async code cannot do,
so it would fail elsewhere with ``MissingGreenlet``. Load it in the query
(``selectinload``), or ask for lazy loading on purpose with ``lazy=True``,
for ``AsyncAttrs.awaitable_attrs`` or ``run_sync``.

Money columns (``MoneyColumns``, or a field hinted ``Money`` in ``table_for``)
are mapped to one ``Money`` attribute per pair.

An aggregate's table needs a ``version`` column: it is what makes a stale
copy fail with ``ConcurrentUpdate`` instead of overwriting a concurrent
change. ``map_imperatively`` refuses an aggregate table without one, unless
the mapping says ``versioned=False`` (an aggregate written by one writer
only, or read-only).
"""

from typing import Any, Final

from sqlalchemy import MetaData, Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import RelationshipProperty, registry, relationship

from domainkit.db.money import guard_money, money_properties
from domainkit.ddd import AggregateRoot
from domainkit.exceptions import ImproperlyConfigured

VERSION: Final = "version"
"""The column that holds an aggregate's version (``AggregateRoot.version``)."""

DEFAULT_LAZY: Final = "select"
"""SQLAlchemy's default ``lazy``, which ``map_imperatively`` makes raise."""

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


def map_imperatively(
    class_: type,
    table: Table,
    *,
    versioned: bool = True,
    **options: Any,  # noqa: ANN401 - SQLAlchemy mapper options.
) -> None:
    """Map ``class_`` to ``table`` once; later calls are ignored.

    An aggregate gets optimistic locking from its table's ``version``
    column: every UPDATE and DELETE checks and bumps the version, so saving
    a stale copy raises ``ConcurrentUpdate``. Relationships in
    ``properties`` that leave ``lazy`` at its default raise on an unloaded
    access, their backrefs too (see the module's docstring). Each pair of
    money columns is mapped to its ``Money`` attribute, unless
    ``properties`` maps that name.

    Args:
        class_: The class to map.
        table: Its table.
        versioned: False maps an aggregate without optimistic locking, on
            purpose: concurrent changes to one row then overwrite each
            other. Classes other than aggregates ignore it.
        **options: SQLAlchemy mapper options; ``version_id_col`` names
            another version column.

    Raises:
        ImproperlyConfigured: ``class_`` is an aggregate, ``versioned`` is
            true, and ``table`` has no ``version`` column.
    """
    if sa_inspect(class_, raiseerr=False) is not None:
        return
    if issubclass(class_, AggregateRoot) and versioned:
        _version_aggregate(class_, table, options)
    given = dict(options.get("properties") or {})
    if money := money_properties(table, given):
        options["properties"] = {**given, **money}
    for prop in dict(options.get("properties") or {}).values():
        if isinstance(prop, RelationshipProperty):
            _raise_on_lazy_loads(prop)
    mapper_registry.map_imperatively(class_, table, **options)
    if money:
        guard_money(class_, table, money)


def _version_aggregate(class_: type, table: Table, options: dict[str, Any]) -> None:
    """Point the aggregate's mapping at its version column, or refuse it.

    A subclass mapped with ``inherits`` shares its base's version, and a
    mapping that passes ``version_id_col`` has chosen; neither needs a
    ``version`` column.
    """
    if options.get("inherits") is not None or "version_id_col" in options:
        return
    if VERSION not in table.c:
        raise ImproperlyConfigured(
            f"{class_.__module__}.{class_.__qualname__} is an aggregate, but its "
            f"table {table.name!r} has no {VERSION!r} column, so concurrent "
            "changes to one row would overwrite each other without an error. "
            f'Add Column("{VERSION}", Integer, nullable=False) to the table '
            '(server_default="0" in the migration for existing rows), or pass '
            "versioned=False to map_imperatively to map it without optimistic "
            "locking on purpose."
        )
    options["version_id_col"] = table.c[VERSION]


def _raise_on_lazy_loads(prop: RelationshipProperty[Any]) -> None:
    """Make ``prop``, and the backref it makes, raise where they would lazy load."""
    if prop.lazy == DEFAULT_LAZY:
        prop.lazy = "raise"
        # SQLAlchemy picks the loader by this key, as it does for its own
        # changes to ``lazy`` (a ``WriteOnlyMapped`` annotation).
        prop.strategy_key = (("lazy", prop.lazy),)
    if isinstance(prop.backref, str):
        prop.backref = (prop.backref, {"lazy": "raise"})
    elif prop.backref is not None:
        prop.backref[1].setdefault("lazy", "raise")


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
