"""A commit SQLite refuses as locked leaves nothing behind on the connection.

In SQLite's default journal, a reader's lock makes a writer's ``COMMIT``
fail with "database is locked" once its busy timeout ends, and SQLite
keeps that transaction open so that the ``COMMIT`` may be tried again.
The connection went back to the pool with it: the next unit of work
handed that connection read the rows that had failed to commit, and its
own commit wrote them.
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import PoolResetState

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.db.connections import _end_transaction_left_open
from domainkit_tests.gadgets import GadgetsUnitOfWork, make_gadget, metadata
from domainkit_tests.test_db_reads import store

pytestmark = pytest.mark.anyio


@pytest.fixture
async def locking(database_access: None, tmp_path: Path) -> AsyncIterator[Path]:
    """A SQLite file in the default journal, with a busy timeout of 0.1 s."""
    path = tmp_path / "locks.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=delete")
    config = {"URL": f"sqlite+aiosqlite:///{path}", "CONNECT_ARGS": {"timeout": 0.1}}
    with override_settings(DATABASES={"default": config}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        await store("first")
        try:
            yield path
        finally:
            await connections.close_all()


def committed(path: Path) -> list[str]:
    """The gadgets the file holds, read apart from the pool."""
    with closing(sqlite3.connect(path)) as connection:
        found = connection.execute("SELECT name FROM framework_test_gadgets")
        return sorted(row[0] for row in found)


async def refused_commit() -> None:
    """Add "second" in work whose commit a reader's lock refuses."""
    async with GadgetsUnitOfWork(readonly=True) as reading:
        await reading.gadgets.filter()
        async with GadgetsUnitOfWork() as writing:
            await writing.gadgets.add(make_gadget("second"))
            with pytest.raises(OperationalError, match="is locked"):
                await writing.commit()


async def test_the_next_writer_commits_only_its_own_rows(locking: Path) -> None:
    """Its commit wrote the refused row too, inside the transaction left open."""
    await refused_commit()

    await store("third")

    assert committed(locking) == ["first", "third"]


async def test_the_next_reader_begins_its_own_snapshot(locking: Path) -> None:
    """It failed to begin ("cannot start a transaction within a transaction")."""
    await refused_commit()

    async with GadgetsUnitOfWork(readonly=True) as after:
        seen = sorted(gadget.name for gadget in await after.gadgets.filter())

    assert seen == ["first"]
    assert committed(locking) == ["first"]


class Driver:
    """A SQLite driver connection, in a transaction or not."""

    def __init__(self, *, in_transaction: bool) -> None:
        """Hold ``in_transaction``."""
        self.in_transaction = in_transaction


class Adapted:
    """SQLAlchemy's adapter of a driver connection, counting rollbacks."""

    def __init__(self, *, in_transaction: bool) -> None:
        """Adapt a driver connection that is ``in_transaction`` or not."""
        self.driver_connection = Driver(in_transaction=in_transaction)
        self.rollbacks = 0

    def rollback(self) -> None:
        """Count a rollback."""
        self.rollbacks += 1


@pytest.mark.parametrize(
    ("in_transaction", "terminate_only", "asyncio_safe", "rollbacks"),
    [
        (True, False, True, 1),  # Left open: rolled back.
        (False, False, True, 0),  # Nothing open.
        (True, True, True, 0),  # Being closed anyway.
        (True, False, False, 0),  # Outside the event loop: the pool terminates it.
    ],
)
def test_a_connection_back_in_the_pool_is_rolled_back_only_when_left_open(
    in_transaction: bool, terminate_only: bool, asyncio_safe: bool, rollbacks: int
) -> None:
    """The pool's reset rolls back what SQLite kept open, and nothing else."""
    connection = Adapted(in_transaction=in_transaction)
    state = PoolResetState(
        transaction_was_reset=True,
        terminate_only=terminate_only,
        asyncio_safe=asyncio_safe,
    )

    _end_transaction_left_open(connection, None, state)  # type: ignore[arg-type]

    assert connection.rollbacks == rollbacks
