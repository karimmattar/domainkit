"""Keep tables append-only in the database itself (PostgreSQL).

Revoking ``UPDATE`` and ``DELETE`` from the application's role is undone
by the next broad ``GRANT``. Two triggers per table refuse updates,
deletes, and truncation for every role, the table owner's included; only
the owner can drop them. ``contrib.audit`` protects its trail this way,
and ``contrib.accounting`` its entries and postings, with the same
function names and bodies (``append_only_statements``), and the same
check that they are there as made (``missing_append_only``,
``unprotected``), in a database's own schema and in each tenant's
(``TENANCY.SCHEMA_DATABASES``). New in 2.9 (ADR 0188); before, each app
had its own copy, and the ledger's counted a trigger given arguments.

On other databases nothing is made and nothing is missing: SQLite has no
roles to keep out.

A table is named as its triggers can name it: in lowercase letters,
digits, and underscores, at most ``MAX_TABLE_NAME`` (42) characters, so
that ``<table>_append_only_truncate`` fits PostgreSQL's 63, and with no
schema of its own, so each tenant's schema gets its own triggers. Every
function here refuses another table with ``ValueError``, on every
database, before it makes or asks anything.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from sqlalchemy import Table, text
from sqlalchemy.dialects.postgresql.base import RESERVED_WORDS
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.db.uow import tenant_schemas

__all__ = [
    "MAX_TABLE_NAME",
    "ROW_TRIGGER",
    "TRUNCATE_TRIGGER",
    "append_only_body",
    "append_only_statements",
    "migrate_command",
    "missing_append_only",
    "protect_append_only",
    "trigger_names",
    "unprotected",
]

ROW_TRIGGER: Final = 1 | 2 | 8 | 16
"""``pg_trigger.tgtype`` of ``BEFORE UPDATE OR DELETE ... FOR EACH ROW``.

The bits are ``ROW`` (1), ``BEFORE`` (2), ``DELETE`` (8), and ``UPDATE``
(16), as PostgreSQL's ``catalog/pg_trigger.h`` defines them."""
TRUNCATE_TRIGGER: Final = 2 | 32
"""``pg_trigger.tgtype`` of ``BEFORE TRUNCATE ... FOR EACH STATEMENT``
(``BEFORE``, 2, and ``TRUNCATE``, 32)."""
MAX_TABLE_NAME: Final = 63 - len("_append_only_truncate")
"""The longest table name whose triggers' names PostgreSQL keeps whole (63 bytes)."""
_TABLE_NAME: Final = re.compile(r"[a-z_][a-z0-9_]*")
"""What PostgreSQL keeps as written when it is not quoted, so every name
made from it (the function, the triggers) is the same quoted or not."""
FIRING: Final = frozenset({"O", "A"})
"""``tgenabled`` of a trigger that fires for the application's sessions.

``O`` fires unless the session is a replica's, ``A`` always; ``R`` fires
only in replica sessions, ``D`` never."""
_OWN_TRIGGERS: Final = text(
    "SELECT c.relname, t.tgname, t.tgenabled::text AS enabled, t.tgtype, "
    "t.tgqual IS NULL AND t.tgnargs = 0 AND cardinality(t.tgattr::int2[]) = 0 "
    "AS plain, p.proname, p.prosrc, l.lanname "
    "FROM pg_trigger AS t "
    "JOIN pg_class AS c ON c.oid = t.tgrelid "
    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
    "JOIN pg_language AS l ON l.oid = p.prolang "
    "WHERE t.tgrelid IN "
    "(SELECT to_regclass(name) FROM unnest(CAST(:tables AS text[])) AS name) "
    "AND NOT t.tgisinternal"
)
"""The triggers on the tables as ``search_path`` finds them, with what decides
whether they fire and what they run."""
_TENANT_TRIGGERS: Final = text(
    "SELECT n.nspname, c.relname, t.tgname, t.tgenabled::text AS enabled, "
    "t.tgtype, "
    "t.tgqual IS NULL AND t.tgnargs = 0 AND cardinality(t.tgattr::int2[]) = 0 "
    "AS plain, p.proname, p.prosrc, l.lanname "
    "FROM pg_trigger AS t "
    "JOIN pg_class AS c ON c.oid = t.tgrelid "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
    "JOIN pg_language AS l ON l.oid = p.prolang "
    "WHERE c.relname = ANY(:tables) AND n.nspname = ANY(:schemas) "
    "AND NOT t.tgisinternal"
)
"""The same triggers in every schema named, in one round trip."""
_HOLDING: Final = text(
    "SELECT n.nspname FROM pg_class AS c "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "WHERE c.relname = :table AND n.nspname = ANY(:schemas)"
)
"""The schemas, of those named, that hold a table."""
_FOUND: Final = text("SELECT to_regclass(:table)")
"""The table as ``search_path`` finds it, if there is one."""

type Expected = Mapping[tuple[str, str], tuple[int, str, str]]
"""Each trigger by table and name: its ``tgtype``, its function, and its body."""


def trigger_names(table: Table) -> tuple[str, str]:
    """The triggers that protect ``table``: of rows, and of truncation.

    The row trigger's name is its function's too: ``<table>_append_only``.

    Raises:
        ValueError: ``table`` has a schema of its own, or a name its
            triggers cannot be named after (see the module).
    """
    function = f"{_checked(table)}_append_only"
    return function, f"{function}_truncate"


def append_only_body(table: Table) -> str:
    """The body of the triggers' function for ``table``: refuse, whatever the change.

    Raises:
        ValueError: As ``trigger_names``.
    """
    _checked(table)
    return f"""
    BEGIN
        RAISE EXCEPTION '{table.name} is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END
    """


def append_only_statements(table: Table) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The statements that protect ``table``, and those that undo them.

    Protecting replaces what is there: the function, and each trigger
    dropped and made again, so a trigger changed in place (disabled, under
    a ``WHEN``, running another function) is put back as it must be.
    Names are not qualified: they are the schema's ``search_path`` finds.
    A table named by a reserved word (``order``) is quoted; the others are
    written as 2.8 wrote them.

    Raises:
        ValueError: As ``trigger_names``.
    """
    function, truncate = trigger_names(table)
    body = append_only_body(table)
    name = f'"{table.name}"' if table.name in RESERVED_WORDS else table.name
    protect = (
        f"""
    CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql
    AS $${body}$$
    """,
        f"DROP TRIGGER IF EXISTS {function} ON {name}",
        (
            f"CREATE TRIGGER {function} BEFORE UPDATE OR DELETE ON {name} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        ),
        f"DROP TRIGGER IF EXISTS {truncate} ON {name}",
        (
            f"CREATE TRIGGER {truncate} BEFORE TRUNCATE ON {name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
        ),
    )
    unprotect = (
        f"DROP TRIGGER IF EXISTS {truncate} ON {name}",
        f"DROP TRIGGER IF EXISTS {function} ON {name}",
        f"DROP FUNCTION IF EXISTS {function}()",
    )
    return protect, unprotect


async def protect_append_only(
    connection: AsyncConnection, tables: Iterable[Table]
) -> list[str]:
    """Make the triggers of each of ``tables`` there is, afresh; the tables protected.

    Only on PostgreSQL, and only the tables the connection's ``search_path``
    finds (a tenant's schema, after ``migrate --tenant``): a table not
    made yet is left for the ``migrate`` that makes it.

    Raises:
        ValueError: As ``trigger_names``, for any of ``tables``.
    """
    tables = [table for table in tables if _checked(table)]
    if connection.dialect.name != "postgresql":
        return []
    protected = []
    for table in tables:
        if await connection.scalar(_FOUND, {"table": table.name}) is None:
            continue
        for statement in append_only_statements(table)[0]:
            await connection.exec_driver_sql(statement)
        protected.append(table.name)
    return protected


async def missing_append_only(
    connection: AsyncConnection, tables: Sequence[Table]
) -> list[str]:
    """The triggers of ``tables`` the connection's schema lacks, disabled, or changed.

    A trigger counts only as ``append_only_statements`` makes it: firing
    for the application's sessions (not ``ENABLE REPLICA`` or
    ``DISABLE``), on the same events, for each row or statement, with no
    ``WHEN``, columns, or arguments, and running the PL/pgSQL function
    made for its table, its body the same but for whitespace. The tables
    are those ``search_path`` finds; a table there is not lacks both. None
    on databases other than PostgreSQL, and for no tables.

    Raises:
        ValueError: As ``trigger_names``, for any of ``tables``.
    """
    expected = _expected(tables)
    if connection.dialect.name != "postgresql" or not tables:
        return []
    found = await connection.execute(
        _OWN_TRIGGERS, {"tables": [table.name for table in tables]}
    )
    present = {(row.relname, row.tgname) for row in found if _made(row, expected)}
    return sorted(name for table, name in expected.keys() - present)


async def unprotected(database: str, tables: Sequence[Table]) -> dict[str, list[str]]:
    """The triggers of ``tables`` that ``database`` lacks, by where it lacks them.

    Where is ``database`` for its own schema, and ``<database>/<schema>``
    for each tenant's schema that holds the first of ``tables``
    (``TENANCY.SCHEMA_DATABASES``); only where something is lacking. A
    database that keeps a schema per tenant, and holds no tables of its
    own (plain ``migrate`` leaves its own schema out), is not asked for
    its own. Empty on databases other than PostgreSQL, and for no tables.
    Two round trips, and two more for every tenant at once.

    Raises:
        ValueError: As ``trigger_names``, for any of ``tables``.
    """
    _expected(tables)
    if not tables:
        return {}
    config = settings.TENANCY
    per_tenant = config is not None and database in config.SCHEMA_DATABASES
    first = tables[0].name
    lacking: dict[str, list[str]] = {}
    async with connections.engine(database).connect() as connection:
        if connection.dialect.name != "postgresql":
            return {}
        own = not per_tenant or await connection.scalar(_FOUND, {"table": first})
        if own and (missing := await missing_append_only(connection, tables)):
            lacking[database] = missing
    if per_tenant:
        lacking |= await _missing_in_tenants(database, tables)
    return lacking


async def _missing_in_tenants(
    database: str, tables: Sequence[Table]
) -> dict[str, list[str]]:
    """The triggers each tenant's schema of ``database`` lacks, by place."""
    tenants = [schema for schema in await tenant_schemas(database) if schema]
    if not tenants:
        return {}
    names = [table.name for table in tables]
    expected = _expected(tables)
    async with connections.engine(database).connect() as connection:
        holding = set(
            (
                await connection.execute(
                    _HOLDING, {"table": names[0], "schemas": tenants}
                )
            ).scalars()
        )
        found = await connection.execute(
            _TENANT_TRIGGERS, {"tables": names, "schemas": tenants}
        )
        present = {
            (row.nspname, row.relname, row.tgname)
            for row in found
            if _made(row, expected)
        }
    return {
        f"{database}/{schema}": missing
        for schema in sorted(holding)
        if (
            missing := sorted(
                name for table, name in expected if (schema, table, name) not in present
            )
        )
    }


def migrate_command(place: str) -> str:
    """The command that makes the triggers of a place ``unprotected`` names.

    ``migrate`` for a database's own schema; ``migrate --tenant <tenant>``
    for a tenant's, as each tenant's schema is migrated on its own.
    """
    _, _, schema = place.partition("/")
    config = settings.TENANCY
    if not schema or config is None:
        return "migrate"
    prefix, _, suffix = config.SCHEMA.partition("{tenant}")
    tenant = schema.removeprefix(prefix).removesuffix(suffix)
    return f"migrate --tenant {tenant}"


def _checked(table: Table) -> str:
    """``table``'s name, if its triggers can be named after it.

    Raises:
        ValueError: It has a schema of its own, or a name that is not
            ``[a-z_][a-z0-9_]*`` of at most ``MAX_TABLE_NAME`` characters.
    """
    if table.schema is not None:
        raise ValueError(
            f"Table {table.fullname!r} names its schema: protect the table of "
            "the schema search_path finds, so each tenant's schema gets its own "
            "triggers."
        )
    if not _TABLE_NAME.fullmatch(table.name):
        raise ValueError(
            f"Table {table.name!r} cannot name its triggers: use lowercase "
            "letters, digits, and underscores, not starting with a digit."
        )
    if len(table.name) > MAX_TABLE_NAME:
        raise ValueError(
            f"Table {table.name!r} has {len(table.name)} characters: its "
            f"triggers' names would be cut at 63; at most {MAX_TABLE_NAME}."
        )
    return table.name


def _expected(tables: Iterable[Table]) -> Expected:
    """The triggers ``append_only_statements`` makes on ``tables``, as made.

    Bodies have their whitespace collapsed, as the check compares them.
    """
    expected = {}
    for table in tables:
        rows, truncate = trigger_names(table)
        body = " ".join(append_only_body(table).split())
        expected[(table.name, rows)] = (ROW_TRIGGER, rows, body)
        expected[(table.name, truncate)] = (TRUNCATE_TRIGGER, rows, body)
    return expected


def _made(row: Any, expected: Expected) -> bool:  # noqa: ANN401 - a row of either query.
    """Whether a trigger fires, and runs, as ``append_only_statements`` made it."""
    return bool(
        row.enabled in FIRING
        and row.plain
        and row.lanname == "plpgsql"
        and expected.get((row.relname, row.tgname))
        == (row.tgtype, row.proname, " ".join(row.prosrc.split()))
    )
