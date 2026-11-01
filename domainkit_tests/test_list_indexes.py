"""Indexes behind the default list orders and the audit history (PERF-29, PERF-30).

A list endpoint reads its first page newest first: without an index on the
ordering, each page scans and sorts the whole table. An aggregate's
history reads the rows whose ``details[field]`` is its id: without an
index of that field, each page scans the whole trail. The indexes of
``AUDIT.HISTORY_FIELDS`` are made by ``migrate`` on PostgreSQL, and
revisions leave them alone.
"""

import asyncio
import io
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import Index, Table, select, text
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.dialects.sqlite.base import SQLiteDialect

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.accounting.tables import entries, holds, postings
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.audit.history import (
    HISTORY_INDEX,
    MAX_NAME,
    aggregate_value,
    history,
    history_index_name,
    index_history,
)
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.limits.tables import subjects, usage
from domainkit.db import connections
from domainkit.db.migrations import include_object
from domainkit.management import call_command
from domainkit_tests.conftest import ALPHA, POSTGRES_VARIABLE

AUDIT: Final = "domainkit.contrib.audit"
LONG: Final = "f" * 64
"""A field whose index name would pass PostgreSQL's 63 characters."""
on_postgres = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
)


def leading(table: Table) -> dict[str, Index]:
    """The table's indexes by the name of their first column."""
    return {
        next(iter(index.columns)).name: index
        for index in table.indexes
        if index.columns
    }


@pytest.mark.parametrize(
    ("table", "column"),
    [
        (entries, "posted_at"),
        (holds, "placed_at"),
        (requests, "submitted_at"),
    ],
)
def test_default_list_orders_have_an_index(table: Table, column: str) -> None:
    """``-posted_at``, ``-placed_at``, ``-submitted_at``: an index leads with each."""
    assert column in leading(table)


def test_no_index_writes_for_nothing() -> None:
    """Postings by entry use the unique key; the subjects' lock stays HOT."""
    alone = [
        index for index in postings.indexes if index.columns.keys() == ["entry_id"]
    ]
    assert alone == []
    assert "touched_at" not in leading(subjects)
    key = leading(usage)["key"]
    assert key.dialect_options["postgresql"]["using"] == "hash"


def test_history_index_names_fit_postgresql() -> None:
    """A short field names its index; a long one gives a digest, the same each time."""
    assert history_index_name("note_id") == f"{HISTORY_INDEX}note_id"
    long = history_index_name(LONG)
    assert long.startswith(HISTORY_INDEX)
    assert len(long) <= MAX_NAME
    assert long == history_index_name(LONG) != history_index_name(LONG[:-1] + "g")


def test_revisions_leave_the_history_indexes_alone() -> None:
    """Reflected history indexes are not compared; other indexes are."""
    history_index = Index(f"{HISTORY_INDEX}note_id", audit_log.c.occurred_at)
    other = Index("ix_domainkit_audit_log_other", audit_log.c.occurred_at)
    elsewhere = Index(f"{HISTORY_INDEX}x", entries.c.posted_at)
    audit_log.indexes.difference_update({history_index, other})
    entries.indexes.discard(elsewhere)
    assert not include_object(history_index, history_index.name, "index", True, None)
    assert include_object(other, other.name, "index", True, None)
    assert include_object(elsewhere, elsewhere.name, "index", True, None)
    assert include_object(history_index, history_index.name, "index", False, None)
    assert include_object(history_index, None, "index", True, None)


def test_the_value_is_written_into_the_sql_on_postgresql() -> None:
    """The field is part of the statement there, as the index has it."""
    on = select(audit_log.c.id).where(aggregate_value("note_id", "postgresql") == "x")
    dialect = PGDialect_asyncpg()  # type: ignore[no-untyped-call]
    assert "details ->> 'note_id'" in str(on.compile(dialect=dialect))
    off = select(audit_log.c.id).where(aggregate_value("note_id", "sqlite") == "x")
    assert "JSON_EXTRACT" in str(off.compile(dialect=SQLiteDialect()))
    with pytest.raises(ValueError, match="not a field name"):
        aggregate_value("x'; DROP TABLE t; --", "postgresql")


@pytest.fixture
def trail(database_url: str, tmp_path: Path) -> Iterator[Path]:
    """The audit trail migrated, with two history fields; the migrations' directory."""
    registry.clear()
    migrations = tmp_path / "migrations"
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=migrations,
        AUDIT={"HISTORY_FIELDS": ["note_id", LONG]},
    ):
        setup()
        call_command("makemigrations", "-m", "init", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield migrations
    registry.clear()


def indexes() -> set[str]:
    """The history indexes the trail has."""

    async def read() -> set[str]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :table"),
                {"table": audit_log.name},
            )
            names = {str(name) for name in found.scalars()}
        await connections.close_all()
        return {name for name in names if name.startswith(HISTORY_INDEX)}

    return asyncio.run(read())


def test_sqlite_gets_no_history_index(trail: Path) -> None:
    """History indexes are PostgreSQL's; SQLite reads by scanning."""
    if os.environ.get(POSTGRES_VARIABLE):
        pytest.skip("SQLite's case")

    async def read() -> list[str]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            )
            names = [str(name) for name in found.scalars()]
        await connections.close_all()
        return names

    assert not [name for name in asyncio.run(read()) if name.startswith(HISTORY_INDEX)]


@on_postgres
def test_migrate_keeps_an_index_per_history_field(trail: Path) -> None:
    """Made by migrate, left by makemigrations, dropped once no field names it."""
    assert indexes() == {history_index_name("note_id"), history_index_name(LONG)}
    output = io.StringIO()
    call_command("makemigrations", "-m", "again", stdout=output)
    assert "No changes detected." in output.getvalue()
    call_command("migrate", stdout=io.StringIO())  # Made already: kept.
    assert indexes() == {history_index_name("note_id"), history_index_name(LONG)}
    with override_settings(AUDIT={"HISTORY_FIELDS": ["other_id"]}):
        call_command("migrate", stdout=io.StringIO())
        assert indexes() == {history_index_name("other_id")}


@on_postgres
def test_no_trail_no_index(database_url: str) -> None:
    """Before ``makemigrations`` wrote the trail, ``migrate`` makes no index."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[AUDIT, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        AUDIT={"HISTORY_FIELDS": ["note_id"]},
    ):
        try:
            setup()

            async def run() -> bool:
                async with connections.engine().begin() as connection:
                    await index_history(connection, "default")
                    made = await connection.run_sync(
                        lambda sync: sync.dialect.has_table(sync, audit_log.name)
                    )
                await connections.close_all()
                return bool(made)

            assert asyncio.run(run()) is False
        finally:
            registry.clear()


@on_postgres
def test_a_history_page_reads_through_its_index(trail: Path) -> None:
    """The plan of a history by ``note_id`` uses its index, prepared or not."""
    note = uuid.uuid4()

    async def explain() -> str:
        async with connections.engine().begin() as connection:
            await connection.execute(
                audit_log.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "occurred_at": datetime(2026, 11, 1, tzinfo=UTC),
                        "actor": "u1",
                        "action": "notes.NoteTitled",
                        "outcome": "success",
                        "details": {"note_id": str(note if n == 0 else uuid.uuid4())},
                    }
                    for n in range(50)
                ],
            )
            await connection.execute(text("SET LOCAL enable_seqscan = off"))
            query = (
                select(audit_log.c.id)
                .where(aggregate_value("note_id", "postgresql") == str(note))
                .order_by(audit_log.c.occurred_at.desc(), audit_log.c.id.desc())
                .limit(51)
            )
            compiled = query.compile(
                dialect=connection.dialect, compile_kwargs={"literal_binds": True}
            )
            plan = await connection.execute(text(f"EXPLAIN {compiled}"))
            lines = "\n".join(str(line) for line in plan.scalars())
        await connections.close_all()
        return lines

    assert history_index_name("note_id") in asyncio.run(explain())
    found = asyncio.run(history(aggregate=("note_id", str(note))))
    asyncio.run(connections.close_all())
    assert len(found) == 1


def test_migrated_tables_need_no_further_revision(
    database_url: str, tmp_path: Path
) -> None:
    """The new and dropped indexes migrate, and compare equal afterwards."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            AUDIT,
            "domainkit.contrib.accounting",
            "domainkit.contrib.approvals",
            "domainkit.contrib.limits",
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        try:
            setup()
            call_command("makemigrations", "-m", "init", stdout=io.StringIO())
            call_command("migrate", stdout=io.StringIO())
            output = io.StringIO()
            call_command("makemigrations", "-m", "again", stdout=output)
        finally:
            registry.clear()
    assert "No changes detected." in output.getvalue()
