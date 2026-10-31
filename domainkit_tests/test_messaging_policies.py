"""Per-task policies, priorities, chains, rates, cron runs, and outbox inspection."""

import asyncio
import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text, update

from domainkit import deadlines
from domainkit.broker import MemoryBroker
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.contrib.messaging import (
    Attempt,
    Backoff,
    MessagingSettings,
    Worker,
    chain,
    current_attempt,
    enqueue,
    periodic,
    task,
)
from domainkit.contrib.messaging import admin as outbox_admin
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging import retry as retry_module
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.cron import Cron
from domainkit.contrib.messaging.dispatch import BrokerWorkers
from domainkit.contrib.messaging.metrics import refresh_outbox
from domainkit.contrib.messaging.outbox import message_row
from domainkit.contrib.messaging.retry import Backoff as RetryBackoff
from domainkit.contrib.messaging.retry import retryable
from domainkit.contrib.messaging.tables import (
    CANCELLED,
    DEAD,
    DONE,
    PENDING,
    messages,
)
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.ratelimit import Decision
from domainkit.management import CommandError
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork
from domainkit_tests.test_messaging import (  # noqa: F401 - fixtures.
    FAST,
    broker_tables,
    brokered,
    delivery,
    immediate_retries,
    messaging,
    outbox,
    ran,
    remember,
    reported,
    run,
    tables,
)

pytestmark = pytest.mark.anyio


class Flaky(Exception):
    """A failure worth retrying."""


class Broken(Exception):
    """A failure that retrying cannot fix."""


@task(name="tests.policy", max_attempts=4, retry_on=(Flaky,), dont_retry_on=(Broken,))
async def with_policy(error: str) -> None:
    """Raise the error named."""
    ran.append(error)
    raise {"flaky": Flaky, "broken": Broken, "other": ValueError}[error](error)


@task(name="tests.backoff", max_attempts=3, backoff=Backoff.fixed(60))
async def backs_off() -> None:
    """Fail, to be retried after the task's own delay."""
    raise Flaky("later")


@task(name="tests.quick", timeout=0.05, max_attempts=1)
async def quick(seconds: float) -> None:
    """Sleep longer than the task's timeout allows."""
    await asyncio.sleep(seconds)


@task(name="tests.bounded", timeout=0.5, max_attempts=1)
async def bounded() -> None:
    """Record the deadline the task runs under, and its statements' timeout."""
    shown = None
    async with NotesUnitOfWork() as uow:
        if connections.engine().dialect.name == "postgresql":
            shown = await uow.session.scalar(text("SHOW statement_timeout"))
    ran.append(("bounded", deadlines.remaining(), deadlines.enforced(), shown))


@task(name="tests.unbounded", max_attempts=1)
async def unbounded() -> None:
    """Record the deadline the task runs under: ``TASK_TIMEOUT``'s."""
    ran.append(("unbounded", deadlines.remaining(), deadlines.enforced(), None))


@task(name="tests.low", priority=-10)
async def low(label: str) -> None:
    """Record the order of runs."""
    ran.append(label)


@task(name="tests.high", priority=50)
async def high(label: str) -> None:
    """Record the order of runs."""
    ran.append(label)


@task(name="tests.charge")
async def charge(order: int) -> dict[str, Any]:
    """The first step of a chain: return the next step's arguments."""
    ran.append(("charge", order, current_request()))
    return {"order": order, "receipt": f"r-{order}"}


@task(name="tests.receipt", queue="emails", priority=7, max_attempts=2)
async def receipt(order: int, receipt: str) -> None:
    """The second step: take the first's result."""
    ran.append(("receipt", order, receipt, current_request()))


@task(name="tests.archive")
async def archive(order: int, receipt: str) -> int:
    """The third step: gets the second's arguments (it returned None)."""
    ran.append(("archive", order, receipt))
    return order * 10


@task(name="tests.total")
async def total(result: int) -> None:
    """The last step: a plain result comes as ``result``."""
    ran.append(("total", result))


@task(name="tests.unstorable")
async def unstorable() -> object:
    """Return something JSON cannot hold."""
    ran.append("unstorable")
    return object()


@task(name="tests.counted", rate="2/minute")
async def counted(number: int) -> None:
    """A task limited to two runs a minute."""
    ran.append(number)


@task(name="tests.attempts", max_attempts=2)
async def attempts() -> None:
    """Record the attempt; fail the first."""
    attempt = current_attempt()
    ran.append(attempt)
    if attempt is not None and not attempt.last:
        raise Flaky("first")


async def rows() -> list[dict[str, Any]]:
    """Every outbox row, oldest first."""
    return await outbox()


async def set_rows(**values: object) -> None:
    """Update every outbox row."""
    async with connections.engine().begin() as connection:
        await connection.execute(update(messages).values(**values))


# Backoff and retry rules ------------------------------------------------------


def test_backoff_doubles_up_to_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exponential delays double from the base and stop at the cap."""
    monkeypatch.setattr(retry_module._random, "uniform", lambda low, high: high)
    exponential = Backoff(base=2, cap=30, jitter=True)
    fixed = Backoff.fixed(15)

    assert [exponential.delay(n) for n in (1, 2, 3, 4, 5, 6)] == [2, 4, 8, 16, 30, 30]
    assert exponential.delay(0) == 2  # Never below the base.
    assert exponential.delay(10_000) == 30  # No overflow.
    assert [fixed.delay(n) for n in (1, 7, 10_000)] == [15, 15, 15]
    assert Backoff(base=40, cap=30).delay(1) == 30
    assert (fixed.exponential, fixed.jitter) == (False, False)


def test_backoff_jitter_draws_from_half_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """With jitter, a delay is between half and all of the ceiling."""
    asked: list[tuple[float, float]] = []

    def draw(low: float, high: float) -> float:
        asked.append((low, high))
        return low

    monkeypatch.setattr(retry_module._random, "uniform", draw)

    assert Backoff(base=8, cap=100).delay(2) == 8
    assert Backoff.fixed(10, jitter=True).delay(3) == 5
    assert Backoff(base=8, cap=100, jitter=False).delay(2) == 16
    assert asked == [(0.5, 1.0), (0.5, 1.0)]
    for _ in range(50):
        assert 4 <= Backoff(base=8, cap=8).delay(1) <= 8


@pytest.mark.parametrize(
    ("base", "cap"),
    [(0, 10), (-1, 10), (1, 0), (math.inf, 10), (1, math.nan), (1, 365 * 86400 + 1)],
)
def test_backoff_delays_are_positive_finite_and_bounded(
    base: float, cap: float
) -> None:
    """Zero, negative, infinite, or longer than a year is refused."""
    with pytest.raises(ImproperlyConfigured) as refused:
        Backoff(base=base, cap=cap)
    name, value = ("base", base) if not 0 < base <= 365 * 86400 else ("cap", cap)
    assert str(refused.value) == (
        f"Backoff {name} must be more than 0 and at most 3.1536e+07 seconds, "
        f"not {value!r}."
    )
    assert Backoff(base=365 * 86400, cap=365 * 86400).delay(1) <= 365 * 86400


def test_errors_worth_retrying() -> None:
    """dont_retry_on wins; retry_on narrows; by default everything is retried."""
    assert retryable(Flaky(), None, ())
    assert retryable(ValueError(), None, ())
    assert not retryable(Broken(), None, (Broken,))
    assert retryable(Flaky(), (Flaky,), ())
    assert not retryable(ValueError(), (Flaky,), ())
    assert not retryable(Flaky(), (Flaky,), (Flaky,))
    assert not retryable(Flaky(), (Exception,), (Flaky,))


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"max_attempts": 0}, "max_attempts must be 1 or more"),
        ({"timeout": 0}, "timeout must be a positive number, not 0"),
        ({"timeout": -1}, "timeout must be a positive number"),
        ({"timeout": math.inf}, "timeout must be a positive number, not inf"),
        ({"timeout": math.nan}, "timeout must be a positive number"),
        ({"priority": 101}, "priority must be from -100 to 100, not 101"),
        ({"priority": -101}, "priority must be from -100 to 100"),
        ({"rate": "10 a minute"}, "rate '10 a minute' must look like"),
        ({"rate": "0/minute"}, "must look like"),
        (
            {"retry_on": [ValueError, "TimeoutError"]},
            "retry_on takes exception classes, not 'TimeoutError'",
        ),
        ({"retry_on": (KeyboardInterrupt,)}, "retry_on takes exception classes"),
        (
            {"dont_retry_on": (int,)},
            "dont_retry_on takes exception classes, not <class 'int'>",
        ),
        ({"queue": "no spaces"}, "Queue names have 1 to 64"),
    ],
)
def test_task_options_are_checked(options: dict[str, Any], error: str) -> None:
    """Bad options fail when the task is declared, for task and periodic alike."""
    with pytest.raises(ImproperlyConfigured, match=error.replace("(", r"\(")):
        task(**options)
    with pytest.raises(ImproperlyConfigured, match=error.replace("(", r"\(")):
        periodic(timedelta(hours=1), **options)


def test_task_options_are_kept() -> None:
    """The registration keeps every option, with defaults for the rest."""
    spec = tasks.get_task("tests.policy")
    assert spec is not None
    assert (spec.retry_on, spec.dont_retry_on, spec.max_attempts) == (
        (Flaky,),
        (Broken,),
        4,
    )
    counted_spec = tasks.get_task("tests.counted")
    assert counted_spec is not None
    assert (counted_spec.rate, counted_spec.priority, counted_spec.timeout) == (
        (2, 60),
        0,
        None,
    )
    assert tasks.spec_for(high).priority == 50
    assert tasks.spec_for(quick).timeout == 0.05
    assert tasks.spec_for(backs_off).backoff == Backoff.fixed(60)
    assert tasks.spec_for(remember).retry_on is None
    with_bounds = task(priority=-100, timeout=1e-9, max_attempts=1)
    assert with_bounds is not None


def test_periodic_tasks_need_one_schedule() -> None:
    """every= or cron=, not both or neither; tz= only with cron=."""
    for options in ({}, {"every": timedelta(hours=1), "cron": "* * * * *"}):
        with pytest.raises(ImproperlyConfigured, match="either every= or cron="):
            periodic(**options)  # type: ignore[arg-type]
    with pytest.raises(ImproperlyConfigured, match="tz= goes with cron="):
        periodic(timedelta(hours=1), tz="UTC")
    with pytest.raises(
        ImproperlyConfigured, match=r"Invalid cron schedule: .*never runs"
    ):
        periodic(cron="0 0 30 2 *")
    with pytest.raises(ImproperlyConfigured, match="Unknown time zone 'Mars/Base'"):
        periodic(cron="0 0 * * *", tz="Mars/Base")

    @periodic(cron="0 3 * * 1-5", tz="Africa/Cairo", name="tests.nightly")
    async def nightly() -> None: ...

    @periodic(cron="@hourly", name="tests.utc_hourly")
    async def utc_hourly() -> None: ...

    try:
        schedules = {spec.key: schedule for spec, schedule in tasks.cron_tasks()}
        assert str(schedules["tests.nightly"]) == "0 3 * * 1-5 (Africa/Cairo)"
        assert schedules["tests.utc_hourly"].tz.key == "UTC"
        assert "tests.nightly" not in [spec.key for spec, _ in tasks.periodic_tasks()]
    finally:
        tasks._tasks.pop("tests.nightly")
        tasks._tasks.pop("tests.utc_hourly")


@pytest.mark.usefixtures("messaging")
def test_contrib_cron_tasks_follow_their_app() -> None:
    """A framework app's cron task is scheduled only while it is installed."""

    async def purge() -> None: ...

    purge.__module__ = "domainkit.contrib.idempotency.tasks"
    tasks._register(
        tasks.TaskSpec("tests.contrib_cron", purge, cron=Cron.parse("0 * * * *"))
    )
    try:
        assert "tests.contrib_cron" not in [spec.key for spec, _ in tasks.cron_tasks()]
    finally:
        tasks._tasks.pop("tests.contrib_cron")


# The worker's use of per-task policies ----------------------------------------


@pytest.mark.usefixtures("tables")
async def test_errors_outside_retry_on_dead_letter_at_once() -> None:
    """retry_on errors are retried; others, and dont_retry_on ones, are not."""
    for error in ("flaky", "broken", "other"):
        await enqueue(with_policy, error=error)

    await Worker().run_once()

    by_error = {row["payload"]["error"]: row for row in await rows()}
    assert [(e, r["status"], r["attempts"]) for e, r in sorted(by_error.items())] == [
        ("broken", DEAD, 1),
        ("flaky", DEAD, 4),
        ("other", DEAD, 1),
    ]
    assert by_error["broken"]["last_error"] == "Broken: broken"
    assert sorted(ran) == ["broken", "flaky", "flaky", "flaky", "flaky", "other"]


@pytest.mark.usefixtures("tables")
async def test_a_task_retries_after_its_own_backoff() -> None:
    """The task's backoff replaces MESSAGING's (which tests make instant)."""
    await enqueue(backs_off)
    before = datetime.now(UTC)

    assert await Worker().run_once() == 1

    (row,) = await rows()
    assert (row["status"], row["attempts"], row["started_at"]) == (PENDING, 1, None)
    available_at = row["available_at"]
    assert isinstance(available_at, datetime)
    assert before + timedelta(seconds=60) <= available_at
    assert available_at <= datetime.now(UTC) + timedelta(seconds=60)


@pytest.mark.usefixtures("tables")
async def test_a_task_runs_under_its_own_timeout() -> None:
    """A task's timeout replaces TASK_TIMEOUT, shorter or longer.

    Each task sleeps ten times one of the two timeouts and a tenth of the
    other, so only the timeout that applies explains how it ends, however
    slow the machine: a sleep never ends early, and a tenth leaves room.
    """

    @task(name="tests.shorter", timeout=0.05, max_attempts=1)
    async def shorter() -> None:
        await asyncio.sleep(0.5)
        ran.append("shorter")

    @task(name="tests.longer", timeout=5, max_attempts=1)
    async def longer() -> None:
        await asyncio.sleep(0.5)
        ran.append("longer")

    def config(task_timeout: float) -> MessagingSettings:
        return MessagingSettings(
            **{**FAST, "TASK_TIMEOUT": task_timeout, "VISIBILITY_TIMEOUT": 60}
        )

    await enqueue(shorter)
    await Worker(config(task_timeout=5)).run_once()
    await enqueue(longer)
    await Worker(config(task_timeout=0.05)).run_once()

    statuses = {row["target"]: row["status"] for row in await rows()}
    assert statuses == {"tests.shorter": DEAD, "tests.longer": DONE}
    assert ran == ["longer"]


@pytest.mark.usefixtures("tables")
async def test_a_task_runs_under_a_deadline_of_its_timeout() -> None:
    """What a task waits on is bounded by the time it has left.

    Its statements' ``statement_timeout`` (on PostgreSQL) and outgoing calls
    see the deadline, as in a request, and its own timer enforces it.
    """
    await enqueue(bounded)
    await enqueue(unbounded)

    await Worker().run_once()

    statuses = {row["target"]: row["status"] for row in await rows()}
    assert statuses == {"tests.bounded": DONE, "tests.unbounded": DONE}
    seen = {name: rest for name, *rest in ran}
    for name, timeout in (("bounded", 0.5), ("unbounded", FAST["TASK_TIMEOUT"])):
        left, enforced, _ = seen[name]
        assert isinstance(left, float)
        assert 0 < left <= timeout
        assert enforced is True
    shown = seen["bounded"][2]
    if shown is not None:  # PostgreSQL: SET LOCAL from the deadline.
        assert re.fullmatch(r"\d+ms", shown)
        assert 0 < int(shown.removesuffix("ms")) <= 500


@pytest.mark.usefixtures("messaging")
async def test_task_timeouts_must_stay_under_the_visibility_timeout() -> None:
    """A task that may run as long as a claim lasts would run twice."""

    @task(name="tests.too_long", timeout=1)
    async def too_long() -> None: ...

    try:
        with pytest.raises(
            ImproperlyConfigured,
            match=r"'tests.too_long' may run 1s; .*VISIBILITY_TIMEOUT \(1s\)",
        ):
            Worker()
        Worker(MessagingSettings(**{**FAST, "VISIBILITY_TIMEOUT": 1.5}))
    finally:
        tasks._tasks.pop("tests.too_long")


@pytest.mark.usefixtures("tables")
async def test_tasks_see_their_attempt() -> None:
    """current_attempt() numbers the runs; outside a worker it is None."""
    await enqueue(attempts)

    await Worker().run_once()

    first, second = ran
    assert isinstance(first, Attempt)
    assert (first.number, first.max_attempts, first.last) == (1, 2, False)
    assert isinstance(second, Attempt)
    assert (second.number, second.last) == (2, True)
    assert first.message_id == second.message_id
    assert current_attempt() is None


# Priorities ---------------------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_higher_priorities_run_first() -> None:
    """Among due messages, higher priority first, then the oldest."""
    await enqueue(low, label="low-1")
    await enqueue(remember, value=uuid.uuid4(), label="default")
    await enqueue(high, label="high-1")
    await enqueue(low, label="low-2")
    await enqueue(high, label="high-2")

    worker = Worker(MessagingSettings(**{**FAST, "BATCH_SIZE": 1}))
    await worker.run_once()

    labels = [entry if isinstance(entry, str) else entry[1] for entry in ran]
    assert labels == ["high-1", "high-2", "default", "low-1", "low-2"]
    assert sorted(row["priority"] for row in await rows()) == [-10, -10, 0, 50, 50]


@pytest.mark.usefixtures("tables")
async def test_scheduled_runs_keep_their_priority() -> None:
    """Periodic and chained messages carry their task's priority."""

    @periodic(timedelta(hours=1), name="tests.urgent_hourly", priority=9)
    async def urgent_hourly() -> None: ...

    try:
        await Worker().schedule_periodic()
    finally:
        tasks._tasks.pop("tests.urgent_hourly")

    (row,) = [row for row in await rows() if row["target"] == "tests.urgent_hourly"]
    assert row["priority"] == 9


# Chains -------------------------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_chains_pass_results_from_step_to_step() -> None:
    """Each step runs after the previous one, with its result as arguments."""
    with bind_request(RequestContext("req-9", None, "POST", "/x", actor="carol")):
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
            uow.defer(chain(charge, receipt, archive, total), order=7)
            await uow.commit()

    (first,) = await rows()
    assert first["target"] == "tests.charge"
    assert first["headers"]["chain"] == [
        "tests.receipt",
        "tests.archive",
        "tests.total",
    ]

    await Worker().run_once()

    assert [entry[:3] for entry in ran] == [
        ("charge", 7, ran[0][2]),
        ("receipt", 7, "r-7"),
        ("archive", 7, "r-7"),
        ("total", 70),
    ]
    for context in (ran[0][2], ran[1][3]):
        assert (context.request_id, context.actor) == ("req-9", "carol")
    stored = {row["target"]: row for row in await rows()}
    assert {target: row["status"] for target, row in stored.items()} == {
        "tests.charge": DONE,
        "tests.receipt": DONE,
        "tests.archive": DONE,
        "tests.total": DONE,
    }
    second = stored["tests.receipt"]
    assert (second["queue"], second["priority"], second["max_attempts"]) == (
        "emails",
        7,
        2,
    )
    assert second["dedupe_key"] == f"chain:{first['id']}"
    # What each step was given is in ``ran``; once done, the rows keep none.
    assert [row["payload"] for row in stored.values()] == [{}] * len(stored)
    assert "chain" not in stored["tests.total"]["headers"]
    assert stored["tests.archive"]["headers"]["chain"] == ["tests.total"]


@pytest.mark.usefixtures("broker_tables")
async def test_a_workers_next_step_is_left_to_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a broker, the step a worker writes is published by the relay, not it."""
    from domainkit.contrib.messaging import dispatch

    published: list[list[uuid.UUID]] = []
    monkeypatch.setattr(
        dispatch,
        "publish_committed",
        lambda config, database, ids: published.append(ids),
    )
    await enqueue(chain(charge, receipt), order=7)
    published.clear()  # The enqueue's own commit publishes its first step.
    worker = Worker()

    (claimed,) = await worker.claim(1)
    await worker.process(claimed)

    assert published == []
    assert [row["target"] for row in await rows() if row["status"] == PENDING] == [
        "tests.receipt"
    ]


@pytest.mark.usefixtures("tables")
async def test_a_failed_step_holds_the_rest_back() -> None:
    """The next step is written only once the previous one succeeds."""
    await enqueue(chain(with_policy, remember), error="broken")
    await enqueue(chain(unstorable, remember))

    await Worker().run_once()

    by_target = {row["target"]: row for row in await rows()}
    assert set(by_target) == {"tests.policy", "tests.unstorable"}
    assert by_target["tests.policy"]["status"] == DEAD
    unstorable_row = by_target["tests.unstorable"]
    assert unstorable_row["status"] == DEAD  # A result JSON cannot hold fails it.
    assert str(unstorable_row["last_error"]).startswith("PydanticSerializationError")


@pytest.mark.usefixtures("tables")
async def test_a_step_whose_run_is_lost_queues_nothing() -> None:
    """If another worker took the message over, its next step is theirs to write."""
    await enqueue(chain(charge, receipt), order=1)
    worker = Worker()
    (claimed,) = await worker.claim(10)
    await set_rows(lock_id=uuid.uuid4())  # Reclaimed meanwhile.

    await worker.process(claimed)

    (row,) = await rows()
    assert row["status"] == PENDING


@pytest.mark.usefixtures("tables")
async def test_a_step_no_longer_registered_dead_letters() -> None:
    """A chain naming a task this process lacks queues it, to fail visibly."""
    row = message_row(
        kind="task",
        target="tests.charge",
        payload={"order": 3},
        headers={"chain": ["tests.gone"]},
        max_attempts=5,
    )
    event = message_row(
        kind="event",
        target="missing.handler",
        payload={},
        headers={"chain": ["tests.charge"]},
        max_attempts=1,
    )
    async with connections.engine().begin() as connection:
        await connection.execute(messages.insert().values([row, event]))

    await Worker().run_once()

    by_target = {r["target"]: r for r in await rows()}
    assert set(by_target) == {"tests.charge", "tests.gone", "missing.handler"}
    gone = by_target["tests.gone"]
    assert (gone["status"], gone["queue"], gone["max_attempts"], gone["priority"]) == (
        DEAD,
        "default",
        10,
        0,
    )
    assert gone["last_error"] == "UnknownTarget: No task 'tests.gone'."


async def test_chains_run_in_process_too() -> None:
    """Calling a chain runs its steps here, as in-memory units of work might."""
    ran.clear()
    await chain(charge, receipt, archive, total)(order=2)

    assert [entry[:3] for entry in ran][1:] == [
        ("receipt", 2, "r-2"),
        ("archive", 2, "r-2"),
        ("total", 20),
    ]


def test_chains_take_registered_tasks_only() -> None:
    """An unregistered step is refused when the chain is built."""

    async def stray() -> None: ...

    with pytest.raises(ImproperlyConfigured, match="is not a task"):
        chain(charge, stray)
    assert chain(charge).keys == ["tests.charge"]


# Rate limits --------------------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_rates_hold_runs_back_without_spending_attempts() -> None:
    """Past its rate, a task waits, one interval more for each held message."""
    for number in range(5):
        await enqueue(counted, number=number)
    before = REGISTRY.get_sample_value(
        "messaging_messages_total", {"kind": "task", "outcome": "throttled"}
    )

    assert await Worker().run_once() == 5

    assert len(ran) == 2  # Which two depends on the claim order.
    waiting = sorted(
        (row for row in await rows() if row["status"] == PENDING),
        key=lambda row: row["available_at"],
    )
    assert [(row["attempts"], row["started_at"]) for row in waiting] == [(0, None)] * 3
    now = datetime.now(UTC)
    delays = [(row["available_at"] - now).total_seconds() for row in waiting]
    assert 25 < delays[0] <= 30  # The next of two a minute frees in 30s.
    assert delays[1] - delays[0] == pytest.approx(30, abs=0.5)
    assert delays[2] - delays[1] == pytest.approx(30, abs=0.5)
    assert (
        REGISTRY.get_sample_value(
            "messaging_messages_total", {"kind": "task", "outcome": "throttled"}
        )
        == (before or 0) + 3
    )


class CountingBackend:
    """A rate limit backend that records its keys and refuses every other hit."""

    hits: ClassVar[list[tuple[str, int, float]]] = []

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Allow the first hit of a pair."""
        self.hits.append((key, limit, period))
        allowed = len(self.hits) % 2 == 1
        return Decision(allowed, 0, 1.0, 0.0 if allowed else 1.0)


@pytest.mark.usefixtures("tables")
async def test_rates_are_counted_by_the_configured_backend() -> None:
    """RATE_LIMIT_BACKEND counts; a shared one (Redis) limits every worker."""
    CountingBackend.hits.clear()
    with override_settings(
        RATE_LIMIT_BACKEND="domainkit_tests.test_messaging_policies.CountingBackend"
    ):
        await enqueue(counted, number=1)
        await enqueue(counted, number=2)
        worker = Worker(MessagingSettings(**{**FAST, "BATCH_SIZE": 1}))
        await worker.run_once()

    assert CountingBackend.hits == [("task:tests.counted", 2, 60)] * 2
    assert ran == [1]


# Cron schedules in the worker ---------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_cron_runs_are_queued_ahead_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The next run is queued before it is due; workers share one row per run."""
    clock = [datetime(2026, 10, 19, 23, 59, 30, tzinfo=UTC)]
    monkeypatch.setattr(worker_module, "_now", lambda: clock[0])

    @periodic(cron="0 3 * * 1-5", tz="Africa/Cairo", name="tests.cairo")
    async def cairo() -> None:
        ran.append("cairo")

    try:
        first = Worker()
        await first.schedule_periodic()
        await first.schedule_periodic()  # Already queued: nothing new.
        await Worker().schedule_periodic()  # Another worker: the same row.
        (queued,) = await rows()
        run_at = datetime(2026, 10, 20, 0, tzinfo=UTC)  # 03:00 in Cairo.
        assert (queued["target"], queued["available_at"]) == ("tests.cairo", run_at)
        assert queued["dedupe_key"] == f"cron:tests.cairo:{int(run_at.timestamp())}"
        assert queued["headers"] == {}

        clock[0] = run_at
        await first.schedule_periodic()  # Due now: the next one is queued.
    finally:
        tasks._tasks.pop("tests.cairo")

    assert [row["available_at"] for row in await rows()] == [
        run_at,
        datetime(2026, 10, 21, 0, tzinfo=UTC),
    ]


@pytest.mark.usefixtures("tables")
async def test_cron_runs_survive_a_failed_write_and_a_schedule_without_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write is retried next round; no next run means nothing queued."""

    @periodic(cron="*/5 * * * *", name="tests.five")
    async def five() -> None: ...

    worker = Worker()
    original = worker_module.write  # type: ignore[attr-defined]
    failures = [ConnectionError("down")]

    async def flaky_write(*args: Any) -> None:  # noqa: ANN401
        if failures:
            raise failures.pop()
        await original(*args)

    monkeypatch.setattr(worker_module, "write", flaky_write)
    try:
        with pytest.raises(ConnectionError):
            await worker.schedule_periodic()
        await worker.schedule_periodic()
        assert len(await rows()) == 1
        monkeypatch.setattr(Cron, "next_after", lambda self, instant: None)
        await Worker().schedule_periodic()
    finally:
        tasks._tasks.pop("tests.five")
    assert len(await rows()) == 1


# Inspecting and cancelling ------------------------------------------------------


@pytest.mark.usefixtures("tables")
async def test_messages_are_listed_by_state() -> None:
    """Waiting by priority, locked ones by lock, dead ones newest first."""
    config = MessagingSettings(**FAST)
    await enqueue(low, label="a")
    await enqueue(high, label="b")
    await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
    await enqueue(counted, number=1)
    worker = Worker()
    (running,) = await worker.claim(1)  # The high-priority one.
    await worker.relay(MemoryBroker(), 1)  # Publishes the next due one: counted.

    pending = await outbox_admin.list_messages(config, "pending", 10)
    locked = await outbox_admin.list_messages(config, "locked", 10)
    now = datetime.now(UTC)

    assert [(r.target, outbox_admin.state_of(r, now)) for r in pending] == [
        ("tests.low", "due"),
        (tasks.qualified_name(remember), "scheduled"),
    ]
    assert [(r.target, outbox_admin.state_of(r, now)) for r in locked] == [
        ("tests.high", "running"),  # Its claim ends long before the publication.
        ("tests.counted", "published"),
    ]
    assert running.target == "tests.high"
    assert await outbox_admin.list_messages(config, "pending", 1, queue="nope") == []
    assert len(await outbox_admin.list_messages(config, "pending", 1)) == 1
    assert await outbox_admin.list_messages(config, "dead", 10) == []
    assert await outbox_admin.dead_letters(config, 10) == []


@pytest.mark.usefixtures("tables")
async def test_waiting_messages_can_be_cancelled_running_ones_not() -> None:
    """Cancel takes a waiting or published message, never a running one."""
    config = MessagingSettings(**FAST)
    for label in ("run", "wait", "publish"):
        await enqueue(high if label == "run" else low, label=label)
    await enqueue(chain(charge, receipt), order=1, delay=timedelta(hours=1))
    worker = Worker()
    (claimed,) = await worker.claim(1)
    broker = MemoryBroker()
    await worker.relay(broker, 1)
    by_label = {row["payload"].get("label", "chain"): row for row in await rows()}

    assert not await outbox_admin.cancel(config, claimed.id)
    for label in ("wait", "publish", "chain"):
        assert await outbox_admin.cancel(config, by_label[label]["id"])
    assert not await outbox_admin.cancel(config, by_label["wait"]["id"])
    assert not await outbox_admin.cancel(config, uuid.uuid4())

    await worker.process(claimed)
    assert not await outbox_admin.cancel(config, claimed.id)  # Done.
    await set_rows(available_at=datetime.now(UTC) - timedelta(seconds=1))
    assert await Worker().run_once() == 0  # Nothing cancelled runs.
    body, _ = broker._queues["default"].popleft()
    answers: list[str] = []
    consumer = BrokerWorkers(
        [Worker()], ["default"], MessagingSettings(**{**FAST, "BROKER": "x"})
    )
    await consumer.deliver(delivery(json.loads(body), answers=answers))
    labels = {row["id"]: label for label, row in by_label.items()}
    labels[claimed.id] = "run"
    finished = {labels[row["id"]]: row for row in await rows()}
    assert {label: row["status"] for label, row in finished.items()} == {
        "run": DONE,
        "wait": CANCELLED,
        "publish": CANCELLED,
        "chain": CANCELLED,
    }
    assert answers == ["ack"]
    assert [entry for entry in ran if isinstance(entry, str)] == ["run"]
    assert finished["wait"]["last_error"] == "Cancelled by an operator."
    assert finished["wait"]["finished_at"] is not None
    assert {row["payload"] == {} for row in finished.values()} == {True}


@pytest.mark.usefixtures("tables")
async def test_lag_is_the_age_of_the_oldest_waiting_due_message() -> None:
    """Scheduled and running messages do not count; published ones do."""
    config = MessagingSettings(**FAST)
    assert await outbox_admin.queue_lag(config) == {}
    await enqueue(low, label="old")
    await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
    await enqueue(counted, number=1)  # Another queue below.
    await set_rows(available_at=datetime.now(UTC) - timedelta(seconds=90))
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages)
            .where(messages.c.target == tasks.qualified_name(remember))
            .values(available_at=datetime.now(UTC) + timedelta(hours=1))
        )
        await connection.execute(
            update(messages)
            .where(messages.c.target == "tests.counted")
            .values(
                queue="other", available_at=datetime.now(UTC) - timedelta(seconds=30)
            )
        )

    lag = await outbox_admin.queue_lag(config)
    assert set(lag) == {"default", "other"}
    assert 90 <= lag["default"] < 100
    assert 30 <= lag["other"] < 40

    await Worker(queues=["default"]).claim(10)  # Running: no longer waiting.
    assert set(await outbox_admin.queue_lag(config)) == {"other"}
    await Worker(queues=["other"]).relay(MemoryBroker(), 10)  # Published: waits.
    assert set(await outbox_admin.queue_lag(config)) == {"other"}


@pytest.mark.usefixtures("tables")
async def test_metrics_show_lag_and_cancelled_messages() -> None:
    """The gauges cover every status and every known queue."""
    await enqueue(low, label="x")
    (row,) = await rows()
    await outbox_admin.cancel(MessagingSettings(**FAST), row["id"])
    await enqueue(low, label="y")
    await set_rows(available_at=datetime.now(UTC) - timedelta(seconds=5))

    await refresh_outbox()

    sample = REGISTRY.get_sample_value
    assert sample("messaging_outbox_messages", {"status": "cancelled"}) == 1
    assert sample("messaging_outbox_messages", {"status": "pending"}) == 1
    assert 5 <= (sample("messaging_outbox_lag_seconds", {"queue": "default"}) or 0) < 15
    assert sample("messaging_outbox_lag_seconds", {"queue": "emails"}) == 0


@pytest.mark.usefixtures("tables")
async def test_cancelled_and_done_messages_are_cleaned_up() -> None:
    """Cleanup drops old finished messages, cancelled ones included; not dead ones."""
    await enqueue(low, label="a")
    await enqueue(low, label="b")
    first, second = await rows()
    config = MessagingSettings(**FAST)
    await outbox_admin.cancel(config, first["id"])
    await Worker().run_once()
    await set_rows(finished_at=datetime.now(UTC) - timedelta(days=30))

    assert await Worker().cleanup() == 2
    assert await rows() == []
    assert second["id"] != first["id"]


@pytest.mark.usefixtures("tables")
async def test_retried_dead_letters_wait_again() -> None:
    """A dead letter queued again has no start time and fresh attempts."""
    await enqueue(with_policy, error="broken")
    await Worker().run_once()
    config = MessagingSettings(**FAST)

    assert await outbox_admin.retry_dead(config, None) == 1

    (row,) = await rows()
    assert (row["status"], row["attempts"], row["started_at"]) == (PENDING, 0, None)


@pytest.mark.usefixtures("messaging")
def test_the_messages_command_lists_and_cancels() -> None:
    """--pending, --locked, --dead, --queue, and --cancel."""

    async def prepare() -> list[uuid.UUID]:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        await enqueue(low, label="a")
        await enqueue(high, label="b")
        await enqueue(with_policy, error="broken")
        # A lease that outlasts the commands below on any machine: --locked
        # must still see it, whatever the commands cost.
        worker = Worker(MessagingSettings(**{**FAST, "VISIBILITY_TIMEOUT": 60}))
        (claimed,) = await worker.claim(1)
        ids = [claimed.id]
        await set_rows(available_at=datetime.now(UTC) - timedelta(seconds=3))
        async with connections.engine().connect() as connection:
            ids += [row.id for row in await connection.execute(messages.select())]
        await connections.close_all()
        return ids

    running, *_ = asyncio.run(prepare())

    pending = run("messages", "--pending").splitlines()
    assert len(pending) == 2
    assert "tests.policy" in pending[0]  # Priority 0 before -10.
    assert pending[1].split()[1:5] == [
        "task",
        "tests.low",
        "queue=default",
        "priority=-10",
    ]
    assert pending[1].split()[5:7] == ["x0/10", "due"]
    assert run("messages", "--pending", "--queue", "emails") == ""
    assert run("messages", "--pending", "--limit", "1").count("\n") == 1
    (locked,) = run("messages", "--locked").splitlines()
    assert locked.startswith(f"{running}  task tests.high")
    assert " running " in locked
    assert run("messages", "--dead") == ""
    counts = run("messages").splitlines()
    assert counts[:5] == [
        "pending  3",
        "held     0",
        "done     0",
        "dead     0",
        "cancelled 0",
    ]
    assert counts[5].startswith("lag default: ")
    with pytest.raises(CommandError, match="--limit must be 1 or more"):
        run("messages", "--dead", "--limit", "0")
    with pytest.raises(SystemExit):
        run("messages", "--dead", "--pending")

    waiting = pending[1].split()[0]
    assert run("messages", "--cancel", waiting) == f"Cancelled {waiting}.\n"
    with pytest.raises(CommandError, match="was not cancelled: it is running"):
        run("messages", "--cancel", str(running))
    with pytest.raises(CommandError, match=r"'nope' is not a message id\.$"):
        run("messages", "--cancel", "nope")
    with pytest.raises(CommandError, match="'x' is not a message id or 'all'"):
        run("messages", "--retry", "x")
    assert "cancelled 1" in run("messages")


def test_settings_bound_the_retry_delays() -> None:
    """Delays past a year are refused in MESSAGING too."""
    with pytest.raises(ValueError, match="less than or equal to 31536000"):
        MessagingSettings(RETRY_MAX_SECONDS=10**9)
    assert RetryBackoff is Backoff
