"""The worker: claims due messages, runs them, and retries failures.

Delivery is at least once. A message whose worker crashes, or runs past
``VISIBILITY_TIMEOUT``, is claimed again, so durable handlers and tasks must
be idempotent. Failures are retried with exponential backoff and jitter;
after ``max_attempts`` the message is dead-lettered and reported. Messages
are claimed with ``FOR UPDATE SKIP LOCKED`` on PostgreSQL, so any number of
workers can share one outbox. There is no ordering guarantee between messages.

A worker outlives database outages: it logs each failure, waits longer each
time (up to ``OUTAGE_MAX_PAUSE``), and carries on. A message whose outcome
could not be written runs again once its lock expires.

A stopping worker gives running messages ``SHUTDOWN_TIMEOUT`` to finish, then
cancels the rest. A cancelled message goes back to the outbox, due at once,
and the cut-short run does not count as an attempt.
"""

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from weakref import WeakKeyDictionary

from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit import clock
from domainkit.apps import registry
from domainkit.broker import Broker, BrokerError
from domainkit.context import SYSTEM_ACTOR, RequestContext, bind_request
from domainkit.db import connections
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.reporting import report_error
from domainkit.tracing import span

from .outbox import message_row, write
from .registry import (
    call_task,
    durable_handlers,
    get_task,
    load_event,
    periodic_tasks,
    qualified_name,
)
from .settings import MessagingSettings
from .tables import DEAD, DONE, PENDING, messages

try:
    from .metrics import observe, observe_relay
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe(kind: str, outcome: str, seconds: float) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

    def observe_relay(outcome: str, count: int = 1) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)
_random = secrets.SystemRandom()
_ERROR_LENGTH = 2000
OUTAGE_MAX_PAUSE: Final = 60.0
"""Longest wait between attempts while an outbox's database fails."""
UNRECORDED: Final = "unrecorded"
"""The outcome of a message that ran but whose outcome could not be written."""
INTERRUPTED: Final = "interrupted"
"""The outcome of a message cancelled before it finished (a stopping worker)."""
RELEASE_TIMEOUT: Final = 5.0
"""Seconds a cancelled message may take to go back to the outbox."""


class UnknownTarget(LookupError):
    """The message names a handler or task this process does not have."""


class AttemptsExhausted(RuntimeError):
    """The worker stopped (crashed, killed) during the message's last attempt."""


class InvalidData(ValueError):
    """Data failed validation; the message names fields and rules, not values."""


def _without_input(exc: BaseException) -> BaseException:
    """``exc``, or for a pydantic ``ValidationError``, one without input values.

    A ``ValidationError``'s text quotes the data it refused, often a task's
    personal arguments, into ``last_error`` (kept with dead letters), logs,
    and error reports. ``InvalidData`` keeps its locations, messages, and
    traceback instead.
    """
    if not isinstance(exc, ValidationError):
        return exc
    problems = "; ".join(  # Each error's location and message, nothing else.
        f"{'.'.join(map(str, error['loc'])) or '(value)'}: {error['msg']}"
        for error in exc.errors()
    )
    return InvalidData(f"{exc.title}: {problems}").with_traceback(exc.__traceback__)


@dataclass(frozen=True, slots=True)
class Claimed:
    """A message this worker holds until ``lock_id`` is released."""

    id: uuid.UUID
    kind: str
    target: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    attempts: int
    max_attempts: int
    lock_id: uuid.UUID


_CLAIMED: Final = (
    messages.c.id,
    messages.c.kind,
    messages.c.target,
    messages.c.payload,
    messages.c.headers,
    messages.c.attempts,
    messages.c.max_attempts,
)
"""The columns a claim returns (``Claimed`` without its lock)."""


def _now() -> datetime:
    """Return the current time in UTC."""
    return clock.now()


def backoff(attempts: int, config: MessagingSettings) -> float:
    """Seconds before retrying after ``attempts`` runs: doubling, capped, jittered."""
    ceiling = min(
        config.RETRY_MAX_SECONDS, config.RETRY_BASE_SECONDS * 2.0 ** (attempts - 1)
    )
    return ceiling * _random.uniform(0.5, 1.0)


class Outage:
    """Failures in a row to reach one outbox, and the pause before trying again."""

    def __init__(self, database: str, poll_interval: float) -> None:
        """Pause ``poll_interval`` after the first failure, doubling after each."""
        self.database = database
        self.poll_interval = poll_interval
        self.failures = 0

    def pause(self) -> float:
        """Seconds to wait after ``failures`` failures in a row."""
        return min(self.poll_interval * 2.0 ** (self.failures - 1), OUTAGE_MAX_PAUSE)

    async def failed(self, exc: Exception, stop: asyncio.Event) -> None:
        """Log the failure, then wait (less, if ``stop`` is set meanwhile)."""
        self.failures += 1
        pause = self.pause()
        logger.error(
            "Could not reach the outbox of %r (%d failure%s in a row); "
            "trying again in %gs",
            self.database,
            self.failures,
            "" if self.failures == 1 else "s",
            pause,
            exc_info=exc,
        )
        with suppress(TimeoutError):
            async with asyncio.timeout(pause):
                await stop.wait()

    def recovered(self) -> None:
        """Log the end of an outage, if one was going on."""
        if self.failures:
            logger.warning(
                "The outbox of %r answers again after %d failures",
                self.database,
                self.failures,
            )
            self.failures = 0


async def finish(tasks: set[asyncio.Task[None]], grace: float) -> None:
    """Give ``tasks`` ``grace`` seconds to end, then cancel and await the rest."""
    if tasks:
        await asyncio.wait(tasks, timeout=grace)
    late = [task for task in tasks if not task.done()]
    if late:
        logger.warning(
            "Stopping %d running message(s) that did not finish in time; they "
            "run again",
            len(late),
        )
    for task in late:
        task.cancel()
    await asyncio.gather(*late, return_exceptions=True)


def _event_classes(base: type[DomainEvent] = DomainEvent) -> dict[str, type[Any]]:
    """Map every loaded event class to its qualified name."""
    found: dict[str, type[Any]] = {}
    for cls in base.__subclasses__():
        found[qualified_name(cls)] = cls
        found.update(_event_classes(cls))
    return found


class Worker:
    """Run outbox messages of one database."""

    def __init__(
        self,
        config: MessagingSettings | None = None,
        *,
        database: str | None = None,
        bus: EventBus | None = None,
        queues: Iterable[str] | None = None,
    ) -> None:
        """Run the outbox of alias ``database`` (default: ``MESSAGING.DATABASE``).

        Every database that hosts domains has its own outbox; only the worker
        of ``MESSAGING.DATABASE`` schedules periodic tasks. ``queues`` limits
        the messages it claims (default: every queue).
        """
        self.config = config or registry.settings(MessagingSettings)
        self.database = database or self.config.DATABASE
        self.bus = bus or event_bus
        self.queues = None if queues is None else frozenset(queues)
        self._scheduled: dict[str, int] = {}
        self._last_cleanup = float("-inf")
        self._declared: WeakKeyDictionary[Broker, set[str]] = WeakKeyDictionary()

    @property
    def engine(self) -> AsyncEngine:
        """The running event loop's engine for this worker's outbox."""
        return connections.engine(self.database)

    @property
    def schedules(self) -> bool:
        """Whether this worker queues periodic tasks (the home outbox's does)."""
        return self.database == self.config.DATABASE

    def outage(self) -> Outage:
        """A new outage tracker for this worker's outbox."""
        return Outage(self.database, self.config.POLL_INTERVAL)

    async def run(self, stop: asyncio.Event) -> None:
        """Process messages until ``stop`` is set, then finish the running ones.

        They get ``SHUTDOWN_TIMEOUT`` seconds; then, or when ``run`` itself is
        cancelled, the rest are cancelled and go back to the outbox.
        """
        running: set[asyncio.Task[None]] = set()
        stopping = asyncio.create_task(stop.wait())
        outage = self.outage()
        try:
            while not stop.is_set():
                free = min(
                    self.config.CONCURRENCY - len(running), self.config.BATCH_SIZE
                )
                try:
                    await self.schedule_periodic()
                    await self.run_cleanup_when_due()
                    # A full worker waits for a free slot, so free is at least 1.
                    claimed = await self.claim(free)
                except Exception as exc:  # noqa: BLE001 - logged; the loop waits.
                    await outage.failed(exc, stop)
                    continue
                outage.recovered()
                for message in claimed:
                    task = asyncio.create_task(self.process(message))
                    running.add(task)
                    task.add_done_callback(running.discard)
                if len(claimed) < free:
                    # Nothing more is due: poll again later (or stop sooner).
                    await asyncio.wait({stopping}, timeout=self.config.POLL_INTERVAL)
                elif len(running) >= self.config.CONCURRENCY:
                    # Every slot is busy: claim again as soon as one frees up.
                    await asyncio.wait(
                        {stopping, *running}, return_when=asyncio.FIRST_COMPLETED
                    )
            await finish(running, self.config.SHUTDOWN_TIMEOUT)
        finally:
            stopping.cancel()
            await finish(running, 0)  # Cancelled: stop the running ones now.

    async def run_once(self) -> int:
        """Run every message that is due now; return how many ran."""
        await self.schedule_periodic()
        processed = 0
        while claimed := await self.claim(self.config.BATCH_SIZE):
            await asyncio.gather(*(self.process(message) for message in claimed))
            processed += len(claimed)
        return processed

    def _due(
        self, now: datetime, limit: int | None, ids: Sequence[uuid.UUID] = ()
    ) -> Any:  # noqa: ANN401 - a subquery.
        """Select due, unlocked messages of this worker's queues (of ``ids``)."""
        due = (
            select(messages.c.id)
            .where(
                messages.c.status == PENDING,
                messages.c.available_at <= now,
                or_(messages.c.locked_until.is_(None), messages.c.locked_until < now),
            )
            .order_by(messages.c.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if ids:
            due = due.where(messages.c.id.in_(ids))
        if self.queues is not None:
            due = due.where(messages.c.queue.in_(self.queues))
        return due.scalar_subquery()

    async def claim(self, limit: int) -> list[Claimed]:
        """Lock up to ``limit`` due messages for this worker."""
        now = _now()
        lock_id = uuid.uuid4()
        statement = (
            update(messages)
            .where(messages.c.id.in_(self._due(now, limit)))
            .values(
                locked_until=now + timedelta(seconds=self.config.VISIBILITY_TIMEOUT),
                lock_id=lock_id,
                attempts=messages.c.attempts + 1,
            )
            .returning(*_CLAIMED)
        )
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).all()
        return [Claimed(**row._mapping, lock_id=lock_id) for row in rows]

    async def relay(self, broker: Broker, limit: int) -> int:
        """Publish up to ``limit`` due messages to ``broker``; return how many went.

        Each message is locked for ``REPUBLISH_AFTER`` and published as a
        pointer: its id, this database, and the lock. Failed publications are
        unlocked, so the next round tries them again.
        """
        now = _now()
        return await self._relay(broker, now, self._due(now, limit))

    async def relay_ids(self, broker: Broker, ids: Sequence[uuid.UUID]) -> int:
        """Publish just-committed messages now, those due and not yet taken."""
        if not ids:
            return 0
        now = _now()
        return await self._relay(broker, now, self._due(now, None, ids))

    async def _relay(self, broker: Broker, now: datetime, due: Any) -> int:  # noqa: ANN401
        """Lock the ``due`` messages, publish their pointers, unlock failures."""
        lock_id = uuid.uuid4()
        statement = (
            update(messages)
            .where(messages.c.id.in_(due))
            .values(
                locked_until=now + timedelta(seconds=self.config.REPUBLISH_AFTER),
                lock_id=lock_id,
            )
            .returning(messages.c.id, messages.c.queue)
        )
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).all()
        if not rows:
            return 0
        declared = self._declared.setdefault(broker, set())
        if new := {row.queue for row in rows} - declared:
            try:
                await broker.declare(new)
            except BrokerError as exc:
                await self._unlock([row.id for row in rows], lock_id, exc)
                return 0
            declared |= new
        sent = await asyncio.gather(
            *(self._publish(broker, row.id, row.queue, lock_id) for row in rows)
        )
        failed = [row for row, ok in zip(rows, sent, strict=True) if not ok]
        if failed:
            # Declare again next time: the queue may be gone (deleted, purged).
            declared.difference_update(row.queue for row in failed)
            await self._unlock([row.id for row in failed], lock_id, None)
        observe_relay("published", len(rows) - len(failed))
        return len(rows) - len(failed)

    async def _publish(
        self, broker: Broker, message_id: uuid.UUID, queue: str, lock_id: uuid.UUID
    ) -> bool:
        """Publish one pointer; report whether the broker took it."""
        pointer = {
            "id": str(message_id),
            "database": self.database,
            "lock": str(lock_id),
        }
        try:
            await broker.publish(
                queue, json.dumps(pointer).encode(), message_id=str(message_id)
            )
        except BrokerError as exc:
            logger.warning(
                "Could not publish %s to queue %r: %s", message_id, queue, exc
            )
            return False
        return True

    async def _unlock(
        self, ids: list[uuid.UUID], lock_id: uuid.UUID, exc: BrokerError | None
    ) -> None:
        """Release messages that did not reach the broker."""
        if exc is not None:
            logger.warning("Could not declare queues: %s", exc)
        observe_relay("failed", len(ids))
        statement = (
            update(messages)
            .where(messages.c.id.in_(ids), messages.c.lock_id == lock_id)
            .values(locked_until=None, lock_id=None)
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def take_over(
        self, message_id: uuid.UUID, relay_lock: uuid.UUID
    ) -> Claimed | None:
        """Claim a delivered message, unless it ran, moved on, or is running.

        The delivery must carry the lock the relay published it under, or
        that lock must have expired; anything else is a stale duplicate.
        """
        now = _now()
        lock_id = uuid.uuid4()
        statement = (
            update(messages)
            .where(
                messages.c.id == message_id,
                messages.c.status == PENDING,
                messages.c.available_at <= now,
                or_(messages.c.lock_id == relay_lock, messages.c.locked_until < now),
            )
            .values(
                locked_until=now + timedelta(seconds=self.config.VISIBILITY_TIMEOUT),
                lock_id=lock_id,
                attempts=messages.c.attempts + 1,
            )
            .returning(*_CLAIMED)
        )
        async with self.engine.begin() as connection:
            row = (await connection.execute(statement)).first()
        return None if row is None else Claimed(**row._mapping, lock_id=lock_id)

    async def process(self, message: Claimed) -> None:
        """Run one message under its original request id and actor; never raises.

        A message no request queued (a periodic task, a command's) runs as
        ``SYSTEM_ACTOR``; one an anonymous request queued, without an actor.

        If its outcome cannot be written, the message runs again once its
        lock expires (``VISIBILITY_TIMEOUT``). If it is cancelled, it goes
        back to the outbox before the cancellation goes on.
        """
        context = RequestContext(
            request_id=message.headers.get("request_id") or message.id.hex,
            client_ip=None,
            method=message.kind.upper(),
            path=message.target,
            actor=message.headers.get("actor")
            or (None if message.headers.get("request_id") else SYSTEM_ACTOR),
        )
        started = time.perf_counter()
        with (
            bind_request(context),
            span("message.process", kind=message.kind, target=message.target),
        ):
            try:
                outcome = await self._run(message, started)
            except asyncio.CancelledError:
                await self._interrupted(message)
                observe(message.kind, INTERRUPTED, time.perf_counter() - started)
                raise
            except Exception as exc:  # The lock runs the message again later.
                logger.error(
                    "Could not record the outcome of %s %s; it runs again once "
                    "its lock expires",
                    message.kind,
                    message.target,
                    exc_info=exc,
                )
                outcome = UNRECORDED
        observe(message.kind, outcome, time.perf_counter() - started)

    async def _run(self, message: Claimed, started: float) -> str:
        """Run ``message`` and record its outcome; return the outcome."""
        try:
            if message.attempts > message.max_attempts:
                raise AttemptsExhausted(
                    "The worker stopped during the last attempt (a crash, "
                    "or a kill before TASK_TIMEOUT)."
                )
            async with asyncio.timeout(self.config.TASK_TIMEOUT):
                await self._execute(message)
        except Exception as exc:  # noqa: BLE001 - logged and retried by _failed.
            permanent = isinstance(exc, UnknownTarget | AttemptsExhausted)
            return await self._failed(message, exc, permanent=permanent)
        await self._release(message, status=DONE, finished_at=_now())
        logger.debug(
            "Ran %s %s in %.1fms",
            message.kind,
            message.target,
            (time.perf_counter() - started) * 1000,
        )
        return DONE

    async def _execute(self, message: Claimed) -> None:
        """Call the handler or task ``message`` names."""
        if message.kind == "event":
            handler = durable_handlers(self.bus).get(message.target)
            event_type = _event_classes().get(message.headers.get("event_type", ""))
            if handler is None or event_type is None:
                raise UnknownTarget(f"No durable handler {message.target!r}.")
            handled, function = handler
            if not issubclass(event_type, handled):
                # Only an outbox row written by hand can pair them.
                raise UnknownTarget(
                    f"{message.target!r} does not handle {event_type.__qualname__}."
                )
            await function(load_event(event_type, message.payload))
        elif message.kind == "task":
            spec = get_task(message.target)
            if spec is None:
                raise UnknownTarget(f"No task {message.target!r}.")
            await call_task(spec.function, message.payload)
        else:
            raise UnknownTarget(f"Unknown message kind {message.kind!r}.")

    async def _failed(
        self, message: Claimed, exc: BaseException, *, permanent: bool
    ) -> str:
        """Schedule a retry, or dead-letter and report; return the outcome."""
        exc = _without_input(exc)
        error = f"{type(exc).__name__}: {exc}"[:_ERROR_LENGTH]
        if permanent or message.attempts >= message.max_attempts:
            await self._release(
                message, status=DEAD, finished_at=_now(), last_error=error
            )
            logger.error(
                "Dead-lettered %s %s after %d attempts",
                message.kind,
                message.target,
                message.attempts,
                exc_info=exc,
            )
            report_error(exc, message_id=str(message.id), target=message.target)
            return DEAD
        delay = backoff(message.attempts, self.config)
        await self._release(
            message,
            status=PENDING,
            available_at=_now() + timedelta(seconds=delay),
            last_error=error,
        )
        logger.warning(
            "%s %s failed (attempt %d of %d); retrying in %.1fs",
            message.kind,
            message.target,
            message.attempts,
            message.max_attempts,
            delay,
            exc_info=exc,
        )
        return "retry"

    async def _interrupted(self, message: Claimed) -> None:
        """Give a cancelled message back, its attempt not counted.

        It was due when claimed, so it is due again at once. If the outbox
        cannot take it in ``RELEASE_TIMEOUT``, it runs again once its lock
        expires.
        """
        try:
            async with asyncio.timeout(RELEASE_TIMEOUT):
                await self._release(
                    message, status=PENDING, attempts=messages.c.attempts - 1
                )
        except Exception as exc:  # Logged; the message's lock expires.
            logger.error(
                "Could not give back %s %s after stopping it; it runs again once "
                "its lock expires",
                message.kind,
                message.target,
                exc_info=exc,
            )

    async def _release(self, message: Claimed, **values: object) -> None:
        """Update ``message`` unless another worker reclaimed it meanwhile."""
        statement = (
            update(messages)
            .where(messages.c.id == message.id, messages.c.lock_id == message.lock_id)
            .values(locked_until=None, lock_id=None, **values)
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def schedule_periodic(self) -> None:
        """Queue each periodic task once per interval, across all workers."""
        if not self.schedules:
            return
        now = _now()
        rows = []
        slots = {}
        for spec, every in periodic_tasks():
            seconds = every.total_seconds()
            slot = int(now.timestamp() / seconds)
            if self._scheduled.get(spec.key) == slot:
                continue
            slots[spec.key] = slot
            rows.append(
                message_row(
                    kind="task",
                    target=spec.key,
                    payload={},
                    headers={},  # No request or actor caused it.
                    max_attempts=spec.max_attempts or self.config.MAX_ATTEMPTS,
                    queue=spec.queue,
                    dedupe_key=f"periodic:{spec.key}:{slot}",
                    available_at=datetime.fromtimestamp(slot * seconds, UTC),
                )
            )
        if rows:
            async with self.engine.begin() as connection:
                await write(connection, connection.dialect.name, rows)
            # Only now: a failed write is tried again on the next round.
            self._scheduled.update(slots)

    async def cleanup(self) -> int:
        """Delete finished messages older than ``RETENTION_SECONDS``."""
        cutoff = _now() - timedelta(seconds=self.config.RETENTION_SECONDS)
        statement = delete(messages).where(
            messages.c.status == DONE, messages.c.finished_at < cutoff
        )
        async with self.engine.begin() as connection:
            result = await connection.execute(statement)
        return int(result.rowcount)

    async def run_cleanup_when_due(self) -> None:
        """Run ``cleanup`` every ``CLEANUP_INTERVAL`` seconds."""
        if time.monotonic() - self._last_cleanup >= self.config.CLEANUP_INTERVAL:
            self._last_cleanup = time.monotonic()
            await self.cleanup()
