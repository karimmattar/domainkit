"""The outbox, durable handlers, background tasks, and the worker."""

import asyncio
import io
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    enqueue,
    periodic,
    task,
)
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.outbox import insert_statements, message_row
from domainkit.contrib.messaging.tables import DEAD, DONE, PENDING, messages
from domainkit.contrib.messaging.worker import Claimed, backoff
from domainkit.db import connections
from domainkit.ddd import event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError, call_command
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, NoteWritten, notes

pytestmark = pytest.mark.anyio

ALPHA = "domainkit_tests.sample_domains.alpha"
MESSAGING = "domainkit.contrib.messaging"
FAST = {
    "RETRY_BASE_SECONDS": 0.001,
    "RETRY_MAX_SECONDS": 0.001,
    "TASK_TIMEOUT": 0.2,
    "VISIBILITY_TIMEOUT": 1,
}

ran: list[Any] = []
reported: list[tuple[str, Mapping[str, object]]] = []


def reporter(exc: BaseException, context: Mapping[str, object]) -> None:
    """Collect reported errors."""
    reported.append((str(exc), context))


@task
async def remember(value: uuid.UUID, label: str = "") -> None:
    """Record the call, with arguments converted back from JSON."""
    ran.append((value, label, current_request()))


@task(name="tests.always_fails", max_attempts=3)
async def always_fails() -> None:
    """Fail every time."""
    raise RuntimeError("still broken")


@task
async def fails_once(key: str) -> None:
    """Fail on the first run only."""
    if key not in ran:
        ran.append(key)
        raise RuntimeError("first run fails")
    ran.append(f"{key}:ok")


@task(max_attempts=1)
async def too_slow() -> None:
    """Outlive TASK_TIMEOUT."""
    await asyncio.sleep(1)


async def durable_handler(event: NoteWritten) -> None:
    """Record the event and who caused it."""
    request = current_request()
    ran.append((event, request.actor if request else None))


@pytest.fixture(autouse=True)
def immediate_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retries due at once, so tests never race the backoff clock."""
    monkeypatch.setattr(worker_module, "backoff", lambda attempts, config: 0.0)


@pytest.fixture
def messaging(database_url: str) -> Iterator[None]:
    """Install messaging on an empty database."""
    ran.clear()
    reported.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MESSAGING=FAST,
        ERROR_REPORTERS=["domainkit_tests.test_messaging.reporter"],
    ):
        setup()
        yield
    registry.clear()


@pytest.fixture
async def tables(messaging: None) -> AsyncIterator[None]:
    """Create the outbox and notes tables in this loop's engine."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
        await connection.run_sync(notes.create)
    yield
    await connections.close_all()


async def outbox() -> list[dict[str, object]]:
    """Return every outbox row."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(
            select(messages).order_by(messages.c.created_at)
        )
        return [dict(row._mapping) for row in rows]


async def test_deferred_tasks_commit_with_the_change(tables: None) -> None:
    """A deferred task is stored by the commit and run by the worker."""
    value = uuid.uuid4()
    with bind_request(RequestContext("req-1", None, "POST", "/x", actor="alice")):
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
            uow.defer(remember, value=value, label="one")
            await uow.commit()
    (row,) = await outbox()
    assert (row["status"], row["kind"], row["headers"]) == (
        PENDING,
        "task",
        {"request_id": "req-1", "actor": "alice"},
    )
    assert ran == []

    assert await Worker().run_once() == 1

    ((seen, label, context),) = ran
    assert (seen, label) == (value, "one")
    assert isinstance(seen, uuid.UUID)
    assert context is not None
    assert (context.request_id, context.actor, context.method) == (
        "req-1",
        "alice",
        "TASK",
    )
    assert (await outbox())[0]["status"] == DONE


async def test_rolled_back_work_queues_nothing(tables: None) -> None:
    """Leaving without commit, or rolling back, drops deferred tasks."""
    async with NotesUnitOfWork() as uow:
        uow.defer(remember, value=uuid.uuid4())
    async with NotesUnitOfWork() as uow:
        uow.defer(remember, value=uuid.uuid4())
        await uow.rollback()
        await uow.commit()

    assert await outbox() == []


async def test_durable_handlers_run_in_the_worker(tables: None) -> None:
    """Durable handlers skip the request and run later with the actor."""
    event_bus.subscribe(NoteWritten, durable_handler, durable=True)
    try:
        note = Note(id=NoteId(uuid.uuid4()), text="t")
        note.record(NoteWritten(note_id=note.id))
        with bind_request(RequestContext("req-2", None, "POST", "/x", actor="bob")):
            async with NotesUnitOfWork() as uow:
                await uow.notes.add(note)
                await uow.commit()
        assert ran == []

        await Worker().run_once()

        ((event, actor),) = ran
        assert isinstance(event, NoteWritten)
        assert (event.note_id, actor) == (note.id, "bob")
    finally:
        event_bus.unsubscribe(NoteWritten, durable_handler)


async def test_failures_retry_then_dead_letter(
    tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Retries use backoff; the last failure dead-letters, logs, and reports."""
    await enqueue(always_fails)

    await Worker().run_once()

    (row,) = await outbox()
    assert (row["status"], row["attempts"]) == (DEAD, 3)
    assert row["last_error"] == "RuntimeError: still broken"
    assert row["finished_at"] is not None
    dead = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [(r.getMessage(), bool(r.exc_info)) for r in dead] == [
        ("Dead-lettered task tests.always_fails after 3 attempts", True)
    ]
    assert reported == [
        ("still broken", {"message_id": str(row["id"]), "target": "tests.always_fails"})
    ]


async def test_a_retry_can_succeed(tables: None) -> None:
    """A task that fails once finishes on its second attempt."""
    await enqueue(fails_once, key="k")

    await Worker().run_once()

    (row,) = await outbox()
    assert (row["status"], row["attempts"], ran) == (DONE, 2, ["k", "k:ok"])
    assert row["finished_at"] is not None


async def test_retries_wait_for_their_backoff(
    tables: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed attempt is pending again after backoff(attempts), with its error."""
    asked: list[tuple[int, MessagingSettings]] = []

    def minute(attempts: int, config: MessagingSettings) -> float:
        asked.append((attempts, config))
        return 60.0

    monkeypatch.setattr(worker_module, "backoff", minute)
    await enqueue(always_fails)
    before = datetime.now(UTC)

    assert await Worker().run_once() == 1

    (row,) = await outbox()
    assert (row["status"], row["attempts"], row["finished_at"]) == (PENDING, 1, None)
    assert row["last_error"] == "RuntimeError: still broken"
    assert [(attempts, type(config)) for attempts, config in asked] == [
        (1, MessagingSettings)
    ]
    available_at = row["available_at"]
    assert isinstance(available_at, datetime)
    assert before + timedelta(seconds=60) <= available_at
    assert available_at <= datetime.now(UTC) + timedelta(seconds=60)
    (record,) = [r for r in caplog.records if r.exc_info]
    assert record.getMessage() == (
        "task tests.always_fails failed (attempt 1 of 3); retrying in 60.0s"
    )


async def test_slow_tasks_time_out(tables: None) -> None:
    """Running past TASK_TIMEOUT counts as a failure."""
    with override_settings(MESSAGING={**FAST, "TASK_TIMEOUT": 0.05}):
        await enqueue(too_slow)
        await Worker(MessagingSettings(**{**FAST, "TASK_TIMEOUT": 0.05})).run_once()

    (row,) = await outbox()
    assert row["status"] == DEAD
    assert str(row["last_error"]).startswith("TimeoutError")


@pytest.mark.parametrize(
    ("kind", "target", "headers"),
    [
        ("task", "missing.task", {}),
        ("event", "missing.handler", {"event_type": "x"}),
        (
            "event",
            "domainkit_tests.test_messaging.durable_handler",
            {"event_type": "x"},
        ),
        ("letter", "anything", {}),
    ],
)
async def test_unknown_targets_dead_letter_at_once(
    tables: None, kind: str, target: str, headers: dict[str, str]
) -> None:
    """Messages this process cannot run are not retried."""
    row = message_row(
        kind=kind, target=target, payload={}, headers=headers, max_attempts=5
    )
    async with connections.engine().begin() as connection:
        await connection.execute(messages.insert().values(row))

    await Worker().run_once()

    (stored,) = await outbox()
    assert (stored["status"], stored["attempts"]) == (DEAD, 1)


async def test_dedupe_keys_and_delays(tables: None) -> None:
    """A dedupe key is queued once; a delayed task waits for its time."""
    for _ in range(2):
        await enqueue(remember, dedupe_key="once", value=uuid.uuid4())
    await enqueue(remember, delay=timedelta(hours=1), value=uuid.uuid4())

    assert await Worker().run_once() == 1
    assert [row["status"] for row in await outbox()] == [DONE, PENDING]


async def test_periodic_tasks_run_once_per_interval(tables: None) -> None:
    """Every worker schedules the slot; only one message exists per slot."""

    @periodic(every=timedelta(hours=1), name="tests.hourly")
    async def hourly() -> None:
        ran.append("hourly")

    worker = Worker()
    try:
        await worker.run_once()
        await worker.run_once()
        await Worker().run_once()
    finally:
        tasks._tasks.pop("tests.hourly")

    (row,) = await outbox()
    assert (row["target"], row["status"], ran) == ("tests.hourly", DONE, ["hourly"])
    assert str(row["dedupe_key"]).startswith("periodic:tests.hourly:")


async def test_each_periodic_task_has_its_own_slot(tables: None) -> None:
    """A task added later is still scheduled; each slot starts on its boundary."""
    intervals = {"tests.hourly": 3600, "tests.ten_minutes": 600}

    @periodic(every=timedelta(hours=1), name="tests.hourly")
    async def hourly() -> None:
        ran.append("hourly")

    worker = Worker()
    try:
        await worker.run_once()

        @periodic(every=timedelta(minutes=10), name="tests.ten_minutes")
        async def ten_minutes() -> None:
            ran.append("ten_minutes")

        await worker.run_once()
    finally:
        for name in intervals:
            tasks._tasks.pop(name)

    rows = await outbox()
    assert sorted(ran) == ["hourly", "ten_minutes"]
    for row in rows:
        slot = int(str(row["dedupe_key"]).rsplit(":", 1)[1])
        seconds = intervals[str(row["target"])]
        assert row["available_at"] == datetime.fromtimestamp(slot * seconds, UTC)


async def test_reclaim_after_visibility_timeout(tables: None) -> None:
    """A crashed worker's message returns; its late release changes nothing."""
    await enqueue(remember, value=uuid.uuid4())
    first = Worker()
    (claimed,) = await first.claim(10)
    assert await Worker().claim(10) == []

    async with connections.engine().begin() as connection:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await connection.execute(update(messages).values(locked_until=past))
    (reclaimed,) = await Worker().claim(10)
    await first._release(claimed, status=DONE)

    (row,) = await outbox()
    assert (row["status"], row["attempts"]) == (PENDING, 2)
    assert row["lock_id"] == reclaimed.lock_id


async def test_cleanup_and_run_loop(tables: None) -> None:
    """The loop processes work until stopped; cleanup drops old finished rows."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker(
        MessagingSettings(**{**FAST, "POLL_INTERVAL": 0.01, "CONCURRENCY": 1})
    )
    stop = asyncio.Event()

    original = worker.process

    async def process_then_stop(message: Claimed) -> None:
        await original(message)
        stop.set()

    worker.process = process_then_stop  # type: ignore[method-assign]
    await worker.run(stop)
    assert len(ran) == 1

    async with connections.engine().begin() as connection:
        old = datetime.now(UTC) - timedelta(days=30)
        await connection.execute(update(messages).values(finished_at=old))
    assert await worker.cleanup() == 1

    stop.set()
    await worker.run(stop)


async def test_a_busy_worker_never_waits_for_the_poll(tables: None) -> None:
    """With every slot taken, a finished message frees one at once, not a poll later."""
    for _ in range(5):
        await enqueue(remember, value=uuid.uuid4())
    worker = Worker(
        MessagingSettings(**{**FAST, "POLL_INTERVAL": 30, "CONCURRENCY": 1})
    )
    stop, all_ran = asyncio.Event(), asyncio.Event()
    original = worker.process

    async def process_and_count(message: Claimed) -> None:
        await original(message)
        if len(ran) == 5:
            all_ran.set()

    worker.process = process_and_count  # type: ignore[method-assign]
    running = asyncio.create_task(worker.run(stop))
    try:
        async with asyncio.timeout(5):
            await all_ran.wait()
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await running

    assert len(ran) == 5


async def test_stopping_lets_running_work_finish(tables: None) -> None:
    """Claims stay within BATCH_SIZE; idle workers poll; stop drains running work."""
    started, release = asyncio.Event(), asyncio.Event()

    @task(name="tests.blocks")
    async def blocks() -> None:
        started.set()
        await release.wait()
        ran.append("finished")

    await enqueue(blocks)
    worker = Worker(
        MessagingSettings(
            **{**FAST, "POLL_INTERVAL": 0.01, "BATCH_SIZE": 1, "CONCURRENCY": 3}
        )
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    async with asyncio.timeout(5):
        await started.wait()
        await asyncio.sleep(0.05)  # Poll at least once while the task runs.
        stop.set()
        asyncio.get_running_loop().call_later(0.05, release.set)
        await running

    assert ran == ["finished"]


async def test_configuration_guards(tables: None) -> None:
    """Unregistered tasks and unsupported dialects are refused."""

    async def not_a_task() -> None: ...

    async with NotesUnitOfWork() as uow:
        uow.defer(not_a_task)
        with pytest.raises(ImproperlyConfigured, match="decorate it with @task"):
            await uow.commit()

    with pytest.raises(ImproperlyConfigured, match="PostgreSQL or SQLite"):
        insert_statements(
            "mysql",
            [
                message_row(
                    kind="task",
                    target="t",
                    payload={},
                    headers={},
                    max_attempts=1,
                    dedupe_key="k",
                )
            ],
        )


async def test_durable_work_needs_messaging_installed(tmp_path: Path) -> None:
    """Without the messaging app a deferred task fails loudly at commit."""
    registry.clear()
    url = f"sqlite+aiosqlite:///{tmp_path / 'plain.sqlite3'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        setup()
        async with connections.engine().begin() as connection:
            await connection.run_sync(notes.create)
        async with NotesUnitOfWork() as uow:
            uow.defer(remember, value=uuid.uuid4())
            with pytest.raises(ImproperlyConfigured, match=r"need 'domainkit\.contrib"):
                await uow.commit()
        await connections.close_all()
    registry.clear()


async def test_registry_rules() -> None:
    """Task keys are unique; periodic intervals are positive."""
    with pytest.raises(ImproperlyConfigured, match="Two tasks"):

        @task(name="tests.always_fails")
        async def impostor() -> None: ...

    with pytest.raises(ImproperlyConfigured, match="positive interval"):
        periodic(every=timedelta(0))

    assert tasks.get_task("nope") is None
    assert tasks.get_task("tests.always_fails") is not None


def test_backoff_doubles_with_jitter() -> None:
    """Delays double per attempt, stay under the cap, and are jittered."""
    config = MessagingSettings(RETRY_BASE_SECONDS=1, RETRY_MAX_SECONDS=10)

    assert 0.5 <= backoff(1, config) <= 1
    assert 4 <= backoff(4, config) <= 8
    assert 5 <= backoff(20, config) <= 10
    with pytest.raises(ValueError, match="VISIBILITY_TIMEOUT"):
        MessagingSettings(TASK_TIMEOUT=10, VISIBILITY_TIMEOUT=5)


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return its output."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


def test_commands(
    messaging: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``runworker`` and ``messages`` commands."""

    async def prepare() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        await enqueue(always_fails)
        await enqueue(remember, value=uuid.uuid4())
        await connections.close_all()

    asyncio.run(prepare())

    # One success and three attempts of the failing task.
    assert run("runworker", "--once") == "Processed 4 message(s).\n"
    assert run("messages") == "pending  0\ndone     1\ndead     1\n"
    (dead_line,) = run("messages", "--dead").splitlines()
    assert "tests.always_fails  x3  RuntimeError: still broken" in dead_line
    dead_id = dead_line.split()[0]
    assert run("messages", "--retry", dead_id) == "Queued 1 message(s).\n"
    assert run("messages", "--retry", "all") == "Queued 0 message(s).\n"
    with pytest.raises(CommandError, match="not a message id"):
        run("messages", "--retry", "x")

    async def stopped_at_once(self: Worker, stop: asyncio.Event) -> None:
        return None

    monkeypatch.setattr(Worker, "run", stopped_at_once)
    assert (
        run("runworker") == "Worker started; press Ctrl+C to stop.\nWorker stopped.\n"
    )


def test_contrib_periodic_tasks_follow_their_app(messaging: None) -> None:
    """A framework app's periodic task is scheduled only while it is installed."""

    async def purge() -> None: ...

    purge.__module__ = "domainkit.contrib.idempotency.tasks"
    tasks._register(
        tasks.TaskSpec("tests.contrib_purge", purge, None, timedelta(hours=1))
    )

    assert "tests.contrib_purge" not in [spec.key for spec, _ in tasks.periodic_tasks()]
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, "domainkit.contrib.idempotency", ALPHA]
    ):
        setup()
        keys = [spec.key for spec, _ in tasks.periodic_tasks()]
    assert "tests.contrib_purge" in keys
