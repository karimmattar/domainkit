"""Workers on a broker: relays publish due messages, consumers run them.

The outbox stays the record of every message (see ADR 0014). A relay per
outbox claims due rows, as the polling worker does, and publishes a
pointer to each: its id, its database, and the relay's lock. A consumer
takes the row over by id, which only works for a delivery that carries the
current lock (or one whose lock expired), runs it as the polling worker
would, and acknowledges it after the outcome is recorded. Retries go back
to the outbox with their delay, and the relay publishes them when due.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable, Sequence

from domainkit.broker import Broker, Delivery, brokers
from domainkit.db.routing import router

from .settings import MessagingSettings
from .worker import Worker

try:
    from .metrics import observe_skipped
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_skipped() -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)


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
        while not stop.is_set():
            await worker.schedule_periodic()
            await worker.run_cleanup_when_due()
            if await worker.relay(broker, batch) < batch:
                try:
                    async with asyncio.timeout(self.config.POLL_INTERVAL):
                        await stop.wait()
                except TimeoutError:
                    pass

    async def deliver(self, delivery: Delivery) -> None:
        """Run one delivered message; acknowledge it once the outcome is recorded."""
        try:
            pointer = json.loads(delivery.body)
            message_id = uuid.UUID(pointer["id"])
            relay_lock = uuid.UUID(pointer["lock"])
            database = str(pointer["database"])
        except (ValueError, KeyError, TypeError):
            logger.error("Dropped an unreadable message from queue %r", delivery.queue)
            await delivery.ack()
            return
        worker = self.outbox(database)
        if worker is None:
            logger.error(
                "Dropped message %s: %r hosts no outbox here", message_id, database
            )
            await delivery.ack()
            return
        try:
            claimed = await worker.take_over(message_id, relay_lock)
        except Exception:
            logger.exception("Could not take over message %s; requeued", message_id)
            await asyncio.sleep(self.config.POLL_INTERVAL)
            await delivery.requeue()
            return
        if claimed is None:
            observe_skipped()
            logger.debug("Skipped message %s: done, running, or moved on", message_id)
        else:
            await worker.process(claimed)
        await delivery.ack()


async def publish_committed(
    config: MessagingSettings, database: str, ids: Sequence[uuid.UUID]
) -> None:
    """Hand just-committed messages to the broker; the relay catches what fails.

    Runs in the committing process, after the commit, when ``BROKER`` and
    ``PUBLISH_ON_COMMIT`` are set. It never raises: the messages are safe
    in the outbox either way.
    """
    if config.BROKER is None or not config.PUBLISH_ON_COMMIT or not ids:
        return
    try:
        await Worker(config, database=database).relay_ids(brokers[config.BROKER], ids)
    except Exception:  # The relay publishes them on its next round.
        logger.exception("Could not publish committed messages; the relay will")


def _hosting_aliases() -> frozenset[str]:
    """Every database alias that holds an outbox."""
    return frozenset(
        alias
        for target in router.hosting_targets()
        for alias in router.aliases_of(target)
    )
