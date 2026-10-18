"""Unique violations in domain terms: a taken value is a ``DuplicateValue``.

PostgreSQL names the constraint or index it enforced (SQLSTATE 23505);
SQLite names the table and columns, or the index on expressions. Either way
the constraint is looked up in the metadata. If the constraint, or its only
column, names a ``DuplicateValue`` subclass under ``info["duplicate"]``,
that class is raised, so each constraint can have its own error code::

    Column("email", String(254), unique=True, info={"duplicate": EmailTaken})

Messages name the fields, never the values, which may be personal data.
Other integrity errors (foreign keys, NOT NULL, checks) are left alone.
"""

from collections.abc import Iterator
from typing import Final

from sqlalchemy import Index, MetaData, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from domainkit.ddd import DuplicateValue

UNIQUE_VIOLATION: Final = "23505"
"""PostgreSQL's SQLSTATE for a unique violation (primary keys included)."""
SQLITE_UNIQUE: Final = frozenset(
    {"SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"}
)
_SQLITE_FAILED: Final = "UNIQUE constraint failed: "

type _Unique = UniqueConstraint | PrimaryKeyConstraint | Index


def duplicate_value(error: IntegrityError, metadata: MetaData) -> DuplicateValue | None:
    """The domain error for a unique violation; None for any other error."""
    driver_error = error.orig
    if getattr(driver_error, "sqlstate", None) == UNIQUE_VIOLATION:
        name, columns = _postgres_constraint(driver_error), None
    elif getattr(driver_error, "sqlite_errorname", None) in SQLITE_UNIQUE:
        name, columns = _sqlite_constraint(str(driver_error))
    else:
        return None
    for unique in _uniques(metadata):
        if (name is not None and unique.name == name) or (
            columns is not None and _columns(unique) == columns
        ):
            fields = [column.name for column in unique.columns]
            constraint = unique.name if isinstance(unique.name, str) else None
            return _declared(unique)(
                _message(fields), constraint=constraint, fields=fields
            )
    fields = [] if columns is None else [column for _, column in columns]
    return DuplicateValue(_message(fields), constraint=name, fields=fields)


def _postgres_constraint(driver_error: BaseException | None) -> str | None:
    """The constraint PostgreSQL named, from psycopg or asyncpg."""
    diag = getattr(driver_error, "diag", None)
    if diag is not None:  # psycopg
        return getattr(diag, "constraint_name", None)
    # SQLAlchemy's asyncpg adapter raises its error from asyncpg's.
    return getattr(getattr(driver_error, "__cause__", None), "constraint_name", None)


def _sqlite_constraint(message: str) -> tuple[str | None, list[tuple[str, str]] | None]:
    """The index, or the table and columns, of SQLite's message.

    ``UNIQUE constraint failed: t.a, t.b`` names columns; an index on
    expressions is named as ``UNIQUE constraint failed: index 'ix_name'``.
    """
    detail = message.removeprefix(_SQLITE_FAILED)
    if detail.startswith("index '"):
        return detail.removeprefix("index '").removesuffix("'"), None
    columns = [name.rpartition(".") for name in detail.split(", ")]
    return None, sorted((table, column) for table, _, column in columns)


def _uniques(metadata: MetaData) -> Iterator[_Unique]:
    """Every unique constraint and unique index of the metadata's tables."""
    for table in metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint | PrimaryKeyConstraint):
                yield constraint
        yield from (index for index in table.indexes if index.unique)


def _columns(unique: _Unique) -> list[tuple[str, str]]:
    """The (table, column) pairs a constraint covers, sorted."""
    return sorted((column.table.name, column.name) for column in unique.columns)


def _declared(unique: _Unique) -> type[DuplicateValue]:
    """The error a constraint, or its only column, names; else ``DuplicateValue``."""
    declared = unique.info.get("duplicate")
    columns = list(unique.columns)
    if declared is None and len(columns) == 1:
        declared = columns[0].info.get("duplicate")
    if declared is None:
        return DuplicateValue
    if not (isinstance(declared, type) and issubclass(declared, DuplicateValue)):
        raise TypeError(
            f'info["duplicate"] of {unique.name} must be a DuplicateValue '
            f"subclass, not {declared!r}."
        )
    return declared


def _message(fields: list[str]) -> str:
    """Say which fields are taken, without their values."""
    if not fields:
        return "This value is already taken."
    verb = "is" if len(fields) == 1 else "are"
    return f"The {' and '.join(fields)} {verb} already taken."
