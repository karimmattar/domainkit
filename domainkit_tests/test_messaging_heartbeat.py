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
from pathlib import Path

import pytest

from domainkit.apps import registry
from domainkit.broker import BrokerUnavailable, brokers
from domainkit.cache import caches
from domainkit.conf import override_settings
from domainkit.contrib.messaging import heartbeat as heartbeats
from domainkit.contrib.messaging.worker import Worker
from domainkit.http.health import readiness_failures
from domainkit.management import call_command
from domainkit_tests.test_messaging import brokered, messaging

__all__ = ["brokered", "messaging"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio


@pytest.mark.usefixtures("messaging")
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


@pytest.mark.usefixtures("messaging")
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


@pytest.mark.usefixtures("brokered")
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


@pytest.mark.usefixtures("messaging")
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


@pytest.mark.usefixtures("brokered")
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


@pytest.mark.usefixtures("messaging")
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
