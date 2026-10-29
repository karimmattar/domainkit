"""Fixtures: rows as JSON, table by table, parents first.

``dumpdata`` writes them, ``loaddata`` reads them back, and ``flush``
empties tables. A fixture is one JSON document::

    {"format": "domainkit.fixture", "version": 1, "tables": [
        {"table": "shop_products", "rows": [{"id": "6f1c...", "name": "Lamp"}]}
    ]}

Values are JSON: numbers and text as they are, decimals, times, durations,
ids, and enums as text, bytes as base64, JSON columns as JSON. Loading
converts them back from each column's type, and only adds rows: one whose
primary key is stored is refused, unless the load is told to replace
(``loaddata --replace``). ``loaddata`` reads numbers with a fraction as
``Decimal``, so a decimal column gets every digit; any other column gets
the float JSON's number always gave it.

Some tables hold state, not data, and are never dumped or loaded: the audit
trail (its pseudonyms' keys would outlive ``forget`` in a copy), tokens,
the outbox, idempotency keys, and any table with ``info={"fixtures":
False}``. ``flush`` keeps append-only tables (``info={"append_only":
True}``, or with triggers of their own on PostgreSQL) and those with
``info={"flush": False}``, so the audit trail survives it.

Guarded tables (``info={"guarded": True}``) hold credentials, grants, or
secrets: users, groups, permissions and their links, tokens, second
factors, content types, object grants, dynamic settings. A row written
there is a login or a privilege, so ``loaddata`` refuses a fixture that
writes one, and ``flush`` keeps them, with every table they reference,
unless the operator says otherwise (``--include-guarded``).

A kept table whose rows are about other tables' objects names the column
that holds each row's model key (``info={"about_model": "content_type"}``,
as object grants do): ``flush`` deletes its rows about the models whose
tables it empties, in the same transaction. A grant goes with its object,
so an object given a flushed one's id later starts without it.

Every operation runs as the schema owner (``MIGRATION_URL`` when set), in
one transaction per database.
"""

import base64
import binascii
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import batched, groupby
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import (
    JSON,
    Column,
    ColumnElement,
    Table,
    delete,
    func,
    inspect,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import TypeEngine

from domainkit.db.connections import connections, create_owner_engine
from domainkit.db.derive import ValueObjectType
from domainkit.db.models import installed_models
from domainkit.db.orm import metadata
from domainkit.db.routing import router
from domainkit.ddd.personal import personal_fields
from domainkit.exceptions import DomainkitError
from domainkit.utils.decimals import jsonable

FORMAT: Final = "domainkit.fixture"
VERSION: Final = 1
STATE_TABLES: Final = frozenset(
    {
        "domainkit_tokens",
        "domainkit_messages",
        "domainkit_idempotency_keys",
        "domainkit_webhook_subscriptions",
        "domainkit_webhook_deliveries",
        "domainkit_webhook_attempts",
    }
)
"""Framework tables of state or secrets, never dumped or loaded (flushed, though).

Webhook subscriptions hold their signing secrets: set them up in each
environment instead.
"""
CHUNK: Final = 500
"""Rows per statement when reading keys or writing rows."""
_TRIGGERED: Final = text(
    "SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE NOT t.tgisinternal AND c.relnamespace = current_schema()::regnamespace"
)


class FixtureError(DomainkitError):
    """A fixture that cannot be loaded as it is; the message says why."""


@dataclass(frozen=True, slots=True)
class TableLoad:
    """What loading does to one table."""

    table: str
    rows: int
    replaced: int
    """Rows already stored under the same primary key, which loading replaces."""
    guarded: bool = False
    """Whether the table is guarded (``guarded``)."""


@dataclass(frozen=True, slots=True)
class FlushPlan:
    """What ``flush`` deletes and what it keeps."""

    counts: dict[str, int]
    """Rows deleted, by table, parents first."""
    kept: list[str]
    """Tables kept for good: append-only, triggered, or ``info={"flush": False}``."""
    guarded: list[str]
    """Tables kept unless guarded ones are included: guarded, or referenced by one."""
    about: dict[str, int]
    """Rows of kept tables (``about_model``) deleted with the objects they are about."""


def to_json(document: Mapping[str, Any], *, indent: int | None) -> str:
    """The fixture as JSON text; NaN and infinity are refused, not written."""
    return json.dumps(document, indent=indent, ensure_ascii=False, allow_nan=False)


def tables_of(database: str) -> list[Table]:
    """Every table the alias ``database`` holds, parents first."""
    target = router.target_of_alias(database)
    return [
        table for table in metadata.sorted_tables if router.holds_table(table, target)
    ]


def guarded(table: Table) -> bool:
    """Whether ``table`` holds credentials, grants, or secrets (``info["guarded"]``)."""
    return table.info.get("guarded") is True


def personal(table: Table) -> bool:
    """Whether ``table`` holds a model with fields marked ``Personal``.

    Such a dump is people's data: where deployed, ``dumpdata`` records it
    and wants a named operator, as ``privacy export`` of one person is
    recorded. A marker in the wrong place still marks personal data.
    """
    for model in installed_models():
        if inspect(model.cls).local_table is not table:
            continue
        try:
            if personal_fields(model.cls):
                return True
        except TypeError:  # A marker below the field: still personal.
            return True
    return False


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
                TableLoad(
                    table.name,
                    len(rows),
                    await _stored(connection, table, rows),
                    guarded(table),
                )
                for table, rows in tables
            ]
    finally:
        await engine.dispose()


async def load(
    database: str,
    tables: Sequence[tuple[Table, list[dict[str, Any]]]],
    *,
    replace: bool,
) -> list[TableLoad]:
    """Write the rows, parents first, in one transaction; then fix sequences.

    Without ``replace`` the rows are only inserted: one whose primary key
    is stored, even one stored since ``plan_load`` counted, fails the load
    with the database's ``IntegrityError``. Returns what each table got,
    counted in the load's own transaction once it holds the tables' write
    lock, so a row stored after ``plan_load`` counted is counted as
    replaced: what the load wrote, not what it planned.
    """
    engine = create_owner_engine(connections.config(database))
    loaded = []
    try:
        async with engine.begin() as connection:
            await _lock(connection, [table for table, _ in tables])
            for table, rows in tables:
                replaced = await _stored(connection, table, rows)
                statement = _insert(connection, table, replace=replace)
                for batch in _batches(rows):
                    await connection.execute(statement, batch)
                loaded.append(
                    TableLoad(table.name, len(rows), replaced, guarded(table))
                )
            await _reset_sequences(connection, [table for table, _ in tables])
    finally:
        await engine.dispose()
    return loaded


def _batches(rows: Sequence[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], ...]]:
    """``rows`` in order, ``CHUNK`` at a time, each batch naming the same columns.

    A batch is one ``executemany``, which takes its columns from its first
    row: a column a later row named and the first left out would be
    dropped without a word. Rows stay in order, so a row a later one
    references (in a table that references itself) is written first.
    """
    for _, alike in groupby(rows, key=frozenset):
        yield from batched(alike, CHUNK, strict=False)


async def _lock(connection: AsyncConnection, tables: Sequence[Table]) -> None:
    """Hold ``tables`` against other writers until the transaction ends.

    PostgreSQL: ``SHARE ROW EXCLUSIVE``, which lets readers in and keeps
    every other writer (another load too) out. SQLite: the database's
    write lock, taken as the transaction begins (``BEGIN IMMEDIATE``).
    """
    if connection.dialect.name == "postgresql":
        if tables:
            names = ", ".join(
                connection.dialect.identifier_preparer.format_table(t) for t in tables
            )
            await connection.exec_driver_sql(
                f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE"
            )
        return
    await connection.exec_driver_sql("BEGIN IMMEDIATE")


async def flush_plan(database: str, *, include_guarded: bool) -> FlushPlan:
    """Rows ``flush`` would delete, by table, and the tables it keeps."""
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.connect() as connection:
            emptied, kept, held = await _flush_tables(
                connection, database, include_guarded=include_guarded
            )
            counts = {
                table.name: int(
                    (
                        await connection.execute(
                            select(func.count()).select_from(table)
                        )
                    ).scalar_one()
                )
                for table in emptied
            }
            about = {
                table.name: int(
                    (
                        await connection.execute(
                            select(func.count()).select_from(table).where(condition)
                        )
                    ).scalar_one()
                )
                for table, condition in _about(emptied, held)
            }
    finally:
        await engine.dispose()
    return FlushPlan(counts, [t.name for t in kept], [t.name for t in held], about)


async def flush(database: str, *, include_guarded: bool) -> None:
    """Delete every row ``database`` holds, children first, but kept tables'."""
    engine = create_owner_engine(connections.config(database))
    try:
        async with engine.begin() as connection:
            emptied, _, held = await _flush_tables(
                connection, database, include_guarded=include_guarded
            )
            for table, condition in _about(emptied, held):
                await connection.execute(delete(table).where(condition))
            for table in reversed(emptied):
                await connection.execute(delete(table))
            await _reset_sequences(connection, emptied)
    finally:
        await engine.dispose()


def _about(
    emptied: Sequence[Table], held: Sequence[Table]
) -> list[tuple[Table, ColumnElement[bool]]]:
    """Each kept table's rows about the objects of ``emptied`` (``about_model``).

    A row names its object's model by its key (``billing.invoice``), as
    ``domainkit.db.models`` gives it; the models of the emptied tables are
    looked up among the installed ones.
    """
    keys = sorted(
        ".".join(model.key)
        for model in installed_models()
        if inspect(model.cls).local_table in emptied
    )
    return [
        (table, table.c[table.info["about_model"]].in_(keys))
        for table in held
        if keys and "about_model" in table.info
    ]


async def _flush_tables(
    connection: AsyncConnection, database: str, *, include_guarded: bool
) -> tuple[list[Table], list[Table], list[Table]]:
    """The tables ``flush`` empties, those it keeps, and the guarded ones it keeps.

    Each list is parents first. A guarded table keeps every table it
    references too: deleting a parent would fail, or cascade into it.
    """
    triggered: set[str] = set()
    if connection.dialect.name == "postgresql":
        triggered = set((await connection.scalars(_TRIGGERED)).all())
    existing = await connection.run_sync(
        lambda sync: set(sync.dialect.get_table_names(sync))
    )
    tables = [table for table in tables_of(database) if table.name in existing]
    held = set() if include_guarded else _referenced(filter(guarded, tables))
    emptied, kept, guarded_kept = [], [], []
    for table in tables:
        if (
            table.info.get("append_only") is True
            or table.info.get("flush") is False
            or table.name in triggered
        ):
            kept.append(table)
        elif table in held:
            guarded_kept.append(table)
        else:
            emptied.append(table)
    return emptied, kept, guarded_kept


def _referenced(tables: Iterable[Table]) -> set[Table]:
    """``tables``, and every table they reference, directly or not."""
    found: set[Table] = set()
    waiting = list(tables)
    while waiting:
        table = waiting.pop()
        if table not in found:
            found.add(table)
            waiting.extend(key.column.table for key in table.foreign_keys)
    return found


def _insert(connection: AsyncConnection, table: Table, *, replace: bool) -> Any:  # noqa: ANN401
    """An insert; with ``replace``, one that replaces the row stored under its key."""
    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    statement = dialect.insert(table)
    keys = list(table.primary_key.columns)
    others = [column for column in table.columns if column not in keys]
    if not (replace and keys):
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

    Decimals, ids, times, durations, and enums become text. A single-field
    value object's column (``ValueObjectType``: a password hash, a
    project's ``ProductName``) holds the field, as the database does.
    """
    if value is None or isinstance(column.type, JSON):
        return value
    if isinstance(column.type, ValueObjectType):
        value = getattr(value, column.type.field)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return jsonable(value)


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
    """One JSON value as the column's Python type; never quoted in errors.

    A ``Decimal`` number (as ``loaddata`` reads one with a fraction) stays
    one for a decimal column, and is the float it always was for any
    other, JSON columns included. A single-field value object's column
    takes the field's value, made into the object.
    """
    if value is None:
        return value
    if isinstance(column.type, ValueObjectType):
        return _value_object(column, column.type, value)
    return _converted(column, column.type, value)


def _converted(column: Column[Any], type_: TypeEngine[Any], value: object) -> object:
    """``value`` as the Python type ``type_`` holds, for ``column``."""
    python_type = _python_type(type_)
    if isinstance(type_, JSON) or python_type is None:
        return _floats(value)
    if not (isinstance(python_type, type) and issubclass(python_type, Decimal)):
        value = _floats(value)
    try:
        if python_type is bytes:
            if not isinstance(value, str):
                raise TypeError
            return base64.b64decode(value, validate=True)
        return TypeAdapter(python_type).validate_python(value)
    except (ValidationError, TypeError, binascii.Error):
        raise FixtureError(
            f"{_where(column)} holds a value that is not {python_type.__name__}."
        ) from None


def _value_object(
    column: Column[Any], type_: ValueObjectType[Any], value: object
) -> object:
    """A value object column's value: its field's, made into the object.

    The object checks what it holds, as it does in the domain, and so does
    the column type (``ValueObjectType.loaded``): a password hash that is
    not a hash, or one no configured hasher would make, is refused.
    Fixtures written before 0.12 hold the object as ``{"<field>": ...}``,
    which reads the same.

    Raises:
        FixtureError: The value is not one the object takes.
    """
    if isinstance(value, dict) and set(value) == {type_.field}:
        value = value[type_.field]
    field = _converted(column, type_.impl_instance, value)
    what = f"{_where(column)} holds a value that is not {type_.cls.__name__}"
    try:
        return type_.loaded(field)
    except FixtureError as exc:  # Says why, never the value.
        raise FixtureError(f"{what}: {exc}") from None
    except (ValueError, TypeError):
        raise FixtureError(f"{what}.") from None


def _where(column: Column[Any]) -> str:
    """``table.column``, as errors name a column."""
    return f"{column.table.name}.{column.name}"


def _floats(value: object) -> object:
    """``value`` with every ``Decimal`` in it a float, as JSON would read it."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _floats(item) for key, item in value.items()}
    return value


def _python_type(type_: TypeEngine[Any]) -> type | None:
    """The Python type a column type holds; a decorator's from what it wraps.

    A type that cannot tell answers ``object`` (SQLAlchemy 2.1) or raises
    ``NotImplementedError`` (types written for 2.0): both mean unknown.
    """
    for candidate in (type_, getattr(type_, "impl_instance", None)):
        try:
            python_type: type = candidate.python_type  # type: ignore[union-attr]
        except (NotImplementedError, AttributeError):
            continue
        if python_type is not object:
            return python_type
    return None
