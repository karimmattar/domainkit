"""What only a PostgreSQL server shows: row locks, triggers, and timeouts.

These run when ``DOMAINKIT_TEST_POSTGRES_URL`` names a server, as in CI, and
are skipped otherwise. The other database tests run on that server too,
through the ``database_url`` fixture.
"""

import asyncio
import io
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Connection, MetaData, Table, delete, select, text, update
from sqlalchemy.exc import DBAPIError

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import UNSET, DatabaseConfig, override_settings
from domainkit.contrib.audit import record
from domainkit.contrib.audit.protection import protect, unprotect
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.messaging import Worker
from domainkit.contrib.messaging.outbox import message_row, write
from domainkit.contrib.messaging.tables import messages
from domainkit.db import UTCDateTime, connections
from domainkit.db.connections import create_engine
from domainkit.management import call_command
from domainkit_tests.conftest import POSTGRES_VARIABLE

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_VARIABLE),
        reason=f"set {POSTGRES_VARIABLE} to a PostgreSQL server",
    ),
]


@pytest.fixture
def installed(database_url: str) -> Iterator[str]:
    """Messaging and audit installed on an empty PostgreSQL database."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=["domainkit.contrib.messaging", "domainkit.contrib.audit"],
        DATABASES={"default": {"URL": database_url}},
        ALPHA=UNSET,
    ):
        setup()
        yield database_url
    registry.clear()


@pytest.fixture
async def due(installed: str) -> AsyncIterator[list[uuid.UUID]]:
    """Twenty messages, all due now."""
    rows = [
        message_row(
            kind="task", target="tests.noop", payload={}, headers={}, max_attempts=3
        )
        for _ in range(20)
    ]
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
        await write(connection, connection.dialect.name, rows)
    yield [row["id"] for row in rows]
    await connections.close_all()


async def test_claims_pass_over_messages_another_worker_holds(
    due: list[uuid.UUID],
) -> None:
    """SKIP LOCKED: a claim takes free rows instead of waiting for locked ones."""
    held = set(due[:5])
    async with connections.engine().connect() as other:
        await other.execute(
            select(messages.c.id).where(messages.c.id.in_(held)).with_for_update()
        )
        async with asyncio.timeout(5):  # Without SKIP LOCKED this waits forever.
            claimed = {message.id for message in await Worker().claim(100)}
        await other.rollback()

    assert claimed == set(due) - held
    async with asyncio.timeout(5):
        assert {message.id for message in await Worker().claim(100)} == held


async def test_concurrent_workers_never_claim_the_same_message(
    due: list[uuid.UUID],
) -> None:
    """Four workers claiming at once split the twenty messages between them."""
    batches = await asyncio.gather(*(Worker().claim(5) for _ in range(4)))
    claimed = [message.id for batch in batches for message in batch]

    assert sorted(claimed) == sorted(due)


class Migration:
    """The part of Alembic's ``op`` that protect() and unprotect() use."""

    def __init__(self, connection: Connection) -> None:
        """Run statements on ``connection``."""
        self.connection = connection

    def get_bind(self) -> Connection:
        """Return the migration's connection."""
        return self.connection

    def execute(self, sqltext: str) -> object:
        """Run one SQL statement."""
        return self.connection.exec_driver_sql(sqltext)


async def test_the_audit_trail_refuses_changes_even_from_its_owner(
    installed: str,
) -> None:
    """The triggers refuse UPDATE, DELETE, and TRUNCATE for every role.

    The test role owns the table and is a superuser, and is refused all the
    same; only dropping the triggers (unprotect) allows a retention job.
    """
    async with connections.engine().begin() as connection:
        await connection.run_sync(audit_log.create)
        await connection.run_sync(lambda sync: protect(Migration(sync)))
    await record("invoices.issue", target="invoice:1")

    for statement in (
        update(audit_log).values(actor="someone else"),
        delete(audit_log),
        text(f"TRUNCATE {audit_log.name}"),
    ):
        async with connections.engine().connect() as connection:
            with pytest.raises(DBAPIError, match="append-only"):
                await connection.execute(statement)
            await connection.rollback()

    async with connections.engine().begin() as connection:
        await connection.run_sync(lambda sync: unprotect(Migration(sync)))
        removed = await connection.execute(delete(audit_log))
    assert removed.rowcount == 1
    await connections.close_all()


async def test_statements_are_cancelled_after_the_timeout(database_url: str) -> None:
    """The server enforces each timeout that connect_args sends it."""
    config = DatabaseConfig.model_validate(
        {
            "URL": database_url,
            "STATEMENT_TIMEOUT": 0.3,
            "LOCK_TIMEOUT": 2,
            "IDLE_IN_TRANSACTION_TIMEOUT": 60,
        }
    )
    engine = create_engine(config)
    try:
        async with engine.connect() as connection:
            shown = [
                (await connection.exec_driver_sql(f"SHOW {name}")).scalar_one()
                for name in (
                    "statement_timeout",
                    "lock_timeout",
                    "idle_in_transaction_session_timeout",
                )
            ]
            with pytest.raises(DBAPIError, match="statement timeout"):
                await connection.exec_driver_sql("SELECT pg_sleep(2)")
    finally:
        await engine.dispose()

    assert shown == ["300ms", "2s", "1min"]


def test_migrations_outlive_the_statement_timeout(
    database_url: str, tmp_path: Path
) -> None:
    """A revision may run longer than STATEMENT_TIMEOUT allows requests."""
    versions = tmp_path / "migrations"
    versions.mkdir()
    (versions / "0001_slow.py").write_text(
        '"""Slow."""\n\n'
        "from alembic import op\n\n"
        'revision = "0001_slow"\n'
        "down_revision = None\n\n\n"
        "def upgrade() -> None:\n"
        '    op.execute("SELECT pg_sleep(1)")\n\n\n'
        "def downgrade() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    database = {"URL": database_url, "STATEMENT_TIMEOUT": 0.3}
    output = io.StringIO()
    with override_settings(MIGRATIONS_DIR=versions, DATABASES={"default": database}):
        call_command("migrate", stdout=output)

    assert output.getvalue() == "Database 'default' is at head.\n"


async def test_timestamps_keep_their_instant_and_come_back_in_utc(
    database_url: str,
) -> None:
    """UTCDateTime is ``timestamp with time zone`` there: offsets are kept."""
    stamps = Table("stamps", MetaData(), Column("at", UTCDateTime))
    noon_at_plus_three = datetime(
        2026, 10, 14, 12, 0, tzinfo=timezone(timedelta(hours=3))
    )
    engine = create_engine(DatabaseConfig.model_validate({"URL": database_url}))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(stamps.create)
            await connection.execute(stamps.insert().values(at=noon_at_plus_three))
            stored = (await connection.execute(select(stamps.c.at))).scalar_one()
            column_type = (
                await connection.exec_driver_sql(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'stamps'"
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert stored == noon_at_plus_three
    assert stored.tzinfo == UTC
    assert column_type == "timestamp with time zone"
