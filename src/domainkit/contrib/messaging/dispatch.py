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
A relay waits out database outages as the polling worker does.

With ``PUBLISH_ON_COMMIT``, a commit hands its message ids to the loop's
``CommitPublisher``, which publishes them in the background: the commit
never waits on the broker.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from contextlib import suppress
from typing import Final
from weakref import WeakKeyDictionary

from domainkit.broker import Broker, BrokerError, Delivery, brokers
from domainkit.db.routing import router

from .settings import MessagingSettings
from .worker import Worker

try:
    from .metrics import observe_skipped
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_skipped() -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)

MAX_UNPUBLISHED: Final = 10_000
"""Committed messages waiting to be published; past it, the relay sends them."""
CLOSE_TIMEOUT: Final = 5.0
"""Seconds shutdown waits for committed messages to reach the broker."""


class BrokerWorkers:
    """Relay the outboxes of ``workers`` and consume ``queues`` from one broker."""

    def __init__(
        self,
        workers: Sequence[Worker],
        queues: Iterable[str],
        config: MessagingSettings,
        *,
        relay: bool = True,
    ) -> None:
        """Relay each worker's outbox; run deliveries of ``queues`` from any outbox.

        Without ``relay``, only consume: other workers relay, schedule periodic
        tasks, and clean up, so this one never polls the database.
        """
        if config.BROKER is None:
            raise ValueError("BrokerWorkers needs MESSAGING.BROKER.")
        self.relays = relay
        self.workers = list(workers)
        self.queues = sorted(set(queues))
        self.config = config
        self.alias = config.BROKER
        self._outboxes = {worker.database: worker for worker in workers}

    def outbox(self, database: str) -> Worker | None:
        """The worker of ``database``'s outbox, if it hosts domains."""
        if database not in self._outboxes and database in _hosting_aliases():
            self._outboxes[database] = Worker(self.config, database=database)
        return self._outboxes.get(database)

    async def run(self, stop: asyncio.Event) -> None:
        """Relay and consume until ``stop`` is set; then finish the running ones."""
        broker = brokers[self.alias]
        active = 0
        idle = asyncio.Event()
        idle.set()

        async def handle(delivery: Delivery) -> None:
            nonlocal active
            active += 1
            idle.clear()
            try:
                await self.deliver(delivery)
            finally:
                active -= 1
                if active == 0:
                    idle.set()

        subscription = await broker.consume(
            self.queues, self.config.CONCURRENCY, handle
        )
        try:
            if self.relays:
                await asyncio.gather(
                    *(
                        self._relay_until(worker, broker, stop)
                        for worker in self.workers
                    )
                )
            else:
                await stop.wait()
        finally:
            await subscription.cancel()
            await idle.wait()  # Deliveries already running finish first.

    async def _relay_until(
        self, worker: Worker, broker: Broker, stop: asyncio.Event
    ) -> None:
        """Schedule, clean up, and relay until ``stop``; poll when nothing is due."""
        batch = self.config.BATCH_SIZE
        outage = worker.outage()
        while not stop.is_set():
            try:
                await worker.schedule_periodic()
                await worker.run_cleanup_when_due()
                relayed = await worker.relay(broker, batch)
            except Exception as exc:  # noqa: BLE001 - logged; the relay waits.
                await outage.failed(exc, stop)
                continue
            outage.recovered()
            if relayed < batch:
                try:
                    async with asyncio.timeout(self.config.POLL_INTERVAL):
                        await stop.wait()
                except TimeoutError:
                    pass

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


class CommitPublisher:
    """Publishes committed messages in the background, so commits never wait.

    One task per event loop offers the submitted ids to the broker, a
    database at a time, with one worker per database, so queues are declared
    once. A slow or failing broker delays only this task, never a commit;
    the backlog stops at ``MAX_UNPUBLISHED``, past which messages wait for
    the relay, as after a crash. The outbox holds them either way.
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
        self._workers: dict[str, Worker] = {}
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task[None] | None = None

    def submit(self, database: str, ids: Sequence[uuid.UUID]) -> None:
        """Queue ``ids``, committed to ``database``'s outbox, for publishing."""
        if self.backlog + len(ids) > MAX_UNPUBLISHED:
            logger.warning(
                "Left %d committed messages to the relay: %d are already waiting",
                len(ids),
                self.backlog,
            )
            return
        self.unpublished.setdefault(database, []).extend(ids)
        self.backlog += len(ids)
        self._idle.clear()
        self._wake.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def flush(self) -> None:
        """Wait until every message submitted so far was offered to the broker."""
        await self._idle.wait()

    async def close(self) -> None:
        """Flush for up to ``CLOSE_TIMEOUT``, then stop; the relay sends the rest."""
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await self.flush()
        except TimeoutError:
            logger.warning(
                "Left %d committed messages to the relay at shutdown", self.backlog
            )
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        """Publish what was submitted, round after round."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            batch, self.unpublished = self.unpublished, {}
            for database, ids in batch.items():
                try:
                    await self._worker(database).relay_ids(brokers[self.alias], ids)
                except Exception:  # The relay publishes them on its next round.
                    logger.exception(
                        "Could not publish committed messages; the relay will"
                    )
                self.backlog -= len(ids)
            if not self.unpublished:
                self._idle.set()

    def _worker(self, database: str) -> Worker:
        """The worker of ``database``'s outbox, kept so it declares queues once."""
        if database not in self._workers:
            self._workers[database] = Worker(self.config, database=database)
        return self._workers[database]


_publishers: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[MessagingSettings, CommitPublisher]
] = WeakKeyDictionary()


def commit_publisher(config: MessagingSettings) -> CommitPublisher:
    """The running loop's publisher for ``config``."""
    loop = asyncio.get_running_loop()
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
