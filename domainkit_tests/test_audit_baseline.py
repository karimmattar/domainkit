"""What staging and production need for a tamper-evident audit trail (N5)."""

import io
import logging
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit import baseline
from domainkit.contrib.audit.baseline import (
    check_at_startup,
    missing_triggers,
    protection_problems,
    settings_problems,
)
from domainkit.contrib.audit.protection import protect, unprotect
from domainkit.contrib.audit.tables import audit_keys, audit_log, audit_seals
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_postgres import Migration

pytestmark = pytest.mark.anyio

AUDIT = "domainkit.contrib.audit"
MESSAGING = "domainkit.contrib.messaging"
ALPHA = "domainkit_tests.sample_domains.alpha"
PRODUCTION = {"ENVIRONMENT": "production", "API_DOCS": False}
COMPLETE = {"LEDGER": "audit", "NAMESPACE": "shop", "SEALED_EXTERNALLY": True}
NO_LEDGER = (
    "AUDIT.LEDGER is None, so the audit trail's hash chain is anchored nowhere "
    "and whoever can write its tables can rewrite it unnoticed; set it to an "
    "immudb alias in LEDGERS."
)
NO_NAMESPACE = (
    "AUDIT.NAMESPACE is None, so audit anchors go under domainkit/audit/<alias>, "
    "a key every project and environment on the same ledger shares; set it, "
    "such as 'shop-production'."
)
NO_SCHEDULE = (
    f"Nothing seals the audit trail on a schedule: install {MESSAGING} and run "
    "a worker, or run 'manage.py auditseal' from cron and set "
    "AUDIT.SEALED_EXTERNALLY = True."
)
on_postgres = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
)


@pytest.fixture
def installed(database_url: str) -> Iterator[str]:
    """The audit app alone, on an empty database, without a ledger."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        LEDGERS={},
        AUDIT={},
    ):
        setup()
        yield database_url
    registry.clear()


@pytest.fixture
def configured() -> Iterator[None]:
    """The audit app on SQLite, so staging and production settings validate."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": "sqlite+aiosqlite://"}},
        LEDGERS={},
        AUDIT={},
    ):
        setup()
        yield
    registry.clear()


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe as staging and production do, on the test database (any server).

    Production settings refuse a test server without TLS, so the probe is
    told the test environment is deployed instead.
    """
    monkeypatch.setattr(baseline, "DEPLOYED_ENVIRONMENTS", frozenset({"test"}))


@pytest.fixture
async def tables(installed: str) -> AsyncIterator[None]:
    """Create the audit tables."""
    async with connections.engine().begin() as connection:
        for table in (audit_log, audit_keys, audit_seals):
            await connection.run_sync(table.create)
    yield
    await connections.close_all()


def test_nothing_is_asked_outside_staging_and_production(configured: None) -> None:
    """Development and tests may run without a ledger or a schedule."""
    assert settings_problems() == []
    assert registry.get_domain_config("audit").deployment_warnings() == []


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_warn_without_a_ledger_or_a_schedule(
    configured: None, environment: str
) -> None:
    """At 0.9.0 ``check`` said nothing of an anchorless, unsealed trail."""
    with override_settings(ENVIRONMENT=environment):
        found = registry.get_domain_config("audit").deployment_warnings()

    assert found == [NO_LEDGER, NO_SCHEDULE]


@pytest.mark.parametrize(
    ("audit", "expected"),
    [
        ({"LEDGER": "audit", "SEALED_EXTERNALLY": True}, [NO_NAMESPACE]),
        ({"NAMESPACE": "shop", "SEALED_EXTERNALLY": True}, [NO_LEDGER]),
        ({"LEDGER": "audit", "NAMESPACE": "shop"}, [NO_SCHEDULE]),
        (COMPLETE, []),
    ],
)
def test_each_part_of_the_baseline_is_asked_for(
    configured: None, audit: dict[str, Any], expected: list[str]
) -> None:
    """A ledger, a namespace on it, and something that seals."""
    with override_settings(ENVIRONMENT="production", AUDIT=audit):
        assert settings_problems() == expected


def test_the_worker_counts_as_a_sealing_schedule() -> None:
    """With messaging installed, the worker seals every ``SEAL_EVERY``."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, MESSAGING, ALPHA],
        DATABASES={"default": {"URL": "sqlite+aiosqlite://"}},
        LEDGERS={},
        AUDIT={"LEDGER": "audit", "NAMESPACE": "shop"},
        ENVIRONMENT="production",
    ):
        setup()
        assert settings_problems() == []


def test_check_lists_the_missing_baseline(configured: None) -> None:
    """``manage.py check`` names each part that is missing."""
    output = io.StringIO()
    with override_settings(**PRODUCTION):
        call_command("check", stdout=output)

    assert f"  Warning: {NO_LEDGER}\n" in output.getvalue()
    assert f"  Warning: {NO_SCHEDULE}\n" in output.getvalue()


async def test_startup_says_nothing_outside_deployments(
    tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The probe runs only in staging and production."""
    with caplog.at_level(logging.WARNING, logger=baseline.__name__):
        await registry.get_domain_config("audit").startup()

    assert caplog.records == []


async def test_startup_warns_of_what_is_missing(
    tables: None, deployed: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Deployed, each missing part is a warning in the log; it still starts."""
    with caplog.at_level(logging.WARNING, logger=baseline.__name__):
        await registry.get_domain_config("audit").startup()

    messages = [record.getMessage() for record in caplog.records]
    assert messages[:2] == [NO_LEDGER, NO_SCHEDULE]


async def test_startup_refuses_with_require_baseline(
    tables: None, deployed: None
) -> None:
    """``REQUIRE_BASELINE`` refuses to start, naming every missing part."""
    with (
        override_settings(AUDIT={"REQUIRE_BASELINE": True}),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        await check_at_startup()

    assert str(refused.value).startswith(
        "AUDIT.REQUIRE_BASELINE refuses to start in test without a "
        f"tamper-evident audit trail:\n  {NO_LEDGER}\n  {NO_SCHEDULE}"
    )


async def test_a_complete_baseline_starts_quietly(
    tables: None, deployed: None, caplog: pytest.LogCaptureFixture
) -> None:
    """With everything in place, nothing is logged and nothing refused."""
    audit = {**COMPLETE, "REQUIRE_BASELINE": True}
    async with connections.engine().begin() as connection:
        await connection.run_sync(lambda sync: protect(Migration(sync)))
        await connection.run_sync(lambda sync: protect(Migration(sync), audit_seals))
    try:
        with (
            override_settings(AUDIT=audit),
            caplog.at_level(logging.WARNING, logger=baseline.__name__),
        ):
            await check_at_startup()
    finally:
        async with connections.engine().begin() as connection:
            await connection.run_sync(lambda sync: unprotect(Migration(sync)))
            await connection.run_sync(
                lambda sync: unprotect(Migration(sync), audit_seals)
            )

    assert caplog.records == []


async def test_a_database_that_does_not_answer_is_named(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's failure is a problem of its own, with no detail of the error."""

    async def unreachable(database: str) -> list[str]:
        raise OperationalError("SELECT 1", {}, OSError("password=hunter2"))

    monkeypatch.setattr(baseline, "missing_triggers", unreachable)

    assert await protection_problems() == [
        (
            "Could not check the audit trail's triggers in 'default' "
            "(OperationalError); run 'manage.py auditverify' once it answers."
        )
    ]


@on_postgres
async def test_the_probe_finds_missing_and_disabled_triggers(tables: None) -> None:
    """Every ``protect()`` trigger must be there, and enabled, on both tables."""
    unprotected = await missing_triggers("default")
    async with connections.engine().begin() as connection:
        await connection.run_sync(lambda sync: protect(Migration(sync)))
    trail_only = await missing_triggers("default")
    async with connections.engine().begin() as connection:
        await connection.run_sync(lambda sync: protect(Migration(sync), audit_seals))
    complete = await missing_triggers("default")
    async with connections.engine().begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE domainkit_audit_log DISABLE TRIGGER "
                "domainkit_audit_log_append_only"
            )
        )
    disabled = await missing_triggers("default")
    problems = await protection_problems()
    async with connections.engine().begin() as connection:
        await connection.run_sync(lambda sync: unprotect(Migration(sync)))
        await connection.run_sync(lambda sync: unprotect(Migration(sync), audit_seals))

    assert unprotected == [
        "domainkit_audit_log_append_only",
        "domainkit_audit_log_append_only_truncate",
        "domainkit_audit_seals_append_only",
        "domainkit_audit_seals_append_only_truncate",
    ]
    assert trail_only == [
        "domainkit_audit_seals_append_only",
        "domainkit_audit_seals_append_only_truncate",
    ]
    assert complete == []
    assert disabled == ["domainkit_audit_log_append_only"]
    assert problems == [
        (
            "The audit trail in 'default' is not append-only: triggers "
            "domainkit_audit_log_append_only are missing or disabled. Call "
            "protect(op) and protect(op, audit_seals) from a revision "
            "(domainkit.contrib.audit.protection) and migrate."
        )
    ]
