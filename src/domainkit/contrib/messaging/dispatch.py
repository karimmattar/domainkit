"""Workers on a broker: relays publish due messages, consumers run them.

The outbox stays the record of every message (see ADR 0014). A relay per
outbox claims due rows, as the polling worker does, and publishes a
pointer to each: its id, its database, and the relay's lock. A consumer
takes the row over by id, which only works for a delivery that carries the
current lock (or one whose lock expired), runs it as the polling worker
would, and acknowledges it after the outcome is recorded. Retries go back
to the outbox with their delay, and the relay publishes them when due.

Every delivery is answered, so a failure never holds a consumer's slot: it
is requeued when its outbox cannot be reached, and acknowledged otherwise.
A relay waits out database outages as the polling worker does. Deliveries
run within the process's ``Budget``, however many queues it consumes.

With ``PUBLISH_ON_COMMIT``, a commit hands its message ids to the loop's
``CommitPublisher``, which publishes them in the background: the commit
never waits on the broker, and a broker that keeps failing trips its
breaker, so commits leave their messages to the relays until it recovers.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from contextlib import suppress
from itertools import batched
from typing import Final
from weakref import WeakKeyDictionary

from domainkit.broker import Broker, BrokerError, Delivery, Subscription, brokers
from domainkit.db.routing import router
from domainkit.tasks import supervise

from .breaker import Breaker, Watched
from .budget import Budget
from .settings import MessagingSettings
from .worker import OUTAGE_MAX_PAUSE, Worker, finish, pause, set_when

try:
    from .metrics import observe_left, observe_skipped
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_skipped() -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

    def observe_left(reason: str, count: int) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)

MAX_UNPUBLISHED: Final = 10_000
"""Committed messages waiting to be published; past it, the relay sends them."""
STOP_CONSUMING_TIMEOUT: Final = 5.0
"""Seconds a stopping worker waits for the broker to stop its deliveries."""
ANSWER_TIMEOUT: Final = 5.0
"""Seconds a stopping worker waits to give back a delivery it will not run."""
RESTART_BACKOFF: Final = 1.0
"""Seconds before a crashed publisher starts again; doubling, up to a minute."""


class BrokerWorkers:
    """Relay the outboxes of ``workers`` and consume ``queues`` from one broker."""

    def __init__(
        self,
        workers: Sequence[Worker],
        queues: Iterable[str],
        config: MessagingSettings,
        *,
        relay: bool = True,
        budget: Budget | None = None,
    ) -> None:
        """Relay each worker's outbox; run deliveries of ``queues`` from any outbox.

        Without ``relay``, only consume: other workers relay, schedule periodic
        tasks, and clean up, so this one never polls the database. Deliveries
        run within ``budget`` (default: one of ``CONCURRENCY`` of their own).
        """
        if config.BROKER is None:
            raise ValueError("BrokerWorkers needs MESSAGING.BROKER.")
        self.relays = relay
        self.workers = list(workers)
        self.queues = sorted(set(queues))
        self.config = config
        self.alias = config.BROKER
        self.budget = budget or Budget(config.CONCURRENCY)
        self._outboxes = {worker.database: worker for worker in workers}

    def outbox(self, database: str) -> Worker | None:
        """The worker of ``database``'s outbox, if it hosts domains."""
        if database not in self._outboxes and database in _hosting_aliases():
            self._outboxes[database] = Worker(
                self.config, database=database, budget=self.budget
            )
        return self._outboxes.get(database)

    async def run(self, stop: asyncio.Event) -> None:
        """Relay and consume until ``stop`` is set; then finish the running ones.

        Running deliveries get ``SHUTDOWN_TIMEOUT`` seconds, counted from the
        stop; then, or when ``run`` itself is cancelled, the rest are
        cancelled, and their messages go back to the outbox. A broker that
        is lost or stalled at shutdown cannot skip that drain, nor hold it
        past ``STOP_CONSUMING_TIMEOUT``.
        """
        broker = brokers[self.alias]
        running: set[asyncio.Task[None]] = set()
        consuming = _Consuming(stop)
        async with asyncio.TaskGroup() as group:

            async def handle(delivery: Delivery) -> None:
                await self._handle(delivery, group, running, consuming)

            subscription = await broker.consume(
                self.queues, self.config.CONCURRENCY, handle
            )
            helpers: list[asyncio.Task[None]] = []
            grace = 0.0
            try:
                if self.relays:
                    for worker in self.workers:
                        helpers.append(
                            group.create_task(self._relay_until(worker, broker, stop))
                        )
                        helpers.append(group.create_task(worker.clean_until(stop)))
                await stop.wait()
            finally:
                for helper in helpers:
                    helper.cancel()
                grace = await self._stop_consuming(subscription)
                consuming.ended.set()
            await finish(running, grace)

    async def _handle(
        self,
        delivery: Delivery,
        group: asyncio.TaskGroup,
        running: set[asyncio.Task[None]],
        consuming: "_Consuming",
    ) -> None:
        """Run ``delivery`` once the budget has room; give it back if stopping."""
        if not await self.budget.acquire(stop=consuming.stop):
            await consuming.give_back(delivery)
            return
        try:
            task = group.create_task(self.deliver(delivery))
        except RuntimeError:  # The run is over: nothing new starts.
            self.budget.release()
            await consuming.give_back(delivery)
            return
        running.add(task)
        task.add_done_callback(running.discard)
        task.add_done_callback(lambda _: self.budget.release())
        await task

    async def _stop_consuming(self, subscription: Subscription) -> float:
        """Stop new deliveries; return what is left of ``SHUTDOWN_TIMEOUT``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.SHUTDOWN_TIMEOUT
        try:
            async with asyncio.timeout(STOP_CONSUMING_TIMEOUT):
                await subscription.cancel()
        except (BrokerError, TimeoutError) as exc:
            logger.warning(
                "Could not stop consuming from broker %r (%s); running messages "
                "still get their time",
                self.alias,
                str(exc) or "no answer in time",
            )
        return max(deadline - loop.time(), 0.0)

    async def _relay_until(
        self, worker: Worker, broker: Broker, stop: asyncio.Event
    ) -> None:
        """Schedule and relay until ``stop``; poll (or wait for a commit) when idle."""
        batch = self.config.BATCH_SIZE
        outage = worker.outage()
        wake = asyncio.Event()
        async with asyncio.TaskGroup() as group:
            helpers = [
                group.create_task(set_when(stop, wake)),
                group.create_task(worker.listen(stop, wake)),
            ]
            try:
                while not stop.is_set():
                    wake.clear()
                    try:
                        await worker.schedule_periodic()
                        relayed = await worker.relay(broker, batch)
                    except Exception as exc:  # noqa: BLE001 - logged; it waits.
                        await outage.failed(exc, stop)
                        continue
                    outage.recovered()
                    if relayed < batch:
                        await pause(wake, self.config.POLL_INTERVAL)
            finally:
                for helper in helpers:
                    helper.cancel()

    async def deliver(self, delivery: Delivery) -> None:
        """Run one delivered message, then answer it; every delivery is answered.

        It is acknowledged once its outcome is recorded, or when it cannot
        run here. It is requeued, after ``POLL_INTERVAL``, when its outbox
        cannot be reached.
        """
        try:
            await self._run(delivery)
        except Exception:
            logger.exception(
                "Could not run message %s from queue %r; requeued",
                delivery.message_id,
                delivery.queue,
            )
            await asyncio.sleep(self.config.POLL_INTERVAL)
            answer = delivery.requeue
        else:
            answer = delivery.ack
        try:
            await answer()
        except BrokerError as exc:  # The broker hands it out again.
            logger.warning(
                "Could not answer message %s from queue %r: %s",
                delivery.message_id,
                delivery.queue,
                exc,
            )

    async def _run(self, delivery: Delivery) -> None:
        """Run the message ``delivery`` points to, unless it cannot run here."""
        try:
            pointer = json.loads(delivery.body)
            message_id = uuid.UUID(pointer["id"])
            relay_lock = uuid.UUID(pointer["lock"])
            database = str(pointer["database"])
        except (ValueError, KeyError, TypeError):
            logger.error("Dropped an unreadable message from queue %r", delivery.queue)
            return
        worker = self.outbox(database)
        if worker is None:
            logger.error(
                "Dropped message %s: %r hosts no outbox here", message_id, database
            )
            return
        claimed = await worker.take_over(message_id, relay_lock)
        if claimed is None:
            observe_skipped()
            logger.debug("Skipped message %s: done, running, or moved on", message_id)
        else:
            await worker.process(claimed)


class _Consuming:
    """A run's consumers: when they must stop, and when they did."""

    def __init__(self, stop: asyncio.Event) -> None:
        """Stop consuming once ``stop`` is set."""
        self.stop = stop
        self.ended = asyncio.Event()
        """Set once the broker was asked to stop delivering (or gave up)."""

    async def give_back(self, delivery: Delivery) -> None:
        """Requeue a delivery that will not run here, once consuming stopped.

        Requeued sooner, the same consumer could be handed it again at once,
        over and over until it stops.
        """
        with suppress(BrokerError, TimeoutError):
            async with asyncio.timeout(STOP_CONSUMING_TIMEOUT + ANSWER_TIMEOUT):
                await self.ended.wait()
                await delivery.requeue()


class CommitPublisher:
    """Publishes committed messages in the background, so commits never wait.

    One task per event loop offers the submitted ids to the broker, a
    database at a time, ``BATCH_SIZE`` ids per round, with one worker per
    database, so queues are declared once. A slow or failing broker delays
    only this task, never a commit; the backlog stops at
    ``MAX_UNPUBLISHED``, past which messages wait for the relay, as after a
    crash, and a broker that keeps failing opens the ``breaker``. The
    outbox holds the messages either way.

    The task runs while there is work and ends when there is none, so an
    idle publisher holds nothing of its event loop. It is supervised
    (``domainkit.tasks.supervise``): it runs in a clean context, never the
    committing request's, a crash is logged and the task starts again
    after a pause that doubles with each crash in a row, and shutdown
    cancels it with the loop's other tasks.
    """

    def __init__(self, config: MessagingSettings) -> None:
        """Publish to ``config.BROKER``; start the task on the first submit."""
        if config.BROKER is None:
            raise ValueError("CommitPublisher needs MESSAGING.BROKER.")
        self.config = config
        self.alias = config.BROKER
        self.unpublished: dict[str, list[uuid.UUID]] = {}
        self.backlog = 0
        """Messages submitted and not yet offered to the broker."""
        self.breaker = Breaker(
            self.alias, config.BREAKER_FAILURES, config.BREAKER_COOLDOWN
        )
        self._workers: dict[str, Worker] = {}
        self._watched: Watched | None = None
        self._task: asyncio.Task[None] | None = None
        self._flushes: list[asyncio.Future[None]] = []
        self._full = False

    def submit(self, database: str, ids: Sequence[uuid.UUID]) -> None:
        """Queue ``ids``, committed to ``database``'s outbox, for publishing."""
        if not self.breaker.allows():
            observe_left("breaker", len(ids))
            return
        if self.backlog + len(ids) > MAX_UNPUBLISHED:
            if not self._full:
                self._full = True
                logger.warning(
                    "Left %d committed messages to the relay: %d are already "
                    "waiting (logged once until the backlog clears)",
                    len(ids),
                    self.backlog,
                )
            observe_left("backlog", len(ids))
            return
        self.unpublished.setdefault(database, []).extend(ids)
        self.backlog += len(ids)
        self._start()

    async def flush(self) -> None:
        """Wait until every message submitted so far was offered to the broker."""
        if not self.backlog:
            return
        flushed = asyncio.get_running_loop().create_future()
        self._flushes.append(flushed)
        try:
            await flushed
        finally:
            if flushed in self._flushes:
                self._flushes.remove(flushed)

    async def close(self) -> None:
        """Flush for up to ``CLOSE_TIMEOUT``, then stop; the relay sends the rest.

        A publication cut short gives its messages back to the outbox
        before the task ends, within ``RELEASE_TIMEOUT``.
        """
        try:
            async with asyncio.timeout(self.config.CLOSE_TIMEOUT):
                await self.flush()
        except TimeoutError:
            logger.warning(
                "Left %d committed messages to the relay at shutdown", self.backlog
            )
            observe_left("shutdown", self.backlog)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self.unpublished.clear()
        self.backlog = 0
        self._idle()

    def _start(self) -> None:
        """Start the supervised task, unless it runs (or waits to restart)."""
        if self._task is None:
            self._task = supervise(
                self._run,
                name=f"messaging.publisher.{self.alias}",
                backoff=RESTART_BACKOFF,
                max_backoff=OUTAGE_MAX_PAUSE,
            )
            self._task.add_done_callback(self._ended)

    def _ended(self, task: asyncio.Task[None]) -> None:
        """Start again for work submitted meanwhile; otherwise wake flushes.

        Once the task is cancelled (shutdown), what is left goes to the relay.
        """
        if task is not self._task:
            return  # ``close`` let it go.
        self._task = None
        if task.cancelled():
            observe_left("shutdown", self.backlog)
            self.unpublished.clear()
            self.backlog = 0
        if self.unpublished:
            self._start()
        else:
            self._idle()

    def _idle(self) -> None:
        """Wake every ``flush``: nothing is left to offer."""
        self._full = False
        for flushed in self._flushes:
            if not flushed.done():
                flushed.set_result(None)
        self._flushes.clear()

    async def _run(self) -> None:
        """Publish what was submitted, ``BATCH_SIZE`` at a time, until none is left.

        Whatever a round does not publish (a failure, a crash, a stop) the
        relay publishes later; the outbox holds it.
        """
        while self.unpublished:
            batch, self.unpublished = self.unpublished, {}
            try:
                for database, ids in batch.items():
                    for chunk in batched(ids, self.config.BATCH_SIZE, strict=False):
                        try:
                            await self._publish(database, chunk)
                        except Exception:  # The relay publishes them later.
                            logger.exception(
                                "Could not publish committed messages; the relay will"
                            )
            finally:
                self.backlog -= sum(map(len, batch.values()))

    async def _publish(self, database: str, ids: Sequence[uuid.UUID]) -> None:
        """Publish one round of ``ids``, unless the breaker says to leave them."""
        if not self.breaker.allows():
            observe_left("breaker", len(ids))
            return
        broker = self._broker()
        if self.breaker.state == "probing":
            try:
                await broker.check()  # Before any row is locked.
            except BrokerError:
                observe_left("breaker", len(ids))
                return
        await self._worker(database).relay_ids(broker, ids)

    def _broker(self) -> Watched:
        """``BROKER``, watched by the breaker; replaced with the broker itself."""
        broker = brokers[self.alias]
        if self._watched is None or self._watched.inner() is not broker:
            self._watched = Watched(broker, self.breaker)
        return self._watched

    def _worker(self, database: str) -> Worker:
        """The worker of ``database``'s outbox, kept so it declares queues once."""
        if database not in self._workers:
            self._workers[database] = Worker(self.config, database=database)
        return self._workers[database]


_publishers: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[MessagingSettings, CommitPublisher]
] = WeakKeyDictionary()


def commit_publisher(config: MessagingSettings) -> CommitPublisher:
    """The running loop's publisher for ``config``.

    Publishers of loops closed without ``close_publishers`` are dropped
    here; what they had not published is left to the relay.
    """
    loop = asyncio.get_running_loop()
    for closed in [other for other in _publishers if other.is_closed()]:
        del _publishers[closed]
    publishers = _publishers.setdefault(loop, {})
    if config not in publishers:
        publishers[config] = CommitPublisher(config)
    return publishers[config]


async def close_publishers() -> None:
    """Flush and stop the running loop's publishers (at shutdown)."""
    for publisher in _publishers.pop(asyncio.get_running_loop(), {}).values():
        await publisher.close()


def publish_committed(
    config: MessagingSettings, database: str, ids: Sequence[uuid.UUID]
) -> None:
    """Hand just-committed messages to the background publisher.

    Runs after the commit, when ``BROKER`` and ``PUBLISH_ON_COMMIT`` are set,
    and returns at once: the commit never waits on the broker.
    """
    if config.BROKER is None or not config.PUBLISH_ON_COMMIT or not ids:
        return
    commit_publisher(config).submit(database, ids)


def _hosting_aliases() -> frozenset[str]:
    """Every database alias that holds an outbox."""
    return frozenset(
        alias
        for target in router.hosting_targets()
        for alias in router.aliases_of(target)
    )
