"""The worker's health signals: ``runworker --heartbeat-file`` and ``--ready-file``.

The heartbeat (user report W2) is rewritten from the worker's own loop,
whatever its checks find: a broker or a cache away must not have the
health check restart the workers that carry on without it (ADR 0142,
audit A28-REL-02). The ready file is rewritten while ``/ready``'s checks
and the broker's hold, and removed when one fails. Both go when the
worker stops.
"""

import asyncio
import io
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from domainkit.apps import registry
from domainkit.broker import BrokerUnavailable, brokers
from domainkit.cache import caches
from domainkit.conf import override_settings
from domainkit.contrib.messaging import heartbeat as heartbeats
from domainkit.contrib.messaging.worker import Worker
from domainkit.db import connections
from domainkit.http.health import readiness_failures
from domainkit.management import call_command
from domainkit_tests.test_messaging import brokered, messaging

__all__ = ["brokered", "messaging"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio


@pytest.fixture
async def checked() -> AsyncIterator[None]:
    """Close what the checks opened (the databases' pools) when the test ends.

    Left to the garbage collector, an SQLite connection warns, and the
    warning lands on whichever test runs then.
    """
    yield
    await connections.close_all()


@pytest.mark.usefixtures("messaging", "checked")
async def test_a_beat_rewrites_both_files_while_every_check_holds(
    tmp_path: Path,
) -> None:
    """All hold: each file holds the time of the beat; either may be left out."""
    path, ready = tmp_path / "alive", tmp_path / "ready"

    assert await heartbeats.beat(path, None, ready) is True
    assert path.read_text().strip()
    assert ready.read_text().strip()
    assert await heartbeats.beat(tmp_path / "alone", None) is True
    assert (tmp_path / "alone").read_text().strip()


@pytest.mark.usefixtures("messaging", "checked")
async def test_a_failing_check_removes_the_ready_file_and_the_heartbeat_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A domain that is not ready: the worker lives, is not ready, and says which."""
    path, ready = tmp_path / "alive", tmp_path / "ready"
    path.write_text("an old beat\n")
    ready.write_text("an old beat\n")

    async def failing(seconds: float) -> list[str]:
        return ["alpha"]

    monkeypatch.setattr(registry, "check", failing)

    assert await heartbeats.beat(path, None, ready) is False
    assert path.read_text() != "an old beat\n"
    assert not ready.exists()
    assert "The worker is not ready, and carries on: failing alpha" in caplog.text


@pytest.mark.usefixtures("brokered", "checked")
async def test_a_broker_that_does_not_answer_leaves_the_heartbeat_beating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker the worker consumes from is checked, for readiness only.

    While it is away, relaying workers run the outbox (ADR 0142): a health
    check that restarted them would stop that work.
    """

    async def down() -> None:
        raise BrokerUnavailable("no route")

    monkeypatch.setattr(brokers["work"], "check", down)
    path = tmp_path / "alive"

    assert await heartbeats.failures("work") == ["broker:work"]
    assert await heartbeats.beat(path, "work") is False
    assert path.read_text().strip()


@pytest.mark.usefixtures("messaging", "checked")
async def test_a_cache_that_does_not_answer_leaves_the_heartbeat_beating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache with ``READINESS`` away fails readiness; the worker lives on.

    Caches fail open (ADR 0012): the worker's work goes on without one.
    """

    async def failing(seconds: float) -> list[str]:
        return ["cache:default"]

    monkeypatch.setattr(caches, "check_all", failing)
    path, ready = tmp_path / "alive", tmp_path / "ready"

    assert await heartbeats.beat(path, None, ready) is False
    assert path.read_text().strip()
    assert not ready.exists()


@pytest.mark.usefixtures("brokered", "checked")
async def test_ready_checks_brokers_only_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``READINESS_BROKERS``: each broker too, once; off, one away is no failure."""

    async def down() -> None:
        raise BrokerUnavailable("no route")

    monkeypatch.setattr(brokers["work"], "check", down)

    # Which checks run is asked, not how fast: a loaded runner took over a
    # second to open SQLite, and the database then counted as away.
    assert await readiness_failures(30.0) == []
    with override_settings(READINESS_BROKERS=True):
        assert await readiness_failures(30.0) == ["broker:work"]
        assert await heartbeats.failures("work") == ["broker:work"]  # Not twice.


@pytest.mark.usefixtures("messaging", "checked")
async def test_the_files_go_when_the_worker_stops(tmp_path: Path) -> None:
    """A stopped worker is no one's: its files are removed."""
    path, ready = tmp_path / "alive", tmp_path / "ready"
    stop = asyncio.Event()
    beating = asyncio.create_task(
        heartbeats.heartbeat(path, 0.01, stop, broker=None, ready=ready)
    )
    for _ in range(500):  # The first beat comes within a round of checks.
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert path.exists()
    assert ready.exists()

    stop.set()
    await beating

    assert not path.exists()
    assert not ready.exists()


@pytest.mark.usefixtures("messaging", "checked")
async def test_a_check_slower_than_the_interval_never_holds_up_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The touches keep their own schedule; a stop cuts the slow round short.

    Before, each touch waited for a round of checks: up to the interval
    and twice ``READINESS_TIMEOUT`` apart, so a long timeout aged the file
    during an outage.
    """

    async def unanswered(broker: str | None) -> list[str]:
        await asyncio.sleep(3600)  # A check bounded by a long READINESS_TIMEOUT.
        return []

    monkeypatch.setattr(heartbeats, "failures", unanswered)
    path = tmp_path / "alive"
    touches: list[float] = []
    touch = heartbeats._touch

    async def timed(target: Path) -> None:
        touches.append(time.monotonic())
        await touch(target)

    monkeypatch.setattr(heartbeats, "_touch", timed)
    stop = asyncio.Event()
    beating = asyncio.create_task(heartbeats.heartbeat(path, 0.05, stop, broker=None))

    await asyncio.sleep(0.6)
    stop.set()
    async with asyncio.timeout(5):
        await beating

    assert len(touches) >= 4
    assert not path.exists()


@pytest.mark.usefixtures("brokered", "checked")
async def test_a_check_that_raises_leaves_the_worker_beating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An error the checks do not expect: logged, not ready, and the beats go on.

    Before, it ended the heartbeat, whose files went with it: a healthy
    worker was restarted.
    """

    async def broken() -> None:
        raise RuntimeError("a bug in the broker's client")

    monkeypatch.setattr(brokers["work"], "check", broken)
    path, ready = tmp_path / "alive", tmp_path / "ready"
    ready.write_text("an old beat\n")

    assert await heartbeats.beat(path, "work", ready) is False
    assert path.read_text().strip()
    assert not ready.exists()
    raised = "The worker's checks raised; it is not ready, and carries on"
    assert raised in caplog.messages

    stop = asyncio.Event()
    beating = asyncio.create_task(
        heartbeats.heartbeat(path, 0.01, stop, broker="work", ready=ready)
    )
    for _ in range(500):
        if caplog.messages.count(raised) >= 3:
            break
        await asyncio.sleep(0.01)
    assert caplog.messages.count(raised) >= 3
    assert not beating.done()
    assert path.exists()
    stop.set()
    await beating


@pytest.mark.usefixtures("messaging", "checked")
async def test_a_ready_file_that_cannot_be_written_is_named_as_such(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The checks held: the round says so, and logs the file it could not write."""
    ready = tmp_path / "gone" / "ready"

    assert await heartbeats.beat(None, None, ready) is True
    assert f"Could not write or remove the ready file {ready}" in caplog.messages
    assert not any("checks raised" in message for message in caplog.messages)


async def test_a_heartbeat_file_that_cannot_be_written_ages_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A write that fails is logged each round; the heartbeat carries on."""

    async def ready(broker: str | None) -> list[str]:
        return []

    monkeypatch.setattr(heartbeats, "failures", ready)
    path = tmp_path / "gone" / "alive"
    stop = asyncio.Event()
    beating = asyncio.create_task(heartbeats.heartbeat(path, 0.01, stop, broker=None))
    failed = f"Could not touch the heartbeat file {path}"
    for _ in range(500):
        if caplog.messages.count(failed) >= 2:
            break
        await asyncio.sleep(0.01)

    assert caplog.messages.count(failed) >= 2
    assert not beating.done()
    stop.set()
    await beating


@pytest.mark.usefixtures("messaging")
def test_runworker_beats_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--heartbeat-file``: the file appears while the worker runs, then goes."""
    path = tmp_path / "alive"
    seen: list[bool] = []

    async def run(self: Worker, stop: asyncio.Event) -> None:
        for _ in range(500):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        seen.append(path.exists())

    monkeypatch.setattr(Worker, "run", run)
    call_command(
        "runworker",
        "--heartbeat-file",
        str(path),
        "--heartbeat-interval",
        "0.05",
        stdout=io.StringIO(),
    )

    assert seen == [True]
    assert not path.exists()


@pytest.mark.usefixtures("messaging")
def test_runworker_writes_a_ready_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--ready-file`` without ``--heartbeat-file``: it appears, then goes."""
    ready = tmp_path / "ready"
    seen: list[bool] = []

    async def run(self: Worker, stop: asyncio.Event) -> None:
        for _ in range(500):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        seen.append(ready.exists())

    monkeypatch.setattr(Worker, "run", run)
    call_command(
        "runworker",
        "--ready-file",
        str(ready),
        "--heartbeat-interval",
        "0.05",
        stdout=io.StringIO(),
    )

    assert seen == [True]
    assert not ready.exists()


def test_the_interval_is_checked() -> None:
    """Zero, or more than an hour, is refused before the worker starts."""
    from domainkit.contrib.messaging.management.commands.runworker import _seconds

    for wrong in ("0", "-1", "3601", "soon"):
        with pytest.raises(Exception, match="seconds"):
            _seconds(wrong)
    assert _seconds("15") == 15.0
