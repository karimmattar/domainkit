"""The worker: claims due messages, runs them, and retries failures.

Delivery is at least once. A message whose worker crashes, or runs past
``VISIBILITY_TIMEOUT``, is claimed again, so durable handlers and tasks must
be idempotent. Failures are retried with exponential backoff and jitter;
after ``max_attempts`` the message is dead-lettered and reported. Messages
are claimed with ``FOR UPDATE SKIP LOCKED`` on PostgreSQL, so any number of
workers can share one outbox. There is no ordering guarantee between messages.
"""

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit.apps import registry
from domainkit.context import RequestContext, bind_request
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
    from .metrics import observe
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe(kind: str, outcome: str, seconds: float) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)
_random = secrets.SystemRandom()
_ERROR_LENGTH = 2000


class UnknownTarget(LookupError):
    """The message names a handler or task this process does not have."""


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


def _now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def backoff(attempts: int, config: MessagingSettings) -> float:
    """Seconds before retrying after ``attempts`` runs: doubling, capped, jittered."""
    ceiling = min(
        config.RETRY_MAX_SECONDS, config.RETRY_BASE_SECONDS * 2.0 ** (attempts - 1)
    )
    return ceiling * _random.uniform(0.5, 1.0)


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
    ) -> None:
        """Run the outbox of alias ``database`` (default: ``MESSAGING.DATABASE``).

        Every database that hosts domains has its own outbox; only the worker
        of ``MESSAGING.DATABASE`` schedules periodic tasks.
        """
        self.config = config or registry.settings(MessagingSettings)
        self.database = database or self.config.DATABASE
        self.bus = bus or event_bus
        self._scheduled: dict[str, int] = {}
        self._last_cleanup = float("-inf")

    @property
    def engine(self) -> AsyncEngine:
        """The running event loop's engine for this worker's outbox."""
        return connections.engine(self.database)

    @property
    def schedules(self) -> bool:
        """Whether this worker queues periodic tasks (the home outbox's does)."""
        return self.database == self.config.DATABASE

    async def run(self, stop: asyncio.Event) -> None:
        """Process messages until ``stop`` is set, then finish the running ones."""
        running: set[asyncio.Task[None]] = set()
        stopping = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                await self.schedule_periodic()
                await self._cleanup_when_due()
                free = min(
                    self.config.CONCURRENCY - len(running), self.config.BATCH_SIZE
                )
                claimed = await self.claim(free) if free > 0 else []
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
        finally:
            stopping.cancel()
        if running:
            await asyncio.gather(*running)

    async def run_once(self) -> int:
        """Run every message that is due now; return how many ran."""
        await self.schedule_periodic()
        processed = 0
        while claimed := await self.claim(self.config.BATCH_SIZE):
            await asyncio.gather(*(self.process(message) for message in claimed))
            processed += len(claimed)
        return processed

    async def claim(self, limit: int) -> list[Claimed]:
        """Lock up to ``limit`` due messages for this worker."""
        now = _now()
        lock_id = uuid.uuid4()
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
        statement = (
            update(messages)
            .where(messages.c.id.in_(due.scalar_subquery()))
            .values(
                locked_until=now + timedelta(seconds=self.config.VISIBILITY_TIMEOUT),
                lock_id=lock_id,
                attempts=messages.c.attempts + 1,
            )
            .returning(
                messages.c.id,
                messages.c.kind,
                messages.c.target,
                messages.c.payload,
                messages.c.headers,
                messages.c.attempts,
                messages.c.max_attempts,
            )
        )
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).all()
        return [Claimed(**row._mapping, lock_id=lock_id) for row in rows]

    async def process(self, message: Claimed) -> None:
        """Run one message under its original request id and actor; never raises."""
        context = RequestContext(
            request_id=message.headers.get("request_id") or message.id.hex,
            client_ip=None,
            method=message.kind.upper(),
            path=message.target,
            actor=message.headers.get("actor"),
        )
        started = time.perf_counter()
        with (
            bind_request(context),
            span("message.process", kind=message.kind, target=message.target),
        ):
            try:
                async with asyncio.timeout(self.config.TASK_TIMEOUT):
                    await self._execute(message)
            except UnknownTarget as exc:
                outcome = await self._failed(message, exc, permanent=True)
            except Exception as exc:  # noqa: BLE001 - logged and retried by _failed.
                outcome = await self._failed(message, exc, permanent=False)
            else:
                await self._release(message, status=DONE, finished_at=_now())
                outcome = DONE
                logger.debug(
                    "Ran %s %s in %.1fms",
                    message.kind,
                    message.target,
                    (time.perf_counter() - started) * 1000,
                )
        observe(message.kind, outcome, time.perf_counter() - started)

    async def _execute(self, message: Claimed) -> None:
        """Call the handler or task ``message`` names."""
        if message.kind == "event":
            handler = durable_handlers(self.bus).get(message.target)
            event_type = _event_classes().get(message.headers.get("event_type", ""))
            if handler is None or event_type is None:
                raise UnknownTarget(f"No durable handler {message.target!r}.")
            await handler[1](load_event(event_type, message.payload))
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
        for spec, every in periodic_tasks():
            seconds = every.total_seconds()
            slot = int(now.timestamp() // seconds)
            if self._scheduled.get(spec.key) == slot:
                continue
            self._scheduled[spec.key] = slot
            rows.append(
                message_row(
                    kind="task",
                    target=spec.key,
                    payload={},
                    headers={"request_id": None, "actor": None},
                    max_attempts=spec.max_attempts or self.config.MAX_ATTEMPTS,
                    dedupe_key=f"periodic:{spec.key}:{slot}",
                    available_at=datetime.fromtimestamp(slot * seconds, UTC),
                )
            )
        if rows:
            async with self.engine.begin() as connection:
                await write(connection, connection.dialect.name, rows)

    async def cleanup(self) -> int:
        """Delete finished messages older than ``RETENTION_SECONDS``."""
        cutoff = _now() - timedelta(seconds=self.config.RETENTION_SECONDS)
        statement = delete(messages).where(
            messages.c.status == DONE, messages.c.finished_at < cutoff
        )
        async with self.engine.begin() as connection:
            result = await connection.execute(statement)
        return int(result.rowcount)

    async def _cleanup_when_due(self) -> None:
        """Run ``cleanup`` every ``CLEANUP_INTERVAL`` seconds."""
        if time.monotonic() - self._last_cleanup >= self.config.CLEANUP_INTERVAL:
            self._last_cleanup = time.monotonic()
            await self.cleanup()
