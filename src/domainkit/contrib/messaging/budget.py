"""One concurrency budget per worker process (``MESSAGING.CONCURRENCY``).

Every source of work in a ``runworker`` process draws from the same budget:
the polling worker of each outbox, ``runworker --once``, and the deliveries
of every queue a broker hands over. A message holds its slot from the claim
(or the delivery) until its outcome is recorded, so the process never runs
more than ``CONCURRENCY`` messages at once, however many outboxes and queues
it serves.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class Budget:
    """Slots for the messages a worker process runs at once.

    Waiters are served first come, first served: a slot given back goes to
    the longest waiter, never to a caller that arrives later.
    """

    def __init__(self, size: int) -> None:
        """Allow ``size`` messages at once."""
        if size < 1:
            raise ValueError("A budget needs at least one slot.")
        self.size = size
        self.used = 0
        """Slots taken now."""
        self._waiters: deque[asyncio.Future[None]] = deque()

    def __repr__(self) -> str:
        """Show how many slots are taken."""
        return f"<Budget {self.used}/{self.size}>"

    @property
    def free(self) -> int:
        """Slots no message holds now."""
        return self.size - self.used

    async def acquire(self, most: int = 1, *, stop: asyncio.Event | None = None) -> int:
        """Wait for a free slot, then take up to ``most``; return how many.

        Returns 0, holding nothing, once ``stop`` is set (or if it already
        is). Give each slot back with ``release``.
        """
        if most < 1:
            raise ValueError("Acquire at least one slot.")
        if stop is not None and stop.is_set():
            return 0
        if not self._waiters and self.free:
            return self._take(most)
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        stopping = None
        if stop is not None:
            stopping = asyncio.ensure_future(stop.wait())
            stopping.add_done_callback(lambda _: waiter.cancel())
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self.release()  # Handed a slot just as it was cancelled.
            task = asyncio.current_task()
            if waiter.cancelled() and task is not None and not task.cancelling():
                return 0  # ``stop`` was set; the task itself goes on.
            raise
        finally:
            if stopping is not None:
                stopping.cancel()
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        # One slot came with the wake-up; the rest only if nobody waits.
        return 1 + (self._take(most - 1) if not self._waiters else 0)

    def release(self, count: int = 1) -> None:
        """Give back ``count`` slots; waiters get them first."""
        if not 0 <= count <= self.used:
            raise ValueError(f"Cannot give back {count} of {self.used} slots.")
        self.used -= count
        while self._waiters and self.free:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.used += 1  # Handed straight to the waiter.
                waiter.set_result(None)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one slot for the block."""
        await self.acquire()
        try:
            yield
        finally:
            self.release()

    def _take(self, most: int) -> int:
        """Take up to ``most`` free slots at once."""
        taken = min(most, self.free)
        self.used += taken
        return taken
