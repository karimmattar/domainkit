"""The worker: claims due messages, runs them, and retries failures.

Delivery is at least once. A message whose worker crashes, or runs past
``VISIBILITY_TIMEOUT``, is claimed again, so durable handlers and tasks must
be idempotent. A commit a handler makes on the outbox's database is refused
once another worker took its message over (``LeaseLost``), and a handler
marked ``inbox`` commits its database work once per message (``runs``).
Failures are retried with exponential backoff and jitter;
after ``max_attempts`` the message is dead-lettered and reported. Messages
are claimed with ``FOR UPDATE SKIP LOCKED`` on PostgreSQL, so any number of
workers can share one outbox. There is no ordering guarantee between messages.

A worker outlives database outages: it logs each failure, waits longer each
time (up to ``OUTAGE_MAX_PAUSE``), and carries on. A message whose outcome
could not be written runs again once its lock expires.

A stopping worker gives running messages ``SHUTDOWN_TIMEOUT`` to finish, then
cancels the rest. A cancelled message goes back to the outbox, due at once,
and the cut-short run does not count as an attempt.

A worker runs at most its ``Budget`` of messages at once (``CONCURRENCY``),
a budget ``runworker`` shares between every outbox and queue of the
process. On PostgreSQL the database's clock decides what is due and when
locks expire (``dbclock``), and commits wake idle workers through
``LISTEN``/``NOTIFY``; polling stays as the fallback. Old messages are
deleted in small batches by a task of their own, never by the claim loop.

Tasks may set their own timeout, retry policy, priority, and rate (see
``task``). A task over its rate goes back to the outbox until the rate
allows it, without spending an attempt. A task in a chain writes the next
step in the transaction that records it as done.
"""

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakKeyDictionary

from pydantic import ValidationError
from sqlalchemy import (
    DateTime,
    Delete,
    delete,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from domainkit import clock, deadlines
from domainkit.apps import registry
from domainkit.broker import Broker, BrokerError
from domainkit.conf import settings
from domainkit.context import SYSTEM_ACTOR, RequestContext, bind_request
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.reporting import report_error
from domainkit.tasks import spawn
from domainkit.tracing import remote_parent, span, trace_headers
from domainkit.utils.module_loading import import_string

from . import dbclock
from .budget import Budget
from .outbox import CHANNEL, message_row, notifies, notify_committed, write
from .registry import (
    CHAIN,
    TaskSpec,
    all_tasks,
    call_task,
    cron_tasks,
    durable_handlers,
    get_task,
    load_event,
    next_arguments,
    periodic_tasks,
    qualified_name,
)
from .retry import Backoff, retryable
from .runs import (
    AlreadyHandled,
    Run,
    forget,
    prune_statement,
    rows_of,
    running,
    unfinished,
    uses_inbox,
    was_handled,
)
from .settings import MessagingSettings
from .tables import (
    BLANKED,
    DEAD,
    DEFAULT_QUEUE,
    DONE,
    FINISHED,
    PENDING,
    SENSITIVE,
    messages,
)

if TYPE_CHECKING:
    from domainkit.http.ratelimit import Backend

try:
    from .metrics import observe, observe_relay
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe(kind: str, outcome: str, seconds: float) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

    def observe_relay(outcome: str, count: int = 1) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)
_ERROR_LENGTH = 2000
OUTAGE_MAX_PAUSE: Final = 60.0
"""Longest wait between attempts while an outbox's database fails."""
UNRECORDED: Final = "unrecorded"
"""The outcome of a message that ran but whose outcome could not be written."""
INTERRUPTED: Final = "interrupted"
"""The outcome of a message cancelled before it finished (a stopping worker)."""
RELEASE_TIMEOUT: Final = 5.0
"""Seconds a cancelled message may take to go back to the outbox.

Unlocking what a relay could not publish gets as long.
"""
LEASE_MARGIN: Final = 5.0
"""Seconds a relay's lock outlasts its broker's ``publish_timeout``."""
HELPER_GRACE: Final = 5.0
"""Seconds a stopping worker lets its relays, cleanup, and listener finish.

They all watch for the stop, so they end at their next step; one still
busy after this is cancelled. A cancelled query costs its connection.
"""
CLEANUP_START: Final = 60.0
"""Most seconds a worker waits before its first cleanup (or ``CLEANUP_INTERVAL``).

Workers started together (a deploy) spread their first cleanups over the
second half of it, and none competes with the first claims for locks.
"""
_random = secrets.SystemRandom()
THROTTLED: Final = "throttled"
"""The outcome of a task held back by its rate; it runs later."""
HANDLED: Final = "handled"
"""The outcome of a message the inbox shows a run committed before; it is done."""
SUPERSEDED: Final = "superseded"
"""The outcome of a run that lost its lease: another worker holds the message."""


class UnknownTarget(LookupError, DomainkitError):
    """The message names a handler or task this process does not have."""


class AttemptsExhausted(RuntimeError, DomainkitError):
    """The worker stopped (crashed, killed) during the message's last attempt."""


class InvalidData(ValueError, DomainkitError):
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


def _sensitive(message: "Claimed", spec: TaskSpec | None) -> bool:
    """Whether ``message``'s arguments are blanked when it dies.

    Its task is ``sensitive``, or it is a chain's step after one: it was
    handed what that step had, or made from it.
    """
    return (spec is not None and spec.sensitive) or bool(message.headers.get(SENSITIVE))


@dataclass(frozen=True, slots=True)
class Attempt:
    """The run a worker is making of a message, as its task sees it."""

    message_id: uuid.UUID
    number: int
    """1 for the first run; retries count up."""
    max_attempts: int

    @property
    def last(self) -> bool:
        """Whether a failure now dead-letters the message."""
        return self.number >= self.max_attempts


_attempt: ContextVar[Attempt | None] = ContextVar("domainkit_attempt", default=None)


def current_attempt() -> Attempt | None:
    """The run of the message being processed, or None outside the worker."""
    return _attempt.get()


@contextmanager
def _attempting(message: "Claimed") -> Iterator[None]:
    """Make ``message``'s run the current attempt in the block."""
    token = _attempt.set(Attempt(message.id, message.attempts, message.max_attempts))
    try:
        yield
    finally:
        _attempt.reset(token)


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


SETTLE_TASK: Final = "domainkit.messaging.settle"
"""The name of the tasks that give leases back (``_shielded``)."""


async def _shielded(work: Coroutine[Any, Any, None], what: str) -> None:
    """Run ``work`` to the end within ``RELEASE_TIMEOUT``, even if cancelled.

    Neither the caller's cancellation nor the shutdown's stops it: a lock
    left behind would hold its messages until it expires. It runs as an
    owned task (``domainkit.tasks``), so the shutdown's ``tasks`` phase
    waits for it before the ``resources`` phase closes the database it
    writes to. A failure is logged, never raised.
    """

    async def bounded() -> None:
        try:
            async with asyncio.timeout(RELEASE_TIMEOUT):
                await work
        except Exception as exc:  # Logged; the locks expire.
            logger.error("Could not %s; their locks expire", what, exc_info=exc)

    async def settle() -> None:
        running = asyncio.ensure_future(bounded())
        while not running.done():  # Bounded: cancelling it waits for the end.
            with suppress(asyncio.CancelledError):
                await asyncio.shield(running)

    await asyncio.shield(spawn(settle(), name=SETTLE_TASK))


def backoff(attempts: int, config: MessagingSettings) -> float:
    """Seconds before retrying after ``attempts`` runs: doubling, capped, jittered."""
    return Backoff(config.RETRY_BASE_SECONDS, config.RETRY_MAX_SECONDS).delay(attempts)


class Outage:
    """Failures in a row to reach one outbox, and the pause before trying again."""

    def __init__(
        self,
        database: str,
        poll_interval: float,
        *,
        doing: str = "reach the outbox of",
        meanwhile: str = "trying again",
        back: str = "The outbox of %r answers again",
    ) -> None:
        """Pause ``poll_interval`` after the first failure, doubling after each.

        The log says what failed (``Could not <doing> <database>``), what
        happens ``meanwhile``, and when it works again (``back``, with the
        database for ``%r``).
        """
        self.database = database
        self.poll_interval = poll_interval
        self.doing = doing
        self.meanwhile = meanwhile
        self.back = back
        self.failures = 0

    def pause(self) -> float:
        """Seconds to wait after ``failures`` failures in a row."""
        return min(self.poll_interval * 2.0 ** (self.failures - 1), OUTAGE_MAX_PAUSE)

    async def failed(self, exc: Exception, stop: asyncio.Event) -> None:
        """Log the failure, then wait (less, if ``stop`` is set meanwhile)."""
        self.failures += 1
        pause = self.pause()
        logger.error(
            "Could not %s %r (%d failure%s in a row); %s in %gs",
            self.doing,
            self.database,
            self.failures,
            "" if self.failures == 1 else "s",
            self.meanwhile,
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
                "%s after %d failures", self.back % (self.database,), self.failures
            )
            self.failures = 0


class Wake(asyncio.Event):
    """Ends an idle worker's wait: a commit, the stop, or the next poll.

    ``listening`` is true while ``listen`` holds a connection that hears
    commits to this outbox, and false before, after, and between them.
    """

    def __init__(self) -> None:
        """Start unset, and not listening."""
        super().__init__()
        self.listening = False


class IdlePolls:
    """How long an idle worker waits before it polls again (PERF-08).

    While a listener hears commits, each poll that finds nothing doubles
    the next wait, from ``POLL_INTERVAL`` up to ``POLL_INTERVAL_IDLE``, and
    a claim or a wake-up starts again from ``POLL_INTERVAL``. Without a
    listener, or without ``POLL_INTERVAL_IDLE``, every wait is
    ``POLL_INTERVAL``. A message that becomes due with nobody told, a
    notification that never arrives included, waits ``POLL_INTERVAL_IDLE``
    at most.
    """

    def __init__(self, config: MessagingSettings) -> None:
        """Wait ``POLL_INTERVAL`` first."""
        self.first = config.POLL_INTERVAL
        self.most = config.POLL_INTERVAL_IDLE or config.POLL_INTERVAL
        self._next = self.first

    def wait(self, listening: bool) -> float:
        """Seconds to wait after a poll that found nothing more to do.

        ``listening``: whether a listener hears commits right now.
        """
        if not listening:
            self._next = self.first
            return self.first
        wait = self._next
        self._next = min(wait * 2.0, self.most)
        return wait

    def reset(self) -> None:
        """Wait ``POLL_INTERVAL`` next: there was work, or a commit."""
        self._next = self.first


async def finish(tasks: set[asyncio.Task[None]], grace: float) -> None:
    """Give ``tasks`` ``grace`` seconds to end, then cancel and await the rest."""
    if tasks and grace > 0:
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
        budget: Budget | None = None,
    ) -> None:
        """Run the outbox of alias ``database`` (default: ``MESSAGING.DATABASE``).

        Every database that hosts domains has its own outbox; only the worker
        of ``MESSAGING.DATABASE`` schedules periodic tasks. ``queues`` limits
        the messages it claims (default: every queue). Workers of one process
        share a ``budget`` (default: one of their own, of ``CONCURRENCY``).
        """
        self.config = config or registry.settings(MessagingSettings)
        self.database = database or self.config.DATABASE
        self.bus = bus or event_bus
        self.queues = None if queues is None else frozenset(queues)
        self.budget = budget or Budget(self.config.CONCURRENCY)
        self._scheduled: dict[str, int] = {}
        self._next_runs: dict[str, datetime] = {}
        self._last_cleanup = float("-inf")
        self._cleanup_batch = self.config.CLEANUP_BATCH_SIZE
        self._prune_batch = self.config.CLEANUP_BATCH_SIZE
        self._declared: WeakKeyDictionary[Broker, set[str]] = WeakKeyDictionary()
        self._limiter: Backend | None = None
        self._rate_slots: dict[str, float] = {}
        for spec in all_tasks():
            if (
                spec.timeout is not None
                and spec.timeout >= self.config.VISIBILITY_TIMEOUT
            ):
                raise ImproperlyConfigured(
                    f"Task {spec.key!r} may run {spec.timeout:g}s; that must stay "
                    f"under MESSAGING.VISIBILITY_TIMEOUT "
                    f"({self.config.VISIBILITY_TIMEOUT:g}s)."
                )

    @property
    def engine(self) -> AsyncEngine:
        """The running event loop's engine for this worker's outbox."""
        return connections.engine(self.database)

    @property
    def dialect(self) -> str:
        """The outbox database's dialect (``postgresql``, ``sqlite``)."""
        return self.engine.dialect.name

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
        cancelled, the rest are cancelled and go back to the outbox. Old
        messages are cleaned up meanwhile, and on PostgreSQL commits wake
        the worker between polls.
        """
        wake = Wake()
        polls = IdlePolls(self.config)
        running: set[asyncio.Task[None]] = set()
        outage = self.outage()
        async with asyncio.TaskGroup() as group:
            helpers = [
                group.create_task(set_when(stop, wake)),
                group.create_task(self.clean_until(stop)),
                group.create_task(self.listen(stop, wake)),
            ]
            try:
                while not stop.is_set():
                    try:
                        await self.schedule_periodic()
                    except Exception as exc:  # noqa: BLE001 - logged; it waits.
                        await outage.failed(exc, stop)
                        continue
                    slots = await self.budget.acquire(self.config.BATCH_SIZE, stop=stop)
                    if not slots:
                        break
                    wake.clear()  # A commit from here on wakes the next wait.
                    try:
                        claimed = await self.claim(slots)
                    except BaseException as exc:
                        self.budget.release(slots)
                        if not isinstance(exc, Exception):
                            raise
                        await outage.failed(exc, stop)
                        continue
                    outage.recovered()
                    self.budget.release(slots - len(claimed))
                    for message in claimed:
                        task = group.create_task(self._process_holding(message))
                        running.add(task)
                        task.add_done_callback(running.discard)
                    if claimed:
                        polls.reset()
                    if len(claimed) < slots and not stop.is_set():
                        # Nothing more is due: wait for a poll, a commit, or stop.
                        # Not once stopped: the clear above may have wiped the
                        # wake-up a stop set while a slot was taken.
                        await pause(wake, polls.wait(wake.listening))
                        if wake.is_set():
                            polls.reset()
                await finish(running, self.config.SHUTDOWN_TIMEOUT)
                await wind_down(helpers, HELPER_GRACE)
            finally:
                for helper in helpers:
                    helper.cancel()  # Cancelled itself: they stop now.

    async def run_once(self) -> int:
        """Run every message that is due now; return how many ran.

        At most ``BATCH_SIZE`` run at once, and no more than the budget
        allows, so workers sharing it (``runworker --once``) stay within it.
        """
        await self.schedule_periodic()
        processed = 0
        while True:
            slots = await self.budget.acquire(self.config.BATCH_SIZE)
            try:
                claimed = await self.claim(slots)
            except BaseException:
                self.budget.release(slots)
                raise
            self.budget.release(slots - len(claimed))
            if not claimed:
                return processed
            async with asyncio.TaskGroup() as group:
                for message in claimed:
                    group.create_task(self._process_holding(message))
            processed += len(claimed)

    async def _process_holding(self, message: "Claimed") -> None:
        """Process ``message``, then give back the slot its claim took."""
        try:
            await self.process(message)
        finally:
            self.budget.release()

    def _due(
        self, now: dbclock.Moment, limit: int | None, ids: Sequence[uuid.UUID] = ()
    ) -> Any:  # noqa: ANN401 - a subquery.
        """Select due, unlocked messages of this worker's queues (of ``ids``)."""
        due = (
            select(messages.c.id)
            .where(
                messages.c.status == PENDING,
                messages.c.available_at <= now,
                or_(messages.c.locked_until.is_(None), messages.c.locked_until < now),
            )
            .order_by(messages.c.priority.desc(), messages.c.available_at)
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
        dialect = self.dialect
        now = dbclock.now(dialect)
        lock_id = uuid.uuid4()
        statement = (
            update(messages)
            .where(messages.c.id.in_(self._due(now, limit)))
            .values(
                locked_until=dbclock.at(dialect, self.config.VISIBILITY_TIMEOUT),
                lock_id=lock_id,
                attempts=messages.c.attempts + 1,
                started_at=now,
            )
            .returning(*_CLAIMED)
        )
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).all()
        return [Claimed(**row._mapping, lock_id=lock_id) for row in rows]

    async def relay(self, broker: Broker, limit: int) -> int:
        """Publish up to ``limit`` due messages to ``broker``; return how many went.

        Each message is published as a pointer: its id, this database, and
        a lock. The lock is a short lease (the broker's ``publish_timeout``
        and ``LEASE_MARGIN``) until the broker confirms the message, then
        lasts ``REPUBLISH_AFTER``. Whatever the broker did not confirm, for
        any reason (a refusal, its deadline, a cancellation), is unlocked
        at once, so the next round sends it again.
        """
        return await self._relay(broker, self._due(dbclock.now(self.dialect), limit))

    async def relay_ids(self, broker: Broker, ids: Sequence[uuid.UUID]) -> int:
        """Publish just-committed messages now, those due and not yet taken.

        They go ``BATCH_SIZE`` at a time: one lock per batch, and at most
        a batch of publications in flight.
        """
        relayed = 0
        for chunk in batched(ids, self.config.BATCH_SIZE, strict=False):
            due = self._due(dbclock.now(self.dialect), None, chunk)
            relayed += await self._relay(broker, due)
        return relayed

    async def _relay(self, broker: Broker, due: Any) -> int:  # noqa: ANN401
        """Lease the ``due`` messages, publish their pointers, settle the leases."""
        dialect = self.dialect
        lock_id = uuid.uuid4()
        lease = broker.publish_timeout + LEASE_MARGIN
        statement = (
            update(messages)
            .where(messages.c.id.in_(due))
            .values(locked_until=dbclock.at(dialect, lease), lock_id=lock_id)
            .returning(messages.c.id, messages.c.queue)
        )
        try:
            async with self.engine.begin() as connection:
                rows = (await connection.execute(statement)).all()
        except asyncio.CancelledError:
            # The driver may commit the lease as its caller is cancelled.
            await _shielded(self._unlock(lock_id), "give back a lease cut off")
            raise
        if not rows:
            return 0
        confirmed: list[uuid.UUID] = []
        failures: list[str] = []
        try:
            async with asyncio.timeout(broker.publish_timeout):
                await self._publish(broker, rows, lock_id, confirmed, failures)
        except TimeoutError:
            failures.append(f"no confirmation within {broker.publish_timeout:g}s")
        finally:
            # Also when cancelled: a lease left behind holds its messages.
            await _shielded(
                self._settle(rows, confirmed, lock_id),
                f"settle {len(rows)} published message(s)",
            )
        if failures:
            logger.warning(
                "Could not publish %d of %d message(s) to the broker (%s); they "
                "go again on the next round",
                len(rows) - len(confirmed),
                len(rows),
                failures[0],
            )
        observe_relay("published", len(confirmed))
        observe_relay("failed", len(rows) - len(confirmed))
        return len(confirmed)

    async def _publish(
        self,
        broker: Broker,
        rows: Sequence[Row[Any]],
        lock_id: uuid.UUID,
        confirmed: list[uuid.UUID],
        failures: list[str],
    ) -> None:
        """Declare new queues, then publish each pointer, all at once.

        The broker's confirmations go to ``confirmed``, its refusals to
        ``failures``; a queue that failed is declared again next time (it
        may be gone: deleted, purged).
        """
        declared = self._declared.setdefault(broker, set())
        if new := {row.queue for row in rows} - declared:
            try:
                await broker.declare(new)
            except BrokerError as exc:
                failures.append(f"declaring {', '.join(map(repr, sorted(new)))}: {exc}")
                return
            declared |= new

        async def publish(message_id: uuid.UUID, queue: str) -> None:
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
                logger.debug("Could not publish %s to %r: %s", message_id, queue, exc)
                failures.append(f"queue {queue!r}: {exc}")
                declared.discard(queue)
            else:
                confirmed.append(message_id)

        async with asyncio.TaskGroup() as group:
            for row in rows:
                group.create_task(publish(row.id, row.queue))

    async def _settle(
        self,
        rows: Sequence[Row[Any]],
        confirmed: Sequence[uuid.UUID],
        lock_id: uuid.UUID,
    ) -> None:
        """Lock the confirmed messages for ``REPUBLISH_AFTER``; unlock the rest.

        Only the relay's own lock changes: a consumer may have taken a
        message over already.
        """
        dialect = self.dialect
        done = set(confirmed)
        unsent = [row.id for row in rows if row.id not in done]
        ours = messages.c.lock_id == lock_id
        async with self.engine.begin() as connection:
            if done:
                await connection.execute(
                    update(messages)
                    .where(messages.c.id.in_(done), ours)
                    .values(
                        locked_until=dbclock.at(dialect, self.config.REPUBLISH_AFTER)
                    )
                )
            if unsent:
                await connection.execute(
                    update(messages)
                    .where(messages.c.id.in_(unsent), ours)
                    .values(locked_until=None, lock_id=None)
                )

    async def _unlock(self, lock_id: uuid.UUID) -> None:
        """Unlock the messages leased under ``lock_id``, if the lease committed.

        A cancellation can cut off the relay as its lease commits: aiosqlite
        commits in its own thread, and a PostgreSQL ``COMMIT`` may finish on
        the server. The relay then never learns which messages it took, and
        without this they would stay locked until the lease ran out.
        """
        async with self.engine.begin() as connection:
            await connection.execute(
                update(messages)
                .where(messages.c.lock_id == lock_id)
                .values(locked_until=None, lock_id=None)
            )

    async def take_over(
        self, message_id: uuid.UUID, relay_lock: uuid.UUID
    ) -> Claimed | None:
        """Claim a delivered message, unless it ran, moved on, or is running.

        The delivery must carry the lock the relay published it under, or
        that lock must have expired; anything else is a stale duplicate.
        """
        dialect = self.dialect
        now = dbclock.now(dialect)
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
                locked_until=dbclock.at(dialect, self.config.VISIBILITY_TIMEOUT),
                lock_id=lock_id,
                attempts=messages.c.attempts + 1,
                started_at=now,
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
        Its span continues the trace of the work that queued it.

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
            remote_parent(message.headers),
            span("message.process", kind=message.kind, target=message.target),
            _attempting(message),
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
        spec = get_task(message.target) if message.kind == "task" else None
        if spec is not None and spec.rate is not None:
            delay = await self._throttle(spec, spec.rate)
            if delay is not None:
                await self._release(
                    message,
                    status=PENDING,
                    attempts=messages.c.attempts - 1,
                    available_at=dbclock.at(self.dialect, delay),
                    started_at=None,
                )
                return THROTTLED
        timeout = self.config.TASK_TIMEOUT
        if spec is not None and spec.timeout is not None:
            timeout = spec.timeout
        run = Run(
            message.id,
            message.target,
            message.lock_id,
            self.database,
            self.config.VISIBILITY_TIMEOUT,
        )
        try:
            if message.attempts > message.max_attempts:
                raise AttemptsExhausted(
                    "The worker stopped during the last attempt (a crash, "
                    "or a kill before TASK_TIMEOUT)."
                )
            # A deadline, not only a timer: the task's statements and outgoing
            # calls are bounded by the time it has left (statement_timeout).
            async with deadlines.timeout(timeout):
                result = await self._execute(message, run)
            follow_up = self._follow_up(message, result)
        except Exception as exc:  # noqa: BLE001 - logged and retried by _failed.
            # The run's own record, whatever the handler made of the error.
            if run.lost:
                return self._superseded(message)
            if run.handled:
                await self._done(message, None)
                logger.info(
                    "Skipped %s %s: a run of it committed before (inbox)",
                    message.kind,
                    message.target,
                )
                return HANDLED
            permanent = isinstance(exc, UnknownTarget | AttemptsExhausted) or (
                spec is not None
                and not retryable(exc, spec.retry_on, spec.dont_retry_on)
            )
            return await self._failed(message, exc, permanent=permanent, spec=spec)
        if not await self._done(message, follow_up):
            return self._superseded(message)
        logger.debug(
            "Ran %s %s in %.1fms",
            message.kind,
            message.target,
            (time.perf_counter() - started) * 1000,
        )
        return DONE

    async def _throttle(self, spec: TaskSpec, rate: tuple[int, int]) -> float | None:
        """Count a run of ``spec``; None if its rate allows it, else seconds to wait.

        Messages held back in a row wait one interval more each, so a backlog
        comes due at the rate instead of all at once.
        """
        if self._limiter is None:
            self._limiter = import_string(settings.RATE_LIMIT_BACKEND)()
        limit, period = rate
        decision = await self._limiter.hit(f"task:{spec.key}", limit, period)
        if decision.allowed:
            return None
        now = clock.monotonic()
        slot = max(now + decision.retry_after, self._rate_slots.get(spec.key, now))
        self._rate_slots[spec.key] = slot + period / limit
        return slot - now

    def _follow_up(self, message: Claimed, result: object) -> dict[str, Any] | None:
        """The outbox row of the chain's next step, if ``message`` has one."""
        chain = message.headers.get(CHAIN)
        if message.kind != "task" or not chain:
            return None
        target, *rest = chain
        spec = get_task(target)
        headers = {**message.headers, **trace_headers()}
        headers.pop(CHAIN)
        if rest:
            headers[CHAIN] = rest
        if _sensitive(message, get_task(message.target)):
            # It is handed what this step had, or made from it.
            headers[SENSITIVE] = True
        return message_row(
            kind="task",
            target=target,
            payload=next_arguments(message.payload, result),
            headers=headers,
            max_attempts=(spec and spec.max_attempts) or self.config.MAX_ATTEMPTS,
            queue=spec.queue if spec else DEFAULT_QUEUE,
            priority=spec.priority if spec else 0,
            dedupe_key=f"chain:{message.id}",
        )

    def _superseded(self, message: Claimed) -> str:
        """Log a run that lost its lease; the worker holding the message runs it."""
        logger.warning(
            "%s %s lost its lease while it ran: another worker took it over and "
            "runs it",
            message.kind,
            message.target,
        )
        return SUPERSEDED

    async def _done(self, message: Claimed, follow_up: dict[str, Any] | None) -> bool:
        """Record ``message`` as done, and queue its chain's next step with it.

        Its arguments are blanked in the same statement: a finished message
        keeps what operators need (its target, times, attempts), not what it
        carried, such as a reset link in a mail. Return whether this run
        still held it; if another worker took it over, nothing changes.
        """
        statement = (
            update(messages)
            .where(messages.c.id == message.id, messages.c.lock_id == message.lock_id)
            .values(
                locked_until=None,
                lock_id=None,
                status=DONE,
                finished_at=dbclock.now(self.dialect),
                payload={},
            )
        )
        async with self.engine.begin() as connection:
            released = (await connection.execute(statement)).rowcount
            if follow_up is not None and released:
                await write(connection, connection.dialect.name, [follow_up])
        if follow_up is not None and released:
            # The relay publishes it, on its next poll or sooner where commits
            # notify (``outbox.notifies``); worker processes run no publisher.
            await notify_committed(
                self.config, self.database, [follow_up], publish=False
            )
        return bool(released)

    async def _execute(self, message: Claimed, run: Run) -> object:
        """Call the handler or task ``message`` names; return a task's result.

        The handler's commits join ``run`` (``runs``). For an ``inbox``
        target, a run that may repeat an earlier one (not the first attempt)
        is skipped if the outbox's inbox holds the message already. A step
        of a chain that hands its result on never uses the inbox.
        """
        function, call = self._target(message)
        run.inbox = uses_inbox(function) and not message.headers.get(CHAIN)
        with running(run):
            if run.inbox and message.attempts > 1:
                async with self.engine.connect() as connection:
                    run.handled = await was_handled(connection, run)
                if run.handled:
                    raise AlreadyHandled(
                        f"Message {message.id} ({message.target}) was handled already."
                    )
            return await call()

    def _target(
        self, message: Claimed
    ) -> tuple[Callable[..., Any], Callable[[], Awaitable[object]]]:
        """The function ``message`` names, and a call of it with its arguments."""
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
            event = load_event(event_type, message.payload)
            return function, lambda: function(event)
        if message.kind == "task":
            spec = get_task(message.target)
            if spec is None:
                raise UnknownTarget(f"No task {message.target!r}.")
            task = spec.function
            return task, lambda: call_task(task, message.payload)
        raise UnknownTarget(f"Unknown message kind {message.kind!r}.")

    async def _failed(
        self,
        message: Claimed,
        exc: BaseException,
        *,
        permanent: bool,
        spec: TaskSpec | None = None,
    ) -> str:
        """Schedule a retry, or dead-letter and report; return the outcome.

        The delay comes from the task's ``backoff``, or ``MESSAGING``'s. A
        ``sensitive`` task's arguments are blanked when it dies, and so are
        those of a chain's step after one (``SENSITIVE``), with its error
        recorded, and its headers say so (``BLANKED``).
        """
        exc = _without_input(exc)
        error = f"{type(exc).__name__}: {exc}"[:_ERROR_LENGTH]
        if permanent or message.attempts >= message.max_attempts:
            blanked: dict[str, object] = {}
            if _sensitive(message, spec):
                blanked = {"payload": {}, "headers": {**message.headers, BLANKED: True}}
            await self._release(
                message,
                status=DEAD,
                finished_at=dbclock.now(self.dialect),
                last_error=error,
                **blanked,
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
        if spec is not None and spec.backoff is not None:
            delay = spec.backoff.delay(message.attempts)
        else:
            delay = backoff(message.attempts, self.config)
        await self._release(
            message,
            status=PENDING,
            available_at=dbclock.at(self.dialect, delay),
            last_error=error,
            started_at=None,
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
                    message,
                    status=PENDING,
                    attempts=messages.c.attempts - 1,
                    started_at=None,
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
        """Queue each periodic task once per interval or cron time, across workers.

        A cron task's next run is queued ahead, as soon as the one before is
        due. A run whose time passes while every worker is down still runs
        once they are back; earlier missed runs are not made up.
        """
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
                self._periodic_row(
                    spec,
                    dedupe_key=f"periodic:{spec.key}:{slot}",
                    available_at=datetime.fromtimestamp(slot * seconds, UTC),
                )
            )
        next_runs = {}
        for spec, schedule in cron_tasks():
            queued = self._next_runs.get(spec.key)
            if queued is not None and queued > now:
                continue  # Its next run is in the outbox already.
            run = schedule.next_after(now)
            if run is None:
                continue  # None in the next fifty years.
            next_runs[spec.key] = run
            rows.append(
                self._periodic_row(
                    spec,
                    dedupe_key=f"cron:{spec.key}:{int(run.timestamp())}",
                    available_at=run,
                )
            )
        if rows:
            async with self.engine.begin() as connection:
                await write(connection, connection.dialect.name, rows)
            # Only now: a failed write is tried again on the next round.
            self._scheduled.update(slots)
            self._next_runs.update(next_runs)

    def _periodic_row(
        self, spec: TaskSpec, *, dedupe_key: str, available_at: datetime
    ) -> dict[str, Any]:
        """The outbox row of one scheduled run of ``spec``."""
        return message_row(
            kind="task",
            target=spec.key,
            payload={},
            headers={},  # No request or actor caused it.
            max_attempts=spec.max_attempts or self.config.MAX_ATTEMPTS,
            queue=spec.queue,
            dedupe_key=dedupe_key,
            available_at=available_at,
            priority=spec.priority,
        )

    async def cleanup(self) -> int:
        """Delete done and cancelled messages older than ``RETENTION_SECONDS``.

        They go in batches of up to ``CLEANUP_BATCH_SIZE``, each in its own
        short transaction, skipping rows other workers lock on PostgreSQL;
        return how many were deleted. A batch that fails halves the next
        one, down to a single row, so statement timeouts slow cleanup down
        but never stop it; batches that go through grow back.
        """
        return await self._clean(None)

    async def _clean(self, stop: asyncio.Event | None) -> int:
        """Run ``cleanup``, stopping between batches once ``stop`` is set."""
        return await self._in_batches(stop, self.cleanup_statement, "_cleanup_batch")

    async def prune_inbox(self) -> int:
        """Delete the inbox rows of messages that finished; return how many.

        A row stays as long as its message may run again: while it is
        pending (waiting, held back by its rate, on its way through a broker,
        running) or dead (an operator may retry it), however long that is.
        Rows of this outbox's messages go in batches, as ``cleanup`` deletes
        messages: up to ``CLEANUP_BATCH_SIZE`` per short transaction, halved
        after a failure and grown back after a success. Rows of messages
        another database's outbox holds (a handler here, its event there)
        are checked against that outbox, a batch at a time.
        """
        return await self._prune(None)

    async def _prune(self, stop: asyncio.Event | None) -> int:
        """Run ``prune_inbox``, stopping between batches once ``stop`` is set."""
        pruned = await self._in_batches(stop, self.inbox_statement, "_prune_batch")
        for outbox in self._other_outboxes():
            pruned += await self._prune_for(outbox, stop)
        return pruned

    def inbox_statement(self, dialect: str, size: int) -> Delete:
        """The ``DELETE`` of up to ``size`` inbox rows of finished messages here."""
        return prune_statement(self.database, size)

    def _other_outboxes(self) -> list[str]:
        """Every other database alias holding an outbox, in routing order."""
        aliases = (
            alias
            for target in router.hosting_targets()
            for alias in router.aliases_of(target)
        )
        return [alias for alias in dict.fromkeys(aliases) if alias != self.database]

    async def _prune_for(self, outbox: str, stop: asyncio.Event | None) -> int:
        """Delete the rows here of ``outbox``'s messages that finished there."""
        size = self.config.CLEANUP_BATCH_SIZE
        pruned = 0
        after: tuple[str, uuid.UUID] | None = None
        while stop is None or not stop.is_set():
            async with self.engine.connect() as connection:
                rows = (await connection.execute(rows_of(outbox, after, size))).all()
            if not rows:
                break
            async with connections.engine(outbox).connect() as connection:
                ids = [row.message_id for row in rows]
                live = set((await connection.execute(unfinished(ids))).scalars())
            if finished := [
                (row.target, row.message_id)
                for row in rows
                if row.message_id not in live
            ]:
                async with self.engine.begin() as connection:
                    result = await connection.execute(forget(finished))
                    pruned += int(result.rowcount)
            if len(rows) < size:
                break
            after = (rows[-1].target, rows[-1].message_id)
        return pruned

    async def _in_batches(
        self,
        stop: asyncio.Event | None,
        statement: Callable[[str, int], Delete],
        batch: str,
    ) -> int:
        """Run ``statement`` until it deletes less than a batch, or ``stop``.

        ``batch`` names the attribute that holds the next batch's size.
        """
        dialect = self.dialect
        deleted = 0
        while stop is None or not stop.is_set():
            size: int = getattr(self, batch)
            try:
                async with self.engine.begin() as connection:
                    result = await connection.execute(statement(dialect, size))
                    count = int(result.rowcount)
            except Exception:
                setattr(self, batch, max(size // 2, 1))
                raise
            deleted += count
            if count < size:
                break
            setattr(self, batch, min(size * 2, self.config.CLEANUP_BATCH_SIZE))
        return deleted

    def cleanup_statement(self, dialect: str, size: int) -> Delete:
        """The ``DELETE`` of up to ``size`` messages past ``RETENTION_SECONDS``.

        Done and cancelled ones only, found through their partial index
        (``FINISHED_INDEX``), oldest first as the index holds them. The
        statuses, the retention, and the size are written into the SQL, not
        bound: a prepared statement's generic plan cannot match bound
        statuses to the index's condition, nor estimate a bound cutoff or
        limit, and scans the whole outbox instead (PERF-02).
        """
        retention = timedelta(seconds=-self.config.RETENTION_SECONDS)
        cutoff: dbclock.Moment
        if dbclock.uses_database(dialect):
            cutoff = func.statement_timestamp(type_=DateTime(timezone=True)) + literal(
                retention, literal_execute=True
            )
        else:
            cutoff = dbclock.at(dialect, retention.total_seconds())
        old = (
            select(messages.c.id)
            .where(
                messages.c.status.in_(
                    [literal(status, literal_execute=True) for status in FINISHED]
                ),
                messages.c.finished_at < cutoff,
            )
            .limit(literal(size, literal_execute=True))
            .with_for_update(skip_locked=True)
        )
        return delete(messages).where(messages.c.id.in_(old.scalar_subquery()))

    async def run_cleanup_when_due(self) -> None:
        """Run ``cleanup`` and ``prune_inbox`` every ``CLEANUP_INTERVAL`` seconds.

        ``clean_until`` does this on a schedule of its own; this is for loops
        of your own.
        """
        if clock.monotonic() - self._last_cleanup >= self.config.CLEANUP_INTERVAL:
            self._last_cleanup = clock.monotonic()
            await self.cleanup()
            await self.prune_inbox()

    async def clean_until(self, stop: asyncio.Event) -> None:
        """Run ``cleanup`` and ``prune_inbox`` soon, then often, until ``stop``.

        Each run comes ``CLEANUP_INTERVAL`` after the last; the first comes
        after ``CLEANUP_START`` (or ``CLEANUP_INTERVAL``, if shorter) times a
        random factor between one half and one.

        Polling workers and relaying workers run it, each in a task of its
        own, so claiming never waits for it; consume-only workers never do.
        A failed run is logged and tried again after ``POLL_INTERVAL``,
        doubling while it keeps failing, up to ``OUTAGE_MAX_PAUSE``.
        """
        failures = Outage(
            self.database,
            self.config.POLL_INTERVAL,
            doing="clean up the outbox of",
            meanwhile="cleaning up again",
            back="Cleaning up the outbox of %r works again",
        )
        first = min(CLEANUP_START, self.config.CLEANUP_INTERVAL)
        with suppress(TimeoutError):
            async with asyncio.timeout(first * _random.uniform(0.5, 1.0)):
                await stop.wait()
        while not stop.is_set():
            try:
                deleted = await self._clean(stop)
                pruned = await self._prune(stop)
            except Exception as exc:  # noqa: BLE001 - logged; it runs again.
                await failures.failed(exc, stop)
                continue
            failures.recovered()
            if deleted:
                logger.info(
                    "Deleted %d finished message(s) from the outbox of %r",
                    deleted,
                    self.database,
                )
            if pruned:
                logger.info(
                    "Deleted %d handled message id(s) from the inbox of %r",
                    pruned,
                    self.database,
                )
            with suppress(TimeoutError):
                async with asyncio.timeout(self.config.CLEANUP_INTERVAL):
                    await stop.wait()

    async def listen(self, stop: asyncio.Event, wake: asyncio.Event) -> None:
        """Set ``wake`` each time a commit notifies this outbox, until ``stop``.

        On PostgreSQL with asyncpg, while ``MESSAGING.NOTIFY`` applies (see
        ``outbox.notifies``); elsewhere it returns at once and polling is
        all there is. So it does through PgBouncer (``PGBOUNCER``): ``LISTEN``
        needs a session of its own, which transaction pooling does not keep.
        It holds one connection of the pool. A lost connection
        is opened again after a pause that grows with each failure in a row;
        meanwhile the worker polls.
        """
        engine = self.engine
        if not (
            notifies(self.config)
            and engine.dialect.name == "postgresql"
            and engine.dialect.driver == "asyncpg"
            and not connections.config(self.database).PGBOUNCER
        ):
            return
        failures = Outage(
            self.database,
            self.config.POLL_INTERVAL,
            doing="listen for new messages in",
            meanwhile="polling until listening again",
            back="Listening for new messages in %r again",
        )
        while not stop.is_set():
            try:
                await self._listen(engine, stop, wake, failures)
            except Exception as exc:  # noqa: BLE001 - logged; polling goes on.
                await failures.failed(exc, stop)

    def wakes_for(self, queue: str) -> bool:
        """Report whether a commit to ``queue`` concerns this worker."""
        return self.queues is None or queue in self.queues

    async def _listen(
        self,
        engine: AsyncEngine,
        stop: asyncio.Event,
        wake: asyncio.Event,
        failures: Outage,
    ) -> None:
        """Listen on one connection until ``stop``, or until it is lost (raise)."""
        lost = asyncio.Event()

        def notified(*args: object) -> None:
            if self.wakes_for(str(args[-1])):  # (connection, pid, channel, payload)
                wake.set()

        async with engine.connect() as connection:
            try:
                raw: Any = (await connection.get_raw_connection()).driver_connection
                raw.add_termination_listener(lambda _: lost.set())
                await raw.add_listener(CHANNEL, notified)
                failures.recovered()
                _hearing(wake, True)
                wake.set()  # Commits made before this went unheard: look now.
                await _first(stop, lost)
            finally:
                _hearing(wake, False)  # Polls go back to POLL_INTERVAL.
                # Never hand a listening connection back to the pool.
                await connection.invalidate()
        if lost.is_set() and not stop.is_set():
            raise ConnectionError("The connection listening for messages was lost.")


def _hearing(wake: asyncio.Event, listening: bool) -> None:
    """Tell ``wake`` (a ``Wake``) whether a listener hears commits for it."""
    if isinstance(wake, Wake):
        wake.listening = listening


async def set_when(event: asyncio.Event, then: asyncio.Event) -> None:
    """Set ``then`` once ``event`` is set."""
    await event.wait()
    then.set()


async def wind_down(tasks: Iterable[asyncio.Task[None]], grace: float) -> None:
    """Let ``tasks``, told to stop, end within ``grace`` seconds; cancel the rest."""
    pending = {task for task in tasks if not task.done()}
    if pending:
        _, late = await asyncio.wait(pending, timeout=grace)
        for task in late:
            task.cancel()


async def pause(wake: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or less if ``wake`` is set meanwhile."""
    with suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await wake.wait()


async def _first(*events: asyncio.Event) -> None:
    """Wait until any of ``events`` is set."""
    waiting = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiting:
            waiter.cancel()
