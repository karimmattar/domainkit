"""What the brokers that are asked for messages share: Redis, NATS, Kafka, SQS.

RabbitMQ pushes messages to its consumers; these brokers are asked. A
subscription runs one pump: while one of its ``prefetch`` slots is free,
it takes every free slot, asks the broker for that many messages (a
``Puller`` waits a little for them), and hands each to the handler on a
task of its own. A message holds its slot until it is answered; only its
first answer counts. A broker that cannot be reached is logged, and the
pump asks again after a pause that grows to ``MAX_PAUSE``: the outbox
stays the record of every message, so an outage delays work, never
loses it.
"""

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from domainkit.broker.base import Broker, Delivery, Handler, Subscription

__all__ = ["Fetched", "PullBroker", "Puller"]

logger = logging.getLogger("domainkit.broker")
FIRST_PAUSE: Final = 0.5
MAX_PAUSE: Final = 30.0
IDLE_PAUSE: Final = 0.05
"""Seconds the pump waits after a fetch that found nothing at once, so a
broker that answers an empty fetch without waiting never spins it."""
MAX_BATCH: Final = 10
"""Messages asked for at once, whatever the free slots."""


@dataclass(frozen=True, slots=True)
class Fetched:
    """A message a puller took: its queue, body, id, and the broker's handle."""

    queue: str
    body: bytes
    message_id: str
    handle: Any


class Puller(ABC):
    """Asks one broker for the messages of a subscription's queues."""

    @abstractmethod
    async def fetch(self, limit: int) -> Sequence[Fetched]:
        """Up to ``limit`` messages, waiting a little for one; maybe none.

        Raises:
            BrokerError: The broker could not be asked.
        """

    @abstractmethod
    async def ack(self, message: Fetched) -> None:
        """Done with ``message``: the broker forgets it."""

    @abstractmethod
    async def requeue(self, message: Fetched) -> None:
        """Not now: the broker hands ``message`` out again."""

    async def close(self) -> None:  # noqa: B027 - optional.
        """Release what the puller holds."""


class _PullSubscription(Subscription):
    """A pump, and the puller it asks."""

    def __init__(self, pump: asyncio.Task[None], puller: Puller) -> None:
        """Own ``pump`` and ``puller``."""
        self._pump = pump
        self._puller = puller

    async def cancel(self) -> None:
        """Stop the pump; deliveries being handled carry on, and may answer."""
        self._pump.cancel()
        await asyncio.gather(self._pump, return_exceptions=True)
        await self._puller.close()


class PullBroker(Broker):
    """A broker that is asked for messages (see the module)."""

    def __init__(self) -> None:
        """Track the deliveries being handled."""
        self._handling: set[asyncio.Task[None]] = set()

    @abstractmethod
    async def puller(self, queues: Sequence[str]) -> Puller:
        """A puller for ``queues``, declared."""

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Pump ``queues`` into ``handler``, at most ``prefetch`` at once in all."""
        names = list(queues)
        await self.declare(names)
        puller = await self.puller(names)
        pump = asyncio.create_task(
            self._pump(puller, asyncio.Semaphore(prefetch), handler)
        )
        return _PullSubscription(pump, puller)

    async def _pump(
        self, puller: Puller, slots: asyncio.Semaphore, handler: Handler
    ) -> None:
        """Ask for as many messages as slots are free, and hand them out."""
        pause = FIRST_PAUSE
        while True:
            await slots.acquire()
            taken = 1
            while taken < MAX_BATCH and not slots.locked():
                await slots.acquire()
                taken += 1
            try:
                messages = await puller.fetch(taken)
            except asyncio.CancelledError:
                for _ in range(taken):
                    slots.release()
                raise
            except Exception as exc:  # noqa: BLE001 - logged; asked again later.
                for _ in range(taken):
                    slots.release()
                logger.warning(
                    "The broker could not be asked for messages (%s); again in %ss.",
                    type(exc).__name__,
                    pause,
                )
                await asyncio.sleep(pause)
                pause = min(pause * 2, MAX_PAUSE)
                continue
            pause = FIRST_PAUSE
            for _ in range(taken - len(messages)):
                slots.release()
            if not messages:
                await asyncio.sleep(IDLE_PAUSE)
            for message in messages:
                task = asyncio.create_task(
                    self._deliver(puller, message, slots, handler)
                )
                self._handling.add(task)
                task.add_done_callback(self._handling.discard)

    async def _deliver(
        self,
        puller: Puller,
        message: Fetched,
        slots: asyncio.Semaphore,
        handler: Handler,
    ) -> None:
        """Run ``handler``; the message holds its slot until it is answered."""
        answered = False

        def settle() -> bool:
            nonlocal answered
            if answered:
                return False
            answered = True
            slots.release()
            return True

        async def ack() -> None:
            if settle():
                await puller.ack(message)

        async def requeue() -> None:
            if settle():
                await puller.requeue(message)

        await handler(
            Delivery(message.queue, message.body, message.message_id, ack, requeue)
        )

    async def close(self) -> None:
        """Wait a moment for deliveries being answered, then let them go."""
        if self._handling:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait(set(self._handling), timeout=1.0)
