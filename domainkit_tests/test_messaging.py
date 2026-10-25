"""The outbox, durable handlers, background tasks, and the worker."""

import asyncio
import io
import json
import logging
import os
import re
import signal
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Final

import pytest
from prometheus_client import REGISTRY
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import insert, select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.broker import (
    BrokerHandler,
    BrokerRefused,
    BrokerUnavailable,
    Delivery,
    MemoryBroker,
    brokers,
)
from domainkit.conf import UNSET, override_settings
from domainkit.context import (
    SYSTEM_ACTOR,
    RequestContext,
    bind_request,
    current_request,
)
from domainkit.contrib.audit.tables import audit_keys, audit_log, audit_seals
from domainkit.contrib.messaging import (
    MessagingSettings,
    Worker,
    admin,
    chain,
    dispatch,
    enqueue,
    periodic,
    task,
)
from domainkit.contrib.messaging import outbox as outbox_module
from domainkit.contrib.messaging import registry as tasks
from domainkit.contrib.messaging import worker as worker_module
from domainkit.contrib.messaging.dispatch import (
    BrokerWorkers,
    CommitPublisher,
    close_publishers,
    commit_publisher,
)
from domainkit.contrib.messaging.outbox import insert_statements, message_row
from domainkit.contrib.messaging.tables import (
    BLANKED,
    CANCELLED,
    DEAD,
    DONE,
    PENDING,
    SENSITIVE,
    messages,
)
from domainkit.contrib.messaging.worker import Claimed, backoff
from domainkit.db import connections
from domainkit.ddd import DomainEvent, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import CommandError, call_command
from domainkit.reporting import flush_reports
from domainkit_tests.sample_domains.alpha.events import GreetingSent
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


@task(name="tests.sends_a_code", max_attempts=1, sensitive=True)
async def sends_a_code(to: str, code: str = "") -> None:
    """Fail to send a one-time code, which must not stay in the outbox."""
    raise RuntimeError("gateway down")


@task(name="tests.sends_a_code_once", max_attempts=1, sensitive=True)
async def sends_a_code_once(to: str, code: str) -> None:
    """Send a one-time code; it works, and hands its arguments on."""
    ran.append(("sent", to))


@task(name="tests.notes_it", max_attempts=1)
async def notes_it(to: str, code: str) -> None:
    """A step after the sensitive one, handed the same arguments; it works."""
    ran.append(("noted", to))


@task(name="tests.files_it", max_attempts=1)
async def files_it(to: str, code: str) -> None:
    """The step after that, handed them again; it fails."""
    raise RuntimeError("bookkeeping down")


@task(queue="emails")
async def send_email(to: str) -> None:
    """A task on its own queue."""
    ran.append(("email", to))


@task(max_attempts=1)
async def too_slow() -> None:
    """Outlive TASK_TIMEOUT."""
    await asyncio.sleep(1)


async def durable_handler(event: NoteWritten) -> None:
    """Record the event and who caused it."""
    request = current_request()
    ran.append((event, request.actor if request else None))


async def any_event_handler(event: DomainEvent) -> None:
    """Record any event it is subscribed to."""
    ran.append((event, None))


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
    headers = dict(row["headers"])  # type: ignore[call-overload]
    headers.pop("traceparent", None)  # When another test set up tracing.
    assert (row["status"], row["kind"], headers) == (
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
    flush_reports()  # Reporters run on their own thread.
    assert reported == [
        ("still broken", {"message_id": str(row["id"]), "target": "tests.always_fails"})
    ]


async def test_invalid_arguments_are_reported_without_their_values(
    tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The error kept, logged, and reported names the field, never its value."""
    await enqueue(remember, value="ann@example.com")

    await Worker().run_once()

    (row,) = await outbox()
    assert row["status"] == DEAD
    assert str(row["last_error"]).startswith(
        "InvalidData: remember: value: Input should be a valid UUID"
    )
    dead = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [type(r.exc_info[1]).__name__ for r in dead if r.exc_info] == ["InvalidData"]
    assert dead[0].exc_info is not None
    assert dead[0].exc_info[2] is not None  # Where it failed is kept.
    flush_reports()  # Reporters run on their own thread.
    ((text, _),) = reported
    assert text.startswith("remember: value: Input should be a valid UUID")
    for kept in (str(row["last_error"]), caplog.text, text):
        assert "ann@example.com" not in kept


def test_only_validation_errors_are_rewritten() -> None:
    """Other errors pass as they are; nested locations are joined with dots."""
    other = RuntimeError("still broken")

    class Order(BaseModel):
        lines: list[int]
        note: str

    with pytest.raises(ValidationError) as caught:
        Order.model_validate({"lines": [1, "two"]})
    with pytest.raises(ValidationError) as whole:
        TypeAdapter(int).validate_python("three")

    assert worker_module._without_input(other) is other
    assert str(worker_module._without_input(caught.value)) == (
        "Order: lines.1: Input should be a valid integer, unable to parse string "
        "as an integer; note: Field required"
    )
    assert str(worker_module._without_input(whole.value)) == (
        "int: (value): Input should be a valid integer, unable to parse string "
        "as an integer"
    )


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


HANDLER = "domainkit_tests.test_messaging.durable_handler"
NOTE_WRITTEN = "domainkit_tests.test_db.NoteWritten"


@pytest.mark.parametrize(
    ("kind", "target", "headers", "error"),
    [
        ("task", "missing.task", {}, "No task 'missing.task'."),
        (
            "event",
            "missing.handler",
            {"event_type": "x"},
            "No durable handler 'missing.handler'.",
        ),
        (
            "event",
            "missing.handler",
            {"event_type": NOTE_WRITTEN},
            "No durable handler 'missing.handler'.",
        ),
        ("event", HANDLER, {"event_type": "x"}, f"No durable handler {HANDLER!r}."),
        ("event", HANDLER, {}, f"No durable handler {HANDLER!r}."),
        ("letter", "anything", {}, "Unknown message kind 'letter'."),
    ],
)
async def test_unknown_targets_dead_letter_at_once(
    tables: None, kind: str, target: str, headers: dict[str, str], error: str
) -> None:
    """Messages this process cannot run are not retried."""
    row = message_row(
        kind=kind, target=target, payload={}, headers=headers, max_attempts=5
    )
    async with connections.engine().begin() as connection:
        await connection.execute(messages.insert().values(row))

    event_bus.subscribe(NoteWritten, durable_handler, durable=True)
    try:
        await Worker().run_once()
    finally:
        event_bus.unsubscribe(NoteWritten, durable_handler)

    (stored,) = await outbox()
    assert (stored["status"], stored["attempts"], stored["last_error"]) == (
        DEAD,
        1,
        f"UnknownTarget: {error}",
    )


async def test_any_event_handlers_run_only_for_their_event_types(
    tables: None,
) -> None:
    """A row pairing a handler with another event dies; each subscribed type runs."""
    greeting = GreetingSent(to="ann")
    written = NoteWritten(note_id=NoteId(uuid.uuid4()))

    async def queue(event: DomainEvent) -> None:
        row = message_row(
            kind="event",
            target=tasks.qualified_name(any_event_handler),
            payload=tasks.dump_event(event),
            headers={"event_type": tasks.qualified_name(type(event))},
            max_attempts=5,
        )
        async with connections.engine().begin() as connection:
            await connection.execute(messages.insert().values(row))

    event_bus.subscribe(NoteWritten, any_event_handler, durable=True)
    try:
        await queue(greeting)
        await Worker().run_once()
        (refused,) = await outbox()
        event_bus.subscribe(GreetingSent, any_event_handler, durable=True)
        await queue(greeting)
        await queue(written)
        await Worker().run_once()
    finally:
        event_bus.unsubscribe(NoteWritten, any_event_handler)
        event_bus.unsubscribe(GreetingSent, any_event_handler)

    assert (refused["status"], refused["attempts"], refused["last_error"]) == (
        DEAD,
        1,
        (
            "UnknownTarget: 'domainkit_tests.test_messaging.any_event_handler' does "
            "not handle GreetingSent."
        ),
    )
    assert sorted(type(event).__name__ for event, _ in ran) == [
        "GreetingSent",
        "NoteWritten",
    ]


async def test_dedupe_keys_and_delays(tables: None) -> None:
    """A dedupe key is queued once; a delayed task waits for its time."""
    for _ in range(2):
        await enqueue(remember, dedupe_key="once", value=uuid.uuid4())
    await enqueue(remember, delay=timedelta(hours=1), value=uuid.uuid4())

    assert await Worker().run_once() == 1
    assert [row["status"] for row in await outbox()] == [DONE, PENDING]


async def test_work_no_request_queued_runs_as_the_system(tables: None) -> None:
    """Commands' and periodic work is the system's; anonymous requests' stays so."""
    first, second = uuid.uuid4(), uuid.uuid4()
    await enqueue(remember, value=first)
    with bind_request(RequestContext("req-3", "203.0.113.9", "POST", "/x")):
        await enqueue(remember, value=second)

    @periodic(every=timedelta(hours=1), name="tests.hourly_actor")
    async def hourly() -> None:
        ran.append(("hourly", "", current_request()))

    try:
        await Worker().run_once()
    finally:
        tasks._tasks.pop("tests.hourly_actor")

    actors = {key: context.actor for key, _, context in ran}
    assert actors == {first: SYSTEM_ACTOR, second: None, "hourly": SYSTEM_ACTOR}


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
    async with asyncio.timeout(5):
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
    """Without the messaging app durable work fails loudly at commit.

    A deferred task, or an event that a durable handler subscribes to.
    """
    refused = re.escape(
        "Durable handlers and deferred tasks need 'domainkit.contrib.messaging' "
        "in INSTALLED_DOMAINS."
    )
    registry.clear()
    url = f"sqlite+aiosqlite:///{tmp_path / 'plain.sqlite3'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        setup()
        async with connections.engine().begin() as connection:
            await connection.run_sync(notes.create)
        async with NotesUnitOfWork() as uow:
            uow.defer(remember, value=uuid.uuid4())
            with pytest.raises(ImproperlyConfigured, match=f"^{refused}$"):
                await uow.commit()
        event_bus.subscribe(NoteWritten, durable_handler, durable=True)
        try:
            note = Note(id=NoteId(uuid.uuid4()), text="t")
            note.record(NoteWritten(note_id=note.id))
            async with NotesUnitOfWork() as uow:
                await uow.notes.add(note)
                with pytest.raises(ImproperlyConfigured, match=f"^{refused}$"):
                    await uow.commit()
        finally:
            event_bus.unsubscribe(NoteWritten, durable_handler)
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
    assert run("messages") == "pending  0\ndone     1\ndead     1\ncancelled 0\n"
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


async def test_finished_messages_keep_their_row_not_their_arguments(
    tables: None,
) -> None:
    """Done and cancelled messages are blanked; dead ones keep theirs to retry.

    A sensitive task's dead message is blanked too, and says so in its
    headers, so a retry leaves it alone.
    """
    await enqueue(remember, value=uuid.uuid4(), label="secret-label")
    await enqueue(always_fails)
    await enqueue(sends_a_code, to="+33612345678", code="493021")
    async with NotesUnitOfWork() as uow:
        uow.defer(remember, value=uuid.uuid4(), label="cancelled-label")
        await uow.commit()
    cancelled = (await outbox())[-1]["id"]
    config = registry.settings(MessagingSettings)
    assert await admin.cancel(config, cancelled)  # type: ignore[arg-type]

    await Worker().run_once()

    rows = {row["target"]: row for row in await outbox() if row["id"] != cancelled}
    done = rows[tasks.qualified_name(remember)]
    assert (done["status"], done["payload"]) == (DONE, {})
    assert BLANKED not in done["headers"]  # type: ignore[operator]
    dead = rows["tests.always_fails"]
    assert (dead["status"], dead["payload"]) == (DEAD, {})  # It takes none.
    assert BLANKED not in dead["headers"]  # type: ignore[operator]
    code = rows["tests.sends_a_code"]
    assert (code["status"], code["payload"]) == (DEAD, {})
    assert code["headers"][BLANKED] is True  # type: ignore[index]
    assert code["last_error"] == "RuntimeError: gateway down"
    async with connections.engine().connect() as connection:
        stopped = await connection.scalar(
            select(messages.c.payload).where(messages.c.id == cancelled)
        )
    assert stopped == {}
    assert await admin.blanked_dead_letters(config, None) == 1  # Of 2 dead.
    assert await admin.blanked_dead_letters(config, dead["id"]) == 0  # type: ignore[arg-type]
    assert await admin.retry_dead(config, None) == 1  # always_fails only.
    assert await admin.blanked_dead_letters(config, None) == 1


async def test_dead_messages_of_other_tasks_keep_their_arguments(
    tables: None,
) -> None:
    """A task that is not sensitive keeps its arguments dead, so it can be retried."""
    await enqueue(fails_once, key="k")
    async with connections.engine().begin() as connection:
        await connection.execute(update(messages).values(max_attempts=1))

    await Worker().run_once()

    (row,) = await outbox()
    assert (row["status"], row["payload"]) == (DEAD, {"key": "k"})
    assert await admin.retry_dead(registry.settings(MessagingSettings), None) == 1
    await Worker().run_once()
    assert ran == ["k", "k:ok"]


async def test_steps_after_a_sensitive_one_are_blanked_when_they_die(
    tables: None,
) -> None:
    """A chain's steps after a sensitive task are sensitive too (SEC-r2-5).

    They are handed its arguments, or what it made of them: dead, at any
    distance from it, they keep none. A chain without a sensitive step
    keeps a dead step's arguments, to be retried.
    """
    await enqueue(chain(sends_a_code_once, notes_it, files_it), to="+336", code="4930")
    await enqueue(chain(notes_it, files_it), to="+337", code="5151")

    for _ in range(3):
        await Worker().run_once()

    rows = await outbox()
    assert sorted(ran) == [("noted", "+336"), ("noted", "+337"), ("sent", "+336")]
    assert [row["status"] for row in rows].count(DONE) == 3
    blanked, kept = sorted(
        (row for row in rows if row["status"] == DEAD),
        key=lambda row: bool(row["payload"]),
    )
    assert (blanked["target"], blanked["payload"]) == ("tests.files_it", {})
    assert blanked["headers"][SENSITIVE] is True  # type: ignore[index]
    assert blanked["headers"][BLANKED] is True  # type: ignore[index]
    assert blanked["last_error"] == "RuntimeError: bookkeeping down"
    assert kept["payload"] == {"to": "+337", "code": "5151"}
    assert SENSITIVE not in kept["headers"]  # type: ignore[operator]
    assert BLANKED not in kept["headers"]  # type: ignore[operator]


def test_retrying_a_blanked_message_says_why_it_cannot(messaging: None) -> None:
    """``messages --retry`` refuses one blanked message, and counts them in all."""

    async def prepare() -> tuple[uuid.UUID, uuid.UUID]:
        async with connections.engine().begin() as connection:
            await connection.run_sync(messages.create)
        await enqueue(sends_a_code, to="+33612345678", code="493021")
        await enqueue(always_fails)
        await Worker().run_once()
        ids = {row["target"]: row["id"] for row in await outbox()}
        await connections.close_all()
        return ids["tests.sends_a_code"], ids["tests.always_fails"]  # type: ignore[return-value]

    blanked_id, failed_id = asyncio.run(prepare())

    with pytest.raises(CommandError) as refused:
        run("messages", "--retry", str(blanked_id))
    assert str(refused.value) == (
        f"{blanked_id} was not queued: its task is sensitive, so its arguments "
        "were blanked when it died. Make and send it again from where it came "
        "from."
    )
    assert run("messages", "--retry", str(failed_id)) == "Queued 1 message(s).\n"
    assert run("messages", "--retry", "all") == (
        "Queued 0 message(s).\n"
        "Left 1 dead message(s) of sensitive tasks: their arguments were blanked "
        "when they died.\n"
    )


def test_retries_and_cancels_are_audited_with_their_operator(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``messages --retry`` and ``--cancel`` leave a row naming who ran them.

    At 0.9.0 re-running every dead handler (``--retry all``) left no trace.
    """
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "ops-1")
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, "domainkit.contrib.audit", ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MESSAGING=FAST,
    ):
        setup()

        async def prepare() -> tuple[uuid.UUID, uuid.UUID]:
            async with connections.engine().begin() as connection:
                await connection.run_sync(messages.create)
                for table in (audit_log, audit_keys, audit_seals):
                    await connection.run_sync(table.create)
            await enqueue(always_fails)
            await Worker().run_once()
            (dead,) = [
                row["id"]
                for row in await outbox()
                if row["target"] == "tests.always_fails"
            ]
            await enqueue(remember, value=uuid.uuid4())
            waiting = (await outbox())[-1]["id"]
            await connections.close_all()
            return dead, waiting  # type: ignore[return-value]

        async def recorded() -> list[dict[str, Any]]:
            async with connections.engine().connect() as connection:
                rows = await connection.execute(
                    select(audit_log).order_by(audit_log.c.occurred_at)
                )
                found = [dict(row._mapping) for row in rows]
            await connections.close_all()
            return found

        dead, waiting = asyncio.run(prepare())
        run("messages", "--retry", str(dead))
        run("messages", "--retry", "all")
        run("messages", "--cancel", str(waiting), "--operator", "alice")
        with pytest.raises(CommandError) as refused:
            run("messages", "--cancel", str(waiting))
        run("messages", "--dead")  # Reading is not recorded.
        rows = [
            row
            for row in asyncio.run(recorded())
            if row["action"].startswith("operator.")
        ]
    registry.clear()

    assert str(refused.value) == (
        f"{waiting} was not cancelled: it is running, already finished, or not in "
        "this outbox."
    )
    assert [(row["action"], row["actor"]) for row in rows] == [
        ("operator.messages.retry", "operator:ops-1"),
        ("operator.messages.retry", "operator:ops-1"),
        ("operator.messages.cancel", "operator:alice"),
        ("operator.messages.cancel", "operator:ops-1"),
    ]
    one, every, cancelled, missed = (row["details"] for row in rows)
    assert one == {
        "message": str(dead),
        "database": "default",
        "queued": 1,
        "blanked": 0,
        "operator_source": "environment",
    }
    assert (every["message"], every["queued"]) == ("all", 0)
    assert (cancelled["message"], cancelled["cancelled"]) == (str(waiting), True)
    assert cancelled["operator_source"] == "option"
    assert missed["cancelled"] is False
    assert {row["outcome"] for row in rows} == {"success"}


def test_retries_and_cancels_reach_the_outbox_named(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--database`` picks the outbox acted on, and the record says which."""
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "ops-1")
    other = f"sqlite+aiosqlite:///{tmp_path / 'other.sqlite3'}"
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, "domainkit.contrib.audit", ALPHA],
        DATABASES={"default": {"URL": database_url}, "other": {"URL": other}},
        MESSAGING=FAST,
    ):
        setup()
        dead, waiting, blanked = (
            message_row(
                kind="task", target=target, payload={}, headers={}, max_attempts=1
            )
            for target in ("tests.always_fails",) * 2 + ("tests.sends_a_code",)
        )
        blanked["headers"] = {BLANKED: True}

        async def prepare() -> None:
            async with connections.engine().begin() as connection:
                for table in (messages, audit_log, audit_keys, audit_seals):
                    await connection.run_sync(table.create)
            async with connections.engine("other").begin() as connection:
                await connection.run_sync(messages.create)
                await connection.execute(insert(messages), [dead, waiting, blanked])
                await connection.execute(
                    update(messages)
                    .where(messages.c.id.in_([dead["id"], blanked["id"]]))
                    .values(status=DEAD)
                )
            await connections.close_all()

        async def statuses() -> tuple[list[str], list[dict[str, Any]]]:
            async with connections.engine("other").connect() as connection:
                found = list(
                    await connection.scalars(
                        select(messages.c.status).order_by(messages.c.status)
                    )
                )
            async with connections.engine().connect() as connection:
                rows = await connection.execute(
                    select(audit_log.c.details)
                    .where(audit_log.c.action.startswith("operator.messages"))
                    .order_by(audit_log.c.occurred_at)
                )
                details = [row.details for row in rows]
            await connections.close_all()
            return found, details

        asyncio.run(prepare())
        assert run("messages", "--retry", "all") == "Queued 0 message(s).\n"
        assert run("messages", "--retry", "all", "--database", "other") == (
            "Queued 1 message(s).\n"
            "Left 1 dead message(s) of sensitive tasks: their arguments were "
            "blanked when they died.\n"
        )
        cancelled = run(
            "messages", "--cancel", str(waiting["id"]), "--database", "other"
        )
        found, details = asyncio.run(statuses())
    registry.clear()

    assert cancelled == f"Cancelled {waiting['id']}.\n"
    assert found == [CANCELLED, DEAD, PENDING]
    assert [(each["database"], each.get("blanked")) for each in details] == [
        ("default", 0),
        ("other", 1),
        ("other", None),
    ]


def test_sensitive_is_a_task_option() -> None:
    """``sensitive`` is off unless asked for."""
    assert tasks.spec_for(sends_a_code).sensitive is True
    assert tasks.spec_for(remember).sensitive is False


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


# Queues and brokers ---------------------------------------------------------------

BROKERED = {**FAST, "BROKER": "work"}


@pytest.fixture
def brokered(database_url: str) -> Iterator[None]:
    """Messaging on an empty database, with an in-memory broker."""
    ran.clear()
    reported.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[MESSAGING, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        BROKERS={"work": {"URL": "memory://"}},
        MESSAGING=BROKERED,
        ERROR_REPORTERS=["domainkit_tests.test_messaging.reporter"],
    ):
        setup()
        yield
    registry.clear()


@pytest.fixture
async def broker_tables(brokered: None) -> AsyncIterator[MemoryBroker]:
    """The outbox and notes tables, and this loop's in-memory broker."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)
        await connection.run_sync(notes.create)
    broker = brokers["work"]
    assert isinstance(broker, MemoryBroker)
    yield broker
    await close_publishers()
    await brokers.close_all()
    await connections.close_all()


async def published(publisher: CommitPublisher | None = None) -> None:
    """Wait until the publisher offered what was committed; fail, never hang."""
    publisher = publisher or commit_publisher(registry.settings(MessagingSettings))
    async with asyncio.timeout(5):
        await publisher.flush()


async def settled(count: int) -> list[dict[str, object]]:
    """Wait until ``count`` messages are done or dead; return every row."""
    for _ in range(500):
        rows = await outbox()
        if sum(row["status"] in {DONE, DEAD} for row in rows) >= count:
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError(f"Not settled: {rows}")


def delivery(body: object, *, answers: list[str]) -> Delivery:
    """A delivery that records how it was answered."""

    async def ack() -> None:
        answers.append("ack")

    async def requeue() -> None:
        answers.append("requeue")

    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    message_id = body.get("id", "m") if isinstance(body, dict) else "m"
    return Delivery("default", raw, message_id, ack, requeue)


async def test_tasks_go_to_their_queues(tables: None) -> None:
    """A task's queue is stored with each message; workers can pick queues."""
    await enqueue(send_email, to="ada@example.com")
    await enqueue(remember, value=uuid.uuid4())

    assert [row["queue"] for row in await outbox()] == ["emails", "default"]
    assert {"default", "emails"} <= tasks.queues()
    with pytest.raises(ImproperlyConfigured, match="Queue names have 1 to 64"):
        task(queue="bad queue")
    with pytest.raises(ImproperlyConfigured, match="Queue names have 1 to 64"):
        periodic(timedelta(hours=1), queue="")

    assert await Worker(queues=["emails"]).run_once() == 1
    assert ran == [("email", "ada@example.com")]
    assert await Worker().run_once() == 1


async def test_periodic_tasks_keep_their_queue(tables: None) -> None:
    """Scheduled runs of a periodic task go to its queue."""

    @periodic(timedelta(hours=1), name="tests.hourly_report", queue="reports")
    async def hourly_report() -> None: ...

    await Worker().schedule_periodic()

    rows = [row for row in await outbox() if row["target"] == "tests.hourly_report"]
    assert [row["queue"] for row in rows] == ["reports"]


async def test_a_message_that_kills_its_worker_is_dead_lettered(
    tables: None,
) -> None:
    """A crash during the last attempt leaves attempts used up: dead, not retried."""
    await enqueue(remember, value=uuid.uuid4())
    async with connections.engine().begin() as connection:
        # The worker died while running the last attempt (attempts was raised).
        await connection.execute(update(messages).values(attempts=10, max_attempts=10))

    assert await Worker().run_once() == 1

    (row,) = await outbox()
    assert (row["status"], row["attempts"]) == (DEAD, 11)
    assert row["last_error"] == (
        "AttemptsExhausted: The worker stopped during the last attempt (a crash, "
        "or a kill before TASK_TIMEOUT)."
    )
    assert ran == []


async def test_the_relay_publishes_pointers_once(broker_tables: MemoryBroker) -> None:
    """Due messages go out once, locked for REPUBLISH_AFTER; lost ones go again."""
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await relay_scenario(broker_tables)


async def test_commits_publish_their_messages_at_once(
    broker_tables: MemoryBroker,
) -> None:
    """With a broker, each commit hands its due messages over right away."""
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        uow.defer(send_email, to="ada@example.com")
        uow.defer(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
        await uow.commit()
    await enqueue(remember, value=uuid.uuid4())
    await published()

    assert (broker_tables.pending("emails"), broker_tables.pending("default")) == (1, 1)
    email, delayed, enqueued = await outbox()
    assert email["lock_id"] is not None
    assert delayed["lock_id"] is None  # Not due: the relay sends it later.
    assert enqueued["lock_id"] is not None

    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await enqueue(remember, value=uuid.uuid4())  # Due, not published.
    # No ids means nothing to publish, not "everything that is due".
    assert await Worker().relay_ids(broker_tables, []) == 0
    assert await Worker().relay(broker_tables, 10) == 1


async def test_failed_commit_publications_leave_work_to_the_relay(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The commit stands whatever happens next; the relay publishes later."""

    async def database_down(self: Worker, broker: object, ids: object) -> int:
        raise ConnectionError("database down")

    with caplog.at_level(logging.WARNING):
        down = Unreachable(declare=False)
        monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: down)
        await enqueue(remember, value=uuid.uuid4())
        await published()
        monkeypatch.undo()
        monkeypatch.setattr(Worker, "relay_ids", database_down)
        await enqueue(remember, value=uuid.uuid4())
        await published()

    first, second = await outbox()
    assert (first["lock_id"], second["lock_id"]) == (None, None)
    assert "Could not publish committed messages; the relay will" in caplog.messages
    assert broker_tables.pending("default") == 0


class Stalled(MemoryBroker):
    """A slow broker: publishing waits until ``release`` is set."""

    def __init__(self) -> None:
        """Hold every publish."""
        super().__init__()
        self.release = asyncio.Event()

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Publish once released."""
        await self.release.wait()
        await super().publish(queue, body, message_id=message_id)


async def test_commits_never_wait_on_the_broker(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck broker holds the background publisher, never the commit."""
    stalled = Stalled()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: stalled)

    async with asyncio.timeout(1):
        await enqueue(remember, value=uuid.uuid4())
    assert stalled.pending("default") == 0
    stalled.release.set()
    await published()

    assert stalled.pending("default") == 1
    assert commit_publisher(registry.settings(MessagingSettings)).backlog == 0


async def test_commits_declare_their_queues_once(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publisher keeps a worker per outbox, so each queue is declared once."""
    declared: list[list[str]] = []
    original = MemoryBroker.declare

    async def counting(self: MemoryBroker, queues: Iterable[str]) -> None:
        declared.append(sorted(queues))
        await original(self, queues)

    monkeypatch.setattr(MemoryBroker, "declare", counting)
    for _ in range(3):
        await enqueue(remember, value=uuid.uuid4())
        await published()

    assert declared == [["default"]]
    assert broker_tables.pending("default") == 3


async def test_the_backlog_is_bounded(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past MAX_UNPUBLISHED waiting messages, commits leave theirs to the relay."""
    stalled = Stalled()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: stalled)
    monkeypatch.setattr(dispatch, "MAX_UNPUBLISHED", 2)

    for _ in range(3):
        await enqueue(remember, value=uuid.uuid4())
    stalled.release.set()
    await published()

    assert stalled.pending("default") == 2
    assert [row["lock_id"] is None for row in await outbox()].count(True) == 1
    assert caplog.messages == [
        (
            "Left 1 committed messages to the relay: 2 are already waiting (logged "
            "once until the backlog clears)"
        )
    ]


async def test_shutdown_waits_for_the_publisher_then_stops_it(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closing lets a stuck publish go in its time; the relay sends the message."""
    stalled = Stalled()
    monkeypatch.setattr(BrokerHandler, "__getitem__", lambda self, alias: stalled)
    config = registry.settings(MessagingSettings)
    await enqueue(remember, value=uuid.uuid4())
    publisher = commit_publisher(config)
    task = publisher._task
    assert task is not None

    async with asyncio.timeout(1):
        await close_publishers(within=0.1)
        await asyncio.wait({task})  # Its give-back may outlast close's time.

    assert "Left 1 committed messages to the relay at shutdown" in (caplog.messages)
    assert commit_publisher(config) is not publisher
    await published(commit_publisher(config))  # A new one has nothing to send.
    left = REGISTRY.get_sample_value(
        "messaging_left_to_relay_total", {"reason": "shutdown"}
    )
    assert left is not None
    assert left >= 1
    (row,) = await outbox()
    # The publication cut short gave its message back: the relay sends it now.
    assert (row["lock_id"], row["locked_until"]) == (None, None)


class RecordingWorker:
    """Stands in for ``Worker``: records what it is built for and asked."""

    made: ClassVar[list[tuple[MessagingSettings, str]]] = []
    relayed: ClassVar[list[tuple[str, object, list[uuid.UUID]]]] = []

    def __init__(self, config: MessagingSettings, *, database: str) -> None:
        """Remember the config and the outbox."""
        self.database = database
        self.made.append((config, database))

    async def relay_ids(self, broker: object, ids: Sequence[uuid.UUID]) -> int:
        """Remember the broker and the ids."""
        self.relayed.append((self.database, broker, list(ids)))
        return len(ids)


async def test_publishers_relay_each_outbox_through_its_own_worker(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ids go to their database's worker, built once, with the broker of BROKER."""
    RecordingWorker.made.clear()
    RecordingWorker.relayed.clear()
    monkeypatch.setattr(dispatch, "Worker", RecordingWorker)
    config = registry.settings(MessagingSettings)
    publisher = dispatch.CommitPublisher(config)
    first, second = uuid.uuid4(), uuid.uuid4()

    publisher.submit("other", [first])
    assert publisher.unpublished == {"other": [first]}
    await published(publisher)
    publisher.submit("other", [second])
    await published(publisher)
    await publisher.close(5)

    assert RecordingWorker.made == [(config, "other")]
    assert [
        (database, broker.inner(), ids)  # type: ignore[attr-defined]
        for database, broker, ids in RecordingWorker.relayed
    ] == [("other", broker_tables, [first]), ("other", broker_tables, [second])]
    assert publisher.backlog == 0


async def test_closed_publishers_start_again_when_used(
    broker_tables: MemoryBroker,
) -> None:
    """After close(), a submit starts the task again."""
    publisher = commit_publisher(registry.settings(MessagingSettings))
    await enqueue(remember, value=uuid.uuid4())
    await published(publisher)
    await publisher.close(5)

    await enqueue(remember, value=uuid.uuid4())
    async with asyncio.timeout(1):
        await published(publisher)

    assert broker_tables.pending("default") == 2


def test_publishers_need_a_broker() -> None:
    """Without MESSAGING.BROKER there is nothing to publish to."""
    refused = r"^CommitPublisher needs MESSAGING\.BROKER\.$"
    with pytest.raises(ValueError, match=refused):
        dispatch.CommitPublisher(MessagingSettings())


async def test_commits_hand_over_their_outbox_and_ids(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publisher gets the unit of work's database and the ids it wrote."""
    submitted: list[tuple[str, list[uuid.UUID]]] = []

    def record(
        self: dispatch.CommitPublisher, database: str, ids: Sequence[uuid.UUID]
    ) -> None:
        submitted.append((database, list(ids)))

    monkeypatch.setattr(dispatch.CommitPublisher, "submit", record)
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        uow.defer(remember, value=uuid.uuid4())
        await uow.commit()
    async with NotesUnitOfWork() as uow:  # Nothing to publish, nothing reported.
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="u"))
        await uow.commit()

    [row] = await outbox()
    assert submitted == [("default", [row["id"]])]
    flush_reports()  # Reporters run on their own thread.
    assert reported == []


async def test_the_messaging_app_closes_the_publishers_at_shutdown(
    brokered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown's drain phase flushes and stops the background publishers.

    The application and management commands both shut down through
    ``close_resources``, so commands close their publishers too.
    """
    from domainkit import lifecycle

    closed: list[bool] = []

    async def record() -> None:
        closed.append(True)

    assert lifecycle._registered[lifecycle.Phase.DRAIN]["publishers"] is (
        close_publishers
    )
    monkeypatch.setitem(
        lifecycle._registered[lifecycle.Phase.DRAIN], "publishers", record
    )
    await lifecycle.close_resources()

    assert closed == [True]


async def test_after_commit_failures_are_reported(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain's after-commit hook failing does not fail the unit of work."""
    from domainkit.contrib.messaging.apps import MessagingConfig

    async def broken(self: MessagingConfig, uow: object) -> None:
        raise RuntimeError("hook broke")

    monkeypatch.setattr(MessagingConfig, "committed", broken)
    async with NotesUnitOfWork() as uow:
        await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="t"))
        uow.defer(remember, value=uuid.uuid4())
        await uow.commit()

    assert len(await outbox()) == 1
    flush_reports()  # Reporters run on their own thread.
    assert reported == [("hook broke", {"domain": "messaging"})]


async def relay_scenario(broker_tables: MemoryBroker) -> None:
    """Relay three messages, one of them delayed, then a lost one again."""
    await enqueue(send_email, to="ada@example.com")
    await enqueue(remember, value=uuid.uuid4())
    await enqueue(remember, value=uuid.uuid4(), delay=timedelta(hours=1))
    worker = Worker()

    assert await worker.relay(broker_tables, 10) == 2
    assert await worker.relay(broker_tables, 10) == 0
    assert (broker_tables.pending("emails"), broker_tables.pending("default")) == (1, 1)
    email, *_ = await outbox()
    assert email["attempts"] == 0
    assert email["locked_until"] is not None

    async with connections.engine().begin() as connection:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await connection.execute(
            update(messages)
            .where(messages.c.id == email["id"])
            .values(locked_until=past)
        )
    assert await worker.relay(broker_tables, 10) == 1  # Lost: published again.
    assert broker_tables.pending("emails") == 2


class Unreachable(MemoryBroker):
    """A broker that is down: declaring or publishing fails."""

    def __init__(self, *, declare: bool) -> None:
        """Fail at ``declare`` (or at publishing)."""
        super().__init__()
        self.fail_declare = declare

    async def declare(self, queues: object) -> None:
        """Fail, or declare."""
        if self.fail_declare:
            raise BrokerUnavailable("down")
        await super().declare(queues)  # type: ignore[arg-type]

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Fail."""
        raise BrokerUnavailable("down")


async def test_unpublished_messages_are_unlocked(
    tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker that is down leaves the messages due for the next round."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker()
    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        await worker.relay(Unreachable(declare=True), 10)
    assert caplog.messages == [
        (
            "Could not publish 1 of 1 message(s) to the broker (declaring 'default': "
            "down); they go again on the next round"
        )
    ]

    for broken in (Unreachable(declare=True), Unreachable(declare=False)):
        assert await worker.relay(broken, 10) == 0
        (row,) = await outbox()
        assert (row["lock_id"], row["locked_until"]) == (None, None)
    assert await worker.relay(MemoryBroker(), 10) == 1  # Due again, and sent.


async def test_take_over_needs_the_published_lock(tables: None) -> None:
    """Only the current publication runs; stale and finished ones are skipped."""
    await enqueue(remember, value=uuid.uuid4())
    broker = MemoryBroker()
    worker = Worker()
    await worker.relay(broker, 10)
    (row,) = await outbox()
    message_id, lock = row["id"], row["lock_id"]
    assert isinstance(message_id, uuid.UUID)
    assert isinstance(lock, uuid.UUID)

    assert await worker.take_over(message_id, uuid.uuid4()) is None
    claimed = await worker.take_over(message_id, lock)
    assert claimed is not None
    assert claimed.attempts == 1
    assert await worker.take_over(message_id, lock) is None  # Now running.
    await worker.process(claimed)
    assert await worker.take_over(message_id, lock) is None  # Done.
    (row,) = await outbox()
    assert (row["status"], row["lock_id"]) == (DONE, None)


async def test_an_expired_publication_can_still_run(tables: None) -> None:
    """A delivery whose lock expired takes the message over with a fresh lock."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker()
    await worker.relay(MemoryBroker(), 10)
    (row,) = await outbox()
    async with connections.engine().begin() as connection:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await connection.execute(update(messages).values(locked_until=past))

    claimed = await worker.take_over(row["id"], uuid.uuid4())  # type: ignore[arg-type]

    assert claimed is not None
    (taken,) = await outbox()
    assert taken["lock_id"] == claimed.lock_id
    assert taken["locked_until"] > datetime.now(UTC)  # type: ignore[operator]
    await worker.process(claimed)
    (done,) = await outbox()
    assert done["status"] == DONE


class Recording(MemoryBroker):
    """An in-memory broker that counts declarations and refuses some queues."""

    def __init__(self, refuse: frozenset[str] = frozenset()) -> None:
        """Refuse publications to ``refuse``."""
        super().__init__()
        self.refuse = refuse
        self.declarations: list[list[str]] = []

    async def declare(self, queues: object) -> None:
        """Record, then declare."""
        names = sorted(queues)  # type: ignore[call-overload]
        self.declarations.append(names)
        await super().declare(names)

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Refuse, or publish."""
        if queue in self.refuse:
            raise BrokerRefused(f"no {queue}")
        await super().publish(queue, body, message_id=message_id)

    def drain(self, queue: str) -> list[tuple[bytes, str]]:
        """Take every waiting message of ``queue``."""
        waiting = []
        while self.pending(queue):
            waiting.append(self._queues[queue].popleft())
        return waiting


async def test_the_relay_works_in_batches_and_counts(
    tables: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A batch has a limit; only failed publications are unlocked and redeclared."""
    await enqueue(send_email, to="ada@example.com")
    for _ in range(3):
        await enqueue(remember, value=uuid.uuid4())
    broker = Recording(refuse=frozenset({"emails"}))
    worker = Worker()
    labels = {"outcome": "published"}
    published = REGISTRY.get_sample_value("messaging_relayed_total", labels) or 0
    failed = (
        REGISTRY.get_sample_value("messaging_relayed_total", {"outcome": "failed"}) or 0
    )

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        assert await worker.relay(broker, 2) == 1
    email, first, *rest = await outbox()
    ((body, message_id),) = broker.drain("default")

    assert (email["lock_id"], email["locked_until"]) == (None, None)
    assert first["lock_id"] is not None
    assert all(row["lock_id"] is None for row in rest)  # Beyond the batch.
    assert json.loads(body) == {
        "id": str(first["id"]),
        "database": "default",
        "lock": str(first["lock_id"]),
    }
    assert message_id == str(first["id"])
    assert caplog.messages == [
        (
            "Could not publish 1 of 2 message(s) to the broker (queue 'emails': no "
            "emails); they go again on the next round"
        )
    ]
    assert REGISTRY.get_sample_value("messaging_relayed_total", labels) == published + 1
    assert (
        REGISTRY.get_sample_value("messaging_relayed_total", {"outcome": "failed"})
        == failed + 1
    )

    assert await worker.relay(broker, 10) == 2
    # "default" stays declared; "emails" failed, so it is declared again.
    assert broker.declarations == [["default", "emails"], ["emails"]]
    assert REGISTRY.get_sample_value("messaging_relayed_total", labels) == published + 3
    assert (
        REGISTRY.get_sample_value("messaging_relayed_total", {"outcome": "failed"})
        == failed + 2
    )


async def test_brokered_workers_run_retry_and_dead_letter(
    broker_tables: MemoryBroker,
) -> None:
    """Through the broker: tasks run, failures retry via the outbox, then die."""
    await enqueue(send_email, to="ada@example.com")
    await enqueue(fails_once, key="k")
    await enqueue(always_fails)
    stop = asyncio.Event()
    # A batch of one: a full batch makes the relay go again without waiting.
    config = registry.settings(MessagingSettings).model_copy(update={"BATCH_SIZE": 1})
    runner = BrokerWorkers([Worker(config)], ["default", "emails"], config)
    running = asyncio.create_task(runner.run(stop))

    rows = await settled(3)
    stop.set()
    await running

    by_target = {row["target"]: row for row in rows}
    assert by_target["tests.always_fails"]["status"] == DEAD
    assert by_target["tests.always_fails"]["attempts"] == 3
    fails_once_row = next(
        row for row in rows if str(row["target"]).endswith("fails_once")
    )
    assert (fails_once_row["status"], fails_once_row["attempts"]) == (DONE, 2)
    assert ("email", "ada@example.com") in ran
    assert "k:ok" in ran


async def test_deliveries_that_cannot_run_are_settled(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Garbage and foreign messages are dropped; a database hiccup requeues."""
    runner = BrokerWorkers([], ["default"], registry.settings(MessagingSettings))
    answers: list[str] = []
    missing = uuid.uuid4()
    pointer = {"id": str(missing), "lock": str(uuid.uuid4()), "database": "elsewhere"}

    with caplog.at_level(logging.DEBUG, "domainkit.contrib.messaging.dispatch"):
        await runner.deliver(delivery(b"not json", answers=answers))
        await runner.deliver(delivery({"id": "x"}, answers=answers))
        await runner.deliver(delivery(pointer, answers=answers))
        pointer["database"] = "default"
        await runner.deliver(delivery(pointer, answers=answers))  # Nothing to run.

    assert answers == ["ack", "ack", "ack", "ack"]
    assert caplog.messages == [
        "Dropped an unreadable message from queue 'default'",
        "Dropped an unreadable message from queue 'default'",
        f"Dropped message {missing}: 'elsewhere' hosts no outbox here",
        f"Skipped message {missing}: done, running, or moved on",
    ]
    assert runner.outbox("default") is runner.outbox("default")
    monkeypatch.setattr(dispatch, "_hosting_aliases", lambda: {"default", "other"})
    other = runner.outbox("other")
    assert other is not None
    assert other.database == "other"

    async def broken(self: Worker, message_id: uuid.UUID, lock: uuid.UUID) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(Worker, "take_over", broken)
    caplog.clear()
    await runner.deliver(delivery(pointer, answers=answers))
    assert answers[-1] == "requeue"
    assert caplog.messages == [
        f"Could not run message {missing} from queue 'default'; requeued"
    ]

    with pytest.raises(ValueError, match=r"^BrokerWorkers needs MESSAGING\.BROKER\.$"):
        BrokerWorkers([], [], MessagingSettings())


gates: dict[str, asyncio.Event] = {}


@task
async def wait_at_gate(name: str) -> None:
    """Start, then wait until the test opens this task's gate."""
    ran.append(f"{name}:started")
    await gates[name].wait()
    ran.append(f"{name}:done")


async def test_stopping_waits_for_every_running_delivery(
    broker_tables: MemoryBroker,
) -> None:
    """Deliveries running when the worker stops all finish before it returns."""
    gates.update(a=asyncio.Event(), b=asyncio.Event())
    await enqueue(wait_at_gate, name="a")
    await enqueue(wait_at_gate, name="b")
    stop = asyncio.Event()
    config = registry.settings(MessagingSettings)
    running = asyncio.create_task(
        BrokerWorkers([Worker()], ["default"], config).run(stop)
    )
    for _ in range(500):
        if {"a:started", "b:started"} <= set(ran):
            break
        await asyncio.sleep(0.01)

    stop.set()
    gates["a"].set()
    for _ in range(500):
        if "a:done" in ran:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    assert not running.done()  # "b" still runs, so the worker waits.
    gates["b"].set()
    await running

    assert {"a:done", "b:done"} <= set(ran)
    assert [row["status"] for row in await outbox()] == [DONE, DONE]


SLOW: Final = {"TASK_TIMEOUT": 30, "VISIBILITY_TIMEOUT": 60}
"""Long enough that only a shutdown stops a task waiting at its gate."""


async def stopped(running: asyncio.Task[None], within: float = 5) -> None:
    """Wait up to ``within`` seconds for ``running`` to end; fail, never hang."""
    await asyncio.wait({running}, timeout=within)
    assert running.done()


def interrupted() -> tuple[float, float]:
    """Tasks stopped before they finished so far, and seconds spent on tasks."""
    labels = {"kind": "task", "outcome": "interrupted"}
    count = REGISTRY.get_sample_value("messaging_messages_total", labels) or 0
    duration = "messaging_message_duration_seconds_sum"
    return count, REGISTRY.get_sample_value(duration, {"kind": "task"}) or 0


async def given_back(name: str) -> None:
    """Check that the task waiting at gate ``name`` went back to the outbox."""
    (row,) = [row for row in await outbox() if row["payload"] == {"name": name}]
    assert row["status"] == PENDING
    assert row["attempts"] == 0  # The cut-short run does not count.
    assert (row["lock_id"], row["locked_until"]) == (None, None)
    assert row["available_at"] <= datetime.now(UTC)  # type: ignore[operator]
    assert f"{name}:done" not in ran


async def test_stopping_gives_back_what_does_not_finish_in_time(
    tables: None, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past SHUTDOWN_TIMEOUT, running messages are cancelled and go back."""
    graces: list[float] = []
    finish = worker_module.finish

    async def drain(running: set[asyncio.Task[None]], grace: float) -> None:
        graces.append(grace)
        await finish(running, grace)

    monkeypatch.setattr(worker_module, "finish", drain)
    gates["late"] = asyncio.Event()
    await enqueue(wait_at_gate, name="late")
    config = MessagingSettings(**{**FAST, **SLOW, "SHUTDOWN_TIMEOUT": 0.05})
    worker, stop, before = Worker(config), asyncio.Event(), interrupted()
    running = asyncio.create_task(worker.run(stop))
    await until(lambda: "late:started" in ran)

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.worker"):
        stop.set()
        await stopped(running)

    assert running.result() is None  # A stop, not a cancellation.
    await given_back("late")
    assert caplog.messages == [
        "Stopping 1 running message(s) that did not finish in time; they run again"
    ]
    assert graces == [0.05]  # It ran until SHUTDOWN_TIMEOUT: the gate never opened.
    count, seconds = interrupted()
    assert count == before[0] + 1
    assert seconds > before[1]  # The cut-short run's time is observed too.
    gates["late"].set()  # It runs again, as a first attempt.
    assert await worker.run_once() == 1
    assert [(row["status"], row["attempts"]) for row in await outbox()] == [(DONE, 1)]


async def test_a_cancelled_worker_gives_its_messages_back_at_once(
    tables: None,
) -> None:
    """Cancelling run() does not wait for SHUTDOWN_TIMEOUT."""
    gates["cut"] = asyncio.Event()
    await enqueue(wait_at_gate, name="cut")
    running = asyncio.create_task(
        Worker(MessagingSettings(**{**FAST, **SLOW})).run(asyncio.Event())
    )
    await until(lambda: "cut:started" in ran)

    running.cancel()
    await stopped(running)  # Well within SHUTDOWN_TIMEOUT's 20 seconds.

    assert running.cancelled()
    await given_back("cut")


async def test_a_message_the_outbox_cannot_take_back_waits_for_its_lock(
    tables: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Giving a message back has RELEASE_TIMEOUT; after it, the lock expires."""
    gates["stuck"] = asyncio.Event()
    await enqueue(wait_at_gate, name="stuck")
    monkeypatch.setattr(worker_module, "RELEASE_TIMEOUT", 0.05)
    worker = Worker(MessagingSettings(**{**FAST, **SLOW, "SHUTDOWN_TIMEOUT": 0}))
    release = worker._release

    async def hangs_when_given_back(message: Claimed, **values: object) -> None:
        if values.get("status") == PENDING:
            await asyncio.Event().wait()
        await release(message, **values)

    monkeypatch.setattr(worker, "_release", hangs_when_given_back)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await until(lambda: "stuck:started" in ran)

    with caplog.at_level(logging.ERROR, "domainkit.contrib.messaging.worker"):
        stop.set()
        await stopped(running)

    (row,) = await outbox()
    assert (row["status"], row["attempts"]) == (PENDING, 1)
    assert row["lock_id"] is not None
    target = "domainkit_tests.test_messaging.wait_at_gate"
    logged = (
        f"Could not give back task {target} after stopping it; it runs again "
        "once its lock expires"
    )
    assert caplog.messages == [logged]
    cause = caplog.records[0].exc_info
    assert cause
    assert cause[0] is TimeoutError


async def test_stopping_gives_back_deliveries_that_do_not_finish_in_time(
    broker_tables: MemoryBroker,
) -> None:
    """Brokered workers drain within SHUTDOWN_TIMEOUT too, then stop the rest.

    "slow" holds the drain for all of SHUTDOWN_TIMEOUT, so the test lasts
    that long; two seconds leave "quick" (one write) room on a slow machine.
    """
    gates.update(quick=asyncio.Event(), slow=asyncio.Event())
    await enqueue(wait_at_gate, name="quick")
    await enqueue(wait_at_gate, name="slow")
    config = MessagingSettings(**{**BROKERED, **SLOW, "SHUTDOWN_TIMEOUT": 2})
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config).run(stop)
    )
    await until(lambda: {"quick:started", "slow:started"} <= set(ran))

    stop.set()
    gates["quick"].set()
    await stopped(running, within=30)

    assert running.result() is None
    assert "quick:done" in ran
    await given_back("slow")


async def test_a_cancelled_brokered_worker_gives_its_deliveries_back(
    broker_tables: MemoryBroker,
) -> None:
    """Cancelling BrokerWorkers.run() stops its deliveries at once."""
    gates["cut"] = asyncio.Event()
    await enqueue(wait_at_gate, name="cut")
    config = MessagingSettings(**{**BROKERED, **SLOW})
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config).run(asyncio.Event())
    )
    await until(lambda: "cut:started" in ran)

    running.cancel()
    await stopped(running)  # Well within SHUTDOWN_TIMEOUT's 20 seconds.

    assert running.cancelled()
    await given_back("cut")


def test_a_second_signal_stops_the_worker_at_once(
    messaging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first signal stops claiming; the second cancels what still runs."""

    async def needs_two_signals(self: Worker, stop: asyncio.Event) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(5):
            await stop.wait()
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(5)  # Cancelled by the second signal.

    monkeypatch.setattr(Worker, "run", needs_two_signals)

    assert run("runworker") == (
        "Worker started; press Ctrl+C to stop.\n"
        "Stopped running messages at once; they run again.\n"
        "Worker stopped.\n"
    )


def test_one_signal_lets_the_worker_finish(
    messaging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first signal only asks the workers to stop."""

    async def drains(self: Worker, stop: asyncio.Event) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(5):
            await stop.wait()
        await asyncio.sleep(0.05)  # Running messages finish meanwhile.

    monkeypatch.setattr(Worker, "run", drains)

    assert run("runworker") == (
        "Worker started; press Ctrl+C to stop.\nWorker stopped.\n"
    )


def test_a_worker_cancelled_otherwise_is_not_reported_as_stopped(
    messaging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a second signal turns a cancellation into a stop."""

    async def cancelled(self: Worker, stop: asyncio.Event) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(Worker, "run", cancelled)

    with pytest.raises(asyncio.CancelledError):
        run("runworker")


async def test_a_broker_must_be_configured(database_url: str) -> None:
    """MESSAGING.BROKER names a BROKERS alias, checked at startup."""
    registry.clear()
    with (
        override_settings(
            INSTALLED_DOMAINS=[MESSAGING],
            DATABASES={"default": {"URL": database_url}},
            MESSAGING={"BROKER": "missing"},
            ALPHA=UNSET,
        ),
        pytest.raises(ImproperlyConfigured, match="BROKERS has no 'missing'"),
    ):
        setup()
    registry.clear()


def test_consume_only_workers_and_their_flag(
    brokered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-relay consumes without polling; it needs a broker."""
    seen: list[bool] = []

    async def stopped_at_once(self: BrokerWorkers, stop: asyncio.Event) -> None:
        seen.append(self.relays)

    monkeypatch.setattr(BrokerWorkers, "run", stopped_at_once)
    run("runworker", "--no-relay")
    assert seen == [False]
    with (
        override_settings(MESSAGING=FAST),
        pytest.raises(CommandError, match=r"^--no-relay needs MESSAGING\.BROKER\.$"),
    ):
        run("runworker", "--no-relay")


async def test_consume_only_workers_never_relay(broker_tables: MemoryBroker) -> None:
    """A consume-only worker runs what arrives and leaves the outbox alone."""
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await enqueue(remember, value=uuid.uuid4())
        stop = asyncio.Event()
        config = registry.settings(MessagingSettings)
        consumer = BrokerWorkers([Worker(config)], ["default"], config, relay=False)
        running = asyncio.create_task(consumer.run(stop))
        await asyncio.sleep(0.05)
        (row,) = await outbox()
        assert row["lock_id"] is None  # Nobody relayed it.
        await Worker(config).relay(broker_tables, 10)
        rows = await settled(1)
        stop.set()
        await running

    assert rows[0]["status"] == DONE


def test_runworker_on_a_broker(brokered: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker command picks the broker and its queues; --queue narrows them."""
    seen: list[list[str]] = []
    claimed: list[frozenset[str] | None] = []

    async def stopped_at_once(self: BrokerWorkers, stop: asyncio.Event) -> None:
        assert isinstance(stop, asyncio.Event)
        seen.append(self.queues)
        claimed.extend(worker.queues for worker in self.workers)

    monkeypatch.setattr(BrokerWorkers, "run", stopped_at_once)
    assert run("runworker", "--queue", "emails") == (
        "Worker started on broker 'work' (queues: emails); press Ctrl+C to stop.\n"
        "Worker stopped.\n"
    )
    every = ", ".join(sorted(tasks.queues()))
    assert run("runworker") == (
        f"Worker started on broker 'work' (queues: {every}); press Ctrl+C to "
        "stop.\nWorker stopped.\n"
    )
    assert seen[0] == ["emails"]
    assert {"default", "emails"} <= set(seen[1])
    assert claimed == [frozenset({"emails"}), None]
    with pytest.raises(CommandError, match="Queue names have 1 to 64"):
        run("runworker", "--queue", "bad queue")


async def until(condition: Callable[[], bool]) -> None:
    """Wait up to five seconds for ``condition`` to hold."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting")


class Flaky:
    """Fail the first ``times`` calls of a worker method, then call through."""

    def __init__(self, original: Any, times: int = 1) -> None:  # noqa: ANN401 - a method.
        """Wrap ``original``."""
        self.original = original
        self.left = times
        self.calls = 0
        self.args: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Raise while failures are left; then behave."""
        self.calls += 1
        self.args.append(args)
        if self.left:
            self.left -= 1
            raise ConnectionError("database down")
        return await self.original(*args, **kwargs)


async def test_the_worker_outlives_database_outages(
    tables: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Polling, claiming, and cleaning up fail for a while; the worker waits."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 0.01}))
    claim = Flaky(worker.claim, times=2)
    monkeypatch.setattr(worker, "claim", claim)
    stop = asyncio.Event()
    original = worker.process

    async def process_then_stop(message: Claimed) -> None:
        # Stop before the poll interval ends, or a slow run claims once more.
        # The worker still finishes this message before run() returns.
        stop.set()
        await original(message)

    monkeypatch.setattr(worker, "process", process_then_stop)
    with caplog.at_level(logging.INFO, "domainkit.contrib.messaging.worker"):
        async with asyncio.timeout(5):
            await worker.run(stop)

    assert len(ran) == 1
    assert claim.calls == 3
    assert caplog.messages == [
        (
            "Could not reach the outbox of 'default' (1 failure in a row); "
            "trying again in 0.01s"
        ),
        (
            "Could not reach the outbox of 'default' (2 failures in a row); "
            "trying again in 0.02s"
        ),
        "The outbox of 'default' answers again after 2 failures",
    ]
    assert all(record.exc_info for record in caplog.records[:2])


async def test_outages_back_off_up_to_a_minute(tables: None) -> None:
    """Each failure in a row doubles the pause, from POLL_INTERVAL to 60s."""
    outage = worker_module.Outage("default", 0.5)
    waits = []
    for _ in range(9):
        outage.failures += 1
        waits.append(outage.pause())
    assert waits == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    outage.recovered()
    assert outage.failures == 0


async def test_a_stop_ends_an_outage_pause_at_once(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker waiting out an outage stops when asked, without the full pause."""
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 30}))
    monkeypatch.setattr(worker, "claim", Flaky(worker.claim, times=99))
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    async with asyncio.timeout(1):
        await running


async def test_unrecorded_outcomes_run_again_later(
    tables: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A lost write of the outcome is logged; the message runs again after its lock."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker(MessagingSettings(**FAST))
    (claimed,) = await worker.claim(10)
    monkeypatch.setattr(worker, "_done", Flaky(worker._done))
    before = REGISTRY.get_sample_value(
        "messaging_messages_total", {"kind": "task", "outcome": "unrecorded"}
    )

    with caplog.at_level(logging.ERROR, "domainkit.contrib.messaging.worker"):
        await worker.process(claimed)

    assert len(ran) == 1
    assert caplog.messages == [
        (
            f"Could not record the outcome of task {claimed.target}; it runs "
            "again once its lock expires"
        )
    ]
    assert caplog.records[0].exc_info
    assert (
        REGISTRY.get_sample_value(
            "messaging_messages_total", {"kind": "task", "outcome": "unrecorded"}
        )
        == (before or 0) + 1
    )
    (row,) = await outbox()
    assert (row["status"], row["lock_id"]) == (PENDING, claimed.lock_id)
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(messages).values(locked_until=datetime.now(UTC) - timedelta(1))
        )
    assert await worker.run_once() == 1
    assert len(ran) == 2


async def test_periodic_slots_survive_a_failed_write(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slot counts as queued only once its row is written."""

    @periodic(every=timedelta(hours=1), name="tests.hourly_outage")
    async def hourly() -> None: ...

    worker = Worker(MessagingSettings(**FAST))
    write = Flaky(outbox_module.write)
    monkeypatch.setattr(worker_module, "write", write)
    with pytest.raises(ConnectionError):
        await worker.schedule_periodic()
    assert await outbox() == []
    await worker.schedule_periodic()
    await worker.schedule_periodic()
    assert [row["target"] for row in await outbox()] == ["tests.hourly_outage"]
    assert write.calls == 2


async def test_the_relay_outlives_database_outages(
    broker_tables: MemoryBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Relaying fails for a while; the relay waits, and its work still runs."""
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        await enqueue(remember, value=uuid.uuid4())
    assert broker_tables.pending("default") == 0
    config = registry.settings(MessagingSettings).model_copy(
        update={"POLL_INTERVAL": 0.01}
    )
    worker = Worker(config)
    monkeypatch.setattr(worker, "relay", Flaky(worker.relay, times=2))
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, "domainkit.contrib.messaging.worker"):
        running = asyncio.create_task(
            BrokerWorkers([worker], ["default"], config).run(stop)
        )
        await settled(1)
        stop.set()
        async with asyncio.timeout(5):
            await running

    assert len(ran) == 1
    assert (
        caplog.messages[-1] == "The outbox of 'default' answers again after 2 failures"
    )
    assert all(record.exc_info for record in caplog.records[:-1])
    relay = worker.relay
    assert {args[1] for args in relay.args} == {config.BATCH_SIZE}  # type: ignore[attr-defined]


async def test_every_delivery_is_settled(
    broker_tables: MemoryBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery whose outcome cannot be recorded still frees its slot."""
    first, second = uuid.uuid4(), uuid.uuid4()
    await enqueue(remember, value=first)
    await enqueue(remember, value=second)
    config = registry.settings(MessagingSettings).model_copy(
        update={"CONCURRENCY": 1, "BATCH_SIZE": 1}
    )
    worker = Worker(config)
    monkeypatch.setattr(worker, "_done", Flaky(worker._done))
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([worker], ["default"], config).run(stop)
    )
    try:
        for _ in range(500):
            if len(ran) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await running

    assert {value for value, _, _ in ran} == {first, second}


async def test_unsettled_answers_are_logged(
    broker_tables: MemoryBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker that cannot take the answer is logged; RabbitMQ redelivers."""
    runner = BrokerWorkers([], ["default"], registry.settings(MessagingSettings))

    async def lost() -> None:
        raise BrokerUnavailable("connection closed")

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.dispatch"):
        await runner.deliver(Delivery("default", b"junk", "m1", lost, lost))

    assert caplog.messages[-1] == (
        "Could not answer message m1 from queue 'default': connection closed"
    )


async def test_answers_that_fail_unexpectedly_are_logged_without_the_body(
    broker_tables: MemoryBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """Any failure to answer is logged, never raised: the broker redelivers."""
    runner = BrokerWorkers([], ["default"], registry.settings(MessagingSettings))

    async def closed() -> None:
        raise RuntimeError("The channel is closed.")

    with caplog.at_level(logging.WARNING, "domainkit.contrib.messaging.dispatch"):
        await runner.deliver(Delivery("default", b"secret", "m1", closed, closed))

    record = caplog.records[-1]
    assert (record.levelno, record.getMessage()) == (
        logging.ERROR,
        (
            "Could not answer message m1 from queue 'default'; the broker will "
            "hand it out again"
        ),
    )
    assert record.exc_info
    assert type(record.exc_info[1]) is RuntimeError
    assert "secret" not in caplog.text


async def test_workers_claim_only_what_they_can_run(
    tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most CONCURRENCY run at once; a freed slot claims exactly one more."""
    gates.update(c0=asyncio.Event(), c1=asyncio.Event(), c2=asyncio.Event())
    for name in ("c0", "c1", "c2"):
        await enqueue(wait_at_gate, name=name)
    worker = Worker(
        MessagingSettings(
            **{**FAST, "POLL_INTERVAL": 0.01, "CONCURRENCY": 2, "BATCH_SIZE": 10}
        )
    )
    claim = Flaky(worker.claim, times=0)
    monkeypatch.setattr(worker, "claim", claim)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))

    def started() -> list[str]:
        return [entry for entry in ran if entry.endswith(":started")]

    try:
        await until(lambda: len(started()) == 2)
        await asyncio.sleep(0.1)  # Several polls' time: nothing more is claimed.
        assert len(started()) == 2
        assert [args[0] for args in claim.args] == [2]
        gates[started()[0].removesuffix(":started")].set()
        await until(lambda: len(started()) == 3)
        assert [args[0] for args in claim.args][:2] == [2, 1]
    finally:
        for gate in gates.values():
            gate.set()
        stop.set()
        async with asyncio.timeout(5):
            await running
    assert 0 not in [args[0] for args in claim.args]


async def test_an_idle_worker_polls_for_new_work(tables: None) -> None:
    """With nothing due, the worker checks again every POLL_INTERVAL."""
    worker = Worker(MessagingSettings(**{**FAST, "POLL_INTERVAL": 0.01}))
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.sleep(0.05)
        await enqueue(remember, value=uuid.uuid4())
        await until(lambda: len(ran) == 1)
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await running


async def test_a_full_batch_is_relayed_again_at_once(
    broker_tables: MemoryBroker,
) -> None:
    """The relay goes again without waiting while batches come back full."""
    with override_settings(MESSAGING={**BROKERED, "PUBLISH_ON_COMMIT": False}):
        for _ in range(3):
            await enqueue(remember, value=uuid.uuid4())
    config = registry.settings(MessagingSettings).model_copy(
        update={"POLL_INTERVAL": 30, "BATCH_SIZE": 1}
    )
    stop = asyncio.Event()
    running = asyncio.create_task(
        BrokerWorkers([Worker(config)], ["default"], config).run(stop)
    )
    try:
        await until(lambda: len(ran) == 3)
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await running


async def test_runs_are_timed_and_logged(
    tables: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The debug log and the duration metric time the run; the context names it."""
    await enqueue(remember, value=uuid.uuid4())
    worker = Worker(MessagingSettings(**FAST))
    (claimed,) = await worker.claim(1)
    readings = iter([100.0, 100.5, 101.0])
    clock = SimpleNamespace(perf_counter=lambda: next(readings))
    monkeypatch.setattr(worker_module, "time", clock)
    seconds = "messaging_message_duration_seconds_sum"
    before = REGISTRY.get_sample_value(seconds, {"kind": "task"}) or 0

    with caplog.at_level(logging.DEBUG, "domainkit.contrib.messaging.worker"):
        await worker.process(claimed)

    assert caplog.messages == [f"Ran task {claimed.target} in 500.0ms"]
    assert REGISTRY.get_sample_value(seconds, {"kind": "task"}) == pytest.approx(
        before + 1.0
    )
    ((_, _, request),) = ran
    assert (request.method, request.path) == ("TASK", claimed.target)
