"""The worker under deploys, odd failures, and skewed clocks (CONC-13 to CONC-19)."""

import asyncio
import logging
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select, update

from domainkit.clock import freeze_time
from domainkit.conf import settings
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    admin,
    enqueue,
    periodic,
    task,
)
from domainkit.contrib.messaging import apps as apps_module
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.apps import MessagingConfig
from domainkit.contrib.messaging.tables import (
    DEAD,
    DONE,
    HELD,
    PENDING,
    UNKNOWN_SINCE,
    messages,
)
from domainkit.contrib.messaging.worker import (
    CRON,
    PERIODIC,
    UNKNOWN,
    HandlerCancelled,
    slot_prefix,
)
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_messaging import (  # noqa: F401
    FAST,
    messaging,
    outbox,
    tables,
)

pytestmark = pytest.mark.anyio

NEW_TASK = "tests.reliability.added_by_the_deploy"
HOUR = 3600.0


def config(**options: object) -> MessagingSettings:
    """``FAST`` with ``options``."""
    return MessagingSettings(**{**FAST, **options})


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry failed runs at once, so a test runs every attempt in one pass."""
    monkeypatch.setattr(worker_module, "backoff", lambda attempts, config: 0.0)


async def added_by_the_deploy(label: str) -> None:
    """A task the new code registers, as the deploy rolls out."""
    ran.append(label)


ran: list[str] = []


@pytest.fixture(autouse=True)
def nothing_ran() -> Iterator[None]:
    """Start each test with nothing run."""
    ran.clear()
    yield
    tasks._tasks.pop(NEW_TASK, None)


async def queue_for_new_code(label: str = "a", key: str | None = None) -> None:
    """Queue ``NEW_TASK`` as the new code does, then forget it as an old worker."""
    task(name=NEW_TASK)(added_by_the_deploy)
    await enqueue(added_by_the_deploy, ordering_key=key, label=label)
    tasks._tasks.pop(NEW_TASK)


async def row_of(target: str = NEW_TASK) -> dict[str, Any]:
    """The outbox row of ``target``."""
    (row,) = [row for row in await outbox() if row["target"] == target]
    return row


# CONC-15: a target this worker does not know waits for one that does.


@pytest.mark.usefixtures("tables")
async def test_an_unknown_task_waits_for_a_worker_that_knows_it() -> None:
    """An old worker gives it back, attempt not spent; a new one runs it."""
    with freeze_time("2026-11-01T09:00:00Z") as clock:
        await queue_for_new_code()
        assert await Worker(config()).run_once() == 1

        row = await row_of()
        assert (row["status"], row["attempts"], row["started_at"]) == (
            PENDING,
            0,
            None,
        )
        assert row["headers"][UNKNOWN_SINCE] == "2026-11-01T09:00:00+00:00"
        assert row["last_error"] == f"UnknownTarget: No task {NEW_TASK!r}."
        assert row["available_at"] == clock.now() + timedelta(
            seconds=FAST["RETRY_BASE_SECONDS"]
        )

        clock.tick(1)
        task(name=NEW_TASK)(added_by_the_deploy)  # The deploy reached it.
        assert await Worker(config()).run_once() == 1

    assert ran == ["a"]
    assert (await row_of())["status"] == DONE


@pytest.mark.usefixtures("tables")
async def test_an_unknown_task_comes_back_after_a_doubling_delay_capped() -> None:
    """Each delay is the time waited so far, between the retry bounds and the grace."""
    settings = config(RETRY_BASE_SECONDS=10, RETRY_MAX_SECONDS=100)
    with freeze_time("2026-11-01T09:00:00Z") as clock:
        await queue_for_new_code()
        delays = []
        for _ in range(6):
            await Worker(settings).run_once()
            row = await row_of()
            delays.append((row["available_at"] - clock.now()).total_seconds())
            clock.move_to(row["available_at"])
        clock.move_to(datetime(2026, 11, 1, 9, 59, 10, tzinfo=UTC))
        await Worker(settings).run_once()
        last = await row_of()

    assert delays == [10.0, 10.0, 20.0, 40.0, 80.0, 100.0]
    # 50 seconds of the hour are left: it comes back as the grace ends.
    assert last["available_at"] == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


@pytest.mark.usefixtures("tables")
async def test_an_unknown_task_is_dead_lettered_once_the_grace_has_passed() -> None:
    """It waits ``UNKNOWN_TARGET_GRACE`` from when a worker first found it unknown."""
    with freeze_time("2026-11-01T09:00:00Z") as clock:
        await queue_for_new_code()
        await Worker(config()).run_once()
        clock.tick(HOUR - 1)
        await Worker(config()).run_once()
        assert (await row_of())["status"] == PENDING
        clock.tick(1)
        await Worker(config()).run_once()

    row = await row_of()
    assert (row["status"], row["attempts"]) == (DEAD, 1)
    assert row["last_error"] == f"UnknownTarget: No task {NEW_TASK!r}."


@pytest.mark.usefixtures("tables")
async def test_no_grace_dead_letters_an_unknown_task_at_once() -> None:
    """``UNKNOWN_TARGET_GRACE = 0``: as before 0.15."""
    await queue_for_new_code()
    await Worker(config(UNKNOWN_TARGET_GRACE=0)).run_once()

    assert (await row_of())["status"] == DEAD


@pytest.mark.usefixtures("tables")
async def test_an_unreadable_first_sighting_counts_from_now() -> None:
    """A hand-edited header does not dead-letter the message early."""
    with freeze_time("2026-11-01T09:00:00Z"):
        await queue_for_new_code()
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(messages).values(headers={UNKNOWN_SINCE: "yesterday"})
            )
        await Worker(config()).run_once()

    row = await row_of()
    assert (row["status"], row["headers"][UNKNOWN_SINCE]) == (
        PENDING,
        "2026-11-01T09:00:00+00:00",
    )


@pytest.mark.usefixtures("tables")
async def test_an_unknown_keyed_task_keeps_its_place_in_the_key() -> None:
    """The key's later messages stay held behind it until it runs."""

    @task(name="tests.reliability.known")
    async def known(label: str) -> None:
        ran.append(label)

    try:
        with freeze_time("2026-11-01T09:00:00Z") as clock:
            await queue_for_new_code("first", key="k")
            await enqueue(known, ordering_key="k", label="second")
            await Worker(config()).run_once()
            assert ran == []
            by_key = sorted(await outbox(), key=lambda row: str(row["sequence"]))
            assert [row["status"] for row in by_key] == [PENDING, HELD]

            clock.tick(5)
            task(name=NEW_TASK)(added_by_the_deploy)
            await Worker(config()).run_once()
    finally:
        tasks._tasks.pop("tests.reliability.known")

    assert ran == ["first", "second"]


@pytest.mark.usefixtures("tables")
async def test_the_wait_is_logged_and_counted_as_its_own_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log says what is missing, the next try, and when it dies."""
    observed: list[str] = []
    with (
        freeze_time("2026-11-01T09:00:00Z"),
        pytest.MonkeyPatch.context() as patch,
        caplog.at_level(logging.WARNING, worker_module.__name__),
    ):
        patch.setattr(
            worker_module,
            "observe",
            lambda kind, outcome, seconds: observed.append(outcome),
        )
        await queue_for_new_code()
        await Worker(config()).run_once()

    assert observed == [UNKNOWN]
    assert caplog.messages == [
        (
            f"task {NEW_TASK}: No task {NEW_TASK!r}. Waiting for a worker that "
            "knows it (a deploy rolling out?); trying again in 0.0s, dead-lettered "
            "after 2026-11-01T10:00:00+00:00"
        )
    ]


@pytest.mark.usefixtures("messaging")
def test_unnamed_tasks_are_warned_about_where_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``messaging.W001`` names each task without ``name=``, where deployed."""

    @task
    async def unnamed() -> None: ...

    @task(name="tests.reliability.named")
    async def named() -> None: ...

    @periodic(every=timedelta(hours=1), name="tests.reliability.named_periodic")
    async def named_periodic() -> None: ...

    config_ = MessagingConfig.__new__(MessagingConfig)
    try:
        assert config_.unnamed_tasks() == []
        # The test's environment, counted as deployed: production's own
        # checks would refuse the test database's settings.
        monkeypatch.setattr(
            apps_module, "DEPLOYED_ENVIRONMENTS", frozenset({settings.ENVIRONMENT})
        )
        found = config_.unnamed_tasks()
    finally:
        for name in ("tests.reliability.named", "tests.reliability.named_periodic"):
            tasks._tasks.pop(name)
        key = tasks.qualified_name(unnamed)
        tasks._tasks.pop(key)

    mine = [each for each in found if key in each.message]
    assert [each.id for each in mine] == ["messaging.W001"]
    assert not any("tests.reliability.named" in each.message for each in found)
    assert mine[0].hint == "Give it a name of its own: @task(name=...)."


# CONC-17: a CancelledError of the handler's own is a failure.


@pytest.mark.usefixtures("tables", "no_backoff")
async def test_a_handlers_own_cancellation_is_recorded_as_handler_cancelled() -> None:
    """The dead letter says what happened."""

    @task(name="tests.reliability.cancelled", max_attempts=1)
    async def cancelled() -> None:
        raise asyncio.CancelledError

    try:
        await enqueue(cancelled)
        await Worker(config()).run_once()
    finally:
        tasks._tasks.pop("tests.reliability.cancelled")

    row = await row_of("tests.reliability.cancelled")
    assert (row["status"], row["last_error"]) == (
        DEAD,
        (
            f"{HandlerCancelled.__name__}: The handler raised CancelledError, "
            "though the worker was not stopping it: it awaited something that "
            "was cancelled."
        ),
    )


@pytest.mark.usefixtures("tables")
async def test_a_stop_still_gives_the_message_back() -> None:
    """A cancellation of the worker's own task is a stop: the attempt is not spent."""
    started = asyncio.Event()

    @task(name="tests.reliability.slow")
    async def slow() -> None:
        started.set()
        await asyncio.sleep(30)

    try:
        await enqueue(slow)
        running = asyncio.create_task(Worker(config()).run_once())
        async with asyncio.timeout(5):
            await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        tasks._tasks.pop("tests.reliability.slow")

    row = await row_of("tests.reliability.slow")
    assert (row["status"], row["attempts"], row["lock_id"]) == (PENDING, 0, None)


# CONC-14: a cut-off claim gives its messages back.


class _CutOnce:
    """An engine whose first transaction commits, then raises ``CancelledError``.

    As a cancellation that lands while the driver commits: the claim is in
    the database, and its caller never learns it.
    """

    def __init__(self, engine: Any) -> None:  # noqa: ANN401
        """Wrap ``engine``."""
        self.engine = engine
        self.dialect = engine.dialect
        self.cut = False

    def begin(self) -> Any:  # noqa: ANN401
        """The engine's transaction; the first one is cut off once committed."""
        wrapper = self
        inner = self.engine.begin()

        class Transaction:
            async def __aenter__(self) -> Any:  # noqa: ANN401
                return await inner.__aenter__()

            async def __aexit__(self, *exc: object) -> None:
                await inner.__aexit__(*exc)
                if not wrapper.cut:
                    wrapper.cut = True
                    raise asyncio.CancelledError

        return Transaction()


class CutWorker(Worker):
    """A worker whose first transaction is cut off as it commits."""

    _cut: _CutOnce | None = None

    @property
    def engine(self) -> Any:  # noqa: ANN401
        """The outbox's engine, cutting off its first transaction."""
        if self._cut is None:
            self._cut = _CutOnce(connections.engine(self.database))
        return self._cut


@task(name="tests.reliability.known_task")
async def added_by_the_deploy_known(label: str) -> None:
    """A task every worker knows."""
    ran.append(label)


@pytest.mark.usefixtures("tables")
async def test_a_give_back_that_fails_is_logged_and_the_lock_runs_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The claim's cancellation still goes on."""

    async def refused(self: Worker, lock_id: uuid.UUID) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(Worker, "_give_back", refused)
    await enqueue(added_by_the_deploy_known, label="x")
    with pytest.raises(asyncio.CancelledError):
        await CutWorker(config()).claim(1)

    assert "Could not give back a claim cut off; their locks expire" in caplog.messages
    assert (await outbox())[0]["lock_id"] is not None


@pytest.mark.usefixtures("tables")
async def test_a_take_over_cut_off_as_it_commits_gives_the_message_back() -> None:
    """As a claim: the consumer's lock goes, and its attempt is not spent."""
    await enqueue(added_by_the_deploy_known, label="x")
    worker = Worker(config())
    (message,) = await worker.claim(1)  # As a relay would lease it.
    async with connections.engine().begin() as connection:
        await connection.execute(update(messages).values(attempts=0, started_at=None))
    with pytest.raises(asyncio.CancelledError):
        await CutWorker(config()).take_over(message.id, message.lock_id)

    row = (await outbox())[0]
    assert (row["lock_id"], row["attempts"], row["started_at"]) == (None, 0, None)


# CONC-16: scheduling apart from claiming; names that fit.


def test_a_task_name_longer_than_the_outbox_column_is_refused() -> None:
    """The outbox stores 255 characters of a task's name."""
    task(name="x" * 255)(added_by_the_deploy)
    tasks._tasks.pop("x" * 255)
    with pytest.raises(ImproperlyConfigured, match="at most 255 characters"):
        task(name="x" * 256)(added_by_the_deploy)


def test_a_scheduled_runs_key_fits_its_column() -> None:
    """A name that leaves no room for the slot's number is hashed."""
    assert slot_prefix(PERIODIC, "tests.hourly") == "periodic:tests.hourly:"
    fits = "x" * (255 - len("periodic::") - 20)
    assert slot_prefix(PERIODIC, fits) == f"periodic:{fits}:"
    hashed = slot_prefix(PERIODIC, fits + "y")
    assert hashed.startswith("periodic:sha256-")
    assert len(hashed) + 20 <= 255
    assert hashed != slot_prefix(PERIODIC, fits + "z")
    assert slot_prefix(CRON, fits + "y") == f"cron:{fits}y:"


@pytest.mark.usefixtures("tables")
async def test_a_failing_schedule_waits_doubling_and_says_when_it_works_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Claims go on meanwhile; the schedule is tried again after its pause."""
    worker = Worker(config(POLL_INTERVAL=10))
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("refused")

    monkeypatch.setattr(worker, "schedule_periodic", flaky)
    with (
        freeze_time("2026-11-01T09:00:00Z") as clock,
        caplog.at_level(logging.WARNING, worker_module.__name__),
    ):
        await enqueue(added_by_the_deploy_known, label="still runs")
        assert await worker.run_once() == 1
        await worker.run_once()  # Within its pause: not tried.
        clock.tick(10)
        await worker.run_once()
        clock.tick(19)
        await worker.run_once()  # Paused for 20 now.
        clock.tick(1)
        await worker.run_once()

    assert calls == 3
    assert ran == ["still runs"]
    assert caplog.messages == [
        (
            "Could not queue the periodic tasks in 'default' (1 failure in a "
            "row); messages still run; queueing them again in 10s"
        ),
        (
            "Could not queue the periodic tasks in 'default' (2 failures in a "
            "row); messages still run; queueing them again in 20s"
        ),
        "Queueing the periodic tasks in 'default' works again after 2 failures",
    ]


# CONC-13: a periodic slot's row outlives the retention while the slot lasts.


@pytest.mark.usefixtures("tables")
async def test_a_long_slots_row_is_deleted_once_the_slot_is_over() -> None:
    """Kept until the slot ends, then cleaned up as any finished message."""

    @periodic(every=timedelta(days=30), name="tests.reliability.monthly")
    async def monthly() -> None: ...

    @periodic(every=timedelta(hours=1), name="tests.reliability.hourly")
    async def hourly() -> None: ...

    seconds = 30 * 86400
    slot = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp() / seconds)
    start = datetime.fromtimestamp(slot * seconds, UTC)
    try:
        with freeze_time(start + timedelta(hours=1)) as clock:
            worker = Worker(config(RETENTION_SECONDS=86400))
            await worker.run_once()
            clock.tick(timedelta(days=2))
            assert await worker.cleanup() == 1  # The hourly run, not the monthly.
            (kept,) = await outbox()
            assert kept["target"] == "tests.reliability.monthly"
            clock.move_to(start + timedelta(days=30, seconds=-1))
            assert await worker.cleanup() == 0
            clock.tick(2)
            assert await worker.cleanup() == 1
    finally:
        for name in ("tests.reliability.monthly", "tests.reliability.hourly"):
            tasks._tasks.pop(name)


@pytest.mark.usefixtures("tables")
async def test_rows_without_a_dedupe_key_are_cleaned_up_beside_long_slots() -> None:
    """The condition that keeps slots never keeps other messages."""

    @periodic(every=timedelta(days=30), name="tests.reliability.monthly_too")
    async def monthly() -> None: ...

    try:
        with freeze_time("2026-11-01T09:00:00Z") as clock:
            await enqueue(added_by_the_deploy_known, label="plain")
            worker = Worker(config(RETENTION_SECONDS=86400))
            (message,) = await worker.claim(1)  # Not the periodic one.
            await worker._done(message, None)
            clock.tick(timedelta(days=2))
            assert await worker.cleanup() == 1
    finally:
        tasks._tasks.pop("tests.reliability.monthly_too")


# CONC-18: the lag by the database's clock.


@pytest.mark.usefixtures("tables")
async def test_the_lag_ignores_a_host_clock_that_runs_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is measured by the clock that wrote the times."""
    await enqueue(added_by_the_deploy_known, label="waiting")
    real = admin._now
    monkeypatch.setattr(admin, "_now", lambda: real() + timedelta(hours=1))

    lag = await admin.queue_lag(config())

    assert set(lag) == {"default"}
    assert 0 <= lag["default"] < 60


@pytest.mark.usefixtures("tables")
async def test_the_lag_under_a_frozen_clock_is_the_frozen_wait() -> None:
    """Tests move the clock; the lag follows it."""
    with freeze_time("2026-11-01T09:00:00Z") as clock:
        await enqueue(added_by_the_deploy_known, label="waiting")
        clock.tick(42)
        assert await admin.queue_lag(config()) == {"default": 42.0}


# CONC-19: a finishing transaction takes the key's turn first.


@pytest.mark.usefixtures("tables")
async def test_finishing_a_keyed_message_takes_its_keys_turn_first() -> None:
    """The key's row, then the message's: the order every writer of the key takes."""
    await enqueue(added_by_the_deploy_known, ordering_key="k", label="one")
    await enqueue(added_by_the_deploy_known, ordering_key="k", label="two")
    worker = Worker(config())
    (message,) = await worker.claim(5)
    statements: list[str] = []

    def record(*args: Any) -> None:  # noqa: ANN401
        written = re.match(r"(?:UPDATE|INSERT INTO) (\w+)", str(args[2]))
        if written is not None:
            statements.append(written[1])

    sync_engine = worker.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        await worker._done(message, None)
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)

    tables_written = [
        each
        for each in statements
        if each in {"domainkit_ordering_keys", "domainkit_messages"}
    ]
    assert tables_written[:2] == ["domainkit_ordering_keys", "domainkit_messages"]
    assert tables_written.count("domainkit_ordering_keys") == 1
    async with connections.engine().connect() as connection:
        found = (
            await connection.execute(
                select(messages.c.status).order_by(messages.c.sequence)
            )
        ).scalars()
        assert list(found) == [DONE, PENDING]


@pytest.mark.usefixtures("tables")
async def test_a_retried_keyed_message_takes_no_turn() -> None:
    """It stays in its key's line: nothing to promote, no turn to take."""
    await enqueue(added_by_the_deploy_known, ordering_key="k", label="one")
    worker = Worker(config())
    (message,) = await worker.claim(1)
    statements: list[str] = []

    def record(*args: Any) -> None:  # noqa: ANN401
        statements.append(str(args[2]))

    sync_engine = worker.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        await worker._release(message, status=PENDING, started_at=None)
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)

    assert not any("domainkit_ordering_keys" in each for each in statements)
