"""SEC-11b-2: a kept cut-off connection keeps a session lock its statement took.

CONC-08 keeps an asyncpg connection whose statement a deadline cut off, once
``_settle`` has rolled its transaction back. But the cancel request reaches
the server after the statement may have finished there: the server ignores
it, and whatever the statement did to the session outlives the rollback.
``pg_advisory_lock`` is the plain case: the lock is the session's, not the
transaction's, the request that asked for it never learned it got it (it
was cut off), so nothing unlocks it, and the pool keeps the connection idle,
holding the lock, for good. Every other caller of that lock now waits until
its own deadline. Before 0.11 the connection was closed, and the lock with
it; ``Reads.STATEMENTS`` connections are reset before reuse
(``RESET_SESSION`` unlocks every advisory lock), the transaction pool's are
not.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.db import connections
from domainkit_tests.gadgets import GadgetsUnitOfWork
from domainkit_tests.test_db_cut_off import (  # noqa: F401 - fixtures.
    PoolEvents,
    asyncpg_only,
    cut_off,
    database,
    patient,
    settling,
    tables,
    through_proxy,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("settling")]
KEY = 4242
LOCK = text("SELECT pg_advisory_lock(:key)")
UNLOCK = text("SELECT pg_advisory_unlock(:key)")
LOCKED = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
    "AND objid = :key AND pid <> pg_backend_pid()"
)


async def lock_held_elsewhere(database_url: str) -> int:
    """Advisory locks on ``KEY`` other sessions hold, once closed ones are gone.

    A closed connection's session takes a moment to end on the server, so
    this waits up to five seconds for the count to fall to zero.
    """
    other = create_async_engine(database_url)
    try:
        async with other.connect() as connection:
            for _ in range(50):
                held = int(await connection.scalar(LOCKED, {"key": KEY}) or 0)
                if not held:
                    return 0
                await asyncio.sleep(0.1)
            return held
    finally:
        await other.dispose()


@asyncpg_only
@pytest.mark.usefixtures("patient", "tables")
async def test_a_cut_off_statement_leaves_no_session_lock_in_the_pool(
    database_url: str,
) -> None:
    """The server grants the lock at once; its answer comes after the deadline.

    The statement ran before (another key, locked and unlocked), so it is
    prepared, and asyncpg sends it in one exchange, as it would any
    statement a process runs often.
    """
    delay = {"s": 0.0}
    async with through_proxy(database_url, delay):
        async with GadgetsUnitOfWork() as uow:
            await uow.session.execute(LOCK, {"key": 1})  # Prepared, and the
            await uow.session.execute(UNLOCK, {"key": 1})  # transaction begun.
            delay["s"] = 0.5
            await cut_off(0.2, uow.session.execute(LOCK, {"key": KEY}))
            delay["s"] = 0.0
        # The request is over: it holds nothing it knows of, and its unit of
        # work is closed. The pool's one connection is idle.
        assert await lock_held_elsewhere(database_url) == 0, (
            "the pool's idle connection holds the advisory lock the cut-off "
            "statement took"
        )


@asyncpg_only
@pytest.mark.usefixtures("patient", "tables")
@pytest.mark.parametrize(
    ("before", "cut", "left"),
    [
        (
            [f"SELECT pg_advisory_lock({KEY})", f"SELECT pg_advisory_unlock({KEY})"],
            f"SELECT pg_advisory_lock({KEY})",
            (
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid()"
            ),
        ),
        (
            ["LISTEN domainkit_cut", "UNLISTEN domainkit_cut"],
            "LISTEN domainkit_cut",
            "SELECT count(*) FROM pg_listening_channels()",
        ),
        (
            ["CREATE TEMP TABLE domainkit_cut (x int)", "DROP TABLE domainkit_cut"],
            "CREATE TEMP TABLE domainkit_cut (x int)",
            "SELECT count(*) FROM pg_tables WHERE tablename = 'domainkit_cut'",
        ),
    ],
    ids=["advisory-lock", "listen", "temporary-table"],
)
async def test_a_statement_cut_off_on_its_own_leaves_nothing_in_the_session(
    database_url: str, before: list[str], cut: str, left: str
) -> None:
    """Outside a transaction, what the statement did is not rolled back: it is undone.

    The connection is kept, and the next caller, on the same connection,
    finds none of it.
    """
    delay = {"s": 0.0}
    async with through_proxy(database_url, delay):
        with PoolEvents() as events:
            async with connections.autocommit() as connection:
                for statement in before:  # Prepared, as a statement run often is.
                    await connection.execute(text(statement))
                delay["s"] = 0.5
                await cut_off(0.2, connection.execute(text(cut)))
                delay["s"] = 0.0
            async with connections.autocommit() as connection:
                assert await connection.scalar(text(left)) == 0
        assert events.counts == (0, 0)  # The same connection, kept.
