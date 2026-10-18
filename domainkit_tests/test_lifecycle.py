"""Closing what a process opened: every resource, even when one fails."""

import asyncio
import logging
from functools import partial

import pytest

from domainkit import lifecycle
from domainkit.broker import brokers
from domainkit.cache import CacheHandler, DummyStore
from domainkit.cache import caches as cache_handler
from domainkit.conf import override_settings
from domainkit.db.connections import connections
from domainkit.ledger import ledgers
from domainkit.lifecycle import close_resources
from domainkit.utils.closing import close_each

pytestmark = pytest.mark.anyio


async def test_every_resource_closes_even_when_one_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing or hung close is logged, and the rest still close, in order."""
    closed: list[str] = []

    async def fails() -> None:
        closed.append("caches")
        raise RuntimeError("cache server gone")

    async def hangs() -> None:
        closed.append("databases")
        await asyncio.Event().wait()

    async def closes(name: str) -> None:
        closed.append(name)

    monkeypatch.setattr(cache_handler, "close_all", fails)
    monkeypatch.setattr(connections, "close_all", hangs)
    monkeypatch.setattr(ledgers, "close_all", partial(closes, "ledgers"))
    monkeypatch.setattr(brokers, "close_all", partial(closes, "brokers"))
    monkeypatch.setattr(lifecycle, "CLOSE_TIMEOUT", 0.05)

    with caplog.at_level(logging.ERROR, "domainkit.lifecycle"):
        async with asyncio.timeout(5):
            await close_resources()

    assert closed == ["caches", "databases", "ledgers", "brokers"]
    assert caplog.messages == [
        "Could not close the caches; closing the rest",
        "Could not close the databases; closing the rest",
    ]
    causes = [record.exc_info for record in caplog.records]
    assert [cause[0] if cause else None for cause in causes] == [
        RuntimeError,
        TimeoutError,
    ]


async def test_close_each_closes_every_item_then_raises_the_first_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Later failures are logged, since only the first can be raised."""
    closed: list[str] = []

    async def close(item: str) -> None:
        closed.append(item)
        if item != "c":
            raise RuntimeError(f"{item} failed")

    with (
        caplog.at_level(logging.ERROR, "domainkit.utils.closing"),
        pytest.raises(RuntimeError, match=r"^a failed$"),
    ):
        await close_each(["a", "b", "c"], close)

    assert closed == ["a", "b", "c"]
    assert caplog.messages == ["Could not close 'b'"]
    cause = caplog.records[0].exc_info
    assert cause
    assert str(cause[1]) == "b failed"
    await close_each(["c"], close)  # Nothing failed: nothing raised.
    assert closed[-1] == "c"


class BrokenStore(DummyStore):
    """A store whose close fails, after recording it."""

    def __init__(self, closed: list[str], name: str, *, fails: bool) -> None:
        """Report closing as ``name``; fail if ``fails``."""
        self.closed = closed
        self.name = name
        self.fails = fails

    async def close(self) -> None:
        """Record the close, then fail if asked to."""
        self.closed.append(self.name)
        if self.fails:
            raise RuntimeError(f"{self.name} would not close")


def test_handlers_close_every_resource_when_one_fails() -> None:
    """A cache that fails to close does not keep the next one open."""
    closed: list[str] = []
    handler = CacheHandler(
        connect=lambda config: BrokenStore(
            closed, config.KEY_PREFIX or "", fails=config.KEY_PREFIX == "one"
        )
    )
    caches = {
        "default": {},
        "one": {"URL": "redis://cache:6379/0", "KEY_PREFIX": "one"},
        "two": {"URL": "redis://cache:6379/0", "KEY_PREFIX": "two"},
    }

    async def use_then_close() -> None:
        handler["one"], handler["two"]
        await handler.close_all()

    with (
        override_settings(CACHES=caches),
        pytest.raises(RuntimeError, match=r"^one would not close$"),
    ):
        asyncio.run(use_then_close())

    assert closed == ["one", "two"]
