"""Fixtures: rows as JSON, table by table, parents first.

``dumpdata`` writes them, ``loaddata`` reads them back, and ``flush``
empties tables. A fixture is one JSON document::

    {"format": "domainkit.fixture", "version": 1, "tables": [
        {"table": "shop_products", "rows": [{"id": "6f1c...", "name": "Lamp"}]}
    ]}

Values are JSON: numbers and text as they are, decimals, times, durations,
ids, and enums as text, bytes as base64, JSON columns as JSON. Loading
converts them back from each column's type, and puts a row whose primary
key exists in place of the stored one.

Some tables hold state, not data, and are never dumped or loaded: the audit
trail (its pseudonyms' keys would outlive ``forget`` in a copy), tokens,
the outbox, idempotency keys, and any table with ``info={"fixtures":
False}``. ``flush`` keeps append-only tables (``info={"append_only":
True}``, or with triggers of their own on PostgreSQL) and those with
``info={"flush": False}``, so the audit trail survives it.

Every operation runs as the schema owner (``MIGRATION_URL`` when set), in
one transaction per database.
"""

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy import (
    JSON,
    Column,
    ColumnElement,
    Table,
    delete,
    func,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import TypeEngine

from domainkit.db.connections import connections, create_owner_engine
from domainkit.db.orm import metadata
from domainkit.db.routing import router

FORMAT: Final = "domainkit.fixture"
VERSION: Final = 1
STATE_TABLES: Final = frozenset(
    {"domainkit_tokens", "domainkit_messages", "domainkit_idempotency_keys"}
)
"""Framework tables of state, never dumped or loaded (flushed, though)."""
CHUNK: Final = 500
"""Rows per statement when reading keys or writing rows."""
_TRIGGERED: Final = text(
    "SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE NOT t.tgisinternal AND c.relnamespace = current_schema()::regnamespace"
)


class FixtureError(Exception):
    """A fixture that cannot be loaded as it is; the message says why."""


@dataclass(frozen=True, slots=True)
class TableLoad:
    """What loading does to one table."""

    table: str
    rows: int
    replaced: int
    """Rows already stored under the same primary key, which loading replaces."""


def to_json(document: Mapping[str, Any], *, indent: int | None) -> str:
    """The fixture as JSON text; NaN and infinity are refused, not written."""
    return json.dumps(document, indent=indent, ensure_ascii=False, allow_nan=False)


def tables_of(database: str) -> list[Table]:
    """Every table the alias ``database`` holds, parents first."""
    target = router.target_of_alias(database)
    return [
        table for table in metadata.sorted_tables if router.holds_table(table, target)
    ]


def fixture_table(table: Table) -> bool:
    """Whether ``table`` holds data fixtures may carry."""
    return table.info.get("fixtures", True) is not False and table.name not in (
        STATE_TABLES
    )


def select_tables(database: str, names: Sequence[str] = ()) -> list[Table]:
    """The fixture tables of ``database``, all or those ``names`` pick.

    A name is a table or a domain label (every table of that domain).

    Raises:
        FixtureError: A name picks nothing.
    """
    tables = [table for table in tables_of(database) if fixture_table(table)]
    if not names:
        return tables
    picked: list[Table] = []
    for name in names:
        found = [t for t in tables if name in {t.name, t.info.get("domain")}]
        if not found:
            raise FixtureError(f"{name!r} names no fixture table in {database!r}.")
        picked.extend(table for table in found if table not in picked)
    return [table for table in tables if table in picked]


async def dump(database: str, tables: Sequence[Table]) -> dict[str, Any]:
    """The fixture of ``tables`` in ``database``, rows in primary key order."""
    documents = []
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.connect() as connection:
            for table in tables:
                query = select(table).order_by(*table.primary_key.columns)
                rows = (await connection.execute(query)).mappings()
                documents.append(
                    {
                        "table": table.name,
                        "rows": [_encoded_row(table, row) for row in rows],
                    }
                )
    finally:
        await engine.dispose()
    return {"format": FORMAT, "version": VERSION, "tables": documents}


def read_fixture(
    document: object, database: str
) -> list[tuple[Table, list[dict[str, Any]]]]:
    """The tables and rows of a fixture for ``database``, parents first.

    Raises:
        FixtureError: The document is not a fixture, names a table
            ``database`` does not hold or fixtures may not carry, or holds a
            value its column cannot take.
    """
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise FixtureError(f'Not a fixture: it needs "format": {FORMAT!r}.')
    if document.get("version") != VERSION:
        raise FixtureError(f"Fixture version {document.get('version')!r} is unknown.")
    entries = document.get("tables")
    if not isinstance(entries, list):
        raise FixtureError('A fixture\'s "tables" is a list.')
    held = {table.name: table for table in tables_of(database)}
    found: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("rows"), list):
            raise FixtureError('Each table of a fixture has a "table" and "rows".')
        name = entry.get("table")
        table = held.get(name) if isinstance(name, str) else None
        if table is None:
            raise FixtureError(f"{database!r} holds no table {name!r}.")
        if not fixture_table(table):
            raise FixtureError(f"{table.name!r} holds state; fixtures may not load it.")
        rows = found.setdefault(table.name, [])
        rows.extend(_decoded_row(table, row) for row in entry["rows"])
    return [
        (table, found[table.name]) for table in held.values() if table.name in found
    ]


async def plan_load(
    database: str, tables: Sequence[tuple[Table, list[dict[str, Any]]]]
) -> list[TableLoad]:
    """How many rows each table gets, and how many of them replace stored ones."""
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.connect() as connection:
            return [
                TableLoad(table.name, len(rows), await _stored(connection, table, rows))
                for table, rows in tables
            ]
    finally:
        await engine.dispose()


async def load(
    database: str, tables: Sequence[tuple[Table, list[dict[str, Any]]]]
) -> None:
    """Write the rows, parents first, in one transaction; then fix sequences."""
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.begin() as connection:
            for table, rows in tables:
                for start in range(0, len(rows), CHUNK):
                    await connection.execute(
                        _upsert(connection, table), rows[start : start + CHUNK]
                    )
            await _reset_sequences(connection, [table for table, _ in tables])
    finally:
        await engine.dispose()


async def flush_plan(database: str) -> tuple[dict[str, int], list[str]]:
    """Rows ``flush`` would delete, by table, and the tables it keeps."""
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.connect() as connection:
            emptied, kept = await _flush_tables(connection, database)
            counts = {
                table.name: int(
                    await connection.scalar(select(func.count()).select_from(table))
                    or 0
                )
                for table in emptied
            }
    finally:
        await engine.dispose()
    return counts, [table.name for table in kept]


async def flush(database: str) -> None:
    """Delete every row ``database`` holds, children first, but kept tables'."""
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.begin() as connection:
            emptied, _ = await _flush_tables(connection, database)
            for table in reversed(emptied):
                await connection.execute(delete(table))
            await _reset_sequences(connection, emptied)
    finally:
        await engine.dispose()


async def _flush_tables(
    connection: AsyncConnection, database: str
) -> tuple[list[Table], list[Table]]:
    """The tables ``flush`` empties, parents first, and those it keeps."""
    triggered: set[str] = set()
    if connection.dialect.name == "postgresql":
        triggered = set((await connection.scalars(_TRIGGERED)).all())
    existing = await connection.run_sync(
        lambda sync: set(sync.dialect.get_table_names(sync))
    )
    emptied, kept = [], []
    for table in tables_of(database):
        if table.name not in existing:
            continue
        if (
            table.info.get("append_only") is True
            or table.info.get("flush") is False
            or table.name in triggered
        ):
            kept.append(table)
        else:
            emptied.append(table)
    return emptied, kept


def _upsert(connection: AsyncConnection, table: Table) -> Any:  # noqa: ANN401
    """An insert that replaces the row stored under the same primary key."""
    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    statement = dialect.insert(table)
    keys = list(table.primary_key.columns)
    others = [column for column in table.columns if column not in keys]
    if not keys:
        return statement
    if not others:
        return statement.on_conflict_do_nothing(index_elements=keys)
    return statement.on_conflict_do_update(
        index_elements=keys,
        set_={column.name: statement.excluded[column.name] for column in others},
    )


async def _stored(
    connection: AsyncConnection, table: Table, rows: Sequence[Mapping[str, Any]]
) -> int:
    """How many of ``rows`` have a primary key the table already holds."""
    keys = list(table.primary_key.columns)
    if not keys:
        return 0
    wanted = [tuple(row.get(key.name) for key in keys) for row in rows]
    stored = 0
    for start in range(0, len(wanted), CHUNK):
        chunk = wanted[start : start + CHUNK]
        condition: ColumnElement[bool] = (
            keys[0].in_([key for (key,) in chunk])
            if len(keys) == 1
            else tuple_(*keys).in_(chunk)
        )
        query = select(func.count()).select_from(table).where(condition)
        stored += int(await connection.scalar(query) or 0)
    return stored


async def _reset_sequences(
    connection: AsyncConnection, tables: Iterable[Table]
) -> None:
    """Make PostgreSQL's serial columns continue after the highest stored id."""
    if connection.dialect.name != "postgresql":
        return
    for table in tables:
        column = table.autoincrement_column
        if column is None:
            continue
        await connection.execute(
            select(
                func.setval(
                    func.pg_get_serial_sequence(table.name, column.name),
                    func.coalesce(func.max(column), 1),
                    func.max(column).isnot(None),
                )
            ).select_from(table)
        )


def _encoded_row(table: Table, row: RowMapping) -> dict[str, Any]:
    """A row as JSON values, by column name."""
    return {column.name: _encoded(column, row[column]) for column in table.columns}


def _encoded(column: Column[Any], value: object) -> object:
    """One value as JSON: bytes as base64, the rest as pydantic writes them.

    Decimals, ids, times, durations, and enums become text.
    """
    if value is None or isinstance(column.type, JSON):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return to_jsonable_python(value)


def _decoded_row(table: Table, row: object) -> dict[str, Any]:
    """A fixture row as the values its columns take.

    Raises:
        FixtureError: The row is not an object, names a column the table
            lacks, or holds a value its column cannot take.
    """
    if not isinstance(row, dict):
        raise FixtureError(f"A row of {table.name!r} is not an object.")
    if unknown := sorted(set(row) - set(table.c.keys())):
        raise FixtureError(f"{table.name!r} has no column {', '.join(unknown)}.")
    return {name: _decoded(table.c[name], value) for name, value in row.items()}


def _decoded(column: Column[Any], value: object) -> object:
    """One JSON value as the column's Python type; never quoted in errors."""
    if value is None or isinstance(column.type, JSON):
        return value
    python_type = _python_type(column.type)
    if python_type is None:
        return value
    where = f"{column.table.name}.{column.name}"
    try:
        if python_type is bytes:
            if not isinstance(value, str):
                raise TypeError
            return base64.b64decode(value, validate=True)
        return TypeAdapter(python_type).validate_python(value)
    except (ValidationError, TypeError, binascii.Error):
        raise FixtureError(
            f"{where} holds a value that is not {python_type.__name__}."
        ) from None


def _python_type(type_: TypeEngine[Any]) -> type | None:
    """The Python type a column type holds; a decorator's from what it wraps."""
    for candidate in (type_, getattr(type_, "impl_instance", None)):
        try:
            return candidate.python_type  # type: ignore[union-attr]
        except (NotImplementedError, AttributeError):
            continue
    return None
