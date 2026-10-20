"""Load shedding: a cap on the HTTP requests a process works on at once.

``ConcurrencyLimitMiddleware`` lets at most ``MAX_CONCURRENT_REQUESTS`` HTTP
requests run at once in each process (each event loop). A request past the
cap waits for a slot, first come first served, for up to
``REQUEST_QUEUE_TIMEOUT`` seconds, behind at most ``MAX_QUEUED_REQUESTS``
others. When the queue is full, or the wait runs out, it is answered at
once: 503 ``overloaded`` with ``Retry-After``. Shedding is logged at most
every ``LOG_INTERVAL`` seconds, with a count.

A request holds its slot until its response starts, so a streamed response
does not keep one, or until it ends. A request cancelled while it waits,
or while it runs (its deadline passed, or the server cancelled it when the
client went away), gives its slot back.

Paths in ``CONCURRENCY_EXEMPT_PATHS`` (the health and readiness probes, and
the metrics scrape) are neither counted nor refused. WebSocket connections
are not counted: their limits are their own.

List it after ``DeadlineMiddleware``, so the wait counts against the
request's deadline, and before ``AuthenticationMiddleware``, so a refused
request costs no database lookup. With ``domainkit[metrics]`` it exports
``http_requests_admitted``, ``http_requests_queued``, and
``http_requests_shed_total`` by reason (``queue_full``, ``queue_timeout``).
"""

import asyncio
import logging
import math
from collections import deque
from contextlib import suppress
from typing import Final
from weakref import WeakKeyDictionary

from fastapi import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import clock
from domainkit.conf import settings
from domainkit.http.errors import error_response

try:
    from domainkit.http.metrics import observe_admitted, observe_queued, observe_shed
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_admitted(change: int) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

    def observe_queued(change: int) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

    def observe_shed(reason: str) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


OVERLOADED: Final = "overloaded"
"""The error code of a request refused for want of a slot."""
QUEUE_FULL: Final = "queue_full"
"""Refused at once: ``MAX_QUEUED_REQUESTS`` were waiting already."""
QUEUE_TIMEOUT: Final = "queue_timeout"
"""Refused after waiting ``REQUEST_QUEUE_TIMEOUT`` for a slot."""
RETRY_AFTER: Final = "1"
"""Seconds a refused client should wait before trying again."""
LOG_INTERVAL: Final = 10.0
"""Seconds between two warnings about shedding."""

logger = logging.getLogger(__name__)


class Slots:
    """At most ``limit`` holders at once; up to ``queue`` more wait in turn.

    Each event loop needs its own: the waiters are futures of one loop.
    """

    def __init__(self, limit: int, queue: int) -> None:
        """Allow ``limit`` holders and ``queue`` waiters."""
        self.limit = limit
        self.queue = queue
        self.held = 0
        """Slots taken, including those handed to a waiter not yet resumed."""
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def waiting(self) -> int:
        """Requests waiting for a slot now."""
        return sum(not waiter.done() for waiter in self._waiters)

    async def acquire(self, wait: float) -> str | None:
        """Take a slot, waiting up to ``wait`` seconds for one.

        Returns None once a slot is taken, or why none was:
        ``QUEUE_FULL`` or ``QUEUE_TIMEOUT``. Cancelled while waiting, it
        takes none, and passes on a slot it was handed meanwhile.
        """
        if self.held < self.limit and not self.waiting:
            self.held += 1
            return None
        if wait <= 0 or self.waiting >= self.queue:
            return QUEUE_FULL
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        observe_queued(1)
        try:
            async with asyncio.timeout(wait):
                await waiter
        except TimeoutError:
            pass  # Handed a slot as the wait ran out, it keeps it (below).
        except BaseException:
            if _handed(waiter):
                self.release()
            raise
        finally:
            observe_queued(-1)
            with suppress(ValueError):  # Already taken off by release().
                self._waiters.remove(waiter)
        return None if _handed(waiter) else QUEUE_TIMEOUT

    def release(self) -> None:
        """Hand the slot to the longest-waiting request, or free it."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self.held -= 1


def _handed(waiter: asyncio.Future[None]) -> bool:
    """Whether ``waiter`` was handed a slot (it may have stopped waiting since)."""
    return waiter.done() and not waiter.cancelled()


class ConcurrencyLimitMiddleware:
    """Refuse HTTP requests past ``MAX_CONCURRENT_REQUESTS`` with 503."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the cap, the queue, and the exempt paths."""
        self.app = app
        self.limit = settings.MAX_CONCURRENT_REQUESTS
        self.queue = settings.MAX_QUEUED_REQUESTS
        self.wait = settings.REQUEST_QUEUE_TIMEOUT
        self.exempt = frozenset(settings.CONCURRENCY_EXEMPT_PATHS)
        self.report = ShedReport(self.limit)
        self._slots: WeakKeyDictionary[asyncio.AbstractEventLoop, Slots] = (
            WeakKeyDictionary()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request in a slot, or refuse it when none frees in time."""
        if (
            self.limit is None
            or scope["type"] != "http"
            or scope["path"] in self.exempt
        ):
            await self.app(scope, receive, send)
            return
        slots = self.slots(self.limit)
        refused = await slots.acquire(self.wait)
        if refused is not None:
            observe_shed(refused)
            self.report.shed(refused)
            await _answer(scope, receive, send)
            return
        observe_admitted(1)
        held = True

        def give_back() -> None:
            nonlocal held
            if held:
                held = False
                slots.release()
                observe_admitted(-1)

        async def send_releasing(message: Message) -> None:
            if message["type"] == "http.response.start":
                give_back()
            await send(message)

        try:
            await self.app(scope, receive, send_releasing)
        finally:
            give_back()

    def slots(self, limit: int) -> Slots:
        """The running event loop's slots, ``limit`` of them."""
        loop = asyncio.get_running_loop()
        if loop not in self._slots:
            self._slots[loop] = Slots(limit, self.queue)
        return self._slots[loop]


class ShedReport:
    """Warn about shedding at most once per ``LOG_INTERVAL``, with a count."""

    def __init__(self, limit: int | None) -> None:
        """Name ``limit`` in the warnings."""
        self.limit = limit
        self.count = 0
        """Requests refused since the last warning."""
        self._quiet_until = -math.inf

    def shed(self, reason: str) -> None:
        """Count one refused request; warn if the last warning is old enough."""
        self.count += 1
        now = clock.monotonic()
        if now < self._quiet_until:
            return
        logger.warning(
            "Shed %d request(s) past MAX_CONCURRENT_REQUESTS (%s); the last: %s",
            self.count,
            self.limit,
            reason,
        )
        self.count = 0
        self._quiet_until = now + LOG_INTERVAL


async def _answer(scope: Scope, receive: Receive, send: Send) -> None:
    """503 ``overloaded``, with ``Retry-After``."""
    response = error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The server is busy; try again shortly.",
        code=OVERLOADED,
    )
    response.headers["Retry-After"] = RETRY_AFTER
    await response(scope, receive, send)
