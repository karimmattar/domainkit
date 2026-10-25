"""Load shedding: a cap on the HTTP requests an event loop works on at once.

``ConcurrencyLimitMiddleware`` lets at most ``MAX_CONCURRENT_REQUESTS`` HTTP
requests run at once on each event loop: a process serving on one loop
admits the cap, and one running a loop per thread admits it on each (the
waiters of a slot belong to one loop). A request past the cap waits for a
slot, first come first served, for up to ``REQUEST_QUEUE_TIMEOUT`` seconds,
behind at most ``MAX_QUEUED_REQUESTS`` others. When the queue is full, or
the wait runs out, it is answered at once: 503 ``overloaded`` with
``Retry-After``. Shedding is logged at most every ``LOG_INTERVAL`` seconds,
with a count.

A request holds its slot until its response starts, so a streamed response
does not keep one, or until it ends. A request cancelled while it waits,
or while it runs (its deadline passed, or the server cancelled it when the
client went away), gives its slot back.

Time spent waiting on the client is not counted: while the application
waits in ``receive()`` for a chunk of the request's body, its slot is lent
to the next request, and taken back, through the queue, when the chunk
arrives. So clients that send their headers and then their body slowly, or
never, hold no slot; ``REQUEST_BODY_TIMEOUT`` answers them 408 (see
``RequestBodyLimitMiddleware``). A chunk that finds no slot free in time is
answered 503 ``overloaded``, as a new request would be (``Overloaded``).

Paths in ``CONCURRENCY_EXEMPT_PATHS`` (the health and readiness probes, and
the metrics scrape) are neither counted nor refused, behind a root path
(``--root-path``) too. WebSocket connections are not counted: their limits
are their own.

List it after ``DeadlineMiddleware``, so the wait counts against the
request's deadline, and before ``AuthenticationMiddleware``, so a refused
request costs no database lookup. A request whose deadline passes while it
waits is answered 503 ``deadline_exceeded`` by ``DeadlineMiddleware``, and
counted and logged as shed (``queue_timeout``) too: it never got a slot.
With ``domainkit[metrics]`` it exports ``http_requests_admitted``,
``http_requests_queued``, and ``http_requests_shed_total`` by reason
(``queue_full``, ``queue_timeout``).
"""

import asyncio
import logging
import math
import threading
from collections import deque
from typing import Any, Final
from weakref import WeakKeyDictionary

from fastapi import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import clock, deadlines
from domainkit.conf import settings
from domainkit.http.errors import APIError, error_response

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
    Taking a slot, refusing one, and leaving the queue cost the same
    however long the queue is.
    """

    def __init__(self, limit: int, queue: int) -> None:
        """Allow ``limit`` holders and ``queue`` waiters."""
        self.limit = limit
        self.queue = queue
        self.held = 0
        """Slots taken, including those handed to a waiter not yet resumed."""
        self._waiters: deque[_Waiter] = deque()
        """Waiters in turn; those that stopped waiting stay until passed over."""
        self._waiting = 0

    @property
    def waiting(self) -> int:
        """Requests waiting for a slot now."""
        return self._waiting

    async def acquire(self, wait: float) -> str | None:
        """Take a slot, waiting up to ``wait`` seconds for one.

        The wait is bounded by the deadline too (``domainkit.deadlines``):
        a deadline that a timer enforces (a request's) cancels it, and one
        that none enforces ends it as ``wait`` running out does.

        Returns None once a slot is taken, or why none was:
        ``QUEUE_FULL`` or ``QUEUE_TIMEOUT``. Cancelled while waiting, it
        takes none, and passes on a slot it was handed meanwhile.
        """
        if self.held < self.limit and not self._waiting:
            self.held += 1
            return None
        if wait <= 0 or self._waiting >= self.queue:
            return QUEUE_FULL
        waiter = _Waiter(self, asyncio.get_running_loop())
        self._waiters.append(waiter)
        self._waiting += 1
        observe_queued(1)
        try:
            async with deadlines.timeout(wait):
                await waiter
        except TimeoutError:
            pass  # Handed a slot as the wait ran out, it keeps it (below).
        except BaseException:
            if _handed(waiter):
                self.release()
            raise
        finally:
            observe_queued(-1)
            waiter.cancel()  # Unless handed: then release() took it off.
            self._sweep()
        return None if _handed(waiter) else QUEUE_TIMEOUT

    def release(self) -> None:
        """Hand the slot to the longest-waiting request, or free it."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                self._waiting -= 1
                return
        self.held -= 1

    def _sweep(self) -> None:
        """Drop waiters that stopped waiting, cheaply, so the queue stays small.

        Those at either end go at once; those in the middle go together once
        they outnumber the waiters, so each costs O(1) on average.
        """
        waiters = self._waiters
        while waiters and waiters[-1].done():
            waiters.pop()
        while waiters and waiters[0].done():
            waiters.popleft()
        if len(waiters) > 2 * self._waiting + _SWEEP_SLACK:
            self._waiters = deque(waiter for waiter in waiters if not waiter.done())


_SWEEP_SLACK: Final = 16
"""Waiters that stopped waiting may stay in the queue up to this many more."""


class _Waiter(asyncio.Future[None]):
    """A place in the queue; cancelling it takes it out of the count at once.

    A request cancelled while it waits cancels its place before it runs
    again, so ``waiting`` never counts it, as ``acquire`` must not.
    """

    def __init__(self, slots: Slots, loop: asyncio.AbstractEventLoop) -> None:
        """A place in ``slots``' queue."""
        super().__init__(loop=loop)
        self._slots = slots

    def cancel(self, msg: Any | None = None) -> bool:  # noqa: ANN401 - asyncio's.
        """Stop waiting; return whether this place was still waiting."""
        cancelled = super().cancel(msg)
        if cancelled:
            self._slots._waiting -= 1
        return cancelled


def _handed(waiter: asyncio.Future[None]) -> bool:
    """Whether ``waiter`` was handed a slot (it may have stopped waiting since)."""
    return waiter.done() and not waiter.cancelled()


class Overloaded(APIError):
    """503 ``overloaded``: a request's body arrived, and no slot was free for it.

    Raised from ``receive()``, so FastAPI's body parsing passes it on, and
    answered as any refused request is, with ``Retry-After``.
    """

    def __init__(self) -> None:
        """Use status 503 and the code of every refusal for want of a slot."""
        super().__init__(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The server is busy; try again shortly.",
            code=OVERLOADED,
            headers={"Retry-After": RETRY_AFTER},
        )


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
        if self.limit is None or scope["type"] != "http" or _path(scope) in self.exempt:
            await self.app(scope, receive, send)
            return
        slots = self.slots(self.limit)
        refused = await self.admit(slots)
        if refused is not None:
            self.refused(refused)
            await _answer(scope, receive, send)
            return
        observe_admitted(1)
        hold = _Hold(self, slots, receive, send)
        try:
            await self.app(scope, hold.receive, hold.send)
        except Overloaded:
            # Raised outside FastAPI's error handling (a middleware that
            # reads the body itself): answer for it.
            if hold.started:
                raise
            await _answer(scope, receive, send)
        finally:
            hold.give_back()

    async def admit(self, slots: Slots) -> str | None:
        """Take a slot of ``slots``; None, or why none was (``Slots.acquire``).

        The queue's wait ends at the deadline too. A request whose deadline
        passes while it waits is answered by ``DeadlineMiddleware``, whose
        timer cancels it, and is counted here as shed (``queue_timeout``).
        """
        try:
            return await slots.acquire(self.wait)
        except asyncio.CancelledError:
            deadline = deadlines.current_deadline()
            if deadline is not None and deadline.expired():
                self.refused(QUEUE_TIMEOUT)
            raise

    def refused(self, reason: str) -> None:
        """Count and report a request refused for want of a slot."""
        observe_shed(reason)
        self.report.shed(reason)

    def slots(self, limit: int) -> Slots:
        """The running event loop's slots, ``limit`` of them."""
        loop = asyncio.get_running_loop()
        if loop not in self._slots:
            self._slots[loop] = Slots(limit, self.queue)
        return self._slots[loop]


class _Hold:
    """One request's slot: lent while it waits for its body, then taken back.

    ``receive`` lends the slot when the call has to wait for the client, and
    takes it back, through the queue, before returning what arrived: the
    request then works only while it holds a slot. Once the body is in, or
    the response has started, ``receive`` lends nothing. ``send`` gives the
    slot back for good when the response starts.
    """

    def __init__(
        self,
        middleware: ConcurrencyLimitMiddleware,
        slots: Slots,
        receive: Receive,
        send: Send,
    ) -> None:
        """Hold a slot of ``slots``, taken for the request already."""
        self.middleware = middleware
        self.slots = slots
        self._receive = receive
        self._send = send
        self.held = True
        """Whether the request holds a slot now."""
        self.lent = False
        """Whether its slot was lent while it waited, and is owed back."""
        self.started = False
        """Whether the response started: the slot is given back for good."""
        self.body_read = False
        """Whether the whole body arrived (or the client went away)."""

    async def receive(self) -> Message:
        """The next message from the client; the slot is lent while it waits.

        A wait that ends in an error (the request was cancelled: its
        deadline passed, or its client left) takes nothing back.

        Raises:
            Overloaded: The body arrived, and no slot freed in time for it.
        """
        if self.body_read or not self.held:
            return await self._receive()
        task = asyncio.current_task()
        # Runs only if the call below waits for the loop: then it is lent.
        lending = asyncio.get_running_loop().call_soon(self._lend, task)
        try:
            message = await self._receive()
        finally:
            lending.cancel()
        self.body_read = message["type"] != "http.request" or not message.get(
            "more_body", False
        )
        if self.lent:
            await self._take_back()
        return message

    async def send(self, message: Message) -> None:
        """Send ``message``; the response starting gives the slot back."""
        if message["type"] == "http.response.start":
            self.give_back()
        await self._send(message)

    def give_back(self) -> None:
        """Free the slot for good: the response started, or the request ended."""
        self.started = True
        self.lent = False
        if self.held:
            self.held = False
            self.slots.release()
            observe_admitted(-1)

    def _lend(self, task: "asyncio.Task[Any] | None") -> None:
        """Lend the slot while the request waits for the client.

        Not when the task is being cancelled (a deadline, or a check for a
        disconnect that will not wait): it is not waiting for the client.
        """
        if self.held and not (task is not None and task.cancelling()):
            self.held = False
            self.lent = True
            self.slots.release()
            observe_admitted(-1)

    async def _take_back(self) -> None:
        """Take a slot again, through the queue, as a new request would.

        Raises:
            Overloaded: The queue was full, or no slot freed in time.
        """
        self.lent = False
        refused = await self.middleware.admit(self.slots)
        if self.started:  # The response started meanwhile: no slot is needed.
            if refused is None:
                self.slots.release()
            return
        if refused is not None:
            self.middleware.refused(refused)
            raise Overloaded
        self.held = True
        observe_admitted(1)


def _path(scope: Scope) -> str:
    """The request's path, without the root path the application is mounted at."""
    path: str = scope["path"]
    root = scope.get("root_path", "").rstrip("/")
    if root and path.startswith(root):
        return path[len(root) :]
    return path


class ShedReport:
    """Warn about shedding at most once per ``LOG_INTERVAL``, with a count.

    Event loops on several threads share a middleware's report (free-threaded
    Python), so the count changes only under its lock.
    """

    def __init__(self, limit: int | None) -> None:
        """Name ``limit`` in the warnings."""
        self.limit = limit
        self.count = 0
        """Requests refused since the last warning."""
        self._quiet_until = -math.inf
        self._lock = threading.Lock()

    def shed(self, reason: str) -> None:
        """Count one refused request; warn if the last warning is old enough."""
        with self._lock:
            self.count += 1
            now = clock.monotonic()
            if now < self._quiet_until:
                return
            count, self.count = self.count, 0
            self._quiet_until = now + LOG_INTERVAL
        logger.warning(
            "Shed %d request(s) past MAX_CONCURRENT_REQUESTS (%s); the last: %s",
            count,
            self.limit,
            reason,
        )


async def _answer(scope: Scope, receive: Receive, send: Send) -> None:
    """503 ``overloaded``, with ``Retry-After``."""
    response = error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The server is busy; try again shortly.",
        code=OVERLOADED,
    )
    response.headers["Retry-After"] = RETRY_AFTER
    await response(scope, receive, send)
