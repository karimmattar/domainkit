"""An in-process broker (``memory://``) for tests and development.

Its queues live in one process and one event loop: the relay and the
consumers of a single ``runworker`` share them, but nothing else does, so
staging and production refuse it.
"""

import asyncio
from collections import deque
from collections.abc import Iterable, Sequence

from domainkit.broker.base import Broker, BrokerRefused, Delivery, Handler, Subscription

type _Message = tuple[bytes, str]


class _MemorySubscription(Subscription):
    """The pump that hands queued messages to a handler."""

    def __init__(self, pump: asyncio.Task[None]) -> None:
        """Own ``pump``."""
        self._pump = pump

    async def cancel(self) -> None:
        """Stop the pump; handlers already running finish."""
        self._pump.cancel()
        await asyncio.gather(self._pump, return_exceptions=True)


class MemoryBroker(Broker):
    """Queues of this process: one consumer gets each message, as in RabbitMQ."""

    def __init__(self) -> None:
        """Start without queues."""
        self._queues: dict[str, deque[_Message]] = {}
        self._arrived = asyncio.Event()
        self._handling: set[asyncio.Task[None]] = set()

    async def declare(self, queues: Iterable[str]) -> None:
        """Create the missing queues."""
        for queue in queues:
            self._queues.setdefault(queue, deque())

    async def publish(self, queue: str, body: bytes, *, message_id: str) -> None:
        """Append to ``queue``; refuse queues nobody declared, as RabbitMQ does."""
        if queue not in self._queues:
            raise BrokerRefused(f"No queue {queue!r} takes the message.")
        self._put(queue, (body, message_id))

    def pending(self, queue: str) -> int:
        """Messages waiting in ``queue`` (tests); 0 for a queue never declared."""
        return len(self._queues.get(queue, ()))

    async def consume(
        self, queues: Iterable[str], prefetch: int, handler: Handler
    ) -> Subscription:
        """Pump the queues into ``handler``, at most ``prefetch`` at once in all."""
        queues = list(queues)
        await self.declare(queues)
        pump = asyncio.create_task(
            self._pump(queues, asyncio.Semaphore(prefetch), handler)
        )
        return _MemorySubscription(pump)

    def _put(self, queue: str, message: _Message) -> None:
        """Queue ``message`` and wake the pumps."""
        self._queues[queue].append(message)
        self._arrived.set()

    async def _pump(
        self, queues: Sequence[str], slots: asyncio.Semaphore, handler: Handler
    ) -> None:
        """Hand out the queues' messages, in turn, while a slot is free.

        One pump serves every queue of a subscription, so while it waits for
        a message it holds one slot, not one per queue: an empty queue never
        keeps a slot from a busy one.
        """
        turn = 0
        while True:
            await slots.acquire()
            try:
                queue, (body, message_id) = await self._next(queues, turn)
            except asyncio.CancelledError:
                slots.release()
                raise
            turn = queues.index(queue) + 1
            task = asyncio.create_task(
                self._deliver(queue, body, message_id, slots, handler)
            )
            self._handling.add(task)
            task.add_done_callback(self._handling.discard)

    async def _next(self, queues: Sequence[str], turn: int) -> tuple[str, _Message]:
        """Take the next message, looking at ``queues`` from ``turn`` on."""
        while True:
            for offset in range(len(queues)):
                queue = queues[(turn + offset) % len(queues)]
                if self._queues[queue]:
                    return queue, self._queues[queue].popleft()
            self._arrived.clear()
            await self._arrived.wait()

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
                self._put(queue, (body, message_id))

        await handler(Delivery(queue, body, message_id, ack, requeue))
