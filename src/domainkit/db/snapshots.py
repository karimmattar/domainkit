"""Put databases back as migrations left them, between tests.

``take_snapshot`` reads every table of every configured database once, right
after migrating: the rows ``migrate`` itself wrote (content types,
permissions, the migration version) included. ``restore_snapshot`` deletes
every row and puts those back, so each test starts from the same state,
whatever the last one committed, through any connection or event loop.

Rows are copied as the driver returns them, without type conversion, so
they go back unchanged; JSON is read as its text, since SQLAlchemy's asyncpg
adapter decodes JSON it reads but only takes text to write. Generated
columns are left to the database. Tables are emptied children first and
refilled parents first. On PostgreSQL, partitions are refilled through their
parent, and tables with triggers of their own (such as an append-only audit
trail) have them turned off while they are emptied, inside the same
transaction. Sequences are not reset.

Both connect as the schema owner (``MIGRATION_URL`` when set): emptying an
append-only table needs its owner. ``restore_snapshot`` deletes every row,
so it runs only with ``ENVIRONMENT = "test"``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    ColumnElement,
    Connection,
    MetaData,
    Select,
    TableClause,
    Text,
    cast,
    column,
    delete,
    insert,
    select,
    text,
)
from sqlalchemy import table as table_clause
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.conf import settings
from domainkit.db.connections import create_owner_engine
from domainkit.exceptions import ImproperlyConfigured

_PARTITIONS = text(
    "SELECT relname FROM pg_class "
    "WHERE relispartition AND relnamespace = current_schema()::regnamespace"
)
_TRIGGERED = text(
    "SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE NOT t.tgisinternal AND c.relnamespace = current_schema()::regnamespace"
)


@dataclass(frozen=True, slots=True)
class TableRows:
    """One table's rows, and whether its own triggers must be off to empty it."""

    table: TableClause
    rows: Sequence[dict[str, Any]]
    triggered: bool = False


type Snapshot = dict[str, list[TableRows]]
"""Each database alias's tables, parents first, with their rows."""


@dataclass(frozen=True, slots=True)
class _Table:
    """A reflected table: the clause to write it, the query to read it."""

    clause: TableClause
    query: Select[Any]
    triggered: bool


async def take_snapshot() -> Snapshot:
    """Read every table of every database in ``DATABASES``."""
    snapshot: Snapshot = {}
    for alias, config in settings.DATABASES.items():
        engine = create_owner_engine(config)
        try:
            async with engine.connect() as connection:
                snapshot[alias] = [
                    TableRows(
                        table.clause,
                        [dict(row) for row in await _rows(connection, table.query)],
                        table.triggered,
                    )
                    for table in await connection.run_sync(_tables)
                ]
        finally:
            await engine.dispose()
    return snapshot


async def restore_snapshot(snapshot: Snapshot) -> None:
    """Empty every table, then put the snapshot's rows back, one database at a time."""
    if settings.ENVIRONMENT != "test":
        raise ImproperlyConfigured(
            f"restore_snapshot deletes every row in DATABASES; it runs only with "
            f'ENVIRONMENT = "test", not {settings.ENVIRONMENT!r}.'
        )
    for alias, tables in snapshot.items():
        engine = create_owner_engine(settings.DATABASES[alias])
        try:
            async with engine.begin() as connection:
                for entry in reversed(tables):
                    await _empty(connection, entry)
                for entry in tables:
                    if entry.rows:
                        await connection.execute(insert(entry.table), entry.rows)
        finally:
            await engine.dispose()


def _tables(connection: Connection) -> list[_Table]:
    """The database's tables, parents first, as plain clauses without types."""
    reflected = MetaData()
    reflected.reflect(connection)
    partitions: set[str] = set()
    triggered: set[str] = set()
    if connection.dialect.name == "postgresql":
        partitions = set(connection.scalars(_PARTITIONS))
        triggered = set(connection.scalars(_TRIGGERED))
    tables: list[_Table] = []
    for table in reflected.sorted_tables:
        if table.name in partitions:
            continue
        columns = [c for c in table.columns if c.computed is None]
        clause = table_clause(table.name, *(column(c.name) for c in columns))
        query = select(*(_raw(clause, c) for c in columns))
        tables.append(_Table(clause, query, table.name in triggered))
    return tables


def _raw(clause: TableClause, reflected: Column[Any]) -> ColumnElement[Any]:
    """The column as the driver returns it; JSON as its text."""
    plain = clause.c[reflected.name]
    if isinstance(reflected.type, JSON):
        return cast(plain, Text).label(reflected.name)
    return plain


async def _rows(connection: AsyncConnection, query: Select[Any]) -> Any:  # noqa: ANN401
    """The query's rows, as mappings."""
    return (await connection.execute(query)).mappings()


async def _empty(connection: AsyncConnection, entry: TableRows) -> None:
    """Delete every row of the entry's table, its own triggers off if it has any."""
    name = connection.dialect.identifier_preparer.format_table(entry.table)
    # The table's name, quoted by the dialect, is all the statements take.
    if entry.triggered:
        # nosemgrep: avoid-sqlalchemy-text  # noqa: ERA001
        await connection.execute(text(f"ALTER TABLE {name} DISABLE TRIGGER USER"))
    await connection.execute(delete(entry.table))
    if entry.triggered:
        # nosemgrep: avoid-sqlalchemy-text  # noqa: ERA001
        await connection.execute(text(f"ALTER TABLE {name} ENABLE TRIGGER USER"))
