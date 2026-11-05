"""Each tenant schema's audit trail is protected, sealed, verified, and forgotten.

On a database that keeps a schema per tenant, a unit of work run as a
tenant writes its audit rows to the tenant's schema. That trail was never
protected by triggers, sealed, verified, or forgotten (A6-4); nor pruned,
exported, or probed at startup (A6-4's leftovers).

``AUDIT.DATABASE`` is another database, ``shared``: one that keeps a schema
per tenant holds no tables in its own schema, and is refused for it.
"""

import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.audit import forget
from domainkit.contrib.audit.baseline import protection_problems
from domainkit.contrib.audit.pruning import export, prune
from domainkit.contrib.audit.trails import in_trail
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_audit_checkpoints import keys

T0 = datetime(2026, 1, 5, 12, tzinfo=UTC)
T1 = T0 + timedelta(days=40)


@pytest.fixture
def tenants(
    database_url: str, make_database: MakeDatabase, tmp_path: Path
) -> Iterator[None]:
    """The audit app, in tenant acme's schema of a new database.

    ``AUDIT.DATABASE`` is ``shared``, a database of its own, migrated by
    plain ``migrate``. Checkpoints are signed with ``test_audit_checkpoints``'
    key.
    """
    if not database_url.startswith("postgresql"):
        pytest.skip(
            "schemas per tenant need PostgreSQL: set DOMAINKIT_TEST_POSTGRES_URL"
        )
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.audit",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={
            "default": {"URL": database_url},
            "shared": {"URL": make_database("shared")},
        },
        MIGRATIONS_DIR=tmp_path / "migrations",
        TENANCY={"SCHEMA_DATABASES": ["default"]},
        AUDIT={**keys(), "DATABASE": "shared"},
        LEDGERS={},
    ):
        setup()
        call_command("makemigrations", "-m", "audit", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        call_command("migrate", "--tenant", "acme", stdout=io.StringIO())
        yield
    registry.clear()


def run_sql(statement: str) -> list[tuple[object, ...]]:
    """Run ``statement`` in a transaction of its own; its rows, if any."""

    async def go() -> list[tuple[object, ...]]:
        async with connections.engine("default").begin() as connection:
            result = await connection.execute(text(statement))
            return [tuple(row) for row in result] if result.returns_rows else []

    return run_async(go())


def shared_actions() -> list[str]:
    """The actions in ``AUDIT.DATABASE``'s own trail, oldest first."""

    async def go() -> list[str]:
        async with connections.engine("shared").connect() as connection:
            rows = await connection.execute(
                text("SELECT action FROM domainkit_audit_log ORDER BY occurred_at")
            )
            return list(rows.scalars())

    return run_async(go())


def run(*arguments: str) -> str:
    """Run the audit command; what it printed."""
    out = io.StringIO()
    call_command("audit", *arguments, stdout=out)
    return out.getvalue()


def test_a_tenants_trail_is_protected_sealed_verified_and_forgotten(
    tenants: None,
) -> None:
    """``migrate --tenant`` protects it; ``audit`` seals, verifies, forgets it."""
    run_sql(
        "INSERT INTO tenant_acme.domainkit_audit_log "
        "(id, occurred_at, actor, action, outcome, details) VALUES "
        "(gen_random_uuid(), now(), 'ann', 'notes.write', 'success', '{}')"
    )
    run_sql(
        "INSERT INTO tenant_acme.domainkit_audit_keys (subject, key, created_at) "
        "VALUES ('ann', 'k', now())"
    )
    refused = []
    for statement in (
        "UPDATE tenant_acme.domainkit_audit_log SET actor = 'bob'",
        "DELETE FROM tenant_acme.domainkit_audit_log",
    ):
        with pytest.raises(DBAPIError) as raised:
            run_sql(statement)
        refused.append("append-only" in str(raised.value))
    sealed = run("seal")
    verified = run("verify")
    forgotten = run_async(forget("ann"))
    keys = run_sql("SELECT subject FROM tenant_acme.domainkit_audit_keys")

    assert refused == [True, True]
    assert sealed == "shared: sealed 0 rows.\ndefault/tenant_acme: sealed 1 row.\n"
    assert verified.endswith("default/tenant_acme: 1 seals, 0 anchors: intact.\n")
    assert forgotten == 1
    assert [key for (key,) in keys if key == "ann"] == []  # The day's stay.


def write_row() -> None:
    """One row in tenant acme's trail, as a unit of work run as acme writes it."""
    run_sql(
        "INSERT INTO tenant_acme.domainkit_audit_log "
        "(id, occurred_at, actor, action, outcome, details) VALUES "
        "(gen_random_uuid(), now(), 'ann', 'notes.write', 'success', '{}')"
    )


def test_a_tenants_trail_is_pruned_and_exported(tenants: None, tmp_path: Path) -> None:
    """``audit prune`` cuts it below a checkpoint; ``--export`` files it by name."""
    for moment, count in ((T0, 3), (T1, 1)):
        for _ in range(count):
            write_row()
        with freeze_time(moment):
            run("seal")
    with freeze_time(T1 + timedelta(days=1)), in_trail("tenant_acme"):
        pruned = run_async(prune("default", before=T1 - timedelta(days=30)))
    with freeze_time(T1 + timedelta(days=1)):
        exported = run_async(export())
    left = run_sql("SELECT count(*) FROM tenant_acme.domainkit_audit_log")
    named = run_sql(
        "SELECT target, details FROM tenant_acme.domainkit_audit_log "
        "WHERE action = 'audit.pruned'"
    )
    with freeze_time(T1 + timedelta(days=2)):
        again = run("prune", "--keep-days", "30")
        verified = run("verify")

    assert pruned.database == "default/tenant_acme"
    assert pruned.checkpoint is not None
    assert (pruned.checkpoint.database, pruned.checkpoint.seq) == (
        "default/tenant_acme",
        3,
    )
    assert pruned.rows == 3
    assert left == [(2,)]  # The row sealed at T1, and the prune's own.
    assert [
        (target, cast(dict[str, Any], details)["rows"]) for target, details in named
    ] == [("default/tenant_acme", 3)]
    assert list(exported["databases"]) == ["shared", "default/tenant_acme"]
    assert len(exported["databases"]["default/tenant_acme"]["checkpoints"]) == 2
    assert again == (
        "shared: nothing to prune.\ndefault/tenant_acme: nothing to prune.\n"
    )
    assert "default/tenant_acme: 1 seals, 0 anchors: intact.\n" in verified
    assert "operator.auditprune" in shared_actions()  # Out of every tenant.


def test_an_audit_database_with_a_schema_per_tenant_is_refused(
    database_url: str,
) -> None:
    """Its own schema holds no tables: records out of a tenant had no trail."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[
                "domainkit.contrib.audit",
                "domainkit_tests.sample_domains.alpha",
            ],
            DATABASES={"default": {"URL": database_url}},
            TENANCY={"SCHEMA_DATABASES": ["default"]},
        ),
        pytest.raises(
            ImproperlyConfigured,
            match=r"^AUDIT\.DATABASE = 'default' keeps a schema per tenant ",
        ),
    ):
        setup()
    registry.clear()


def test_the_startup_probe_checks_each_tenants_trail(tenants: None) -> None:
    """A trigger disabled in a tenant's schema is reported by the trail's name.

    The database's own schema holds no trail here (plain ``migrate`` leaves
    it out), and is not reported for it.
    """
    assert run_async(protection_problems()) == []

    run_sql(
        "ALTER TABLE tenant_acme.domainkit_audit_seals "
        "DISABLE TRIGGER domainkit_audit_seals_append_only"
    )
    (problem,) = run_async(protection_problems())

    assert "The audit trail in 'default/tenant_acme' is not append-only" in str(problem)
    assert "domainkit_audit_seals_append_only" in str(problem)
    assert "Run 'manage.py migrate --tenant acme'" in str(problem)
