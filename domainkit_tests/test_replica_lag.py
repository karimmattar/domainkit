"""Read replicas with ``MAX_LAG``: read while within it, else the primary is.

User report D1: read-only work took turns on the replicas however far
behind they were, or whether they answered at all, and ``/ready`` failed
with any of them. A replica with ``MAX_LAG`` is now read only while a
probe, run in the background at most once a second, found it within that
bound; read-only work reads the primary meanwhile, and comes back once the
replica caught up (ADR 0159). A real standby is not at hand: the probe is
stubbed (``Lags``), and on PostgreSQL its statements are replaced by ones
that answer as a standby would.
"""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any, Final

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import clock, tasks
from domainkit.conf import override_settings
from domainkit.conf.global_settings import Settings
from domainkit.db import connections, replicas
from domainkit.db.routing import router
from domainkit_tests.conftest import POSTGRES_VARIABLE, MakeDatabase
from domainkit_tests.gadgets import GadgetsUnitOfWork, make_gadget, metadata

pytestmark = pytest.mark.anyio
LOCAL: Final = "sqlite+aiosqlite:///x.sqlite3"
ON_POSTGRES: Final = bool(os.environ.get(POSTGRES_VARIABLE))
postgres_only = pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}")


class Lags:
    """``measure_lag`` in place of the probe: each replica's lag, as the test sets it.

    A lag may be an exception, which the probe raises. While ``gate`` is an
    event, probes wait for it.
    """

    def __init__(self) -> None:
        """Every replica 0.1 seconds behind, until told otherwise."""
        self.lags: dict[str, float | BaseException] = {}
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(
        self, replica: str, primary: str, max_lag: float
    ) -> tuple[float, float]:
        """The lag set for ``replica``, as of when the probe began."""
        self.calls.append(replica)
        at = clock.monotonic()
        if self.gate is not None:
            await self.gate.wait()
        lag = self.lags.get(replica, 0.1)
        if isinstance(lag, BaseException):
            raise lag
        return lag, at


@pytest.fixture
def lags(monkeypatch: pytest.MonkeyPatch) -> Lags:
    """Stub the probe of every replica."""
    stub = Lags()
    monkeypatch.setattr(replicas, "measure_lag", stub)
    return stub


async def probed() -> None:
    """Let this loop's probes in flight finish."""
    state = replicas._loops.find()
    if state is not None:
        await asyncio.gather(*state.probes.values())


async def read_alias() -> str:
    """The alias a read-only unit of work reads, once entered."""
    async with GadgetsUnitOfWork(readonly=True) as uow:
        return uow.alias


async def read_names() -> list[str]:
    """The gadgets a read-only unit of work finds."""
    async with GadgetsUnitOfWork(readonly=True) as uow:
        return sorted(gadget.name for gadget in await uow.gadgets.filter())


def switches(replica: str, to: str) -> float:
    """``db_replica_switches_total`` of ``replica`` toward ``to``."""
    labels = {"database": replica, "to": to}
    return REGISTRY.get_sample_value("db_replica_switches_total", labels) or 0.0


def checked(urls: dict[str, str], max_lag: float = 5.0) -> dict[str, Any]:
    """``DATABASES``: ``default`` at its URL, every other alias its replica."""
    names = [alias for alias in urls if alias != "default"]
    return {
        "default": {"URL": urls["default"], "REPLICAS": names},
        **{alias: {"URL": urls[alias], "MAX_LAG": max_lag} for alias in names},
    }


@pytest.fixture
async def one_replica(make_database: MakeDatabase) -> AsyncIterator[None]:
    """``default`` with replica ``copy``, read within 5 seconds of it."""
    urls = {"default": make_database("primary"), "copy": make_database("copy")}
    with override_settings(DATABASES=checked(urls)):
        yield
        await connections.close_all()


@pytest.fixture
async def two_replicas(make_database: MakeDatabase) -> AsyncIterator[None]:
    """``default`` with replicas ``one`` and ``two``, each read within 5 seconds."""
    urls = {
        "default": make_database("primary"),
        "one": make_database("one"),
        "two": make_database("two"),
    }
    with override_settings(DATABASES=checked(urls)):
        yield
        await connections.close_all()


def test_only_a_replica_takes_max_lag_from_one_second_to_a_day() -> None:
    """A bound on a primary would bound nothing; under a second, it would flap."""
    copy = {"URL": LOCAL, "MAX_LAG": 1}
    Settings.model_validate(
        {"DATABASES": {"default": {"URL": LOCAL, "REPLICAS": ["copy"]}, "copy": copy}}
    )
    with pytest.raises(ValidationError, match=r"MAX_LAG bounds a read replica's lag"):
        Settings.model_validate(
            {"DATABASES": {"default": {"URL": LOCAL, "MAX_LAG": 5}}}
        )
    for refused in (0.5, 86_401):
        with pytest.raises(ValidationError, match="MAX_LAG"):
            Settings.model_validate(
                {
                    "DATABASES": {
                        "default": {"URL": LOCAL, "REPLICAS": ["copy"]},
                        "copy": {"URL": LOCAL, "MAX_LAG": refused},
                    }
                }
            )


async def test_a_replica_is_read_once_known_to_be_within_max_lag(
    one_replica: None, lags: Lags
) -> None:
    """Nothing known yet: the primary, while a probe runs; then the replica."""
    with clock.freeze_time():
        assert await read_alias() == "default"
        await probed()

        assert await read_alias() == "copy"
        assert await read_alias() == "copy"
    assert lags.calls == ["copy"]  # Read again only once a second has passed.


async def test_reads_leave_a_replica_past_max_lag_and_come_back(
    one_replica: None, lags: Lags, caplog: pytest.LogCaptureFixture
) -> None:
    """Each switch is a log line and a count; nothing else is, nor stays unread."""
    left, back = switches("copy", "primary"), switches("copy", "replica")
    caplog.set_level(logging.INFO, "domainkit.db.replicas")
    with clock.freeze_time() as frozen:
        await read_alias()
        await probed()
        lags.lags["copy"] = 7.5
        frozen.tick(1)

        assert await read_alias() == "copy"  # 0.1 + 1 is within 5; a probe starts.
        await probed()
        assert await read_alias() == "default"
        assert switches("copy", "primary") == left + 1
        assert REGISTRY.get_sample_value(
            "db_replica_lag_seconds", {"database": "copy"}
        ) == pytest.approx(7.5)

        lags.lags["copy"] = 0.2
        frozen.tick(1)
        assert await read_alias() == "default"  # 7.5 + 1 is past 5; a probe starts.
        await probed()
        assert await read_alias() == "copy"
        assert switches("copy", "replica") == back + 1
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        (
            "WARNING",
            (
                "Read-only work on 'default' reads the primary, not replica "
                "'copy': it is 7.5 seconds behind, over its MAX_LAG of 5"
            ),
        ),
        (
            "INFO",
            (
                "Read-only work on 'default' reads replica 'copy' again: it is "
                "0.2 seconds behind"
            ),
        ),
    ]


async def test_reads_leave_a_replica_whose_lag_cannot_be_read(
    one_replica: None, lags: Lags, caplog: pytest.LogCaptureFixture
) -> None:
    """Out of reach, refused, too slow, or a lag that cannot be told: the primary."""
    refused = ConnectionRefusedError("refused")
    lags.lags["copy"] = refused
    with clock.freeze_time() as frozen:
        await read_alias()
        await probed()
        assert await read_alias() == "default"
        (record,) = caplog.records
        assert record.getMessage().endswith("its lag could not be read")
        assert record.exc_info is not None
        assert record.exc_info[1] is refused

        caplog.clear()
        lags.lags["copy"] = math.inf
        frozen.tick(1)
        await read_alias()
        await probed()
        assert await read_alias() == "default"
    assert caplog.records == []  # Still unread: no switch.
    assert (
        REGISTRY.get_sample_value("db_replica_lag_seconds", {"database": "copy"})
        != math.inf
    )


async def test_an_unknown_lag_is_logged_as_unknown(
    one_replica: None, lags: Lags, caplog: pytest.LogCaptureFixture
) -> None:
    """No transaction replayed since the standby started, and not all of the WAL."""
    lags.lags["copy"] = math.inf
    with clock.freeze_time():
        await read_alias()
        await probed()

        assert await read_alias() == "default"
    (record,) = caplog.records
    assert "its lag is unknown" in record.getMessage()


async def test_reads_never_go_to_another_replica_past_its_bound(
    two_replicas: None, lags: Lags
) -> None:
    """Another replica within its bound is read; with none, the primary."""
    lags.lags["one"] = 30.0
    with clock.freeze_time() as frozen:
        await read_alias()
        await probed()
        assert {await read_alias() for _ in range(6)} == {"two"}

        lags.lags["two"] = 30.0
        frozen.tick(1)
        await read_alias()
        await probed()
        assert {await read_alias() for _ in range(6)} == {"default"}

        lags.lags["one"] = lags.lags["two"] = 0.1
        frozen.tick(1)
        await read_alias()
        await probed()
        assert {await read_alias() for _ in range(6)} == {"one", "two"}


async def test_a_reading_aged_past_max_lag_reads_the_primary_while_a_probe_runs(
    one_replica: None, lags: Lags, caplog: pytest.LogCaptureFixture
) -> None:
    """A replica that stopped since may be its lag plus that age behind.

    One probe runs at a time, however many reads find the reading old;
    reads never wait for it.
    """
    lags.lags["copy"] = 1.0
    with clock.freeze_time() as frozen:
        await read_alias()
        await probed()
        lags.gate = asyncio.Event()
        frozen.tick(1)
        assert await read_alias() == "copy"  # 1 + 1 is within 5.
        await asyncio.sleep(0)  # The probe begins, and waits.

        frozen.tick(3.5)
        assert [await read_alias() for _ in range(3)] == ["default"] * 3
        assert lags.calls == ["copy", "copy"]

        lags.gate.set()
        await probed()
        assert await read_alias() == "copy"
    assert caplog.records == []  # The reading aged; nothing switched.


async def test_a_task_reads_its_own_writes_once_the_replica_replayed_them(
    one_replica: None, lags: Lags
) -> None:
    """After a commit, the task reads the primary until a later reading.

    Other tasks read the replica meanwhile. Here the two databases hold
    different rows, so what is read shows where.
    """
    for alias in ("default", "copy"):
        async with connections.engine(alias).begin() as connection:
            await connection.run_sync(metadata.create_all)
    async with AsyncSession(connections.engine("copy")) as session:
        session.add(make_gadget("replicated"))
        await session.commit()

    def elsewhere(
        work: Callable[[], Coroutine[Any, Any, list[str]]],
    ) -> asyncio.Task[list[str]]:
        """``work`` in a task of its own, which committed nothing."""
        return tasks.spawn(work(), name="test.elsewhere")

    with clock.freeze_time() as frozen:
        await read_alias()
        await probed()
        assert await read_names() == ["replicated"]
        for name in ("written", "written too"):
            async with GadgetsUnitOfWork() as uow:
                await uow.gadgets.add(make_gadget(name))
                await uow.commit()

        assert await read_names() == ["written", "written too"]
        assert await elsewhere(read_names) == ["replicated"]
        frozen.tick(1)
        assert await read_names() == ["written", "written too"]  # A probe starts.
        await probed()
        assert await read_names() == ["replicated"]


async def test_a_unit_of_work_entered_again_checks_its_replica_again(
    one_replica: None, lags: Lags
) -> None:
    """``run`` enters a unit of work for each run: each finds where to read then."""
    with clock.freeze_time() as frozen:
        await read_alias()
        await probed()
        uow = GadgetsUnitOfWork(readonly=True)
        async with uow:
            assert uow.alias == "copy"
        lags.lags["copy"] = 30.0
        frozen.tick(1)
        async with uow:
            assert uow.alias == "copy"
        await probed()

        async with uow:
            assert uow.alias == "default"


async def test_new_settings_are_read_afresh(one_replica: None, lags: Lags) -> None:
    """A reading made under other settings is no reading."""
    databases = {
        alias: connections.config(alias).model_copy() for alias in ("default", "copy")
    }
    with clock.freeze_time():
        await read_alias()
        await probed()
        assert await read_alias() == "copy"

        with override_settings(DATABASES=databases):
            assert await read_alias() == "default"
            await probed()
            assert await read_alias() == "copy"
    assert lags.calls == ["copy", "copy"]


def test_without_an_event_loop_a_replica_with_max_lag_is_not_chosen(
    make_database: MakeDatabase,
) -> None:
    """Nothing is known there, and no probe can start; others are read as before."""
    urls = {"default": make_database("primary"), "copy": make_database("copy")}
    databases = checked(urls)
    with override_settings(DATABASES=databases):
        assert GadgetsUnitOfWork(readonly=True).alias == "default"
    databases["copy"].pop("MAX_LAG")
    with override_settings(DATABASES=databases):
        assert GadgetsUnitOfWork(readonly=True).alias == "copy"
        assert router.read_alias("default", "copy") == "copy"


async def test_ready_leaves_out_a_replica_with_max_lag(
    make_database: MakeDatabase, tmp_path: Path
) -> None:
    """Reads go to the primary while it is away: it takes no process out of rotation.

    A replica without ``MAX_LAG`` still fails ``/ready``: reads have nowhere
    else to go.
    """
    gone = f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'copy.sqlite3'}"
    urls = {"default": make_database("primary"), "copy": gone}
    databases = checked(urls)
    with override_settings(DATABASES=databases):
        connections._read_engine("copy")  # Its STATEMENTS pool is left out too.
        assert await connections.check_all(5.0) == []
        await connections.close_all()
    databases["copy"].pop("MAX_LAG")
    with override_settings(DATABASES=databases):
        assert await connections.check_all(5.0) == ["database:copy"]
        await connections.close_all()


async def test_a_replica_out_of_reach_is_found_by_the_real_probe(
    make_database: MakeDatabase, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No stub: the probe cannot open it, and reads stay on the primary."""
    gone = f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'copy.sqlite3'}"
    urls = {"default": make_database("primary"), "copy": gone}
    with override_settings(DATABASES=checked(urls)), clock.freeze_time():
        await read_alias()
        await probed()
        assert await read_alias() == "default"
        await connections.close_all()
    (record,) = caplog.records
    assert record.getMessage().endswith("its lag could not be read")


async def test_a_replica_that_answers_and_replays_nothing_is_not_behind(
    make_database: MakeDatabase,
) -> None:
    """Up to date as of the probe's start.

    On SQLite it only has to answer; on PostgreSQL a server not in recovery
    is its own primary (as when a test points the replica at it).
    """
    urls = {"default": make_database("primary"), "copy": make_database("copy")}
    with override_settings(DATABASES=checked(urls)), clock.freeze_time():
        started = clock.monotonic()
        assert await replicas.measure_lag("copy", "default", 5.0) == (0.0, started)
        await read_alias()
        await probed()
        assert await read_alias() == "copy"
        await connections.close_all()


def standby(age: str) -> Any:  # noqa: ANN401 - a statement.
    """What a standby answers, ``age`` seconds after it replayed its last commit."""
    return text(f"SELECT true, {age}::float8")


@postgres_only
async def test_a_standby_within_max_lag_is_as_far_behind_as_its_replay(
    one_replica: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary is not asked: its WAL would fail here."""
    monkeypatch.setattr(replicas, "REPLAY_AGE", standby("0.25"))
    monkeypatch.setattr(replicas, "WAL_END", text("SELECT 1 / 0"))
    with clock.freeze_time():
        started = clock.monotonic()
        assert await replicas.measure_lag("copy", "default", 5.0) == (0.25, started)


@postgres_only
async def test_an_idle_primary_does_not_make_its_standby_look_behind(
    one_replica: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its last commit long ago, the standby has replayed all of the primary's WAL.

    Both databases are on one server here, so the replica's WAL, which a
    server not in recovery compares, ends where the primary's does or later.
    """
    monkeypatch.setattr(replicas, "REPLAY_AGE", standby("30"))
    with clock.freeze_time():
        asked = clock.monotonic()
        assert await replicas.measure_lag("copy", "default", 5.0) == (0.0, asked)
    monkeypatch.setattr(replicas, "REPLAY_AGE", standby("NULL"))
    assert (await replicas.measure_lag("copy", "default", 5.0))[0] == 0.0


@postgres_only
async def test_a_standby_short_of_the_primarys_wal_is_as_far_behind_as_its_replay(
    one_replica: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind the primary's WAL, the replay's age is the lag; with none, unknown."""
    monkeypatch.setattr(replicas, "REPLAY_AGE", standby("30"))
    monkeypatch.setattr(
        replicas, "REPLAYED", text("SELECT CAST(:position AS text) IS NULL")
    )
    assert (await replicas.measure_lag("copy", "default", 5.0))[0] == 30.0
    monkeypatch.setattr(replicas, "REPLAY_AGE", standby("NULL"))
    assert (await replicas.measure_lag("copy", "default", 5.0))[0] == math.inf


async def test_writes_without_replicas_note_nothing(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a database with replicas has reads that must wait for a commit."""
    notes: list[str] = []
    monkeypatch.setattr(replicas, "note_commit", notes.append)
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with GadgetsUnitOfWork() as uow:
            await uow.gadgets.add(make_gadget())
            await uow.commit()
        await connections.close_all()
    assert notes == []
