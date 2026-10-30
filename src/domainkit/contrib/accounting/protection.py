"""Keep entries and postings append-only in the database itself (PostgreSQL).

Triggers refuse every ``UPDATE``, ``DELETE``, and ``TRUNCATE`` of the two
tables, for every role, the owner's included, as ``contrib.audit``'s
``protect()`` does for the audit trail. Here they need no revision of the
project's: after each ``migrate``, ``protect`` makes them and their
functions afresh, on the schema owner's connection, whatever was there.
A correction is a reversing entry. In staging and production the
application checks at startup that they are there as ``protect`` makes
them: enabled, of the right type, with no ``WHEN`` or column list,
running the function with the right body (``accounting.E001``). On
other databases nothing is created: SQLite has no roles to keep out.
"""

import logging
from typing import Final

from sqlalchemy import Table, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import connections

from .settings import AccountingSettings
from .tables import entries, postings
from .uow import accounting_alias

PROTECTED: Final = (entries, postings)
ROW_TRIGGER: Final = 1 | 2 | 8 | 16
"""``pg_trigger.tgtype`` of ``BEFORE UPDATE OR DELETE ... FOR EACH ROW``."""
TRUNCATE_TRIGGER: Final = 2 | 32
"""``pg_trigger.tgtype`` of ``BEFORE TRUNCATE ... FOR EACH STATEMENT``."""
FIRING: Final = ("O", "A")
"""``pg_trigger.tgenabled`` of a trigger that fires for the application."""
logger = logging.getLogger(__name__)

_TRIGGERS: Final = text(
    """
    SELECT c.relname, t.tgname, t.tgtype, p.proname, l.lanname, p.prosrc
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_proc p ON p.oid = t.tgfoid AND p.pronamespace = c.relnamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE NOT t.tgisinternal AND pg_table_is_visible(c.oid)
      AND t.tgenabled IN ('O', 'A') AND t.tgqual IS NULL
      AND cardinality(t.tgattr::int2[]) = 0
      AND c.relname IN (:entries, :postings)
    """
)
"""The enabled triggers of the two tables that fire for every row (no
``WHEN``, no ``UPDATE OF``), with the function each runs, from the
tables' schema."""


def triggers(table: Table) -> dict[str, int]:
    """The triggers that protect ``table``, by name, with the type each must have."""
    function = f"{table.name}_append_only"
    return {function: ROW_TRIGGER, f"{function}_truncate": TRUNCATE_TRIGGER}


def statements(table: Table, name: str) -> tuple[str, ...]:
    """The statements that make trigger ``name`` of ``table`` afresh."""
    function = f"{table.name}_append_only"
    when = (
        "BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW"
        if name == function
        else "BEFORE TRUNCATE ON {table} FOR EACH STATEMENT"
    ).format(table=table.name)
    return (
        f"DROP TRIGGER IF EXISTS {name} ON {table.name}",
        f"CREATE TRIGGER {name} {when} EXECUTE FUNCTION {function}()",
    )


def body(table: Table) -> str:
    """The body of the triggers' function for ``table``: refuse, whatever the change."""
    return f"""
    BEGIN
        RAISE EXCEPTION '{table.name} is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END
    """


def function_statement(table: Table) -> str:
    """The statement that makes the triggers' function for ``table``."""
    return f"""
    CREATE OR REPLACE FUNCTION {table.name}_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $${body(table)}$$
    """


def _words(source: str) -> list[str]:
    """``source`` without its layout, to compare a function's body."""
    return source.split()


async def missing(connection: AsyncConnection) -> list[str]:
    """The protecting triggers the database lacks, has disabled, or made otherwise.

    A trigger counts when it is enabled, of its type, fires for every row
    (no ``WHEN``, no column list), and runs the table's function, in
    PL/pgSQL, with the body ``protect`` gives it.
    """
    found = await connection.execute(
        _TRIGGERS, {"entries": entries.name, "postings": postings.name}
    )
    present = {
        (row.relname, row.tgname, row.tgtype, row.proname, row.lanname): row.prosrc
        for row in found
    }
    return sorted(
        name
        for table in PROTECTED
        for name, kind in triggers(table).items()
        if _words(
            present.get(
                (table.name, name, kind, f"{table.name}_append_only", "plpgsql"), ""
            )
        )
        != _words(body(table))
    )


async def protect(connection: AsyncConnection, database: str) -> None:
    """After ``migrate``: make the triggers and their functions afresh.

    Each is dropped and made again, so a trigger changed in place (a
    ``WHEN``, another function) is put back. Only on PostgreSQL, in the
    database that holds the ledger, once its tables exist
    (``makemigrations`` may not have written them yet).
    """
    if connection.dialect.name != "postgresql" or database != accounting_alias():
        return
    names = {table.name for table in PROTECTED}
    exists = await connection.run_sync(
        lambda sync: names <= set(inspect(sync).get_table_names())
    )
    if not exists:
        return
    for table in PROTECTED:
        await connection.exec_driver_sql(function_statement(table))
        for name in triggers(table):
            for statement in statements(table, name):
                await connection.exec_driver_sql(statement)


async def check_protection() -> None:
    """Where deployed, refuse to start (or warn) without the triggers, on PostgreSQL.

    Raises:
        CheckFailed: ``ACCOUNTING.REQUIRE_PROTECTION``, and the triggers
            are missing or could not be checked.
    """
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
        return
    problems = await protection_problems()
    if not problems:
        return
    if registry.settings(AccountingSettings).REQUIRE_PROTECTION:
        raise CheckFailed(
            "ACCOUNTING.REQUIRE_PROTECTION refuses to start in "
            f"{settings.ENVIRONMENT} with entries that can be changed",
            problems,
        )
    for problem in problems:
        logger.warning("%s: %s", problem.id.replace(".E", ".W"), problem)


async def protection_problems() -> list[CheckMessage]:
    """``accounting.E001``: the ledger's database lacks triggers, or did not answer."""
    alias = accounting_alias()
    try:
        async with connections.engine(alias).connect() as connection:
            if connection.dialect.name != "postgresql":
                return []
            lacking = await missing(connection)
    except (SQLAlchemyError, OSError) as exc:
        return [
            CheckMessage(
                "accounting.E002",
                f"Could not check the ledger's triggers in {alias!r} "
                f"({type(exc).__name__}).",
            )
        ]
    if not lacking:
        return []
    return [
        CheckMessage(
            "accounting.E001",
            f"Entries and postings in {alias!r} can be changed: triggers "
            f"{', '.join(lacking)} are missing, disabled, or changed. Run "
            "'manage.py migrate', which makes them.",
        )
    ]
