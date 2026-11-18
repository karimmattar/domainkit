"""Keep entries and postings append-only in the database itself (PostgreSQL).

Triggers refuse every ``UPDATE``, ``DELETE``, and ``TRUNCATE`` of the two
tables, for every role, the owner's included, as ``contrib.audit``'s
``protect()`` does for the audit trail: both are
``domainkit.db.protection``'s, the same function names and bodies, and
the same check. Here they need no revision of the project's: after each
``migrate``, ``protect`` makes them and their functions afresh, on the
schema owner's connection, whatever was there. A correction is a
reversing entry.
In staging and production the application checks at startup that they
are there as ``protect`` makes them: enabled, of the right type, with no
``WHEN``, column list, or arguments (since 2.9, as the trail's), running
the function with the right body (``accounting.E001``). On other
databases nothing is created: SQLite has no roles to keep out.
"""

import logging
from typing import Final

from sqlalchemy import Table
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.apps import registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.protection import ROW_TRIGGER as ROW_TRIGGER
from domainkit.db.protection import TRUNCATE_TRIGGER as TRUNCATE_TRIGGER
from domainkit.db.protection import (
    append_only_body,
    append_only_statements,
    migrate_command,
    missing_append_only,
    protect_append_only,
    trigger_names,
    unprotected,
)

from .settings import AccountingSettings
from .tables import entries, postings
from .uow import accounting_alias

PROTECTED: Final = (entries, postings)
logger = logging.getLogger(__name__)


def triggers(table: Table) -> dict[str, int]:
    """The triggers that protect ``table``, by name, with the type each must have."""
    return dict(zip(trigger_names(table), (ROW_TRIGGER, TRUNCATE_TRIGGER), strict=True))


def statements(table: Table, name: str) -> tuple[str, ...]:
    """The statements that make trigger ``name`` of ``table`` afresh."""
    made = append_only_statements(table)[0]
    return made[1:3] if name == trigger_names(table)[0] else made[3:5]


def body(table: Table) -> str:
    """The body of the triggers' function for ``table``: refuse, whatever the change."""
    return append_only_body(table)


def function_statement(table: Table) -> str:
    """The statement that makes the triggers' function for ``table``."""
    return append_only_statements(table)[0][0]


async def missing(connection: AsyncConnection) -> list[str]:
    """The protecting triggers the connection's schema lacks, has disabled, or changed.

    A trigger counts when it is enabled, of its type, fires for every row
    (no ``WHEN``, no column list, no arguments), and runs the table's
    function, in PL/pgSQL, with the body ``protect`` gives it.
    """
    return await missing_append_only(connection, PROTECTED)


async def protect(connection: AsyncConnection, database: str) -> None:
    """After ``migrate``: make the triggers and their functions afresh.

    Each is dropped and made again, so a trigger changed in place (a
    ``WHEN``, another function) is put back. Only on PostgreSQL, in the
    database that holds the ledger, for each table once it exists
    (``makemigrations`` may not have written them yet).
    """
    if connection.dialect.name != "postgresql" or database != accounting_alias():
        return
    await protect_append_only(connection, PROTECTED)


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
        by_place = await unprotected(alias, PROTECTED)
    except (SQLAlchemyError, OSError) as exc:
        return [
            CheckMessage(
                "accounting.E002",
                f"Could not check the ledger's triggers in {alias!r} "
                f"({type(exc).__name__}).",
            )
        ]
    return [
        CheckMessage(
            "accounting.E001",
            f"Entries and postings in {place!r} can be changed: triggers "
            f"{', '.join(lacking)} are missing, disabled, or changed. Run "
            f"'manage.py {migrate_command(place)}', which makes them.",
        )
        for place, lacking in by_place.items()
    ]
