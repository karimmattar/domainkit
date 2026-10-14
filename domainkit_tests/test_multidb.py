"""Several databases end to end: migrations per target, outboxes per database.

These run on SQLite, and on PostgreSQL when ``DOMAINKIT_TEST_POSTGRES_URL``
is set (see ``make_database``).
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, inspect, select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, override_settings
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.messaging import Worker, periodic
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections, create_engine
from domainkit.db import migrations as migrations_module
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError
from domainkit.testing import IN_MEMORY, migrated_database
from domainkit_tests.conftest import MakeDatabase
from domainkit_tests.test_db import NotesUnitOfWork, notes
from domainkit_tests.test_management import run
from domainkit_tests.test_messaging import ran, remember

ALPHA = "domainkit_tests.sample_domains.alpha"
MESSAGING = "domainkit.contrib.messaging"
AUDIT = "domainkit.contrib.audit"

schema = MetaData(naming_convention=NAMING_CONVENTION)
"""What the migrations compare: a domain table, an untagged one, the outbox."""
things = Table(
    "alpha_things",
    schema,
    Column("id", Integer, primary_key=True),
    info={"domain": "alpha"},
)
notes.to_metadata(schema)
messages.to_metadata(schema)


def tables_in(alias: str) -> set[str]:
    """Names of the tables in database ``alias``."""

    async def read() -> set[str]:
        engine = create_engine(connections.config(alias))
        try:
            async with engine.connect() as connection:
                names = await connection.run_sync(
                    lambda sync: inspect(sync).get_table_names()
                )
        finally:
            await engine.dispose()
        return set(names) - {"alembic_version"}

    return asyncio.run(read())


def create_tables(*aliases: str, tables: tuple[Table, ...]) -> None:
    """Create ``tables`` in each of ``aliases``."""

    async def create() -> None:
        for alias in aliases:
            engine = create_engine(connections.config(alias))
            try:
                async with engine.begin() as connection:
                    for table in tables:
                        await connection.run_sync(table.create)
            finally:
                await engine.dispose()

    asyncio.run(create())


@pytest.fixture
def two_targets(
    tmp_path: Path, make_database: MakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Alpha routed to ``reports``; messaging everywhere; untagged tables in default."""
    monkeypatch.setattr(migrations_module, "metadata", schema)
    versions = tmp_path / "migrations"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, MESSAGING],
        DATABASES={
            "default": {"URL": make_database("default")},
            "reports": {"URL": make_database("reports")},
        },
        DATABASE_ROUTING={"alpha": "reports"},
        MIGRATIONS_DIR=versions,
    ):
        setup()
        yield versions


@pytest.fixture
def sharded(
    tmp_path: Path, make_database: MakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Alpha routed to a shard group of two databases."""
    monkeypatch.setattr(migrations_module, "metadata", schema)
    versions = tmp_path / "migrations"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, MESSAGING],
        DATABASES={
            "default": {"URL": make_database("default")},
            "shard_a": {"URL": make_database("shard_a")},
            "shard_b": {"URL": make_database("shard_b")},
        },
        SHARDS={"tenants": {"DATABASES": ["shard_a", "shard_b"], "BUCKETS": 16}},
        DATABASE_ROUTING={"alpha": "tenants"},
        MIGRATIONS_DIR=versions,
    ):
        setup()
        yield versions


def test_each_target_has_its_own_history(two_targets: Path) -> None:
    """Revisions per target hold only the tables routed there."""
    created = run("makemigrations", "-m", "initial").splitlines()
    default_revision, reports_revision = (
        Path(line.removeprefix("Created ").removesuffix(".")) for line in created
    )

    assert default_revision.parent == two_targets
    assert reports_revision.parent == two_targets / "reports"
    default_text = default_revision.read_text(encoding="utf-8")
    reports_text = reports_revision.read_text(encoding="utf-8")
    assert "test_notes" in default_text
    assert "alpha_things" not in default_text
    assert "alpha_things" in reports_text
    assert "test_notes" not in reports_text
    assert "domainkit_messages" in default_text
    assert "domainkit_messages" in reports_text


def test_migrate_moves_every_target(two_targets: Path) -> None:
    """Migrate upgrades each target; check and --check see no gaps."""
    run("makemigrations", "-m", "initial")

    migrated = run("migrate")

    assert migrated == (
        "Database 'default' is at head.\nDatabase 'reports' is at head.\n"
    )
    assert tables_in("default") == {notes.name, "domainkit_messages"}
    assert tables_in("reports") == {"alpha_things", "domainkit_messages"}
    assert run("makemigrations", "--check") == "No changes detected.\n"
    assert run("check").startswith("System check identified no issues.")
    reports = run("showmigrations", "--database", "reports").splitlines()
    assert [line[:3] for line in reports] == ["[X]"]
    with pytest.raises(CommandError, match="name the target with --database"):
        run("migrate", reports[0].split()[1])
    assert run("migrate", "base", downgrade=True) == (
        "Database 'default' is at base.\nDatabase 'reports' is at base.\n"
    )
    assert tables_in("reports") == set()


def test_missing_migrations_name_their_target(two_targets: Path) -> None:
    """--check reports which target lacks a revision."""
    with pytest.raises(CommandError, match="Model changes have no migration: default"):
        run("makemigrations", "--check")
    assert run("makemigrations", "--merge") == "Nothing to merge.\n"


def test_a_shard_group_shares_one_history(sharded: Path) -> None:
    """One revision directory per group, applied to each of its databases."""
    created = run("makemigrations", "-m", "initial").splitlines()

    assert [Path(line).parent.name for line in created] == ["migrations", "tenants"]
    assert run("migrate").splitlines() == [
        "Database 'default' is at head.",
        "Database 'shard_a' is at head.",
        "Database 'shard_b' is at head.",
    ]
    for alias in ("shard_a", "shard_b"):
        assert tables_in(alias) == {"alpha_things", "domainkit_messages"}
    assert run("showmigrations", "--database", "tenants").splitlines()[::2] == [
        "shard_a:",
        "shard_b:",
    ]
    assert run("makemigrations", "--check", "--database", "tenants") == (
        "No changes detected.\n"
    )
    assert run("migrate", "--database", "shard_b").splitlines() == [
        "Database 'shard_b' is at head."
    ]


def test_shard_groups_migrate_one_database_at_a_time(sharded: Path) -> None:
    """The migration functions take aliases; a group name is refused."""
    with pytest.raises(ImproperlyConfigured, match="migrate each of its aliases"):
        migrations_module.upgrade("head", "tenants")
    assert migrations_module.versions_dir("tenants") == sharded / "tenants"
    assert migrations_module.migration_targets() == ("default", "tenants")


@pytest.mark.anyio
async def test_each_shard_has_its_own_outbox(sharded: Path) -> None:
    """Durable work commits in the shard's outbox; that shard's worker runs it."""
    for alias in ("default", "shard_a", "shard_b"):
        async with connections.engine(alias).begin() as connection:
            await connection.run_sync(messages.create)
            await connection.run_sync(notes.create)
    key = next(
        f"tenant-{n}"
        for n in range(100)
        if router.alias_for("tenants", f"tenant-{n}") == "shard_b"
    )
    value = uuid.uuid4()
    ran.clear()

    async with NotesUnitOfWork(database="tenants", shard_key=key) as uow:
        uow.defer(remember, value=value)
        await uow.commit()

    assert await Worker(database="shard_a").run_once() == 0
    assert await Worker(database="shard_b").run_once() == 1
    assert [entry[0] for entry in ran] == [value]
    await connections.close_all()


@pytest.mark.anyio
async def test_only_the_home_outbox_schedules_periodic_tasks(sharded: Path) -> None:
    """Periodic tasks are queued once, in MESSAGING.DATABASE."""
    for alias in ("default", "shard_a"):
        async with connections.engine(alias).begin() as connection:
            await connection.run_sync(messages.create)

    @periodic(every=timedelta(hours=1), name="tests.hourly")
    async def hourly() -> None: ...

    home, shard = Worker(), Worker(database="shard_a")
    await home.schedule_periodic()
    await shard.schedule_periodic()

    assert (home.schedules, shard.schedules) == (True, False)
    for alias, expected in (("default", 1), ("shard_a", 0)):
        async with connections.engine(alias).connect() as connection:
            rows = (await connection.execute(select(messages.c.target))).all()
        assert len(rows) == expected
    await connections.close_all()


def test_runworker_and_messages_cover_every_outbox(sharded: Path) -> None:
    """Runworker runs every hosting database; messages reads one by alias."""
    create_tables("default", "shard_a", "shard_b", tables=(messages,))

    async def queue(alias: str) -> None:
        engine = create_engine(connections.config(alias))
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    insert(messages).values(
                        id=uuid.uuid4(),
                        kind="task",
                        target="tests.nothing_here",
                        payload={},
                        headers={},
                        status="pending",
                        attempts=0,
                        max_attempts=1,
                        available_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(queue("shard_a"))
    asyncio.run(queue("shard_b"))

    assert run("messages", "--database", "shard_a").splitlines()[0] == "pending  1"
    assert run("runworker", "--once") == "Processed 2 message(s).\n"
    dead = run("messages", "--database", "shard_b", "--dead").splitlines()
    assert len(dead) == 1
    assert "tests.nothing_here" in dead[0]
    assert (
        run("runworker", "--once", "--database", "tenants")
        == "Processed 0 message(s).\n"
    )


@pytest.mark.parametrize(
    ("app", "setting"),
    [
        (MESSAGING, "MESSAGING"),
        (AUDIT, "AUDIT"),
        ("domainkit.contrib.idempotency", "IDEMPOTENCY"),
    ],
)
@pytest.mark.parametrize("database", ["tenants", "shard_a", "missing"])
def test_home_databases_are_standalone_aliases(
    sharded: Path, app: str, setting: str, database: str
) -> None:
    """Groups, shard aliases, and unknown aliases are refused at setup."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[ALPHA, app], **{setting: {"DATABASE": database}}
        ),
        pytest.raises(ImproperlyConfigured, match=f"{setting}.DATABASE = '{database}'"),
    ):
        setup()


def test_auditlog_reads_any_database(two_targets: Path) -> None:
    """Events are audited where they commit, so the log reads by alias."""
    registry.clear()
    with override_settings(INSTALLED_DOMAINS=[ALPHA, AUDIT], ALPHA=UNSET):
        setup()
        create_tables("reports", tables=(audit_log,))

        async def record_one() -> None:
            engine = create_engine(connections.config("reports"))
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        insert(audit_log).values(
                            id=uuid.uuid4(),
                            occurred_at=datetime.now(UTC),
                            actor="user:7",
                            action="alpha.thing_made",
                            outcome="success",
                            details={},
                        )
                    )
            finally:
                await engine.dispose()

        asyncio.run(record_one())

        assert "alpha.thing_made" in run("auditlog", "--database", "reports")


def test_the_plugin_migrates_every_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each in-memory database becomes its own file, migrated up, down, and up."""
    monkeypatch.setattr(migrations_module, "metadata", schema)
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA],
        DATABASES={"default": {"URL": IN_MEMORY}, "reports": {"URL": IN_MEMORY}},
        DATABASE_ROUTING={"alpha": "reports"},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        with migrated_database(tmp_path) as url:
            assert url.endswith("test.sqlite3")

    assert (tmp_path / "test.sqlite3").is_file()
    assert (tmp_path / "test_reports.sqlite3").is_file()
