"""One way to keep tables append-only, which the audit trail and the ledger share.

``domainkit.db.protection`` makes the triggers and checks they are there
as made (ADR 0188), in a database's own schema and in each tenant's.
Before 2.9 audit and accounting had a copy each, and the copies had
drifted: the ledger's check counted a trigger given arguments, which the
audit trail's did not (A28-ARCH-07). The ledger is never kept per tenant
(``DATABASE_ROUTING['accounting']`` refuses such a database); each
tenant's audit trail is checked in ``test_audit_tenants.py``.
"""

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.accounting import protection as ledger
from domainkit.contrib.accounting.tables import entries, postings
from domainkit.contrib.audit import protection as trail
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.db.protection import (
    ROW_TRIGGER,
    TRUNCATE_TRIGGER,
    append_only_body,
    append_only_statements,
    migrate_command,
    missing_append_only,
    protect_append_only,
    trigger_names,
    unprotected,
)
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_accounting import books  # noqa: F401 - the fixture.

ENTRIES = "domainkit_accounting_entries_append_only"
LEDGER = (entries, postings)
BODY_2_8 = """
    BEGIN
        RAISE EXCEPTION '{} is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END
    """
"""The body both apps wrote in 2.8: triggers made then must still count."""


def test_the_trail_and_the_ledger_make_their_triggers_one_way() -> None:
    """The same statements, function names, and bodies for both apps, as in 2.8."""
    made, undone = append_only_statements(entries)

    assert trail.statements(audit_log) == append_only_statements(audit_log)
    assert ledger.function_statement(entries) == made[0]
    assert ledger.statements(entries, ENTRIES) == made[1:3]
    assert ledger.statements(entries, f"{ENTRIES}_truncate") == made[3:5]
    assert ledger.body(entries) == append_only_body(entries)
    assert ledger.triggers(entries) == {
        ENTRIES: ROW_TRIGGER,
        f"{ENTRIES}_truncate": TRUNCATE_TRIGGER,
    }
    assert trail.trigger_names(audit_log) == trigger_names(audit_log)
    for table in (audit_log, entries):
        assert append_only_body(table) == BODY_2_8.format(table.name)
        assert trigger_names(table) == (
            f"{table.name}_append_only",
            f"{table.name}_append_only_truncate",
        )
    assert f"$${BODY_2_8.format(entries.name)}$$" in made[0]
    assert undone == (
        f"DROP TRIGGER IF EXISTS {ENTRIES}_truncate ON domainkit_accounting_entries",
        f"DROP TRIGGER IF EXISTS {ENTRIES} ON domainkit_accounting_entries",
        f"DROP FUNCTION IF EXISTS {ENTRIES}()",
    )


@pytest.mark.parametrize(
    ("tenancy", "place", "command"),
    [
        (None, "default", "migrate"),
        (None, "default/tenant_acme", "migrate"),
        ({"SCHEMA_DATABASES": ["default"]}, "default", "migrate"),
        (
            {"SCHEMA_DATABASES": ["default"]},
            "default/tenant_acme",
            "migrate --tenant acme",
        ),
        (
            {"SCHEMA_DATABASES": ["default"], "SCHEMA": "org_{tenant}_data"},
            "default/org_acme_data",
            "migrate --tenant acme",
        ),
    ],
)
def test_each_place_names_the_command_that_protects_it(
    tenancy: dict[str, object] | None, place: str, command: str
) -> None:
    """A tenant's schema is migrated, and so protected, as that tenant."""
    with override_settings(TENANCY=tenancy):
        assert migrate_command(place) == command


@pytest.mark.anyio
async def test_other_databases_get_no_triggers_and_lack_none(
    make_database: MakeDatabase,
) -> None:
    """Only PostgreSQL has roles to keep out: elsewhere nothing is made or asked."""
    url = make_database("plain")
    if url.startswith("postgresql"):
        pytest.skip("a database other than PostgreSQL")
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            assert await protect_append_only(connection, LEDGER) == []
            assert await missing_append_only(connection, LEDGER) == []
        assert await unprotected("default", LEDGER) == {}
        await connections.close_all()


@pytest.mark.anyio
@pytest.mark.usefixtures("books")
async def test_a_trigger_given_arguments_counts_as_changed() -> None:
    """The ledger's check is the trail's: no ``WHEN``, columns, or arguments."""
    async with connections.engine().begin() as connection:
        if connection.dialect.name != "postgresql":
            pytest.skip("the triggers are PostgreSQL's")
        await connection.exec_driver_sql(
            f"DROP TRIGGER {ENTRIES} ON domainkit_accounting_entries"
        )
        await connection.exec_driver_sql(
            f"CREATE TRIGGER {ENTRIES} BEFORE UPDATE OR DELETE "
            "ON domainkit_accounting_entries FOR EACH ROW "
            f"EXECUTE FUNCTION {ENTRIES}('some', 'arguments')"
        )
        found = await ledger.missing(connection)
        await ledger.protect(connection, "default")
        remade = await ledger.missing(connection)

    assert (found, remade) == ([ENTRIES], [])
