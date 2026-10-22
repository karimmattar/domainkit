"""Databases put back as a snapshot found them, between a project's tests."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import (
    Column,
    Computed,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Row,
    String,
    Table,
    column,
    delete,
    func,
    insert,
    select,
    table,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from domainkit.conf import override_settings
from domainkit.contrib.audit.protection import protect
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.db import UTCDateTime, connections
from domainkit.db.snapshots import restore_snapshot, take_snapshot
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import POSTGRES_VARIABLE, MakeDatabase
from domainkit_tests.test_postgres import Migration

pytestmark = pytest.mark.anyio

on_postgres = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
)

metadata = MetaData()
shops = Table(
    "framework_test_shops",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(20), nullable=False),
)
items = Table(
    "framework_test_items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("shop_id", ForeignKey(shops.c.id), nullable=False),
    Column("price", Numeric(12, 2), nullable=False),
    Column("added", UTCDateTime, nullable=False),
    Column("doubled", Numeric(12, 2), Computed("price * 2", persisted=True)),
)
ADDED = datetime(2026, 10, 18, 9, 30, 15, 123456, tzinfo=UTC)


@pytest.fixture
def two_databases(make_database: MakeDatabase) -> Iterator[None]:
    """Two empty databases, ``default`` and ``reports``, in the settings."""
    with override_settings(
        DATABASES={
            "default": {"URL": make_database("default")},
            "reports": {"URL": make_database("reports")},
        }
    ):
        yield


async def rows(alias: str, table: Table) -> list[Row[Any]]:
    """Every row of ``table``, in key order."""
    async with connections.engine(alias).connect() as connection:
        found = await connection.execute(select(table).order_by(*table.primary_key))
        return list(found)


async def test_rows_come_back_exactly_in_every_database(two_databases: None) -> None:
    """Later rows go, deleted and changed ones come back with the same values.

    Children are emptied before their parents and refilled after them, which
    PostgreSQL's foreign keys insist on; generated columns are recomputed.
    """
    for alias in ("default", "reports"):
        async with connections.engine(alias).begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(insert(shops).values(id=1, name="first"))
            await connection.execute(
                insert(items).values(
                    id=1, shop_id=1, price=Decimal("0.10"), added=ADDED
                )
            )
    snapshot = await take_snapshot()
    before = {alias: await rows(alias, items) for alias in ("default", "reports")}

    for alias in ("default", "reports"):
        async with connections.engine(alias).begin() as connection:
            await connection.execute(insert(shops).values(id=2, name="second"))
            await connection.execute(
                insert(items).values(
                    id=2, shop_id=2, price=Decimal("5.00"), added=ADDED
                )
            )
            await connection.execute(delete(items).where(items.c.id == 1))
            await connection.execute(shops.update().values(name="renamed"))
    await restore_snapshot(snapshot)

    for alias in ("default", "reports"):
        assert await rows(alias, shops) == [(1, "first")]
        assert await rows(alias, items) == before[alias]
        assert before[alias] == [(1, 1, Decimal("0.10"), ADDED, Decimal("0.20"))]
    await connections.close_all()


async def test_a_table_empty_in_the_snapshot_is_emptied(database_url: str) -> None:
    """Restoring twice is the same as once; an empty snapshot empties."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        snapshot = await take_snapshot()
        async with connections.engine().begin() as connection:
            await connection.execute(insert(shops).values(id=1, name="later"))

        await restore_snapshot(snapshot)
        await restore_snapshot(snapshot)

        assert await rows("default", shops) == []
        await connections.close_all()


async def test_restoring_deletes_nothing_outside_tests(database_url: str) -> None:
    """Only test settings may have every row deleted."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        snapshot = await take_snapshot()
        async with connections.engine().begin() as connection:
            await connection.execute(insert(shops).values(id=1, name="kept"))

        with (
            override_settings(ENVIRONMENT="local"),
            pytest.raises(ImproperlyConfigured, match="not 'local'"),
        ):
            await restore_snapshot(snapshot)

        assert await rows("default", shops) == [(1, "kept")]
        await connections.close_all()


async def test_snapshots_connect_as_the_schema_owner(database_url: str) -> None:
    """``MIGRATION_URL`` is used when set: emptying protected tables needs it."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(insert(shops).values(id=1, name="first"))
    unreachable = make_url(database_url).set(database="/nonexistent/framework_test")
    owner_only = {
        "URL": unreachable.render_as_string(hide_password=False),
        "MIGRATION_URL": database_url,
    }
    with override_settings(DATABASES={"default": owner_only}):
        snapshot = await take_snapshot()
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.execute(insert(shops).values(id=2, name="later"))
        with override_settings(DATABASES={"default": owner_only}):
            await restore_snapshot(snapshot)

        assert await rows("default", shops) == [(1, "first")]
        await connections.close_all()


def entry(action: str) -> dict[str, object]:
    """An audit trail row for ``action``."""
    return {
        "id": uuid4(),
        "occurred_at": ADDED,
        "actor": "user:1",
        "action": action,
        "outcome": "succeeded",
        "details": {"action": action, "amounts": ["1.10", 2]},
    }


@on_postgres
async def test_append_only_tables_are_restored_and_stay_protected(
    database_url: str,
) -> None:
    """The audit trail's triggers are off only while it is emptied."""
    first = entry("first")
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(audit_log.create)
            await connection.run_sync(lambda sync: protect(Migration(sync)))
            await connection.execute(insert(audit_log).values(first))
        snapshot = await take_snapshot()
        async with connections.engine().begin() as connection:
            await connection.execute(insert(audit_log).values(entry("later")))

        await restore_snapshot(snapshot)

        restored = await rows("default", audit_log)
        assert [(row.id, row.action, row.details) for row in restored] == [
            (first["id"], "first", {"action": "first", "amounts": ["1.10", 2]})
        ]
        async with connections.engine().connect() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                await connection.execute(delete(audit_log))
        await connections.close_all()


@on_postgres
async def test_tables_with_any_name_are_restored(database_url: str) -> None:
    """A protected table's name is quoted, whatever characters it holds."""
    statements = (
        'CREATE TABLE "framework_test_odd""; --" (id int PRIMARY KEY)',
        (
            "CREATE FUNCTION framework_test_refuse() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only'; END $$"
        ),
        (
            'CREATE TRIGGER refuse BEFORE DELETE ON "framework_test_odd""; --" '
            "FOR EACH ROW EXECUTE FUNCTION framework_test_refuse()"
        ),
        'INSERT INTO "framework_test_odd""; --" VALUES (1)',
    )
    odd = table('framework_test_odd"; --', column("id"))
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            for statement in statements:
                await connection.exec_driver_sql(statement)
        snapshot = await take_snapshot()
        async with connections.engine().begin() as connection:
            await connection.execute(insert(odd).values(id=2))

        await restore_snapshot(snapshot)

        async with connections.engine().connect() as connection:
            found = await connection.execute(select(odd.c.id))
            assert found.scalars().all() == [1]
            with pytest.raises(DBAPIError, match="append-only"):
                await connection.execute(delete(odd))
        await connections.close_all()


@on_postgres
async def test_partitions_are_refilled_through_their_parent(database_url: str) -> None:
    """A partition's rows are restored once, through the partitioned table.

    The tables after it (``framework_test_shops``) are restored too.
    """
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(shops.create)
            for statement in (
                (
                    "CREATE TABLE framework_test_readings (id int, day date) "
                    "PARTITION BY RANGE (day)"
                ),
                (
                    "CREATE TABLE framework_test_readings_2026 "
                    "PARTITION OF framework_test_readings "
                    "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
                ),
                "INSERT INTO framework_test_readings VALUES (1, '2026-10-18')",
            ):
                await connection.exec_driver_sql(statement)
        snapshot = await take_snapshot()
        async with connections.engine().begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO framework_test_readings VALUES (2, '2026-10-19')"
            )
            await connection.execute(insert(shops).values(id=1, name="later"))

        await restore_snapshot(snapshot)

        async with connections.engine().connect() as connection:
            found = await connection.execute(
                text("SELECT id FROM framework_test_readings_2026")
            )
            assert found.scalars().all() == [1]
        assert await rows("default", shops) == []
        await connections.close_all()


async def counts() -> dict[str, int]:
    """How many rows each auth table holds, and the migration version's."""
    names = [
        "alembic_version",
        "domainkit_content_types",
        "domainkit_permissions",
        "domainkit_users",
        "domainkit_groups",
        "domainkit_group_permissions",
        "domainkit_users_groups",
        "domainkit_users_permissions",
    ]
    async with connections.engine().connect() as connection:
        return {
            name: (
                await connection.execute(select(func.count()).select_from(table(name)))
            ).scalar_one()
            for name in names
        }


async def test_a_migrated_project_comes_back_to_what_migrate_wrote(
    auth_project: str,
) -> None:
    """Users, groups, and their links go; content types and permissions stay."""
    snapshot = await take_snapshot()
    migrated = await counts()
    async with AuthUnitOfWork() as uow:
        alice = await uow.users.create_user("alice")
        editors = await uow.groups.create("editors")
        editors.grant(await uow.permissions.get("shop.publish_product"))
        alice.grant(await uow.permissions.get("shop.add_product"))
        alice.join(editors)
        await uow.commit()
    changed = await counts()

    await restore_snapshot(snapshot)

    assert await counts() == migrated
    assert migrated["domainkit_permissions"] > 0
    assert migrated["alembic_version"] == 1
    assert migrated["domainkit_users"] == 0
    assert {name for name in changed if changed[name] != migrated[name]} == {
        "domainkit_users",
        "domainkit_groups",
        "domainkit_group_permissions",
        "domainkit_users_groups",
        "domainkit_users_permissions",
    }
    await connections.close_all()
