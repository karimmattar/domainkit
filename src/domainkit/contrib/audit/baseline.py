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
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from domainkit.apps import registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import DEPLOYED_ENVIRONMENTS, settings
from domainkit.db.protection import (
    ROW_TRIGGER,
    TRUNCATE_TRIGGER,
    append_only_body,
    migrate_command,
    trigger_names,
    unprotected,
)

from .personal import audited_databases
from .settings import AuditSettings, Baseline
from .tables import audit_log, audit_seals

MESSAGING: Final = "domainkit.contrib.messaging"
PROBE_TIMEOUT: Final = 10.0
"""Seconds the startup probe of a database's triggers may take."""
TRAIL: Final = (audit_log, audit_seals)
"""The tables ``protect()`` makes append-only, the trail first."""
type Expected = tuple[int, str, str]
"""A trigger's ``tgtype``, its function's name, and the function's body."""


def _protected() -> dict[tuple[str, str], Expected]:
    """The triggers ``protect()`` makes on both tables, by table and name.

    Bodies have their whitespace collapsed, as the probe compares them.
    """
    expected = {}
    for table in TRAIL:
        rows, truncate = trigger_names(table)
        body = " ".join(append_only_body(table).split())
        expected[(table.name, rows)] = (ROW_TRIGGER, rows, body)
        expected[(table.name, truncate)] = (TRUNCATE_TRIGGER, rows, body)
    return expected


PROTECTED: Final = _protected()
"""The triggers ``protect()`` makes on both tables, and what they must be."""

logger = logging.getLogger(__name__)


def required(part: Baseline) -> bool:
    """Whether ``AUDIT.REQUIRE`` refuses without ``part``, or only warns."""
    return part in registry.settings(AuditSettings).REQUIRE


def _problem(number: int, part: Baseline, message: str) -> CheckMessage:
    """Problem ``number``, of ``part``: an error if required, else a warning."""
    level = "E" if required(part) else "W"
    return CheckMessage(f"audit.{level}{number:03}", message)


def is_required(problem: str) -> bool:
    """Whether ``problem`` is one ``AUDIT.REQUIRE`` refuses (``audit.E00N``)."""
    return isinstance(problem, CheckMessage) and problem.id.startswith("audit.E")


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
                "ledger",
                "AUDIT.LEDGER is None, so the audit trail's hash chain is "
                "anchored nowhere and whoever can write its tables can rewrite "
                "it unnoticed; set it to an immudb alias in LEDGERS.",
            )
        )
    elif config.NAMESPACE is None:
        problems.append(
            _problem(
                2,
                "namespace",
                "AUDIT.NAMESPACE is None, so audit anchors go under "
                "domainkit/audit/<alias>, a key every project and environment on "
                "the same ledger shares; set it, such as 'shop-production'.",
            )
        )
    if not (registry.is_installed(MESSAGING) or config.SEALED_EXTERNALLY):
        problems.append(
            _problem(
                3,
                "sealing",
                f"Nothing seals the audit trail on a schedule: install {MESSAGING} "
                "and run a worker, or run 'manage.py audit seal' from cron and set "
                "AUDIT.SEALED_EXTERNALLY = True.",
            )
        )
    return problems


async def missing_triggers(database: str) -> list[str]:
    """The ``protect()`` triggers ``database``'s own schema lacks, disabled, or changed.

    A trigger counts only as ``protect()`` makes it: firing for the
    application's sessions (not ``ENABLE REPLICA`` or ``DISABLE``), on the
    same events, for each row or statement, with no ``WHEN``, columns, or
    arguments, and running the plpgsql function ``protect()`` writes, its
    body the same but for whitespace (``domainkit.db.protection``, which
    the ledger's entries share). None on databases other than PostgreSQL:
    ``protect()`` does nothing there. None either on a database that keeps
    a schema per tenant and holds no trail of its own: plain ``migrate``
    leaves its own schema out. Each tenant's trail is checked too, by
    ``protection_problems``.
    """
    return (await unprotected(database, TRAIL)).get(database, [])


async def protection_problems() -> list[str]:
    """One line per audited PostgreSQL database without every trigger."""
    problems: list[str] = []
    for database in audited_databases():
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                by_trail = await unprotected(database, TRAIL)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            problems.append(
                _problem(
                    4,
                    "triggers",
                    f"Could not check the audit trail's triggers in {database!r} "
                    f"({type(exc).__name__}); run 'manage.py audit verify' once it "
                    "answers.",
                )
            )
            continue
        problems.extend(
            _problem(
                5,
                "triggers",
                f"The audit trail in {trail!r} is not append-only: triggers "
                f"{', '.join(lacking)} are missing, disabled, or changed. Run "
                f"'manage.py {migrate_command(trail)}', which makes them.",
            )
            for trail, lacking in by_trail.items()
        )
    return problems


async def check_at_startup() -> None:
    """Refuse what ``AUDIT.REQUIRE`` requires, and warn of the rest, where deployed.

    Each part of the baseline counts on its own (user report C2): a project
    without a ledger may still require the append-only triggers.

    Raises:
        CheckFailed: A part ``AUDIT.REQUIRE`` names is missing.
    """
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
        return
    problems = settings_problems() + await protection_problems()
    refused = [problem for problem in problems if is_required(problem)]
    for problem in problems:
        if problem not in refused:
            logger.warning("%s", problem)
    if refused:
        raise CheckFailed(
            f"AUDIT.REQUIRE refuses to start in {settings.ENVIRONMENT} without a "
            "tamper-evident audit trail",
            refused,
        )
