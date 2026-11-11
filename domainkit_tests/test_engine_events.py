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

from domainkit_tests import conftest, engine_events
from domainkit_tests.engine_events import CHECKED_OUT_AT, OPENED, leak_log, leaks

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
    listed = leaks.pop()  # This one was made on purpose: the summary stays clean.
    assert listed.startswith(
        "taken during domainkit_tests/test_engine_events.py::"
        "test_the_collectors_close_names_where_it_was_taken"
    )
    assert in_take(listed.splitlines())
    assert "closed during domainkit_tests/test_engine_events.py::" in listed


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


@pytest.mark.usefixtures("database_access")
async def test_an_open_connection_is_kept_until_its_pool_closes_it(
    tmp_path: Path,
) -> None:
    """Given back and still open, it is known by its address; disposed, forgotten."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kept.sqlite3'}")
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        address = id(raw.dbapi_connection._connection)  # type: ignore[union-attr]

    where = OPENED[address]
    await engine.dispose()

    test = "test_an_open_connection_is_kept_until_its_pool_closes_it[asyncio] (call)"
    assert where.splitlines()[0].endswith(test)
    assert f"{__file__}:" in where
    assert f"{engine_events.__file__}:" not in where  # Not the listeners' own.
    assert address not in OPENED


def test_a_failure_that_quotes_an_unclosed_connection_names_who_opened_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning's address is looked up once; other failures list nothing."""
    monkeypatch.setattr(engine_events, "leaks", [])
    monkeypatch.setitem(OPENED, 0x7FCDE7F0E350, "test_a (call)\n  a.py:1 in make")
    failure = (
        "ResourceWarning: <aiosqlite.core.Connection object at 0x7fcde7f0e350> "
        "was deleted before being closed. Please use 'async with' or '.close()'\n"
        "Exception ignored in: <function Connection.__del__ at 0x7fce01ea5080>\n"
        "<aiosqlite.core.Connection object at 0x7fcde7f0e350> was deleted"
    )

    engine_events.name_the_opener("AssertionError: 1 != 2", "test_b")
    engine_events.name_the_opener(failure, "test_b")
    engine_events.name_the_opener(failure, "test_c")  # Named once: it is gone.

    assert engine_events.leaks == [
        (
            "opened during test_a (call)\n  a.py:1 in make\n"
            "never closed; the collector deleted it during test_b"
        )
    ]
    assert 0x7FCDE7F0E350 not in OPENED


@pytest.mark.usefixtures("database_access")
async def test_an_engine_dropped_with_its_connection_open_is_named(
    tmp_path: Path,
) -> None:
    """The real thing: aiosqlite's warning, from a connection no pool closed."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drop.sqlite3'}")
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        served: aiosqlite.Connection = raw.dbapi_connection._connection  # type: ignore[union-attr]
    del connection, raw, engine  # Never disposed: the pool goes with its engine.

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        # What the collector calls when nothing holds the connection.
        served.__del__()  # type: ignore[no-untyped-call]
    # It stopped aiosqlite's thread, which answers on this loop: let it end.
    await anyio.to_thread.run_sync(served._thread.join, 10)
    [warning] = [each for each in caught if "deleted before" in str(each.message)]
    engine_events.name_the_opener(str(warning.message), "the test it failed")

    listed = leaks.pop()  # This one was made on purpose: the summary stays clean.
    assert listed.splitlines()[0].startswith("opened during domainkit_tests/")
    assert "test_an_engine_dropped_with_its_connection_open_is_named" in listed
    assert listed.endswith(
        "never closed; the collector deleted it during the test it failed"
    )


class Reporter:
    """What the summary writes to, in place of pytest's terminal."""

    def __init__(self) -> None:
        """Nothing written yet."""
        self.written: list[str] = []

    def section(self, title: str, **markup: object) -> None:
        """A heading."""
        self.written.append(f"== {title}")

    def line(self, text: str) -> None:
        """A line."""
        self.written.append(text)


def test_the_runs_summary_lists_the_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI reads the summary: the test that leaked is named there, or nothing is."""
    reporter = Reporter()
    monkeypatch.setattr(engine_events, "leaks", [])
    conftest.pytest_terminal_summary(reporter)  # type: ignore[arg-type]
    assert reporter.written == []

    engine_events.leaks.append("taken during test_a\n  a.py:1 in take")
    conftest.pytest_terminal_summary(reporter)  # type: ignore[arg-type]

    assert reporter.written == [
        "== leaked database connections",
        "taken during test_a\n  a.py:1 in take",
        "",
    ]


def test_workers_hand_their_leaks_to_the_process_that_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With pytest-xdist the tests run in workers, and one process reports."""
    from types import SimpleNamespace

    monkeypatch.setattr(engine_events, "leaks", ["taken during test_b"])
    worker = SimpleNamespace(config=SimpleNamespace(workeroutput={}))
    alone = SimpleNamespace(config=SimpleNamespace())

    conftest.pytest_sessionfinish(worker)  # type: ignore[arg-type]
    conftest.pytest_sessionfinish(alone)  # type: ignore[arg-type]
    monkeypatch.setattr(engine_events, "leaks", [])
    conftest.pytest_testnodedown(
        SimpleNamespace(workeroutput=worker.config.workeroutput)
    )
    conftest.pytest_testnodedown(SimpleNamespace())  # A worker that crashed.

    assert worker.config.workeroutput == {conftest.LEAKS: ["taken during test_b"]}
    assert engine_events.leaks == ["taken during test_b"]
