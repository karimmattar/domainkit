"""pytest fixtures for code that publishes events and queues background work.

Enable them next to ``domainkit.testing`` in the project's ``pyproject.toml``::

    [tool.pytest.ini_options]
    addopts = "-p domainkit.testing -p domainkit.contrib.messaging.testing"

The fixtures work in sync and async tests:

``captured_events``
    Every domain event published on the process-wide bus after a commit,
    in order: ``captured_events.of_type(UserCreated)``,
    ``captured_events.assert_published(UserCreated, username="ann")``.
``outbox``
    Every message committed during the test (deferred tasks, durable
    handler calls, ``enqueue``): ``outbox.queued(send_welcome)``,
    ``outbox.assert_queued(send_welcome, user_id=user.id)``,
    ``outbox.assert_not_queued(...)``. ``outbox.run_queued()`` (or ``await
    outbox.arun_queued()`` in async tests) runs every due message here, as
    a worker would, until none is left, chained steps included; with
    ``delayed=True`` it runs delayed ones and retries too. It raises
    ``QueuedWorkFailed`` if any run failed.
``execute_deferred``
    Runs what each commit queues right after it commits, before
    ``commit()`` (or ``enqueue``) returns. Failures are kept in
    ``execute_deferred.failures`` and fail the test at its end unless the
    test clears them.
"""

import asyncio
import contextvars
from collections.abc import Coroutine, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import pytest
from sqlalchemy import or_, update

from domainkit import clock
from domainkit.db.routing import router
from domainkit.ddd import DomainEvent, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management.base import run_async

from .outbox import watchers
from .registry import Chain, Task, TaskSpec, dump_arguments, qualified_name, spec_for
from .tables import PENDING, messages
from .worker import Claimed, Worker, _without_input

__all__ = [
    "CapturedEvents",
    "DeferredExecutor",
    "Drained",
    "Outbox",
    "QueuedMessage",
    "QueuedWorkFailed",
    "drain",
]

MAX_ROUNDS: Final = 100
"""Rounds ``run_queued`` makes before it decides the work never ends."""


class QueuedWorkFailed(AssertionError):
    """Queued work failed, or kept queueing more, while a test ran it."""


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """A message committed to an outbox during a test."""

    id: Any
    database: str
    kind: str
    """``task``, or ``event`` for a durable handler's call."""
    target: str
    """The task's key, or the durable handler's qualified name."""
    arguments: dict[str, Any]
    """The JSON arguments (the event, for a durable handler)."""
    queue: str
    priority: int
    available_at: datetime
    dedupe_key: str | None
    headers: dict[str, Any]


class CapturedEvents:
    """Domain events published after commits, in order."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[DomainEvent] = []

    async def record(self, event: DomainEvent) -> None:
        """Keep ``event`` (the bus calls this)."""
        self.events.append(event)

    def __iter__(self) -> Iterator[DomainEvent]:
        """Every captured event."""
        return iter(self.events)

    def __len__(self) -> int:
        """How many were captured."""
        return len(self.events)

    def of_type[E: DomainEvent](self, event_type: type[E]) -> list[E]:
        """The captured events of ``event_type`` (subclasses included)."""
        return [event for event in self.events if isinstance(event, event_type)]

    def assert_published[E: DomainEvent](
        self, event_type: type[E], **fields: object
    ) -> E:
        """Return the one ``event_type`` event with ``fields``; fail otherwise."""
        found = [
            event
            for event in self.of_type(event_type)
            if all(
                getattr(event, name, _MISSING) == value
                for name, value in fields.items()
            )
        ]
        if len(found) != 1:
            raise AssertionError(
                f"Expected one {event_type.__qualname__} with {fields}, found "
                f"{len(found)} among {[type(e).__qualname__ for e in self.events]}."
            )
        return found[0]

    def assert_not_published(self, event_type: type[DomainEvent]) -> None:
        """Fail if any ``event_type`` event was published."""
        if found := self.of_type(event_type):
            raise AssertionError(
                f"Expected no {event_type.__qualname__}, found {len(found)}."
            )

    def clear(self) -> None:
        """Forget what was captured so far."""
        self.events.clear()


_MISSING: Final = object()


def _target(task: Task | Chain | str) -> str:
    """The outbox target of a task, chain, durable handler, or key."""
    if isinstance(task, str):
        return task
    if isinstance(task, Chain):
        task = task.steps[0]
    try:
        return spec_for(task).key
    except ImproperlyConfigured:  # A durable handler.
        return qualified_name(task)


@dataclass(slots=True)
class Drained:
    """What one ``drain`` ran."""

    ran: int = 0
    failures: list[str] = field(default_factory=list)


class _Worker(Worker):
    """A worker that notes each failed run."""

    def __init__(self, failures: list[str], database: str) -> None:
        """Run ``database``'s outbox; add failures to ``failures``."""
        super().__init__(database=database)
        self.failures = failures

    async def _failed(
        self,
        message: Claimed,
        exc: BaseException,
        *,
        permanent: bool,
        spec: TaskSpec | None = None,
    ) -> str:
        """Note the failure, then retry or dead-letter as a worker does."""
        outcome = await super()._failed(message, exc, permanent=permanent, spec=spec)
        error = _without_input(exc)
        self.failures.append(
            f"{message.kind} {message.target} ({outcome}): "
            f"{type(error).__name__}: {error}"
        )
        return outcome


def _outboxes() -> list[str]:
    """Every database alias holding an outbox."""
    return list(
        dict.fromkeys(
            alias
            for target in router.hosting_targets()
            for alias in router.aliases_of(target)
        )
    )


async def drain(*, delayed: bool = False, max_rounds: int = MAX_ROUNDS) -> Drained:
    """Run every due message of every outbox here, until none is left.

    Each round claims up to ``BATCH_SIZE`` messages per outbox and runs
    them; what they queue runs in the next round. With ``delayed``, messages
    waiting for their time (delays, retries) are made due first, round after
    round. Periodic tasks are not queued.

    Raises:
        QueuedWorkFailed: messages were still due after ``max_rounds``.
    """
    drained = Drained()
    workers = [_Worker(drained.failures, alias) for alias in _outboxes()]
    for _ in range(max_rounds):
        ran = 0
        for worker in workers:
            if delayed:
                await _make_due(worker)
            claimed = await worker.claim(worker.config.BATCH_SIZE)
            await asyncio.gather(*(worker.process(message) for message in claimed))
            ran += len(claimed)
        if not ran:
            return drained
        drained.ran += ran
    raise QueuedWorkFailed(
        f"Queued work kept coming after {max_rounds} rounds ({drained.ran} runs): "
        "does a task queue itself?"
    )


async def _make_due(worker: Worker) -> None:
    """Make every waiting message of ``worker``'s outbox due now."""
    now = clock.now()
    statement = (
        update(messages)
        .where(
            messages.c.status == PENDING,
            messages.c.available_at > now,
            or_(messages.c.locked_until.is_(None), messages.c.locked_until < now),
        )
        .values(available_at=now)
    )
    async with worker.engine.begin() as connection:
        await connection.execute(statement)


def _sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run ``coroutine`` to the end from sync code, in a loop of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_async(coroutine)
    context = contextvars.copy_context()  # A frozen clock, a bound request.
    with ThreadPoolExecutor(max_workers=1) as pool:  # Called from a running loop.
        return pool.submit(context.run, run_async, coroutine).result()


class Outbox:
    """Messages committed during a test, and a way to run them."""

    def __init__(self) -> None:
        """Start with nothing seen."""
        self.messages: list[QueuedMessage] = []

    async def watch(self, database: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Keep the committed ``rows`` (messaging calls this)."""
        self.messages.extend(
            QueuedMessage(
                id=row["id"],
                database=database,
                kind=row["kind"],
                target=row["target"],
                arguments=dict(row["payload"]),
                queue=row["queue"],
                priority=row["priority"],
                available_at=row["available_at"],
                dedupe_key=row["dedupe_key"],
                headers=dict(row["headers"]),
            )
            for row in rows
        )

    def queued(self, task: Task | Chain | str | None = None) -> list[QueuedMessage]:
        """Messages committed so far, or those of ``task`` (a function or key)."""
        if task is None:
            return list(self.messages)
        target = _target(task)
        return [message for message in self.messages if message.target == target]

    def assert_queued(
        self, task: Task | Chain | str, **arguments: object
    ) -> QueuedMessage:
        """Return the one message of ``task`` whose arguments include ``arguments``.

        Values compare as stored: a UUID as its string, a model as its JSON.
        """
        expected = dump_arguments(arguments)
        found = [
            message
            for message in self.queued(task)
            if all(
                message.arguments.get(name, _MISSING) == value
                for name, value in expected.items()
            )
        ]
        if len(found) != 1:
            raise AssertionError(
                f"Expected one message of {_target(task)} with {expected}, found "
                f"{len(found)} among {[m.target for m in self.messages]}."
            )
        return found[0]

    def assert_not_queued(self, task: Task | Chain | str) -> None:
        """Fail if a message of ``task`` was committed."""
        if found := self.queued(task):
            raise AssertionError(f"Expected no {_target(task)}, found {len(found)}.")

    def clear(self) -> None:
        """Forget the messages seen so far (they stay in the outbox)."""
        self.messages.clear()

    async def arun_queued(
        self,
        *,
        delayed: bool = False,
        raise_errors: bool = True,
        max_rounds: int = MAX_ROUNDS,
    ) -> int:
        """Run the due messages here, as ``drain`` does; return how many ran.

        Raises:
            QueuedWorkFailed: a run failed (unless not ``raise_errors``), or
                work kept coming.
        """
        drained = await drain(delayed=delayed, max_rounds=max_rounds)
        if drained.failures and raise_errors:
            raise QueuedWorkFailed(
                "Queued work failed:\n" + "\n".join(drained.failures)
            )
        return drained.ran

    def run_queued(
        self,
        *,
        delayed: bool = False,
        raise_errors: bool = True,
        max_rounds: int = MAX_ROUNDS,
    ) -> int:
        """``arun_queued`` for sync tests."""
        return _sync(
            self.arun_queued(
                delayed=delayed, raise_errors=raise_errors, max_rounds=max_rounds
            )
        )


class DeferredExecutor:
    """Runs committed messages right after each commit, in the committing loop."""

    def __init__(self) -> None:
        """Start with no failures."""
        self.failures: list[str] = []
        self.ran = 0
        self._running = False

    async def watch(self, database: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Run what is due now (messaging calls this after each commit).

        Work that commits more messages while this runs is picked up by the
        same run.
        """
        if self._running:
            return
        self._running = True
        try:
            drained = await drain()
        finally:
            self._running = False
        self.ran += drained.ran
        self.failures.extend(drained.failures)


@pytest.fixture
def captured_events() -> Iterator[CapturedEvents]:
    """Capture the domain events published on the process-wide bus."""
    captured = CapturedEvents()
    event_bus.subscribe(DomainEvent, captured.record)
    yield captured
    event_bus.unsubscribe(DomainEvent, captured.record)


@pytest.fixture
def outbox() -> Iterator[Outbox]:
    """See the messages committed during the test, and run them."""
    seen = Outbox()
    watchers.append(seen.watch)
    yield seen
    watchers.remove(seen.watch)


@pytest.fixture
def execute_deferred() -> Iterator[DeferredExecutor]:
    """Run each commit's messages before ``commit()`` returns."""
    executor = DeferredExecutor()
    watchers.append(executor.watch)
    yield executor
    watchers.remove(executor.watch)
    if executor.failures:
        raise QueuedWorkFailed("Deferred work failed:\n" + "\n".join(executor.failures))
