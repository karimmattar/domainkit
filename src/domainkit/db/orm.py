"""Shared SQLAlchemy metadata and imperative mapping.

Domains declare ``Table`` objects on ``metadata`` in their infrastructure layer
and map aggregates with ``map_imperatively`` from ``DomainConfig.ready()``.
The domain model itself never imports SQLAlchemy.

A table may name its domain label in ``info={"domain": ...}``; migrations then
ignore it while that domain is not installed (``domainkit.contrib`` apps do).
"""

from typing import Any, Final

from sqlalchemy import MetaData, Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import registry

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
