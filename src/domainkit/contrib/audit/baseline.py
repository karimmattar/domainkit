"""What staging and production need for a tamper-evident trail.

A trail is append-only and anchored only when four things hold, and each is
easy to forget: a ledger to anchor on (``AUDIT.LEDGER``), a namespace that
keeps its anchors apart on a shared ledger (``AUDIT.NAMESPACE``), something
that seals on a schedule (the worker, with ``domainkit.contrib.messaging``,
or ``auditseal`` from cron, ``AUDIT.SEALED_EXTERNALLY``), and the
``protect()`` triggers on PostgreSQL. ``check`` lists the settings that
are missing; the application, when it starts, also asks each audited
PostgreSQL database for the triggers. Both warn, unless
``AUDIT.REQUIRE_BASELINE``, which refuses to start; 0.10 refuses by default.
"""

import asyncio
import logging
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured

from .personal import audited_databases
from .protection import ROW_TRIGGER, TRUNCATE_TRIGGER, function_body, trigger_names
from .settings import AuditSettings
from .tables import audit_log, audit_seals

MESSAGING: Final = "domainkit.contrib.messaging"
PROBE_TIMEOUT: Final = 10.0
"""Seconds the startup probe of a database's triggers may take."""
_TRIGGERS: Final = text(
    "SELECT c.relname, t.tgname, t.tgenabled::text AS enabled, t.tgtype, "
    "t.tgqual IS NULL AND t.tgnargs = 0 AND cardinality(t.tgattr::int2[]) = 0 "
    "AS plain, p.proname, p.prosrc, l.lanname "
    "FROM pg_trigger AS t "
    "JOIN pg_class AS c ON c.oid = t.tgrelid "
    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
    "JOIN pg_language AS l ON l.oid = p.prolang "
    "WHERE t.tgrelid IN (to_regclass(:log), to_regclass(:seals)) "
    "AND NOT t.tgisinternal"
)
"""The triggers on the trail and its seals, as ``search_path`` finds them,
with what decides whether they fire and what they run."""
FIRING: Final = frozenset({"O", "A"})
"""``tgenabled`` of a trigger that fires for the application's sessions.

``O`` fires unless the session is a replica's, ``A`` always; ``R`` fires
only in replica sessions, ``D`` never."""
type Expected = tuple[int, str, str]
"""A trigger's ``tgtype``, its function's name, and the function's body."""


def _protected() -> dict[tuple[str, str], Expected]:
    """The triggers ``protect()`` makes on both tables, by table and name.

    Bodies have their whitespace collapsed, as the probe compares them.
    """
    expected = {}
    for table in (audit_log, audit_seals):
        rows, truncate = trigger_names(table)
        body = " ".join(function_body(table).split())
        expected[(table.name, rows)] = (ROW_TRIGGER, rows, body)
        expected[(table.name, truncate)] = (TRUNCATE_TRIGGER, rows, body)
    return expected


PROTECTED: Final = _protected()
"""The triggers ``protect()`` makes on both tables, and what they must be."""

logger = logging.getLogger(__name__)


def settings_problems() -> list[str]:
    """What the settings leave out in staging and production; none elsewhere."""
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
        return []
    config = registry.settings(AuditSettings)
    problems = []
    if config.LEDGER is None:
        problems.append(
            "AUDIT.LEDGER is None, so the audit trail's hash chain is anchored "
            "nowhere and whoever can write its tables can rewrite it unnoticed; "
            "set it to an immudb alias in LEDGERS."
        )
    elif config.NAMESPACE is None:
        problems.append(
            "AUDIT.NAMESPACE is None, so audit anchors go under "
            "domainkit/audit/<alias>, a key every project and environment on "
            "the same ledger shares; set it, such as 'shop-production'."
        )
    if not (registry.is_installed(MESSAGING) or config.SEALED_EXTERNALLY):
        problems.append(
            f"Nothing seals the audit trail on a schedule: install {MESSAGING} "
            "and run a worker, or run 'manage.py auditseal' from cron and set "
            "AUDIT.SEALED_EXTERNALLY = True."
        )
    return problems


async def missing_triggers(database: str) -> list[str]:
    """The ``protect()`` triggers ``database`` lacks, has disabled, or changed.

    A trigger counts only as ``protect()`` makes it: firing for the
    application's sessions (not ``ENABLE REPLICA`` or ``DISABLE``), on the
    same events, for each row or statement, with no ``WHEN``, columns, or
    arguments, and running the plpgsql function ``protect()`` writes, its
    body the same but for whitespace. None on databases other than
    PostgreSQL: ``protect()`` does nothing there.
    """
    async with connections.engine(database).connect() as connection:
        if connection.dialect.name != "postgresql":
            return []
        found = await connection.execute(
            _TRIGGERS, {"log": audit_log.name, "seals": audit_seals.name}
        )
        present = {
            (row.relname, row.tgname)
            for row in found
            if row.enabled in FIRING
            and row.plain
            and row.lanname == "plpgsql"
            and PROTECTED.get((row.relname, row.tgname))
            == (row.tgtype, row.proname, " ".join(row.prosrc.split()))
        }
    return sorted(name for table, name in PROTECTED.keys() - present)


async def protection_problems() -> list[str]:
    """One line per audited PostgreSQL database without every trigger."""
    problems = []
    for database in audited_databases():
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                missing = await missing_triggers(database)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            problems.append(
                f"Could not check the audit trail's triggers in {database!r} "
                f"({type(exc).__name__}); run 'manage.py auditverify' once it "
                "answers."
            )
            continue
        if missing:
            problems.append(
                f"The audit trail in {database!r} is not append-only: triggers "
                f"{', '.join(missing)} are missing, disabled, or changed. Call "
                "protect(op) and protect(op, audit_seals) from a revision "
                "(domainkit.contrib.audit.protection) and migrate."
            )
    return problems


async def check_at_startup() -> None:
    """Warn, or refuse with ``AUDIT.REQUIRE_BASELINE``, in staging and production.

    Raises:
        ImproperlyConfigured: ``REQUIRE_BASELINE`` is set and something
            is missing.
    """
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
        return
    problems = settings_problems() + await protection_problems()
    if not problems:
        return
    if registry.settings(AuditSettings).REQUIRE_BASELINE:
        listed = "\n".join(f"  {problem}" for problem in problems)
        raise ImproperlyConfigured(
            f"AUDIT.REQUIRE_BASELINE refuses to start in {settings.ENVIRONMENT} "
            f"without a tamper-evident audit trail:\n{listed}"
        )
    for problem in problems:
        logger.warning("%s", problem)
