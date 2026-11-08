"""The tests' engine events name the test that leaks a pooled connection.

The garbage collector closes a leaked connection when it gets to it, which
differs between interpreters (a cycle's weak references may be cleared
before their callbacks run). So these tests make the call its callback
makes, SQLAlchemy's ``_finalize_fairy`` with ``is_gc_cleanup``, which
``engine_events`` relies on too.
"""

import warnings
import weakref
from pathlib import Path

import aiosqlite
import anyio
import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from domainkit_tests.engine_events import CHECKED_OUT_AT, leak_log

pytestmark = pytest.mark.anyio


class Gone:
    """An object nothing keeps, so a weak reference to it is dead at once."""


async def take(url: str) -> AsyncConnection:
    """Take a connection from a new engine's pool, as a leaking test would."""
    connection = await create_async_engine(url).connect()
    await connection.execute(text("SELECT 1"))
    return connection


def in_take(lines: list[str]) -> bool:
    """Whether a line names this file's ``take``, where the connection was taken."""
    return any(
        line.strip().startswith(f"{__file__}:") and line.endswith(" in take")
        for line in lines
    )


@pytest.mark.usefixtures("database_access")
async def test_a_checkout_keeps_the_test_and_the_lines_that_took_it(
    tmp_path: Path,
) -> None:
    """Domainkit's frames and its tests', across SQLAlchemy's greenlet."""
    connection = await take(f"sqlite+aiosqlite:///{tmp_path / 'taken.sqlite3'}")
    raw = await connection.get_raw_connection()
    test, frames = raw.info[CHECKED_OUT_AT]
    await connection.close()
    await connection.engine.dispose()

    assert test.endswith(
        "test_a_checkout_keeps_the_test_and_the_lines_that_took_it[asyncio] (call)"
    )
    assert in_take(frames)
    assert any(
        "in test_a_checkout_keeps_the_test_and_the_lines_that_took_it" in line
        for line in frames
    )
    assert not [line for line in frames if "site-packages" in line]


@pytest.mark.usefixtures("database_access")
async def test_the_collectors_close_names_where_it_was_taken(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """What the collector runs on a leaked connection logs where it was taken."""
    connection = await take(f"sqlite+aiosqlite:///{tmp_path / 'leak.sqlite3'}")
    raw = await connection.get_raw_connection()
    served: aiosqlite.Connection = raw.dbapi_connection._connection  # type: ignore[union-attr]
    # The call the collector makes once the connection is unreachable: the
    # same function and arguments, with the connection marked as collected.
    _, collect, arguments, keywords = raw._finalizer.detach()
    raw._connection_record.fairy_ref = weakref.ref(Gone())

    with caplog.at_level("ERROR", logger=leak_log.name), warnings.catch_warnings():
        warnings.simplefilter("ignore", exc.SAWarning)  # The leak's own warning.
        collect(*arguments, **keywords)
    # It stopped aiosqlite's thread, which answers on this loop: let it end.
    await anyio.to_thread.run_sync(served._thread.join, 10)
    assert not served._thread.is_alive()
    await connection.engine.dispose()

    [logged] = [each for each in caplog.records if each.name == leak_log.name]
    message = logged.getMessage()
    assert "was never given back" in message
    assert "test_the_collectors_close_names_where_it_was_taken" in message
    assert in_take(message.splitlines())


async def test_a_connection_given_back_is_not_named(
    database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Closing a connection, or detaching it on purpose, logs nothing."""
    engine = create_async_engine(database_url)
    with caplog.at_level("ERROR", logger=leak_log.name):
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            await connection.invalidate()  # Detached and closed, by request.
        await engine.dispose()

    assert [each for each in caplog.records if each.name == leak_log.name] == []
