"""Each tenant schema's audit trail is protected, sealed, verified, and forgotten.

On a database that keeps a schema per tenant, a unit of work run as a
tenant writes its audit rows to the tenant's schema. That trail was never
protected by triggers, sealed, verified, or forgotten (A6-4).
"""

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit import forget
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import run_async


@pytest.fixture
def tenants(database_url: str, tmp_path: Path) -> Iterator[None]:
    """The audit app, in tenant acme's schema of a new database."""
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
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        TENANCY={"SCHEMA_DATABASES": ["default"]},
        AUDIT={},
        LEDGERS={},
    ):
        setup()
        call_command("makemigrations", "-m", "audit", stdout=io.StringIO())
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
    assert sealed == "default/tenant_acme: sealed 1 row.\n"
    assert verified == "default/tenant_acme: 1 seals, 0 anchors: intact.\n"
    assert forgotten == 1
    assert [key for (key,) in keys if key == "ann"] == []  # The day's stay.
