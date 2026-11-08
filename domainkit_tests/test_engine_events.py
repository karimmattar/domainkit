"""The tests' engine events name the test that leaks a pooled connection."""

import gc
import warnings
from pathlib import Path
from threading import Thread

import anyio
import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit_tests.engine_events import leak_log

pytestmark = pytest.mark.anyio


async def leak(url: str) -> Thread:
    """Take a connection from the pool, and never give it back.

    Returns the thread that serves it, which ends once the collector has
    closed it.
    """
    connection = await create_async_engine(url).connect()
    await connection.execute(text("SELECT 1"))
    raw = await connection.get_raw_connection()
    thread: Thread = raw.dbapi_connection._connection._thread  # type: ignore[union-attr]
    return thread


@pytest.mark.usefixtures("database_access")
async def test_a_leaked_connection_names_where_it_was_taken(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The collector closes it later; the log says where and in which test."""
    serving = await leak(f"sqlite+aiosqlite:///{tmp_path / 'leak.sqlite3'}")

    with caplog.at_level("ERROR", logger=leak_log.name), warnings.catch_warnings():
        warnings.simplefilter("ignore", exc.SAWarning)  # The leak's own warning.
        gc.collect()
    # Its thread answers on this loop: let it end before the loop closes.
    await anyio.to_thread.run_sync(serving.join, 10)
    assert not serving.is_alive()

    [record] = [each for each in caplog.records if each.name == leak_log.name]
    message = record.getMessage()
    assert "was never given back" in message
    assert "test_a_leaked_connection_names_where_it_was_taken" in message
    assert f"{__file__}:" in message
    assert " in leak\n" in message


async def test_a_connection_given_back_is_not_named(
    database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Closing a connection, or detaching it on purpose, logs nothing."""
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        await connection.invalidate()  # Detached and closed, by request.
    await engine.dispose()

    with caplog.at_level("ERROR", logger=leak_log.name):
        gc.collect()

    assert [each for each in caplog.records if each.name == leak_log.name] == []
