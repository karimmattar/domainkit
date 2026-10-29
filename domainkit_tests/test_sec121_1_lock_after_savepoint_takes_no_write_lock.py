"""SEC-121-1: on SQLite, ``uow.lock()`` after a first savepoint takes no write lock.

``lock()`` on SQLite is the database's write lock: ``_begin_immediate``
sends ``BEGIN IMMEDIATE`` unless the driver is in a transaction already.
0.12.1's ``_begin_before_savepoint`` sends a deferred ``BEGIN`` before a
unit of work's first ``SAVEPOINT``, and the transaction now outlives the
savepoint's ``RELEASE``. A service that opens ``uow.savepoint()`` (and
only reads in it, or rolls it back) and then calls ``lock(name)`` is in a
deferred transaction holding no write lock: ``lock()`` returns at once and
holds nothing, so a second unit of work's ``lock()`` of the same name also
returns. Both go on past the lock, and whatever they do outside the
database (a payout, a call to a provider) happens twice; one of them fails
only at its first write. Before 0.12.1 a released savepoint's ``RELEASE``
committed, and the ``lock()`` after it began with ``BEGIN IMMEDIATE``
(the first test passes on 0.12.0).

The same holds after a savepoint rolled back (no ``RELEASE``) and inside
one (``lock()`` in an atomic batch item), where the ``SAVEPOINT`` itself
began the deferred transaction before 0.12.1 too: older code.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit_tests.gadgets import GadgetsUnitOfWork, metadata

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sqlite_file(database_access: None, tmp_path: Path) -> AsyncIterator[str]:
    """The gadget tables in a SQLite file; a writer waits 0.1 s, then fails."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'gadgets.sqlite3'}"
    with override_settings(
        DATABASES={"default": {"URL": url, "CONNECT_ARGS": {"timeout": 0.1}}}
    ):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        yield url
        await connections.close_all()


class Refused(Exception):
    """Rolls a savepoint back."""


async def read_and_refuse(uow: GadgetsUnitOfWork) -> None:
    """Read in a savepoint, then roll it back (``ROLLBACK TO``, no ``RELEASE``)."""
    try:
        async with uow.savepoint():
            await uow.gadgets.count()
            raise Refused
    except Refused:
        pass


async def test_a_lock_after_a_released_savepoint_is_the_write_lock(
    sqlite_file: str,
) -> None:
    """``lock()`` after a savepoint released holds the write lock (0.12.1)."""
    async with GadgetsUnitOfWork() as one:
        async with one.savepoint():
            await one.gadgets.count()
        await one.lock("payout:acct-1")
        async with GadgetsUnitOfWork() as two:
            with pytest.raises(OperationalError, match="database is locked"):
                await two.lock("payout:acct-1")


async def test_a_lock_after_a_rolled_back_savepoint_is_the_write_lock(
    sqlite_file: str,
) -> None:
    """``lock()`` after a savepoint holds the write lock: another lock waits."""
    async with GadgetsUnitOfWork() as one:
        await read_and_refuse(one)
        await one.lock("payout:acct-1")
        async with GadgetsUnitOfWork() as two:
            with pytest.raises(OperationalError, match="database is locked"):
                await two.lock("payout:acct-1")


async def test_a_lock_inside_a_savepoint_is_the_write_lock(
    sqlite_file: str,
) -> None:
    """``lock()`` in a savepoint (an atomic batch item) holds the write lock."""
    async with GadgetsUnitOfWork() as one, one.savepoint():
        await one.lock("payout:acct-1")
        async with GadgetsUnitOfWork() as two:
            with pytest.raises(OperationalError, match="database is locked"):
                await two.lock("payout:acct-1")
