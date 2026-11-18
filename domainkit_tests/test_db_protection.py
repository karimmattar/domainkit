"""One way to keep tables append-only, which the audit trail and the ledger share.

``domainkit.db.protection`` makes the triggers and checks they are there
as made (ADR 0188), in a database's own schema and in each tenant's.
Before 2.9 audit and accounting had a copy each, and the copies had
drifted: the ledger's check counted a trigger given arguments, which the
audit trail's did not (A28-ARCH-07). The ledger is never kept per tenant
(``DATABASE_ROUTING['accounting']`` refuses such a database); each
tenant's audit trail is checked in ``test_audit_tenants.py``.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.sql.elements import quoted_name

from domainkit.conf import override_settings
from domainkit.contrib.accounting import protection as ledger
from domainkit.contrib.accounting.tables import entries, postings
from domainkit.contrib.audit import protection as trail
from domainkit.contrib.audit.tables import audit_log
from domainkit.db import connections
from domainkit.db.protection import (
    MAX_TABLE_NAME,
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
@pytest.mark.usefixtures("database_access")
async def test_other_databases_get_no_triggers_and_lack_none(tmp_path: Path) -> None:
    """Only PostgreSQL has roles to keep out: elsewhere nothing is made or asked.

    A SQLite file of its own, whichever database the suite runs on.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'plain.sqlite3'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            assert await protect_append_only(connection, LEDGER) == []
            assert await missing_append_only(connection, LEDGER) == []
        assert await unprotected("default", LEDGER) == {}
        await connections.close_all()


@pytest.mark.anyio
async def test_a_database_is_asked_where_its_tables_are(
    make_database: MakeDatabase,
) -> None:
    """Its own schema, lacking every trigger; but per tenant, none before a tenant.

    A database that keeps a schema per tenant holds no tables of its own,
    and lacks nothing until a tenant's schema holds them.
    """
    url = make_database("places")
    if not url.startswith("postgresql"):
        pytest.skip("the triggers are PostgreSQL's")
    with override_settings(DATABASES={"default": {"URL": url}}):
        own = await unprotected("default", LEDGER)
        with override_settings(TENANCY={"SCHEMA_DATABASES": ["default"]}):
            per_tenant = await unprotected("default", LEDGER)
        await connections.close_all()

    assert own == {
        "default": [
            ENTRIES,
            f"{ENTRIES}_truncate",
            "domainkit_accounting_postings_append_only",
            "domainkit_accounting_postings_append_only_truncate",
        ]
    }
    assert per_tenant == {}


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


def table(name: str, schema: str | None = None) -> Table:
    """A table of one column, ``name``d, of a metadata of its own."""
    return Table(
        name, MetaData(), Column("id", Integer, primary_key=True), schema=schema
    )


LONGEST = "price_history_" + "x" * (MAX_TABLE_NAME - len("price_history_"))


@pytest.mark.parametrize(
    ("refused", "message"),
    [
        (table("PriceHistory"), "lowercase letters, digits, and underscores"),
        (table("price-history"), "lowercase letters, digits, and underscores"),
        (table("1st_prices"), "not starting with a digit"),
        (table(LONGEST + "y"), "43 characters: its triggers' names would be cut"),
        (table("price_history", schema="billing"), "'billing.price_history' names"),
    ],
    ids=["mixed-case", "dash", "digit-first", "long", "schema"],
)
@pytest.mark.anyio
async def test_a_table_its_triggers_cannot_name_is_refused(
    make_database: MakeDatabase, refused: Table, message: str
) -> None:
    """Before anything is made or asked, on every database (review of A28-ARCH-07).

    Until then a long name gave triggers whose names PostgreSQL cut, and
    which never counted as made; a mixed-case name gave no trigger at all;
    and a table of a named schema protected the one ``search_path`` found.
    """
    with override_settings(DATABASES={"default": {"URL": make_database("names")}}):
        for call in (trigger_names, append_only_body, append_only_statements):
            with pytest.raises(ValueError, match=message):
                call(refused)
        async with connections.engine().begin() as connection:
            with pytest.raises(ValueError, match=message):
                await protect_append_only(connection, [entries, refused])
            with pytest.raises(ValueError, match=message):
                await missing_append_only(connection, [refused])
        with pytest.raises(ValueError, match=message):
            await unprotected("default", [refused])
        await connections.close_all()

    assert len(LONGEST) == MAX_TABLE_NAME == 42


@pytest.mark.anyio
async def test_no_tables_lack_nothing(make_database: MakeDatabase) -> None:
    """``unprotected`` of no tables is empty, not an ``IndexError``."""
    with override_settings(DATABASES={"default": {"URL": make_database("none")}}):
        async with connections.engine().begin() as connection:
            assert await protect_append_only(connection, []) == []
            assert await missing_append_only(connection, []) == []
        assert await unprotected("default", []) == {}
        await connections.close_all()


@pytest.mark.anyio
async def test_the_longest_name_and_a_reserved_word_are_protected(
    make_database: MakeDatabase,
) -> None:
    """42 characters keep the triggers' names whole; reserved words are quoted.

    ``order`` is in SQLAlchemy's list; ``lateral`` only PostgreSQL 16's.
    """
    url = make_database("words")
    if not url.startswith("postgresql"):
        pytest.skip("the triggers are PostgreSQL's")
    # SQLAlchemy quotes `order` itself, and `lateral` only when told to.
    lateral = table(quoted_name("lateral", quote=True))
    tables = [table(LONGEST), table("order"), lateral]
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            for each in tables:
                await connection.run_sync(each.create)
                await connection.execute(each.insert().values(id=1))
            protected = await protect_append_only(connection, tables)
        lacking = await unprotected("default", tables)
        refusals = []
        for each in tables:
            async with connections.engine().begin() as connection:
                try:
                    await connection.execute(each.delete())
                except Exception as exc:  # noqa: BLE001 - the driver's refusal.
                    refusals.append(f"{each.name} is append-only" in str(exc))
        await connections.close_all()

    assert protected == [LONGEST, "order", "lateral"]
    assert lacking == {}
    assert refusals == [True, True, True]
    assert append_only_statements(table("order"))[0][1] == (
        'DROP TRIGGER IF EXISTS order_append_only ON "order"'
    )


@pytest.mark.parametrize(
    "word",
    ["order", "collation", "concurrently", "lateral", "system_user", "tablesample"],
)
def test_postgresqls_reserved_words_are_quoted(word: str) -> None:
    """Each word PostgreSQL 16 reserves is quoted, SQLAlchemy's list or not.

    ``order`` is in SQLAlchemy's list; the five others are not. Unquoted,
    each is a syntax error where a table's name goes (``pg_get_keywords()``:
    categories ``R`` and ``T``).
    """
    protect, unprotect = append_only_statements(table(word))
    assert protect[1] == f'DROP TRIGGER IF EXISTS {word}_append_only ON "{word}"'
    assert unprotect[0] == (
        f'DROP TRIGGER IF EXISTS {word}_append_only_truncate ON "{word}"'
    )


class Revision:
    """The part of ``alembic.op`` that audit's ``protect`` uses; it records."""

    def __init__(self, dialect: str) -> None:
        """A revision run on ``dialect``."""
        self.dialect = dialect
        self.statements: list[str] = []

    def get_bind(self) -> Any:  # noqa: ANN401 - only its dialect is read.
        """A connection of the dialect."""
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, sqltext: str) -> None:
        """Record ``sqltext``."""
        self.statements.append(sqltext)


def written_by_2_8(name: str) -> list[str]:
    """What 2.8's ``protect`` then ``unprotect`` wrote for the table ``name``."""
    function = f"{name}_append_only"
    return [
        f"""
    CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql
    AS $${BODY_2_8.format(name)}$$
    """,
        f"DROP TRIGGER IF EXISTS {function} ON {name}",
        (
            f"CREATE TRIGGER {function} BEFORE UPDATE OR DELETE ON {name} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        ),
        f"DROP TRIGGER IF EXISTS {function}_truncate ON {name}",
        (
            f"CREATE TRIGGER {function}_truncate BEFORE TRUNCATE ON {name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
        ),
        f"DROP TRIGGER IF EXISTS {function}_truncate ON {name}",
        f"DROP TRIGGER IF EXISTS {function} ON {name}",
        f"DROP FUNCTION IF EXISTS {function}()",
    ]


def test_audits_protect_takes_a_projects_tables_as_2_8_did() -> None:
    """A revision's ``protect(op, table)`` and ``unprotect`` are not checked.

    A project's revisions call them with tables of their own, which 2.8
    took whatever their names (PostgreSQL cuts a long trigger name, with a
    notice, and the trigger still refuses) or schema (nothing is run on
    SQLite). The new API checks; these keep 2.8's statements, so an
    upgrade's ``migrate`` runs the revisions it ran (review of A28-ARCH-07).
    """
    long, kept = table("x" * 45), table("price_history", schema="billing")
    postgres, sqlite = Revision("postgresql"), Revision("sqlite")
    for op in (postgres, sqlite):
        for each in (long, kept):
            trail.protect(op, each)
            trail.unprotect(op, each)

    assert postgres.statements == written_by_2_8(long.name) + written_by_2_8(
        "price_history"
    )
    assert sqlite.statements == []
    assert trail.statements(audit_log) == append_only_statements(audit_log)
    with pytest.raises(ValueError, match="45 characters"):
        append_only_statements(long)
