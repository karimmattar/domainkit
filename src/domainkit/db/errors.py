"""Integrity errors in domain terms: what the database refused, never the values.

A unique violation is a ``DuplicateValue``. PostgreSQL names the constraint
or index it enforced (SQLSTATE 23505); SQLite names the table and columns,
or the index on expressions. Either way the constraint is looked up in the
metadata. If the constraint, or its only column, names a ``DuplicateValue``
subclass under ``info["duplicate"]``, that class is raised, so each
constraint can have its own error code::

    Column("email", String(254), unique=True, info={"duplicate": EmailTaken})

The other integrity errors are described by ``integrity_violation``: a
foreign key (23503), a NOT NULL column (23502), or a CHECK constraint
(23514), from the SQLSTATE on PostgreSQL and the error's name on SQLite.

Errors name fields as the API knows them: a column's ``info["field"]``, or
the attribute a mapped class keeps it in, or else the column's name.
Messages name the fields, never the values, which may be personal data.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import UnmappedColumnError

from domainkit.db.orm import mapper_registry
from domainkit.ddd import DuplicateValue

UNIQUE_VIOLATION: Final = "23505"
"""PostgreSQL's SQLSTATE for a unique violation (primary keys included)."""
SQLITE_UNIQUE: Final = frozenset(
    {"SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"}
)
_SQLITE_FAILED: Final = "UNIQUE constraint failed: "

type ViolationKind = Literal["foreign_key", "not_null", "check"]

POSTGRES_KINDS: Final[dict[str, ViolationKind]] = {
    "23503": "foreign_key",
    "23502": "not_null",
    "23514": "check",
}
"""The other integrity SQLSTATEs, by kind."""
SQLITE_KINDS: Final[dict[str, ViolationKind]] = {
    "SQLITE_CONSTRAINT_FOREIGNKEY": "foreign_key",
    "SQLITE_CONSTRAINT_NOTNULL": "not_null",
    "SQLITE_CONSTRAINT_CHECK": "check",
}
"""The same, by SQLite's extended error name."""
_SQLITE_NOT_NULL: Final = "NOT NULL constraint failed: "
_SQLITE_CHECK: Final = "CHECK constraint failed: "
_STILL_REFERENCED: Final = "update or delete on table"
"""How PostgreSQL starts a foreign key error about a row others refer to."""

type _Unique = UniqueConstraint | PrimaryKeyConstraint | Index


@dataclass(frozen=True, slots=True)
class IntegrityViolation:
    """A refused change other than a duplicate: what was broken, not with what."""

    kind: ViolationKind
    constraint: str | None = None
    """The constraint's name, when the database gave it."""
    fields: tuple[str, ...] = ()
    """The fields involved, as the API names them; empty when unknown."""
    still_referenced: bool | None = None
    """Foreign keys: True when a row others refer to was deleted or changed,
    False when the row refers to one that does not exist, None if unknown."""


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
            fields = [field_name(column) for column in unique.columns]
            constraint = unique.name if isinstance(unique.name, str) else None
            return _declared(unique)(
                _message(fields), constraint=constraint, fields=fields
            )
    fields = [] if columns is None else [column for _, column in columns]
    return DuplicateValue(_message(fields), constraint=name, fields=fields)


def integrity_violation(
    error: IntegrityError, metadata: MetaData
) -> IntegrityViolation | None:
    """Describe a foreign key, NOT NULL, or CHECK violation; None for others.

    Unique violations are ``duplicate_value``'s. The constraint and columns
    come from the driver and are looked up in ``metadata`` for the fields'
    names; nothing of the row is kept.
    """
    driver_error = error.orig
    state = getattr(driver_error, "sqlstate", None) or getattr(
        driver_error, "pgcode", None
    )
    if isinstance(state, str) and state in POSTGRES_KINDS:
        return _postgres_violation(POSTGRES_KINDS[state], driver_error, metadata)
    name = getattr(driver_error, "sqlite_errorname", None)
    if isinstance(name, str) and name in SQLITE_KINDS:
        return _sqlite_violation(SQLITE_KINDS[name], str(driver_error), metadata)
    return None


def field_name(column: Column[object]) -> str:
    """The name the API gives ``column``.

    ``info["field"]`` when set; else the attribute of the class mapped onto
    its table that holds it; else the column's own name.
    """
    declared = column.info.get("field")
    if isinstance(declared, str):
        return declared
    for mapper in mapper_registry.mappers:
        if mapper.local_table is column.table:
            try:
                return mapper.get_property_by_column(column).key
            except UnmappedColumnError:
                continue
    return column.name


def _postgres_violation(
    kind: ViolationKind, driver_error: BaseException | None, metadata: MetaData
) -> IntegrityViolation:
    """Read the constraint, table, and column PostgreSQL reported."""
    details = _diagnostics(driver_error)
    constraint = details.get("constraint_name")
    table = _table(metadata, details.get("table_name"))
    if kind == "not_null":
        return IntegrityViolation(
            kind, None, _fields(table, [details.get("column_name")])
        )
    found = _constraint(table, constraint)
    if kind == "check":
        return IntegrityViolation(kind, constraint, _constraint_fields(table, found))
    referenced = (details.get("message_primary") or "").startswith(_STILL_REFERENCED)
    fields = () if referenced else _constraint_fields(table, found)
    return IntegrityViolation(kind, constraint, fields, referenced)


def _sqlite_violation(
    kind: ViolationKind, message: str, metadata: MetaData
) -> IntegrityViolation:
    """Read what SQLite's message names: a column, a check, or nothing."""
    if kind == "not_null":
        table_name, _, column = message.removeprefix(_SQLITE_NOT_NULL).rpartition(".")
        return IntegrityViolation(
            kind, None, _fields(_table(metadata, table_name), [column])
        )
    if kind == "check":
        name = message.removeprefix(_SQLITE_CHECK)
        for table in metadata.tables.values():
            if (found := _constraint(table, name)) is not None:
                return IntegrityViolation(kind, name, _constraint_fields(table, found))
        return IntegrityViolation(kind, name)
    return IntegrityViolation(kind)  # SQLite names neither side of a foreign key.


def _diagnostics(driver_error: BaseException | None) -> dict[str, str | None]:
    """The constraint, table, column, and message of a PostgreSQL error.

    psycopg keeps them in ``diag``; SQLAlchemy's asyncpg adapter raises its
    error from asyncpg's, which has them as attributes.
    """
    names = ("constraint_name", "table_name", "column_name")
    diag = getattr(driver_error, "diag", None)
    if diag is not None:  # psycopg
        found = {name: getattr(diag, name, None) for name in names}
        found["message_primary"] = getattr(diag, "message_primary", None)
        return found
    cause = getattr(driver_error, "__cause__", None)
    found = {name: getattr(cause, name, None) for name in names}
    message = getattr(cause, "message", None)
    found["message_primary"] = message if isinstance(message, str) else None
    return found


def _table(metadata: MetaData, name: str | None) -> Table | None:
    """The table called ``name`` in ``metadata``, if any."""
    return None if name is None else metadata.tables.get(name)


def _constraint(
    table: Table | None, name: str | None
) -> CheckConstraint | ForeignKeyConstraint | None:
    """The check or foreign key constraint of ``table`` called ``name``."""
    if table is None or name is None:
        return None
    constraints = [*table.constraints]
    for column in table.columns:
        constraints.extend(column.constraints)
    for constraint in constraints:
        if (
            isinstance(constraint, CheckConstraint | ForeignKeyConstraint)
            and constraint.name == name
        ):
            return constraint
    return None


def _constraint_fields(
    table: Table | None,
    constraint: CheckConstraint | ForeignKeyConstraint | None,
) -> tuple[str, ...]:
    """The fields of the table's columns ``constraint`` covers."""
    if table is None or constraint is None:
        return ()
    return _fields(table, [column.name for column in constraint.columns])


def _fields(table: Table | None, columns: list[str | None]) -> tuple[str, ...]:
    """The fields of ``columns`` that ``table`` has."""
    if table is None:
        return ()
    return tuple(
        field_name(table.c[column])
        for column in columns
        if column is not None and column in table.c
    )


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
