"""An in-process broker (``memory://``) for tests and development.

Its queues live in one process and one event loop: the relay and the
consumers of a single ``runworker`` share them, but nothing else does, so
staging and production refuse it.
"""

import asyncio
from collections.abc import Iterable

from domainkit.broker.base import Broker, BrokerRefused, Delivery, Handler, Subscription

type _Message = tuple[bytes, str]


class _MemorySubscription(Subscription):
    """The pumps that hand queued messages to a handler."""

    def __init__(self, pumps: list[asyncio.Task[None]]) -> None:
        """Own ``pumps``."""
        self._pumps = pumps

    async def cancel(self) -> None:
        """Stop the pumps; handlers already running finish."""
        for pump in self._pumps:
            pump.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)


class MemoryBroker(Broker):
    """Queues of this process: one consumer gets each message, as in RabbitMQ."""

    def __init__(self) -> None:
        """Start without queues."""
        self._queues: dict[str, asyncio.Queue[_Message]] = {}
        self._handling: set[asyncio.Task[None]] = set()

    async def declare(self, queues: Iterable[str]) -> None:
        """Create the missing queues."""
        for queue in queues:
            self._queues.setdefault(queue, asyncio.Queue())

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Append to ``queue``; refuse queues nobody declared, as RabbitMQ does."""
        if queue not in self._queues:
            raise BrokerRefused(f"No queue {queue!r} takes the message.")
        self._queues[queue].put_nowait((body, message_id))

    def pending(self, queue: str) -> int:
        """Messages waiting in ``queue`` (tests); 0 for a queue never declared."""
        return self._queues[queue].qsize() if queue in self._queues else 0

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Pump each queue into ``handler``, at most ``prefetch`` at once."""
        queues = list(queues)
        await self.declare(queues)
        slots = asyncio.Semaphore(prefetch)
        pumps = [
            asyncio.create_task(self._pump(queue, slots, handler)) for queue in queues
        ]
        return _MemorySubscription(pumps)

    async def _pump(
        self, queue: str, slots: asyncio.Semaphore, handler: Handler
    ) -> None:
        """Hand out ``queue``'s messages while a slot is free."""
        source = self._queues[queue]
        while True:
            await slots.acquire()
            try:
                body, message_id = await source.get()
            except asyncio.CancelledError:
                slots.release()
                raise
            task = asyncio.create_task(
                self._deliver(queue, body, message_id, slots, handler)
            )
            self._handling.add(task)
            task.add_done_callback(self._handling.discard)

    async def _deliver(
        self,
        queue: str,
        body: bytes,
        message_id: str,
        slots: asyncio.Semaphore,
        handler: Handler,
    ) -> None:
        """Run ``handler``; the message holds its slot until it is answered.

        As in RabbitMQ, a handler may answer after it returns, and only the
        first answer counts.
        """
        answered = False

        def settle() -> bool:
            nonlocal answered
            if answered:
                return False
            answered = True
            slots.release()
            return True

        async def ack() -> None:
            settle()

        async def requeue() -> None:
            if settle():
                self._queues[queue].put_nowait((body, message_id))

        await handler(Delivery(queue, body, message_id, ack, requeue))
