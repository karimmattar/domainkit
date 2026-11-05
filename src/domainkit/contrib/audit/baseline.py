"""What staging and production need for a tamper-evident trail.

A trail is append-only and anchored only when four things hold, and each is
easy to forget: a ledger to anchor on (``AUDIT.LEDGER``), a namespace that
keeps its anchors apart on a shared ledger (``AUDIT.NAMESPACE``), something
that seals on a schedule (the worker, with ``domainkit.contrib.messaging``,
or ``audit seal`` from cron, ``AUDIT.SEALED_EXTERNALLY``), and the
``protect()`` triggers on PostgreSQL. ``check`` lists the settings that
are missing; the application, when it starts, also asks each audited
PostgreSQL database for the triggers, in its own schema and in each
tenant's schema that holds a trail (``TENANCY.SCHEMA_DATABASES``). With
``AUDIT.REQUIRE_BASELINE``, the default since 0.10, both refuse: each
problem is an error (``audit.E001`` to ``audit.E005``), which
``SILENCED_CHECKS`` cannot silence. A project that sets it to False gets
the same problems as warnings (``audit.W001`` to ``audit.W005``), as in 0.9.
"""

import asyncio
import logging
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from domainkit.apps import registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db import connections
from domainkit.db.uow import tenant_schemas

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
_OWN_TRAIL: Final = text("SELECT to_regclass(:log)")
"""The connection's own schema's trail, if it holds one."""
_TENANT_TRAILS: Final = text(
    "SELECT n.nspname FROM pg_class AS c "
    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
    "WHERE c.relname = :log AND n.nspname = ANY(:schemas)"
)
"""The tenant schemas, of those named, that hold an audit trail."""
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
    "WHERE c.relname IN (:log, :seals) AND n.nspname = ANY(:schemas) "
    "AND NOT t.tgisinternal"
)
"""The same triggers in every tenant schema named, in one round trip."""
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


def required() -> bool:
    """Whether the baseline refuses (``AUDIT.REQUIRE_BASELINE``), or only warns."""
    return registry.settings(AuditSettings).REQUIRE_BASELINE


def _problem(number: int, message: str) -> CheckMessage:
    """Baseline problem ``number``: an error while required, else a warning."""
    level = "E" if required() else "W"
    return CheckMessage(f"audit.{level}{number:03}", message)


def settings_problems() -> list[str]:
    """What the settings leave out in staging and production; none elsewhere."""
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
        return []
    config = registry.settings(AuditSettings)
    problems: list[str] = []
    if config.LEDGER is None:
        problems.append(
            _problem(
                1,
                "AUDIT.LEDGER is None, so the audit trail's hash chain is "
                "anchored nowhere and whoever can write its tables can rewrite "
                "it unnoticed; set it to an immudb alias in LEDGERS.",
            )
        )
    elif config.NAMESPACE is None:
        problems.append(
            _problem(
                2,
                "AUDIT.NAMESPACE is None, so audit anchors go under "
                "domainkit/audit/<alias>, a key every project and environment on "
                "the same ledger shares; set it, such as 'shop-production'.",
            )
        )
    if not (registry.is_installed(MESSAGING) or config.SEALED_EXTERNALLY):
        problems.append(
            _problem(
                3,
                f"Nothing seals the audit trail on a schedule: install {MESSAGING} "
                "and run a worker, or run 'manage.py audit seal' from cron and set "
                "AUDIT.SEALED_EXTERNALLY = True.",
            )
        )
    return problems


async def missing_triggers(database: str) -> list[str]:
    """The ``protect()`` triggers ``database`` lacks, has disabled, or changed.

    A trigger counts only as ``protect()`` makes it: firing for the
    application's sessions (not ``ENABLE REPLICA`` or ``DISABLE``), on the
    same events, for each row or statement, with no ``WHEN``, columns, or
    arguments, and running the plpgsql function ``protect()`` writes, its
    body the same but for whitespace. None on databases other than
    PostgreSQL: ``protect()`` does nothing there. None either on a database
    that keeps a schema per tenant and holds no trail of its own: plain
    ``migrate`` leaves its own schema out (``missing_in_tenant_trails``).
    """
    tenancy = settings.TENANCY
    per_tenant = tenancy is not None and database in tenancy.SCHEMA_DATABASES
    async with connections.engine(database).connect() as connection:
        if connection.dialect.name != "postgresql":
            return []
        if (
            per_tenant
            and await connection.scalar(_OWN_TRAIL, {"log": audit_log.name}) is None
        ):
            return []
        found = await connection.execute(
            _TRIGGERS, {"log": audit_log.name, "seals": audit_seals.name}
        )
        present = {(row.relname, row.tgname) for row in found if _as_protect_made(row)}
    return sorted(name for table, name in PROTECTED.keys() - present)


async def missing_in_tenant_trails(database: str) -> dict[str, list[str]]:
    """The ``protect()`` triggers each tenant's trail of ``database`` lacks.

    By trail (``<alias>/<schema>``), for each tenant schema that holds an
    audit trail; empty where the database keeps no schema per tenant. Two
    round trips, however many tenants there are.
    """
    tenants = [schema for schema in await tenant_schemas(database) if schema]
    if not tenants:
        return {}
    async with connections.engine(database).connect() as connection:
        trails = set(
            (
                await connection.execute(
                    _TENANT_TRAILS, {"log": audit_log.name, "schemas": tenants}
                )
            ).scalars()
        )
        found = await connection.execute(
            _TENANT_TRIGGERS,
            {"log": audit_log.name, "seals": audit_seals.name, "schemas": tenants},
        )
        present = {
            (row.nspname, row.relname, row.tgname)
            for row in found
            if _as_protect_made(row)
        }
    return {
        f"{database}/{schema}": missing
        for schema in sorted(trails)
        if (
            missing := sorted(
                name
                for table, name in PROTECTED
                if (schema, table, name) not in present
            )
        )
    }


def _as_protect_made(row: Any) -> bool:  # noqa: ANN401 - a row of either query.
    """Whether a trigger fires, and runs, as ``protect()`` made it."""
    return bool(
        row.enabled in FIRING
        and row.plain
        and row.lanname == "plpgsql"
        and PROTECTED.get((row.relname, row.tgname))
        == (row.tgtype, row.proname, " ".join(row.prosrc.split()))
    )


async def protection_problems() -> list[str]:
    """One line per audited PostgreSQL database without every trigger."""
    problems: list[str] = []
    for database in audited_databases():
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                missing = await missing_triggers(database)
                by_trail = {database: missing} if missing else {}
                by_trail |= await missing_in_tenant_trails(database)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            problems.append(
                _problem(
                    4,
                    f"Could not check the audit trail's triggers in {database!r} "
                    f"({type(exc).__name__}); run 'manage.py audit verify' once it "
                    "answers.",
                )
            )
            continue
        problems.extend(
            _problem(
                5,
                f"The audit trail in {trail!r} is not append-only: triggers "
                f"{', '.join(lacking)} are missing, disabled, or changed. Run "
                f"'manage.py {_migrate(trail)}', which makes them.",
            )
            for trail, lacking in by_trail.items()
        )
    return problems


def _migrate(trail: str) -> str:
    """The command that makes a trail's triggers: a tenant's is migrated as it."""
    _, _, schema = trail.partition("/")
    config = settings.TENANCY
    if not schema or config is None:
        return "migrate"
    prefix, _, suffix = config.SCHEMA.partition("{tenant}")
    tenant = schema.removeprefix(prefix).removesuffix(suffix)
    return f"migrate --tenant {tenant}"


async def check_at_startup() -> None:
    """Warn, or refuse with ``AUDIT.REQUIRE_BASELINE``, in staging and production.

    Raises:
        CheckFailed: ``REQUIRE_BASELINE`` is set and something
            is missing.
    """
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
        return
    problems = settings_problems() + await protection_problems()
    if not problems:
        return
    if required():
        raise CheckFailed(
            f"AUDIT.REQUIRE_BASELINE refuses to start in {settings.ENVIRONMENT} "
            "without a tamper-evident audit trail",
            problems,
        )
    for problem in problems:
        logger.warning("%s", problem)
