"""The worker's health signal: ``runworker --heartbeat-file`` (user report W2).

The file is rewritten while ``/ready``'s checks and the broker's hold, from
the worker's own loop; a failing check leaves it to age, and it goes when
the worker stops.
"""

import asyncio
import io
from pathlib import Path

import pytest

from domainkit.apps import registry
from domainkit.broker import BrokerUnavailable, brokers
from domainkit.conf import override_settings
from domainkit.contrib.messaging import heartbeat as heartbeats
from domainkit.contrib.messaging.worker import Worker
from domainkit.http.health import readiness_failures
from domainkit.management import call_command
from domainkit_tests.test_messaging import brokered, messaging

__all__ = ["brokered", "messaging"]  # The fixtures the tests take.

pytestmark = pytest.mark.anyio


@pytest.mark.usefixtures("messaging")
async def test_a_beat_rewrites_the_file_while_every_check_holds(tmp_path: Path) -> None:
    """All hold: the file holds the time of the beat."""
    path = tmp_path / "alive"

    assert await heartbeats.beat(path, None) is True
    assert path.read_text().strip()


@pytest.mark.usefixtures("messaging")
async def test_a_failing_check_leaves_the_file_to_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A domain that is not ready: no beat, and the log says which."""
    path = tmp_path / "alive"
    path.write_text("an old beat\n")

    async def failing(seconds: float) -> list[str]:
        return ["alpha"]

    monkeypatch.setattr(registry, "check", failing)

    assert await heartbeats.beat(path, None) is False
    assert path.read_text() == "an old beat\n"
    assert "failing alpha" in caplog.text


@pytest.mark.usefixtures("brokered")
async def test_a_broker_that_does_not_answer_fails_the_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker the worker consumes from is checked too."""

    async def down() -> None:
        raise BrokerUnavailable("no route")

    monkeypatch.setattr(brokers["work"], "check", down)

    assert await heartbeats.failures("work") == ["broker:work"]


@pytest.mark.usefixtures("brokered")
async def test_ready_checks_brokers_only_when_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``READINESS_BROKERS``: each broker too, once; off, one away is no failure."""

    async def down() -> None:
        raise BrokerUnavailable("no route")

    monkeypatch.setattr(brokers["work"], "check", down)

    assert await readiness_failures(1.0) == []
    with override_settings(READINESS_BROKERS=True):
        assert await readiness_failures(1.0) == ["broker:work"]
        assert await heartbeats.failures("work") == ["broker:work"]  # Not twice.


@pytest.mark.usefixtures("messaging")
async def test_the_file_goes_when_the_worker_stops(tmp_path: Path) -> None:
    """A stopped worker is no one's: its file is removed."""
    path = tmp_path / "alive"
    stop = asyncio.Event()
    beating = asyncio.create_task(heartbeats.heartbeat(path, 0.01, stop, broker=None))
    for _ in range(500):  # The first beat comes within a round of checks.
        if path.exists():
            break
        await asyncio.sleep(0.01)
    assert path.exists()

    stop.set()
    await beating

    assert not path.exists()


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


def test_the_interval_is_checked() -> None:
    """Zero, or more than an hour, is refused before the worker starts."""
    from domainkit.contrib.messaging.management.commands.runworker import _seconds

    for wrong in ("0", "-1", "3601", "soon"):
        with pytest.raises(Exception, match="seconds"):
            _seconds(wrong)
    assert _seconds("15") == 15.0
