"""Workers and the publisher under outages, overload, and shutdown.

The concurrency budget, the database's clock, publication leases, the
publisher's breaker and supervision, chunked rounds, brokered shutdown,
batched cleanup, LISTEN/NOTIFY wake-ups (PostgreSQL), and the outbox gauges.
Tests wait on events, never on the wall clock; each wait is bounded so a
regression fails instead of hanging.
"""

import asyncio
import gc
import io
import logging
import uuid
import weakref
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit import clock, setup, tasks
from domainkit.apps import registry
from domainkit.broker import (
    Broker,
    BrokerHandler,
    BrokerRefused,
    BrokerUnavailable,
    Delivery,
    Handler,
    MemoryBroker,
    Subscription,
    brokers,
)
from domainkit.conf import UNSET, override_settings
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.contrib.idempotency.operations import (
    Operation,
    run_operation,
    running_operation,
)
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    dbclock,
    dispatch,
    enqueue,
    task,
)
from domainkit.contrib.messaging import metrics as metrics_module
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.breaker import Breaker, Watched
from domainkit.contrib.messaging.budget import Budget
from domainkit.contrib.messaging.dispatch import (
    BrokerWorkers,
    CommitPublisher,
    commit_publisher,
)
from domainkit.contrib.messaging.metrics import refresh_outbox
from domainkit.contrib.messaging.outbox import notifies
from domainkit.contrib.messaging.tables import DONE, PENDING, messages
from domainkit.contrib.messaging.worker import LEASE_MARGIN, Claimed
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    ALPHA,
    BROKERED,
    FAST,
    MESSAGING,
    SLOW,
    Recording,
    Stalled,
    broker_tables,
    brokered,
    immediate_retries,
    messaging,
    outbox,
    published,
    remember,
    tables,
)

pytestmark = pytest.mark.anyio

WAIT: Final = 30.0
"""The longest any test waits for something; a regression fails, never hangs."""


async def within(awaitable: Any) -> Any:  # noqa: ANN401 - any awaitable.
    """Await ``awaitable``, failing after ``WAIT`` seconds."""
    async with asyncio.timeout(WAIT):
        return await awaitable


def postgres() -> bool:
    """Whether the test database is PostgreSQL (else SQLite)."""
    return connections.engine().dialect.name == "postgresql"


class Gate:
    """Tasks wait at it until opened; the test sees each start and finish."""

    def __init__(self) -> None:
        """Start closed, with nothing running."""
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running = 0
        self.peak = 0
        self.all_open = False
        self._open: dict[str, asyncio.Event] = {}
        self._changed = asyncio.Event()

    async def enter(self, name: str) -> None:
        """Start ``name``, wait until it may go on, then finish it."""
        self.started.append(name)
        self.running += 1
        self.peak = max(self.peak, self.running)
        self._changed.set()
        try:
            if not self.all_open:
                await self._event(name).wait()
        finally:
            self.running -= 1
            self._changed.set()
        self.finished.append(name)

    def open(self, name: str | None = None) -> None:
        """Let ``name`` (default: every task, now and later) go on."""
        if name is None:
            self.all_open = True
            for gate in self._open.values():
                gate.set()
        else:
            self._event(name).set()

    async def until(self, condition: Callable[[], bool]) -> None:
        """Wait until ``condition`` holds."""
        async with asyncio.timeout(WAIT):
            while not condition():
                self._changed.clear()
                await self._changed.wait()

    def _event(self, name: str) -> asyncio.Event:
        return self._open.setdefault(name, asyncio.Event())


gate = Gate()


@pytest.fixture(autouse=True)
def fresh_gate() -> None:
    """A closed gate for each test."""
    global gate
    gate = Gate()


@task(name="tests.runtime.at_gate")
async def at_gate(name: str) -> None:
    """Wait at the gate (the default queue)."""
    await gate.enter(name)


@task(name="tests.runtime.at_gate_emails", queue="emails")
async def at_gate_emails(name: str) -> None:
    """Wait at the gate (the emails queue)."""
    await gate.enter(name)


class WatchedBudget(Budget):
    """A budget that tells the test when callers start waiting for a slot."""

    def __init__(self, size: int) -> None:
        """Count waits from zero."""
        super().__init__(size)
        self.waits = 0
        self._changed = asyncio.Event()

    async def acquire(self, most: int = 1, *, stop: asyncio.Event | None = None) -> int:
        """Count a caller that will wait, then acquire."""
        stopped = stop is not None and stop.is_set()
        if not stopped and (self._waiters or not self.free):
            self.waits += 1
            self._changed.set()
        return await super().acquire(most, stop=stop)

    async def until_waiting(self, count: int) -> None:
        """Wait until ``count`` callers waited for a slot."""
        async with asyncio.timeout(WAIT):
            while self.waits < count:
                self._changed.clear()
                await self._changed.wait()


def logged(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    """The messages logger ``name`` logged."""
    return [record.getMessage() for record in caplog.records if record.name == name]


def statements(engine: AsyncEngine) -> list[tuple[str, Any]]:
    """Record each statement (and its parameters) the engine sends from now."""
    seen: list[tuple[str, Any]] = []

    def record(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy's event arguments.
        seen.append((args[2], args[3]))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return seen


def work_broker() -> MemoryBroker:
    """The running loop's in-memory broker of ``BROKERS["work"]``."""
    broker = brokers["work"]
    assert isinstance(broker, MemoryBroker)
    return broker


async def rows() -> list[dict[str, Any]]:
    """Every outbox row, oldest first."""
    return await outbox()


# The budget -----------------------------------------------------------------


async def test_a_budget_hands_out_slots_first_come_first_served() -> None:
    """Callers take what is free; waiters are served in order, one slot each."""
    budget = WatchedBudget(3)
    assert await budget.acquire(2) == 2
    assert (budget.used, budget.free, repr(budget)) == (2, 1, "<Budget 2/3>")
    assert await budget.acquire(5) == 1  # Only what is free.

    first = asyncio.create_task(budget.acquire(4))
    second = asyncio.create_task(budget.acquire())
    await budget.until_waiting(2)
    budget.release(2)

    assert await within(first) == 1  # Not more while the second waited.
    assert await within(second) == 1
    assert budget.used == 3
    budget.release(3)
    async with budget.slot():
        assert budget.used == 1
    assert budget.used == 0


async def test_a_woken_waiter_takes_what_is_free() -> None:
    """Once its turn comes, a waiter takes up to what it asked for."""
    budget = WatchedBudget(5)
    await budget.acquire(5)
    waiting = asyncio.create_task(budget.acquire(2))
    await budget.until_waiting(1)
    budget.release(5)

    assert await within(waiting) == 2
    assert budget.used == 2


async def test_a_budget_waiter_stops_or_is_cancelled_cleanly() -> None:
    """A stop or a cancellation leaves nothing held; a handed slot moves on."""
    budget = WatchedBudget(1)
    await budget.acquire()
    stop = asyncio.Event()
    stopping = asyncio.create_task(budget.acquire(stop=stop))
    await budget.until_waiting(1)
    stop.set()
    assert await within(stopping) == 0
    assert await budget.acquire(stop=stop) == 0  # Stopped already.

    cancelled = asyncio.create_task(budget.acquire())
    await budget.until_waiting(2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(cancelled)
    assert (budget.used, list(budget._waiters)) == (1, [])

    handed = asyncio.create_task(budget.acquire())
    following = asyncio.create_task(budget.acquire())
    await budget.until_waiting(4)
    budget.release()  # The slot goes to ``handed``, which is cancelled first.
    handed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(handed)
    assert await within(following) == 1
    assert budget.used == 1


async def test_a_budget_refuses_impossible_counts() -> None:
    """Sizes and counts are checked; a waiter that gave up is skipped."""
    with pytest.raises(ValueError, match=r"^A budget needs at least one slot\.$"):
        Budget(0)
    budget = Budget(1)
    with pytest.raises(ValueError, match="Acquire at least one slot"):
        await budget.acquire(0)
    with pytest.raises(ValueError, match="Cannot give back 1 of 0 slots"):
        budget.release()
    await budget.acquire()
    gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    gone.cancel()
    budget._waiters.append(gone)
    budget.release()

    assert (budget.used, list(budget._waiters)) == (0, [])


@pytest.mark.usefixtures("tables")
async def test_workers_of_one_process_share_one_budget() -> None:
    """Two workers, one budget of two: never more than two messages at once."""
    for name in ("d1", "d2"):
        await enqueue(at_gate, name=name)
    for name in ("e1", "e2"):
        await enqueue(at_gate_emails, name=name)
    budget = WatchedBudget(2)
    config = MessagingSettings(**{**FAST, **SLOW, "POLL_INTERVAL": 30})
    workers = [
        Worker(config, queues=["default"], budget=budget),
        Worker(config, queues=["emails"], budget=budget),
    ]
    stop = asyncio.Event()
    running = [asyncio.create_task(worker.run(stop)) for worker in workers]

    await gate.until(lambda: len(gate.started) == 2)
    await budget.until_waiting(2)  # Both workers wait for a slot.
    assert gate.running == 2
    gate.open(gate.started[0])
    await gate.until(lambda: len(gate.started) == 3)
    gate.open()
    await gate.until(lambda: len(gate.finished) == 4)
    stop.set()
    await within(asyncio.gather(*running))

    assert gate.peak == 2
    assert {row["status"] for row in await rows()} == {DONE}


@pytest.mark.usefixtures("tables")
async def test_run_once_stays_within_the_shared_budget() -> None:
    """``runworker --once`` across outboxes runs no more than the budget."""
    await enqueue(at_gate, name="d1")
    await enqueue(at_gate_emails, name="e1")
    budget = WatchedBudget(1)
    config = MessagingSettings(**{**FAST, **SLOW})
    once = asyncio.gather(
        Worker(config, queues=["default"], budget=budget).run_once(),
        Worker(config, queues=["emails"], budget=budget).run_once(),
    )

    await gate.until(lambda: len(gate.started) == 1)
    await budget.until_waiting(1)
    gate.open()

    assert await within(once) == [1, 1]
    assert gate.peak == 1
    assert budget.used == 0


@pytest.mark.usefixtures("tables")
async def test_a_failed_claim_gives_its_slots_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slots taken for a claim that fails (or is cancelled) are given back."""
    worker = Worker(MessagingSettings(**FAST))

    async def broken(limit: int) -> list[Claimed]:
        raise ConnectionError("database down")

    monkeypatch.setattr(worker, "claim", broken)
    with pytest.raises(ConnectionError):
        await worker.run_once()

    assert worker.budget.used == 0


@pytest.mark.usefixtures("messaging")
def test_runworker_shares_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every worker of the command, and its broker consumers, draw from one budget."""
    budgets: list[Budget] = []

    async def record(self: Worker) -> int:
        budgets.append(self.budget)
        return 0

    monkeypatch.setattr(Worker, "run_once", record)
    monkeypatch.setattr(router, "hosting_targets", lambda: ("a", "b"))
    monkeypatch.setattr(router, "aliases_of", lambda target: (target,))
    call_command("runworker", "--once", stdout=io.StringIO())

    assert len(budgets) == 2
    assert budgets[0] is budgets[1]
    assert budgets[0].size == registry.settings(MessagingSettings).CONCURRENCY


# The database's clock --------------------------------------------------------------


def test_the_database_decides_the_time_on_postgresql() -> None:
    """PostgreSQL's clock decides unless a test froze time; SQLite's is the host's."""
    assert dbclock.uses_database("postgresql")
    assert not dbclock.uses_database("sqlite")
    assert str(dbclock.now("postgresql")) == "statement_timestamp()"
    assert str(dbclock.at("postgresql", 60)).startswith("statement_timestamp() + ")
    with clock.freeze_time("2040-01-01T00:00:00Z"):
        assert not dbclock.uses_database("postgresql")
        assert dbclock.at("postgresql", 60) == datetime(2040, 1, 1, 0, 1, tzinfo=UTC)
        assert dbclock.now("sqlite") == datetime(2040, 1, 1, tzinfo=UTC)


@pytest.mark.usefixtures("tables")
async def test_hosts_with_skewed_clocks_neither_steal_nor_delay_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On PostgreSQL the outbox's clock decides; SQLite's clock is the host's anyway."""
    on_postgres = postgres()
    real = clock.now
    skew = timedelta(0)
    monkeypatch.setattr(clock, "now", lambda: real() + skew)
    config = MessagingSettings(**{**FAST, **SLOW})

    await enqueue(remember, value=uuid.uuid4())
    await Worker(config).claim(10)
    skew = timedelta(hours=1)  # A host an hour ahead does not take it over.
    assert len(await Worker(config).claim(10)) == (0 if on_postgres else 1)

    await all_done()
    skew = timedelta(0)
    await enqueue(remember, value=uuid.uuid4())
    skew = timedelta(hours=-1)  # A host an hour behind still sees due work.
    assert len(await Worker(config).claim(10)) == (1 if on_postgres else 0)

    await all_done()
    skew = timedelta(hours=1)  # A writer an hour ahead writes work due now.
    await enqueue(remember, value=uuid.uuid4())
    skew = timedelta(0)
    assert len(await Worker(config).claim(10)) == (1 if on_postgres else 0)


async def all_done() -> None:
    """Mark every message done, so the next scenario starts clean."""
    async with connections.engine().begin() as connection:
        await connection.execute(update(messages).values(status=DONE))


@pytest.mark.usefixtures("tables")
async def test_a_frozen_clock_moves_the_outbox_on_every_database() -> None:
    """Under freeze_time the frozen clock decides, on PostgreSQL too."""
    with clock.freeze_time("2040-01-01T00:00:00Z") as frozen:
        await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
        assert await Worker().run_once() == 0
        frozen.tick(timedelta(hours=2))
        assert await Worker().run_once() == 1

    (row,) = await rows()
    assert row["finished_at"] == datetime(2040, 1, 1, 2, tzinfo=UTC)
    assert row["started_at"] == datetime(2040, 1, 1, 2, tzinfo=UTC)


# Publication leases ---------------------------------------------------------------


class Holding(MemoryBroker):
    """A broker that holds each publication until released, and says when one came."""

    def __init__(self, publish_timeout: float = 30.0) -> None:
        """Hold publications; let each run up to ``publish_timeout``."""
        super().__init__()
        self.publish_timeout = publish_timeout
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Say a publication arrived, then wait to be released."""
        self.arrived.set()
        await self.release.wait()
        await super().publish(queue, body, message_id=message_id)


def seconds_left(row: dict[str, Any]) -> float:
    """Seconds until ``row``'s lock expires."""
    return float((row["locked_until"] - datetime.now(UTC)).total_seconds())


@pytest.mark.usefixtures("tables")
async def test_a_cut_short_publication_gives_its_messages_back_at_once() -> None:
    """Cancelled mid-publish, the relay unlocks: the next round sends them (N1).

    Before, the messages stayed locked for REPUBLISH_AFTER (an hour), and a
    relay round right after relayed nothing.
    """
    await enqueue(remember, value=uuid.uuid4())
    broker = Holding()
    relaying = asyncio.create_task(Worker().relay(broker, 10))
    await within(broker.arrived.wait())

    (row,) = await rows()
    assert 0 < seconds_left(row) <= broker.publish_timeout + LEASE_MARGIN
    relaying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(relaying)

    (row,) = await rows()
    assert (row["lock_id"], row["locked_until"]) == (None, None)
    assert await Worker().relay(MemoryBroker(), 10) == 1


@pytest.mark.usefixtures("tables")
async def test_confirmed_publications_are_locked_for_republish_after() -> None:
    """The lease becomes REPUBLISH_AFTER once the broker confirms."""
    await enqueue(remember, value=uuid.uuid4())
    broker = Holding()
    relaying = asyncio.create_task(Worker().relay(broker, 10))
    await within(broker.arrived.wait())
    (leased,) = await rows()
    broker.release.set()

    assert await within(relaying) == 1
    (row,) = await rows()
    assert row["lock_id"] == leased["lock_id"]
    assert seconds_left(row) > 3000  # REPUBLISH_AFTER is an hour.


@pytest.mark.usefixtures("tables")
async def test_a_round_past_its_deadline_gives_back_what_was_not_confirmed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broker slower than its publish_timeout loses the round, not the messages."""
    await enqueue(remember, value=uuid.uuid4())

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        assert await within(Worker().relay(Holding(publish_timeout=0.05), 10)) == 0

    (row,) = await rows()
    assert (row["lock_id"], row["locked_until"]) == (None, None)
    assert caplog.messages == [
        (
            "Could not publish 1 of 1 message(s) to the broker (no confirmation "
            "within 0.05s); they go again on the next round"
        )
    ]


@pytest.mark.usefixtures("tables")
async def test_a_second_cancellation_cannot_stop_the_give_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The give-back is shielded: cancelling the relay again does not cut it."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker()
    settling, go_on = asyncio.Event(), asyncio.Event()
    settle = worker._settle

    async def slow_settle(*args: Any) -> None:  # noqa: ANN401
        settling.set()
        await go_on.wait()
        await settle(*args)

    monkeypatch.setattr(worker, "_settle", slow_settle)
    broker = Holding()
    relaying = asyncio.create_task(worker.relay(broker, 10))
    await within(broker.arrived.wait())
    relaying.cancel()
    await within(settling.wait())
    relaying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(relaying)
    (still,) = await rows()
    go_on.set()
    await within(asyncio.gather(*worker_module._settling))

    assert still["lock_id"] is not None
    (row,) = await rows()
    assert (row["lock_id"], row["locked_until"]) == (None, None)


@pytest.mark.usefixtures("tables")
@pytest.mark.parametrize("failure", ["error", "hang"])
async def test_a_give_back_that_fails_leaves_the_lease_to_expire(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed or hung give-back is logged within RELEASE_TIMEOUT; the round ends."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker()
    monkeypatch.setattr(worker_module, "RELEASE_TIMEOUT", 0.05)

    async def broken(*args: Any) -> None:  # noqa: ANN401
        if failure == "error":
            raise ConnectionError("database down")
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_settle", broken)
    with caplog.at_level(logging.ERROR, "domainkit.contrib.messaging.worker"):
        assert await within(worker.relay(MemoryBroker(), 10)) == 1

    assert caplog.messages == [
        "Could not settle 1 published message(s); their locks expire"
    ]
    cause = caplog.records[0].exc_info
    assert cause is not None
    assert cause[0] is (ConnectionError if failure == "error" else TimeoutError)
    (row,) = await rows()
    assert 0 < seconds_left(row) <= MemoryBroker.publish_timeout + LEASE_MARGIN


def test_shutdown_must_outlast_a_publication(database_url: str) -> None:
    """MESSAGING.CLOSE_TIMEOUT must cover the broker's PUBLISH_TIMEOUT."""
    registry.clear()
    refused = (
        r"MESSAGING\.CLOSE_TIMEOUT \(10s\) must be at least "
        r"BROKERS\['work'\]\.PUBLISH_TIMEOUT \(20s\)"
    )
    with (
        override_settings(
            INSTALLED_DOMAINS=[MESSAGING],
            DATABASES={"default": {"URL": database_url}},
            BROKERS={"work": {"URL": "memory://", "PUBLISH_TIMEOUT": 20}},
            MESSAGING={"BROKER": "work"},
            ALPHA=UNSET,
        ),
        pytest.raises(ImproperlyConfigured, match=refused),
    ):
        setup()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING],
        DATABASES={"default": {"URL": database_url}},
        BROKERS={"work": {"URL": "memory://", "PUBLISH_TIMEOUT": 20}},
        MESSAGING={"BROKER": "work", "CLOSE_TIMEOUT": 20},
        ALPHA=UNSET,
    ):
        setup()
    registry.clear()


# Chunked rounds -------------------------------------------------------------------


class Counting(MemoryBroker):
    """A broker that counts the publications in flight at once."""

    def __init__(self) -> None:
        """Count from zero."""
        super().__init__()
        self.flying = 0
        self.peak = 0

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Publish, letting the others in flight run meanwhile."""
        self.flying += 1
        self.peak = max(self.peak, self.flying)
        for _ in range(3):
            await asyncio.sleep(0)  # A yield, as a network round trip makes.
        await super().publish(queue, body, message_id=message_id)
        self.flying -= 1


@pytest.mark.usefixtures("broker_tables")
async def test_rounds_lock_and_publish_a_batch_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed ids go BATCH_SIZE per lock, at most a batch in flight (N11)."""
    rounds: list[int] = []
    relay = Worker._relay

    async def counting(self: Worker, broker: Broker, due: object) -> int:
        sent = await relay(self, broker, due)
        rounds.append(sent)
        return sent

    monkeypatch.setattr(Worker, "_relay", counting)
    with override_settings(
        MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False, "BATCH_SIZE": 2}
    ):
        for _ in range(5):
            await enqueue(remember, value=uuid.uuid4())
        config = registry.settings(MessagingSettings)
        broker = Counting()
        ids = [row["id"] for row in await rows()]

        assert await Worker(config).relay_ids(broker, ids) == 5
        assert (rounds, broker.peak) == ([2, 2, 1], 2)
        assert await Worker(config).relay_ids(broker, []) == 0

        rounds.clear()
        async with connections.engine().begin() as connection:  # Not published.
            await connection.execute(
                update(messages).values(lock_id=None, locked_until=None)
            )
        publisher = CommitPublisher(config)
        monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: broker)
        publisher.submit("default", ids)
        await published(publisher)
        await publisher.close()

    assert rounds == [2, 2, 1]


# The publisher's breaker ------------------------------------------------------


def breaker_open(broker: str) -> float | None:
    """The breaker gauge of ``broker``."""
    return REGISTRY.get_sample_value(
        "messaging_publisher_breaker_open", {"broker": broker}
    )


def left(reason: str) -> float:
    """Committed messages left to the relay for ``reason`` so far."""
    sample = REGISTRY.get_sample_value(
        "messaging_left_to_relay_total", {"reason": reason}
    )
    return sample or 0.0


def test_the_breaker_opens_after_failures_in_a_row_and_closes_on_a_confirmation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failures in a row open it; after a cooldown a probe decides; it logs twice."""
    down = BrokerUnavailable("down")
    with (
        clock.freeze_time() as frozen,
        caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.breaker"),
    ):
        breaker = Breaker("unit", failures=2, cooldown=30)
        assert (breaker.state, breaker.allows()) == ("closed", True)
        assert repr(breaker) == "<Breaker 'unit' closed>"
        assert breaker_open("unit") == 0
        breaker.failed(down)
        breaker.reached()  # A refusal is an answer: the count starts over.
        breaker.failed(down)
        assert breaker.state == "closed"
        breaker.failed(down)
        assert (breaker.state, breaker.allows(), breaker_open("unit")) == (
            "open",
            False,
            1,
        )
        frozen.tick(30)
        assert (breaker.state, breaker.allows()) == ("probing", True)
        breaker.failed(down)  # The probe failed: another cooldown, no new log.
        assert breaker.state == "open"
        frozen.tick(30)
        breaker.succeeded()
        breaker.succeeded()  # Closed already: nothing more is logged.

    assert (breaker.state, breaker.failures, breaker_open("unit")) == ("closed", 0, 0)
    assert caplog.messages == [
        (
            "Broker 'unit' failed 2 time(s) in a row (down); commits leave their "
            "messages to the relay, trying again every 30s"
        ),
        "Broker 'unit' confirms messages again; commits publish them again",
    ]


class Down(MemoryBroker):
    """A broker that is down: checking, declaring and publishing fail."""

    async def check(self) -> None:
        """Fail."""
        raise BrokerUnavailable("down")

    async def declare(self, queues: Iterable[str]) -> None:
        """Fail."""
        raise BrokerUnavailable("down")

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Fail."""
        raise BrokerUnavailable("down")


async def test_the_breaker_hears_every_answer_of_the_broker() -> None:
    """Failures count; refusals and declarations do not; confirmations close it."""
    breaker = Breaker("answers", failures=10, cooldown=30)
    broker = Recording(refuse=frozenset({"emails"}))
    watched = Watched(broker, breaker)
    unreachable = Down()  # Watched brokers are held weakly: keep it.
    down = Watched(unreachable, breaker)

    breaker.failures = 2
    await watched.declare(["default", "emails"])
    await watched.check()
    assert breaker.failures == 2  # Answers, but not confirmations.
    breaker.failures = 0
    for call in (
        down.check(),
        down.declare(["x"]),
        down.publish("x", b"", message_id="m"),
    ):
        with pytest.raises(BrokerUnavailable):
            await call
    assert breaker.failures == 3
    with pytest.raises(BrokerRefused):
        await watched.publish("emails", b"{}", message_id="m")
    assert breaker.failures == 0
    breaker.failures = 2
    await watched.publish("default", b"{}", message_id="m")
    assert breaker.failures == 0
    assert broker.drain("default") == [(b"{}", "m")]
    await watched.publish("default", b"[]", message_id="n")
    delivered: asyncio.Queue[Delivery] = asyncio.Queue()
    subscription = await watched.consume(["default"], 1, delivered.put)
    delivery = await within(delivered.get())
    await delivery.ack()
    await subscription.cancel()
    assert (delivery.body, delivery.message_id) == (b"[]", "n")

    assert watched.publish_timeout == broker.publish_timeout
    gone = Watched(MemoryBroker(), breaker)  # Dropped, as with its loop.
    gc.collect()
    with pytest.raises(BrokerUnavailable, match=r"^The broker was closed\.$"):
        await gone.publish("default", b"{}", message_id="m")


@pytest.mark.usefixtures("broker_tables")
async def test_a_failing_broker_trips_the_publishers_breaker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past BREAKER_FAILURES, commits skip the round; a probe checks before locking."""
    memory = work_broker()
    current: list[Broker] = [Down()]
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: current[0])
    rounds: list[object] = []
    relay = Worker._relay

    async def counting(self: Worker, broker: Broker, due: object) -> int:
        rounds.append(due)
        return await relay(self, broker, due)

    monkeypatch.setattr(Worker, "_relay", counting)
    with (
        override_settings(MESSAGING={**BROKERED, "BREAKER_FAILURES": 2}),
        caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.breaker"),
    ):
        config = registry.settings(MessagingSettings)
        publisher = commit_publisher(config)
        cooldown = config.BREAKER_COOLDOWN
        for _ in range(2):
            await enqueue(remember, value=uuid.uuid4())
            await published(publisher)
        assert (publisher.breaker.state, len(rounds)) == ("open", 2)

        before = left("breaker")
        await enqueue(remember, value=uuid.uuid4())  # Left to the relay at once.
        await published(publisher)
        assert (len(rounds), left("breaker"), publisher.backlog) == (2, before + 1, 0)

        assert publisher.breaker.opened_at is not None
        publisher.breaker.opened_at -= cooldown  # The probe's check fails first.
        await enqueue(remember, value=uuid.uuid4())
        await published(publisher)
        assert (publisher.breaker.state, len(rounds)) == ("open", 2)

        publisher.breaker.opened_at -= cooldown
        current[0] = memory
        await enqueue(remember, value=uuid.uuid4())
        await published(publisher)

    assert (publisher.breaker.state, len(rounds)) == ("closed", 3)
    assert memory.pending("default") == 1
    assert [row["lock_id"] is None for row in await rows()] == [True] * 4 + [False]
    opened, closed = logged(caplog, "domainkit.contrib.messaging.breaker")
    assert opened.startswith("Broker 'work' failed 2 time(s) in a row (down)")
    assert closed == "Broker 'work' confirms messages again; commits publish them again"


@pytest.mark.usefixtures("broker_tables")
async def test_a_full_backlog_is_logged_once_until_it_clears(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """While the backlog stays full, commits leave theirs quietly, logged once."""
    first, second = Stalled(), Stalled()
    current = [first]
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: current[0])
    monkeypatch.setattr(dispatch, "MAX_UNPUBLISHED", 1)
    before = left("backlog")
    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.dispatch"):
        for _ in range(3):  # One waits for the stalled broker; two are left.
            await enqueue(remember, value=uuid.uuid4())
        first.release.set()
        await published()
        current[0] = second  # Cleared: the next time it fills, it logs again.
        for _ in range(3):
            await enqueue(remember, value=uuid.uuid4())
        second.release.set()
        await published()

    assert left("backlog") == before + 4
    assert len(logged(caplog, "domainkit.contrib.messaging.dispatch")) == 2
    assert (first.pending("default"), second.pending("default")) == (1, 1)


# The publisher's task ----------------------------------------------------------


@pytest.mark.usefixtures("broker_tables")
async def test_the_publisher_never_runs_in_a_requests_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first committing request's id, actor, and idempotency key stay with it."""
    memory = work_broker()
    seen: list[tuple[object, object]] = []
    relay_ids = Worker.relay_ids

    async def spy(self: Worker, broker: Broker, ids: Any) -> int:  # noqa: ANN401
        seen.append((current_request(), running_operation()))
        return await relay_ids(self, broker, ids)

    monkeypatch.setattr(Worker, "relay_ids", spy)
    request = RequestContext("req-first", None, "POST", "/a", actor="alice")
    with bind_request(request), run_operation(Operation("keyhash", "token")):
        await enqueue(remember, value=uuid.uuid4())
    await published()
    with bind_request(RequestContext("req-second", None, "POST", "/b")):
        await enqueue(remember, value=uuid.uuid4())
    await published()

    assert seen == [(None, None), (None, None)]
    assert memory.pending("default") == 2


@pytest.mark.usefixtures("broker_tables")
async def test_an_idle_publisher_holds_no_task() -> None:
    """Its task ends once everything is offered, and starts again on a commit."""
    memory = work_broker()
    publisher = commit_publisher(registry.settings(MessagingSettings))
    await enqueue(remember, value=uuid.uuid4())
    await published(publisher)
    assert publisher._task is None
    await enqueue(remember, value=uuid.uuid4())
    await published(publisher)

    assert publisher._task is None
    assert memory.pending("default") == 2


@pytest.mark.usefixtures("broker_tables")
async def test_a_crashed_publisher_starts_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash is logged and the task restarts after a pause; the relay has the rest."""
    memory = work_broker()
    monkeypatch.setattr(dispatch, "RESTART_BACKOFF", 0.01)
    crashed = False
    batched = dispatch.batched  # type: ignore[attr-defined]

    def crash_once(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("a bug")
        return batched(*args, **kwargs)

    monkeypatch.setattr(dispatch, "batched", crash_once)
    with caplog.at_level(logging.ERROR, "domainkit.tasks"):
        await enqueue(remember, value=uuid.uuid4())
        await published()
        await enqueue(remember, value=uuid.uuid4())
        await published()

    first, second = await rows()
    assert first["lock_id"] is None  # Its round crashed: the relay sends it.
    assert second["lock_id"] is not None
    assert memory.pending("default") == 1
    (crash,) = logged(caplog, "domainkit.tasks")
    assert crash.startswith("Task 'messaging.publisher.work' crashed; restarting it")


@pytest.mark.usefixtures("broker_tables")
async def test_a_publisher_cancelled_at_shutdown_leaves_the_rest_to_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown's task phase cancels a publisher close missed; its messages go back."""
    holding = Holding()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: holding)
    publisher = commit_publisher(registry.settings(MessagingSettings))
    await enqueue(remember, value=uuid.uuid4())
    await within(holding.arrived.wait())
    publisher.submit("default", [uuid.uuid4()])  # Waits behind the first.
    before = left("shutdown")

    loop = asyncio.get_running_loop()
    assert await within(tasks.shutdown(loop.time() + WAIT)) == []
    await published(publisher)

    assert (publisher.backlog, publisher.unpublished, publisher._task) == (0, {}, None)
    assert left("shutdown") == before + 1
    (row,) = await rows()
    assert (row["lock_id"], row["locked_until"]) == (None, None)


@pytest.mark.usefixtures("brokered")
def test_closed_loops_keep_no_publishers() -> None:
    """Publishers of loops closed without close_publishers are let go (N12)."""

    async def commit_once() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create, checkfirst=True)
        await enqueue(remember, value=uuid.uuid4())
        await published()
        await brokers.close_all()
        await connections.close_all()

    closed = []
    for _ in range(3):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(commit_once())
        loop.close()
        closed.append(weakref.ref(loop))
        del loop
    gc.collect()
    assert [ref() for ref in closed] == [None, None, None]

    kept = asyncio.new_event_loop()  # Still referenced: dropped on the next use.
    kept.run_until_complete(commit_once())
    kept.close()
    assert kept in dispatch._publishers
    asyncio.run(commit_once())
    assert kept not in dispatch._publishers


# Brokered shutdown --------------------------------------------------------------


class Unstoppable(MemoryBroker):
    """A broker whose consumers will not stop: the call fails, or hangs."""

    def __init__(self, failure: BrokerUnavailable | None) -> None:
        """Fail with ``failure``, or hang without one."""
        super().__init__()
        self.failure = failure

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Consume; stopping fails or hangs once the pump has stopped."""
        subscription = await super().consume(queues, prefetch, handler)
        failure = self.failure

        class Stuck(Subscription):
            async def cancel(self) -> None:
                await subscription.cancel()
                if failure is not None:
                    raise failure
                await asyncio.Event().wait()

        return Stuck()


@pytest.mark.parametrize("failure", [BrokerUnavailable("connection lost"), None])
@pytest.mark.usefixtures("broker_tables")
async def test_a_lost_or_stalled_broker_neither_skips_nor_holds_the_drain(
    failure: BrokerUnavailable | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Running deliveries still finish; stopping the consumers has a deadline (N3)."""
    broker = Unstoppable(failure)
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: broker)
    monkeypatch.setattr(dispatch, "STOP_CONSUMING_TIMEOUT", 0.05)
    await enqueue(at_gate, name="running")
    config = MessagingSettings(**{**BROKERED, **SLOW})
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config).run(stop)
    )
    await gate.until(lambda: gate.started == ["running"])

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.dispatch"):
        stop.set()
        gate.open()
        await within(running)

    assert gate.finished == ["running"]
    (row,) = await rows()
    assert row["status"] == DONE
    reason = "connection lost" if failure else "no answer in time"
    assert logged(caplog, "domainkit.contrib.messaging.dispatch") == [
        (
            f"Could not stop consuming from broker 'work' ({reason}); running "
            "messages still get their time"
        )
    ]


@pytest.mark.usefixtures("broker_tables")
async def test_deliveries_that_arrive_while_stopping_are_given_back() -> None:
    """A delivery waiting for a slot when the worker stops is requeued, not run."""
    memory = work_broker()
    await enqueue(at_gate, name="first")
    await enqueue(at_gate, name="second")
    config = MessagingSettings(**{**BROKERED, **SLOW, "CONCURRENCY": 2})
    budget = WatchedBudget(1)  # The broker hands out two; only one may run.
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config, budget=budget).run(stop)
    )
    await gate.until(lambda: len(gate.started) == 1)
    await budget.until_waiting(1)

    stop.set()
    gate.open()
    await within(running)

    assert len(gate.finished) == 1
    assert memory.pending("default") == 1  # Given back to the broker.
    statuses = sorted(row["status"] for row in await rows())
    assert statuses == [DONE, PENDING]


@pytest.mark.usefixtures("broker_tables")
async def test_a_delivery_after_the_run_is_given_back() -> None:
    """A delivery the broker hands over once the run ended starts nothing."""
    memory = work_broker()
    config = registry.settings(MessagingSettings)
    workers = BrokerWorkers([Worker(config)], ["default"], config, relay=False)
    handlers: list[Handler] = []
    consuming = asyncio.Event()
    consume = memory.consume

    async def keep(queues: Iterable[str], prefetch: int, handler: Handler) -> Any:  # noqa: ANN401
        handlers.append(handler)
        consuming.set()
        return await consume(queues, prefetch, handler)

    memory.consume = keep  # type: ignore[method-assign]
    running = asyncio.create_task(workers.run(asyncio.Event()))
    await within(consuming.wait())
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within(running)
    answers: list[str] = []

    async def requeue() -> None:
        answers.append("requeue")

    async def ack() -> None:
        answers.append("ack")

    await within(handlers[0](Delivery("default", b"{}", "late", ack, requeue)))

    assert answers == ["requeue"]
    assert workers.budget.used == 0


# Structured concurrency -------------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_a_failing_helper_is_never_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug in a worker's own task ends the worker with it, not silently (R15)."""
    worker = Worker(MessagingSettings(**FAST))

    async def broken(stop: asyncio.Event) -> None:
        raise RuntimeError("a bug in cleanup")

    monkeypatch.setattr(worker, "clean_until", broken)
    with pytest.raises(ExceptionGroup) as raised:
        await within(worker.run(asyncio.Event()))

    assert raised.group_contains(RuntimeError, match="a bug in cleanup")


@pytest.mark.usefixtures("broker_tables")
async def test_a_failing_relay_is_never_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug in a relay ends the brokered worker, its consumers stopped."""

    async def broken(self: BrokerWorkers, *args: object) -> None:
        raise RuntimeError("a bug in the relay")

    monkeypatch.setattr(BrokerWorkers, "_relay_until", broken)
    config = registry.settings(MessagingSettings)
    with pytest.raises(ExceptionGroup) as raised:
        await within(
            BrokerWorkers([Worker(config)], ["default"], config).run(asyncio.Event())
        )

    assert raised.group_contains(RuntimeError, match="a bug in the relay")


# Cleanup ------------------------------------------------------------------------------


async def finished_long_ago(count: int) -> None:
    """Queue and run ``count`` messages, finished a month ago."""
    for _ in range(count):
        await enqueue(remember, value=uuid.uuid4())
    await Worker().run_once()
    async with connections.engine().begin() as connection:
        month_ago = datetime.now(UTC) - timedelta(days=30)
        await connection.execute(update(messages).values(finished_at=month_ago))


@pytest.mark.usefixtures("tables")
async def test_cleanup_deletes_in_small_batches() -> None:
    """Each statement deletes at most CLEANUP_BATCH_SIZE rows (FX-15)."""
    await finished_long_ago(5)
    worker = Worker(MessagingSettings(**{**FAST, "CLEANUP_BATCH_SIZE": 2}))
    seen = statements(worker.engine)

    assert await worker.cleanup() == 5
    deletes = [text for text, _ in seen if text.lstrip().upper().startswith("DELETE")]
    assert len(deletes) == 3
    assert worker._cleanup_batch == 2
    assert await worker.cleanup() == 0


class FlakyEngine:
    """An engine whose transactions fail to start while failures are left."""

    def __init__(self, engine: AsyncEngine, failures: int) -> None:
        """Fail the next ``failures`` transactions."""
        self.engine = engine
        self.dialect = engine.dialect
        self.failures = failures

    def begin(self) -> Any:  # noqa: ANN401 - the engine's context manager.
        """Fail, as a statement timeout does, or begin."""
        if self.failures:
            self.failures -= 1
            raise OperationalError("DELETE", {}, Exception("statement timeout"))
        return self.engine.begin()


@pytest.mark.usefixtures("tables")
async def test_failed_cleanup_batches_shrink_then_grow_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing batch halves the next, down to one; full batches double back."""
    await finished_long_ago(5)
    worker = Worker(MessagingSettings(**{**FAST, "CLEANUP_BATCH_SIZE": 8}))
    flaky = FlakyEngine(worker.engine, failures=4)
    monkeypatch.setattr(Worker, "engine", property(lambda self: flaky))

    sizes = []
    for _ in range(4):
        with pytest.raises(OperationalError):
            await worker.cleanup()
        sizes.append(worker._cleanup_batch)
    assert sizes == [4, 2, 1, 1]

    assert await worker.cleanup() == 5  # Batches of 1, 2, then 4 (2 left).
    assert worker._cleanup_batch == 4


@pytest.mark.usefixtures("tables")
async def test_cleanup_runs_on_its_own_schedule_and_outlives_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures are logged and retried; a run that works says so, and stops on stop."""
    await finished_long_ago(1)
    monkeypatch.setattr(worker_module._random, "uniform", lambda low, high: low)
    config = {**FAST, "POLL_INTERVAL": 0.01, "CLEANUP_INTERVAL": 0.01}
    worker = Worker(MessagingSettings(**config))
    stop = asyncio.Event()
    clean = worker._clean
    calls = 0

    async def flaky(stopping: asyncio.Event | None) -> int:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("database down")
        deleted = await clean(stopping)
        if calls == 4:
            stop.set()
        return deleted

    monkeypatch.setattr(worker, "_clean", flaky)
    with caplog.at_level(logging.INFO, "domainkit.contrib.messaging.worker"):
        await within(worker.clean_until(stop))

    assert calls == 4
    assert caplog.messages == [
        (
            "Could not clean up the outbox of 'default' (1 failure in a row); "
            "cleaning up again in 0.01s"
        ),
        (
            "Could not clean up the outbox of 'default' (2 failures in a row); "
            "cleaning up again in 0.02s"
        ),
        "Cleaning up the outbox of 'default' works again after 2 failures",
        "Deleted 1 finished message(s) from the outbox of 'default'",
    ]


@pytest.mark.usefixtures("tables")
async def test_a_slow_cleanup_never_holds_up_claiming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup has a task of its own: claims go on while it hangs (ASYNC-07)."""
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 0.01}))
    monkeypatch.setattr(worker_module, "CLEANUP_START", 0.0)
    cleaning = asyncio.Event()

    async def hangs(stop: asyncio.Event | None) -> int:
        cleaning.set()
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(worker, "_clean", hangs)
    monkeypatch.setattr(worker_module, "HELPER_GRACE", 0.05)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await within(cleaning.wait())
    await enqueue(at_gate, name="claimed")
    await gate.until(lambda: gate.started == ["claimed"])
    gate.open()
    await gate.until(lambda: gate.finished == ["claimed"])
    stop.set()

    await within(running)


@pytest.mark.usefixtures("tables")
async def test_cleanup_when_due_follows_the_frozen_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_cleanup_when_due counts its interval on domainkit.clock."""
    worker = Worker(MessagingSettings(**FAST))
    calls: list[int] = []

    async def count() -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(worker, "cleanup", count)
    with clock.freeze_time() as frozen:
        await worker.run_cleanup_when_due()
        await worker.run_cleanup_when_due()
        frozen.tick(worker.config.CLEANUP_INTERVAL)
        await worker.run_cleanup_when_due()

    assert len(calls) == 2


# LISTEN/NOTIFY ----------------------------------------------------------------------


def test_commits_notify_only_without_a_publishing_broker() -> None:
    """NOTIFY applies unless turned off, or a broker publishes each commit."""
    assert notifies(MessagingSettings())
    assert not notifies(MessagingSettings(NOTIFY=False))
    assert not notifies(MessagingSettings(BROKER="work"))
    assert notifies(MessagingSettings(BROKER="work", PUBLISH_ON_COMMIT=False))


@pytest.mark.usefixtures("tables")
async def test_commits_notify_the_queues_of_messages_due_at_once() -> None:
    """One pg_notify per queue due now, sent with the commit; delayed work waits."""
    seen = statements(connections.engine())
    from domainkit_tests.test_messaging import send_email

    await enqueue(send_email, to="ada@example.com")
    await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
    with override_settings(MESSAGING={**FAST, "NOTIFY": False}):
        await enqueue(remember, value=uuid.uuid4())

    notified = [tuple(parameters) for text, parameters in seen if "pg_notify" in text]
    if postgres():
        assert notified == [("domainkit_messages", "emails")]
    else:
        assert notified == []


def test_workers_wake_only_for_their_queues() -> None:
    """A worker of some queues ignores commits to the others."""
    config = MessagingSettings()
    assert Worker(config, database="default").wakes_for("emails")
    assert Worker(config, database="default", queues=["emails"]).wakes_for("emails")
    assert not Worker(config, database="default", queues=["emails"]).wakes_for("x")


@pytest.mark.usefixtures("tables")
async def test_commits_wake_idle_workers() -> None:
    """On PostgreSQL a commit wakes the listener; elsewhere it returns at once."""
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 30}))
    stop, wake = asyncio.Event(), asyncio.Event()
    listening = asyncio.create_task(worker.listen(stop, wake))
    if not postgres():
        await within(listening)
        assert not wake.is_set()  # Polling is all there is.
        return

    await within(wake.wait())  # Listening: look once for what came before.
    wake.clear()
    await enqueue(remember, value=uuid.uuid4())
    await within(wake.wait())
    stop.set()
    await within(listening)


@pytest.mark.usefixtures("tables")
async def test_a_lost_listener_listens_again(caplog: pytest.LogCaptureFixture) -> None:
    """A listening connection that drops is opened again; polling covers the gap."""
    if not postgres():
        return  # Nothing listens on SQLite; see test_commits_wake_idle_workers.
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 0.01}))
    stop, wake = asyncio.Event(), asyncio.Event()
    name = "domainkit.contrib.messaging.worker"
    with caplog.at_level(logging.WARNING, name):
        listening = asyncio.create_task(worker.listen(stop, wake))
        await within(wake.wait())
        wake.clear()
        async with connections.engine().connect() as connection:
            await connection.exec_driver_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND query LIKE 'LISTEN %' "
                "AND pid <> pg_backend_pid()"
            )
        await within(wake.wait())  # Listening again.
        wake.clear()
        await enqueue(remember, value=uuid.uuid4())
        await within(wake.wait())
        stop.set()
        await within(listening)

    failed, back = logged(caplog, name)
    assert failed == (
        "Could not listen for new messages in 'default' (1 failure in a row); "
        "polling until listening again in 0.01s"
    )
    assert back == "Listening for new messages in 'default' again after 1 failures"


@pytest.mark.usefixtures("tables")
async def test_an_idle_worker_starts_committed_work_at_once() -> None:
    """With a 30-second poll, a commit still starts its message at once (PostgreSQL)."""
    poll = 30 if postgres() else 0.01
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": poll}))
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await enqueue(at_gate, name="woken")
    gate.open()
    await gate.until(lambda: gate.finished == ["woken"])
    stop.set()

    await within(running)


# The outbox gauges ------------------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_outbox_gauges_are_counted_once_per_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrapes within METRICS_TTL reuse one count (ASYNC-17)."""
    counted: list[int] = []
    queue_counts = metrics_module.queue_counts  # type: ignore[attr-defined]

    async def counting(config: MessagingSettings, database: str | None = None) -> Any:  # noqa: ANN401
        counted.append(1)
        return await queue_counts(config, database)

    monkeypatch.setattr(metrics_module, "queue_counts", counting)
    await enqueue(remember, value=uuid.uuid4())
    with clock.freeze_time() as frozen:
        await refresh_outbox()
        await refresh_outbox()
        assert len(counted) == 1
        frozen.tick(15)
        await refresh_outbox()
        assert len(counted) == 2
        frozen.tick(15)
        with override_settings(MESSAGING={**FAST, "METRICS_TTL": 0}):
            await refresh_outbox()
            await refresh_outbox()
        assert len(counted) == 4

    pending = REGISTRY.get_sample_value(
        "messaging_outbox_messages", {"status": "pending"}
    )
    assert pending == 1


@pytest.mark.usefixtures("tables")
async def test_concurrent_scrapes_share_one_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scrapes during a count wait for it; one that gives up does not stop it."""
    counted: list[int] = []
    counting, go_on = asyncio.Event(), asyncio.Event()

    async def slow(
        config: MessagingSettings, database: str | None = None
    ) -> dict[str, int]:
        counted.append(1)
        counting.set()
        await go_on.wait()
        return {"pending": 7}

    monkeypatch.setattr(metrics_module, "queue_counts", slow)
    scrapes = [asyncio.create_task(refresh_outbox()) for _ in range(3)]
    await within(counting.wait())
    assert "messaging.outbox_gauges" in {task.get_name() for task in tasks.owned()}
    scrapes[0].cancel()
    go_on.set()
    results = await within(asyncio.gather(*scrapes, return_exceptions=True))

    assert counted == [1]
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1:] == [None, None]
    pending = REGISTRY.get_sample_value(
        "messaging_outbox_messages", {"status": "pending"}
    )
    assert pending == 7
    dead = REGISTRY.get_sample_value("messaging_outbox_messages", {"status": "dead"})
    assert dead == 0  # None counted: none there.


@pytest.mark.usefixtures("tables")
async def test_a_failed_count_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A count that fails reaches the scrape; the next scrape counts again."""
    calls: list[int] = []
    queue_counts = metrics_module.queue_counts  # type: ignore[attr-defined]

    async def flaky(config: MessagingSettings, database: str | None = None) -> Any:  # noqa: ANN401
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("database down")
        return await queue_counts(config, database)

    monkeypatch.setattr(metrics_module, "queue_counts", flaky)
    with pytest.raises(ConnectionError):
        await refresh_outbox()
    await refresh_outbox()

    assert len(calls) == 2


# uvloop -------------------------------------------------------------------------------


@pytest.mark.usefixtures("brokered")
def test_workers_run_on_uvloop() -> None:
    """Polling and brokered workers run on uvloop (runworker uses it when installed)."""
    uvloop = pytest.importorskip("uvloop")

    async def main() -> list[str]:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
            await enqueue(remember, value=uuid.uuid4())
            assert await Worker().run_once() == 1
            await enqueue(at_gate, name="brokered")
            gate.open()
            config = registry.settings(MessagingSettings)
            stop = asyncio.Event()
            running = asyncio.create_task(
                BrokerWorkers([Worker(config)], ["default"], config).run(stop)
            )
            await gate.until(lambda: gate.finished == ["brokered"])
            stop.set()
            await within(running)
        statuses = [str(row["status"]) for row in await rows()]
        await brokers.close_all()
        await connections.close_all()
        return statuses

    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        assert runner.run(main()) == [DONE, DONE]


@pytest.mark.usefixtures("tables")
async def test_a_count_failing_after_its_scrapes_gave_up_is_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The scrapes still waiting get the error; asyncio logs nothing about it."""
    counting, go_on = asyncio.Event(), asyncio.Event()

    async def failing(config: MessagingSettings, database: str | None = None) -> Any:  # noqa: ANN401
        counting.set()
        await go_on.wait()
        raise ConnectionError("database down")

    monkeypatch.setattr(metrics_module, "queue_counts", failing)
    with caplog.at_level(logging.DEBUG):
        gave_up = asyncio.create_task(refresh_outbox())
        waiting = asyncio.create_task(refresh_outbox())
        await within(counting.wait())
        gave_up.cancel()
        go_on.set()
        results = await within(asyncio.gather(gave_up, waiting, return_exceptions=True))
        counting.clear()
        go_on.clear()
        alone = asyncio.create_task(refresh_outbox())  # Then nobody is left.
        await within(counting.wait())
        alone.cancel()
        go_on.set()
        with pytest.raises(asyncio.CancelledError):
            await within(alone)
        if left := list(metrics_module._counting.values()):
            await within(asyncio.wait(left))
        gc.collect()

    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], ConnectionError)
    assert logged(caplog, "asyncio") == []
    assert logged(caplog, "domainkit.tasks") == []  # The scrapes have the error.


# Winding down -------------------------------------------------------------------


async def relaying_into(broker: Holding) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """Start a relaying worker whose first publication ``broker`` holds."""
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await enqueue(remember, value=uuid.uuid4())
    config = registry.settings(MessagingSettings)
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config).run(stop)
    )
    await within(broker.arrived.wait())
    return stop, running


@pytest.mark.usefixtures("broker_tables")
async def test_a_stopping_relay_finishes_its_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop lets the round under way publish; it is not cut short."""
    broker = Holding()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: broker)
    stop, running = await relaying_into(broker)

    stop.set()
    broker.release.set()
    await within(running)

    (row,) = await rows()
    assert row["lock_id"] is not None
    assert seconds_left(row) > 3000  # Confirmed: REPUBLISH_AFTER.
    assert broker.pending("default") == 1


@pytest.mark.usefixtures("broker_tables")
async def test_a_relay_that_will_not_stop_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After HELPER_GRACE, a relay still publishing is cancelled; it gives back."""
    broker = Holding()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: broker)
    monkeypatch.setattr(dispatch, "HELPER_GRACE", 0.05)
    stop, running = await relaying_into(broker)

    stop.set()
    await within(running)

    (row,) = await rows()
    assert (row["lock_id"], row["locked_until"]) == (None, None)


@pytest.mark.usefixtures("tables")
async def test_cleanup_stops_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once stopped, cleanup finishes the batch under way and starts no other."""
    await finished_long_ago(3)
    monkeypatch.setattr(worker_module, "CLEANUP_START", 0.0)
    worker = Worker(MessagingSettings(**{**FAST, "CLEANUP_BATCH_SIZE": 1}))
    stop = asyncio.Event()

    def stop_at_the_first_delete(*args: Any) -> None:  # noqa: ANN401
        if args[2].lstrip().upper().startswith("DELETE"):
            stop.set()

    event.listen(
        worker.engine.sync_engine, "before_cursor_execute", stop_at_the_first_delete
    )
    await within(worker.clean_until(stop))

    assert len(await rows()) == 2


# Edges ------------------------------------------------------------------------------


@pytest.mark.usefixtures("broker_tables")
async def test_a_commit_as_a_round_ends_starts_the_publisher_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ids submitted just as the task ends are published by a new task."""
    memory = work_broker()
    run = CommitPublisher._run
    committed = False

    async def then_a_commit(self: CommitPublisher) -> None:
        nonlocal committed
        await run(self)
        if not committed:  # After the loop's last look, before the task ends.
            committed = True
            await enqueue(remember, value=uuid.uuid4())

    monkeypatch.setattr(CommitPublisher, "_run", then_a_commit)
    await enqueue(remember, value=uuid.uuid4())
    await published()

    assert memory.pending("default") == 2


async def test_a_flush_that_gives_up_leaves_the_others_waiting() -> None:
    """A cancelled flush is skipped when the publisher goes idle; the others wake."""
    publisher = CommitPublisher(MessagingSettings(BROKER="work"))
    publisher.backlog = 1  # Something waits to be offered.
    gave_up = asyncio.create_task(publisher.flush())
    waiting = asyncio.create_task(publisher.flush())
    await asyncio.sleep(0)  # Both wait for the publisher now.
    gave_up.cancel()
    publisher._idle()

    assert await within(waiting) is None
    with pytest.raises(asyncio.CancelledError):
        await gave_up
    assert publisher._flushes == []


@pytest.mark.usefixtures("tables")
async def test_a_worker_outlives_failing_schedules(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Queueing periodic tasks may fail too; the worker waits and goes on."""
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 0.01}))
    schedule = worker.schedule_periodic
    failed = False

    async def flaky() -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise ConnectionError("database down")
        await schedule()

    monkeypatch.setattr(worker, "schedule_periodic", flaky)
    await enqueue(at_gate, name="after the outage")
    gate.open()
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, "domainkit.contrib.messaging.worker"):
        running = asyncio.create_task(worker.run(stop))
        await gate.until(lambda: gate.finished == ["after the outage"])
        stop.set()
        await within(running)

    assert logged(caplog, "domainkit.contrib.messaging.worker")[0].startswith(
        "Could not reach the outbox of 'default' (1 failure in a row)"
    )


@pytest.mark.usefixtures("tables")
async def test_a_count_cancelled_at_shutdown_ends_its_scrapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown cancels a count under way; its scrapes end, and it is forgotten."""
    counting = asyncio.Event()

    async def hangs(config: MessagingSettings, database: str | None = None) -> Any:  # noqa: ANN401
        counting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(metrics_module, "queue_counts", hangs)
    scrape = asyncio.create_task(refresh_outbox())
    await within(counting.wait())
    loop = asyncio.get_running_loop()
    assert await within(tasks.shutdown(loop.time() + WAIT)) == []

    with pytest.raises(asyncio.CancelledError):
        await within(scrape)
    assert dict(metrics_module._counting) == {}


@pytest.mark.usefixtures("broker_tables")
async def test_a_breaker_opening_mid_round_leaves_the_rest_to_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once open, the round's remaining batches are left; nothing more is locked."""
    down = Down()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: down)
    with override_settings(
        MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False, "BATCH_SIZE": 1}
    ):
        for _ in range(3):
            await enqueue(remember, value=uuid.uuid4())
    with override_settings(MESSAGING={**BROKERED, "BREAKER_FAILURES": 1}):
        config = registry.settings(MessagingSettings).model_copy(
            update={"BATCH_SIZE": 1}
        )
        publisher = CommitPublisher(config)
        before = left("breaker")
        publisher.submit("default", [row["id"] for row in await rows()])
        await published(publisher)

    assert publisher.breaker.state == "open"
    assert left("breaker") == before + 2
    assert all(row["lock_id"] is None for row in await rows())


@pytest.mark.usefixtures("tables")
async def test_commits_to_other_queues_do_not_wake_a_worker() -> None:
    """A worker of some queues hears every commit, and wakes only for its own."""
    if not postgres():
        return  # Nothing listens on SQLite; see test_commits_wake_idle_workers.
    worker = Worker(MessagingSettings(**FAST), queues=["emails"])
    stop, wake = asyncio.Event(), asyncio.Event()
    listening = asyncio.create_task(worker.listen(stop, wake))
    await within(wake.wait())
    wake.clear()
    from domainkit_tests.test_messaging import send_email

    await enqueue(remember, value=uuid.uuid4())  # Heard first, and ignored.
    await enqueue(send_email, to="ada@example.com")
    await within(wake.wait())
    stop.set()
    await within(listening)


@pytest.mark.usefixtures("brokered")
def test_outboxes_found_later_share_the_workers_settings_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery from another outbox runs within the same budget and settings."""
    config = registry.settings(MessagingSettings).model_copy(
        update={"POLL_INTERVAL": 0.5}
    )
    monkeypatch.setattr(dispatch, "_hosting_aliases", lambda: {"default", "other"})
    workers = BrokerWorkers([], ["default"], config)

    other = workers.outbox("other")

    assert other is not None
    assert (other.config, other.budget) == (config, workers.budget)
    assert other.config is config
    assert other.budget is workers.budget


def answers_recorded(
    failure: BaseException | None = None,
) -> tuple[Delivery, list[str]]:
    """A delivery whose requeue is recorded, and raises ``failure`` if given."""
    answers: list[str] = []

    async def requeue() -> None:
        answers.append("requeue")
        if failure is not None:
            raise failure

    async def ack() -> None:
        answers.append("ack")

    return Delivery("default", b"{}", "m", ack, requeue), answers


async def test_deliveries_are_given_back_once_consuming_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give-backs wait for consuming to stop, quietly, and never for long."""
    consuming = dispatch._Consuming(asyncio.Event())
    delivery, answers = answers_recorded(BrokerUnavailable("connection lost"))
    giving_back = asyncio.create_task(consuming.give_back(delivery))
    await asyncio.sleep(0)  # It waits for consuming to stop.
    assert answers == []
    consuming.ended.set()
    await within(giving_back)  # The broker's failure is not raised.
    assert answers == ["requeue"]

    monkeypatch.setattr(dispatch, "STOP_CONSUMING_TIMEOUT", 0.01)
    monkeypatch.setattr(dispatch, "ANSWER_TIMEOUT", 0.01)
    never = dispatch._Consuming(asyncio.Event())
    delivery, answers = answers_recorded()
    await within(never.give_back(delivery))  # Consuming never stopped: given up.
    assert answers == []


@pytest.mark.usefixtures("broker_tables")
async def test_a_relays_listener_that_will_not_stop_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relay's own helpers get HELPER_GRACE too, then they are cancelled."""

    async def deaf(self: Worker, stop: asyncio.Event, wake: asyncio.Event) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Worker, "listen", deaf)
    monkeypatch.setattr(dispatch, "HELPER_GRACE", 0.01)
    config = registry.settings(MessagingSettings)
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config).run(stop)
    )
    await asyncio.sleep(0)
    stop.set()

    await within(running)
