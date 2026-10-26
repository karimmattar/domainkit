"""Load shedding: a cap on requests at once, a short queue, and fast 503s."""

import asyncio
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import deadlines
from domainkit.clock import freeze_time
from domainkit.conf import Settings, override_settings
from domainkit.conf.global_settings import (
    CONCURRENCY_MIDDLEWARE,
    DEADLINE_MIDDLEWARE,
    DEFAULT_MIDDLEWARE,
)
from domainkit.http import get_asgi_application, shedding
from domainkit.http.deadlines import DeadlineMiddleware
from domainkit.http.shedding import (
    LOG_INTERVAL,
    QUEUE_FULL,
    QUEUE_TIMEOUT,
    ConcurrencyLimitMiddleware,
    ShedReport,
    Slots,
)
from domainkit.testing import AsyncTestClient
from domainkit_tests import deadline_routes
from domainkit_tests.deadline_routes import Counter

pytestmark = pytest.mark.anyio

LOGGER = "domainkit.http.shedding"
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Requests waiting for a slot, counted as the queue metric sees them."""
    count = Counter()
    monkeypatch.setattr(shedding, "observe_queued", count.add)
    return count


@pytest.fixture
def admitted(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Slots held, counted as the admission metric sees them."""
    count = Counter()
    monkeypatch.setattr(shedding, "observe_admitted", count.add)
    return count


# Slots --------------------------------------------------------------------------


async def test_slots_admit_up_to_the_limit_then_queue_in_order(
    queued: Counter,
) -> None:
    """Holders first; waiters get freed slots first come, first served."""
    slots = Slots(limit=2, queue=5)
    assert await slots.acquire(1.0) is None
    assert await slots.acquire(1.0) is None
    assert slots.held == 2

    order: list[str] = []

    async def wait(name: str) -> None:
        assert await slots.acquire(60.0) is None
        order.append(name)

    first = asyncio.create_task(wait("first"))
    await queued.reach(1)
    second = asyncio.create_task(wait("second"))
    await queued.reach(2)
    assert slots.waiting == 2
    slots.release()
    await first
    assert order == ["first"]
    slots.release()
    await second
    assert order == ["first", "second"]
    assert (slots.held, slots.waiting) == (2, 0)
    slots.release()
    slots.release()
    assert slots.held == 0


async def test_slots_refuse_when_the_queue_is_full_or_the_wait_ends(
    queued: Counter,
) -> None:
    """No place in the queue is an answer at once; a wait that ends, later."""
    slots = Slots(limit=1, queue=1)
    assert await slots.acquire(1.0) is None
    assert await slots.acquire(0) == QUEUE_FULL
    waiting = asyncio.create_task(slots.acquire(60.0))
    await queued.reach(1)
    assert await slots.acquire(60.0) == QUEUE_FULL
    assert await Slots(limit=1, queue=0).acquire(60.0) is None
    slots.release()
    assert await waiting is None

    timed = Slots(limit=1, queue=1)
    await timed.acquire(1.0)
    assert await timed.acquire(0.01) == QUEUE_TIMEOUT
    assert (timed.held, timed.waiting) == (1, 0)
    assert not timed._waiters  # Nothing left behind to skip later.
    assert queued.value == 0


async def test_a_waiter_cancelled_takes_no_slot_and_passes_one_on(
    queued: Counter,
) -> None:
    """Cancelled while waiting, or just after being handed a slot."""
    slots = Slots(limit=1, queue=5)
    await slots.acquire(1.0)
    gone = asyncio.create_task(slots.acquire(60.0))
    await queued.reach(1)
    gone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gone
    assert (slots.held, slots.waiting, queued.value) == (1, 0, 0)

    handed = asyncio.create_task(slots.acquire(60.0))
    await queued.reach(1)
    after = asyncio.create_task(slots.acquire(60.0))
    await queued.reach(2)
    slots.release()  # Hands the slot to ``handed``...
    handed.cancel()  # ...which is cancelled before it runs again.
    with pytest.raises(asyncio.CancelledError):
        await handed
    assert await after is None  # It passed the slot on.
    assert (slots.held, slots.waiting) == (1, 0)


async def test_a_freed_slot_is_not_left_to_a_waiter_that_gave_up(
    queued: Counter,
) -> None:
    """Waiters that stopped waiting are skipped, and a free slot is taken."""
    slots = Slots(limit=1, queue=5)
    await slots.acquire(1.0)
    gone = asyncio.create_task(slots.acquire(60.0))
    await queued.reach(1)
    gone.cancel()  # Cancelled, but not yet off the queue.
    slots.release()
    assert slots.held == 0
    assert await slots.acquire(0) is None  # Not queued behind ``gone``.
    with pytest.raises(asyncio.CancelledError):
        await gone


class Unwalked(deque[Any]):
    """A queue that fails the test if it is walked or searched."""

    def __iter__(self) -> Iterator[Any]:
        """Refuse: walking it costs as much as it is long."""
        raise AssertionError("walked the queue")

    def remove(self, value: Any) -> None:  # noqa: ANN401 - any waiter.
        """Refuse: searching it costs as much as it is long."""
        raise AssertionError("searched the queue")


async def test_queue_operations_cost_the_same_however_long_it_is(
    queued: Counter,
) -> None:
    """Refusing, counting, and leaving never walk the queue (MAX_QUEUED_REQUESTS).

    Refused requests, the ones that should cost least under overload, cost
    the same with 10 waiters or 100,000.
    """
    slots = Slots(limit=1, queue=100)
    await slots.acquire(1.0)
    slots._waiters = Unwalked()
    waiting = [asyncio.create_task(slots.acquire(60.0)) for _ in range(100)]
    async with asyncio.timeout(30):  # A regression fails here; it never hangs.
        await queued.reach(100)

    assert await slots.acquire(60.0) == QUEUE_FULL
    assert slots.waiting == 100
    waiting[50].cancel()  # In the middle: passed over later, not searched for.
    with pytest.raises(asyncio.CancelledError):
        await waiting[50]
    assert slots.waiting == 99
    assert len(slots._waiters) == 100
    waiting[0].cancel()  # At the front: taken off at once.
    with pytest.raises(asyncio.CancelledError):
        await waiting[0]
    assert (slots.waiting, len(slots._waiters)) == (98, 99)
    for _ in range(98):
        slots.release()
    assert await asyncio.gather(*waiting[1:50], *waiting[51:]) == [None] * 98
    assert (slots.held, slots.waiting, len(slots._waiters)) == (1, 0, 0)


async def test_waiters_that_left_the_middle_are_swept_together(
    queued: Counter,
) -> None:
    """Places given up in the middle go once they outnumber those still waiting.

    The queue's length stays within twice the waiters (and a little), and
    the waiters left keep their turn.
    """
    slots = Slots(limit=1, queue=100)
    await slots.acquire(1.0)
    order: list[int] = []

    async def wait(n: int) -> None:
        assert await slots.acquire(60.0) is None
        order.append(n)
        slots.release()

    waiting = [asyncio.create_task(wait(n)) for n in range(100)]
    async with asyncio.timeout(30):
        await queued.reach(100)
    for task in waiting[10:90]:  # One after another, each off the queue first.
        task.cancel()
        await asyncio.wait([task])

    assert slots.waiting == 20
    # Swept once 41 waited, as the 59 that left outnumbered them (2 x 41 + 16):
    # the 21 that left since stay until passed over.
    assert len(slots._waiters) == 41
    slots.release()
    await asyncio.gather(*waiting[:10], *waiting[90:])
    assert order == [*range(10), *range(90, 100)]
    assert (slots.held, slots.waiting, len(slots._waiters)) == (0, 0, 0)


async def test_the_wait_ends_at_a_deadline_no_timer_enforces(
    queued: Counter,
) -> None:
    """A task that only shares a deadline stops waiting when it passes.

    It is refused as a wait that ran out is (``queue_timeout``); with no time
    left at all, it is refused at once.
    """
    slots = Slots(limit=1, queue=5)
    await slots.acquire(1.0)

    async def wait_under(seconds: float) -> str | None:
        token = deadlines._current.set(deadlines.Deadline(seconds))
        try:
            return await slots.acquire(60.0)
        finally:
            deadlines._current.reset(token)

    async with asyncio.timeout(30):  # Long before the queue's own 60 seconds.
        assert await asyncio.create_task(wait_under(0.0)) == QUEUE_TIMEOUT
        assert await asyncio.create_task(wait_under(0.01)) == QUEUE_TIMEOUT
    assert (slots.held, slots.waiting, len(slots._waiters), queued.value) == (
        1,
        0,
        0,
        0,
    )


# The middleware -----------------------------------------------------------------


class Gated:
    """An ASGI app whose requests each wait for the test to let them finish."""

    def __init__(self, *, stream: bool = False) -> None:
        """Answer at once and then hold the body, if ``stream``."""
        self.stream = stream
        self.admitted = Counter()
        self.gates: list[asyncio.Event] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the admission, read the request, wait for the gate, answer."""
        gate = asyncio.Event()
        self.gates.append(gate)
        self.admitted.add()
        assert (await receive())["type"] == "http.request"
        if self.stream:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        await gate.wait()
        if not self.stream:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})


class Call:
    """One request to an ASGI app, run as a task, with what it answered."""

    def __init__(self, app: ASGIApp, path: str = "/work", root_path: str = "") -> None:
        """Start the request."""
        self.sent: list[Message] = []
        self.started = asyncio.Event()
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": root_path,
            "headers": [],
        }

        async def run() -> None:
            await app(scope, self.receive, self.send)

        self.task = asyncio.create_task(run())

    @staticmethod
    async def receive() -> Message:
        """No body."""
        return {"type": "http.request", "body": b""}

    async def send(self, message: Message) -> None:
        """Keep what the app sent."""
        self.sent.append(message)
        if message["type"] == "http.response.start":
            self.started.set()

    async def answered(self) -> int:
        """Wait for the request to end; return its status."""
        await self.task
        return int(self.sent[0]["status"])

    def header(self, name: bytes) -> bytes | None:
        """A header of the answer."""
        return dict(self.sent[0]["headers"]).get(name)


@asynccontextmanager
async def capped(
    app: ASGIApp,
    **changes: Any,  # noqa: ANN401 - settings.
) -> AsyncIterator[ConcurrencyLimitMiddleware]:
    """``app`` behind the cap: one request at once and no queue, unless changed."""
    defaults = {"MAX_CONCURRENT_REQUESTS": 1, "MAX_QUEUED_REQUESTS": 0}
    with override_settings(**{**defaults, **changes}):
        yield ConcurrencyLimitMiddleware(app)


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The middleware's warnings."""
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


async def test_requests_past_the_cap_are_refused_at_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """503 overloaded with Retry-After, and a warning; then the slot frees."""
    caplog.set_level(logging.WARNING, LOGGER)
    app = Gated()
    async with capped(app) as middleware:
        first = Call(middleware)
        await app.admitted.reach(1)
        refused = Call(middleware)
        assert await refused.answered() == 503
        assert refused.header(b"retry-after") == b"1"
        assert b'"code":"overloaded"' in refused.sent[1]["body"]
        assert b"The server is busy" in refused.sent[1]["body"]
        app.gates[0].set()
        assert await first.answered() == 200
        later = Call(middleware)
        await app.admitted.reach(2)
        app.gates[1].set()
        assert await later.answered() == 200
        assert middleware.slots(1).held == 0

    assert messages(caplog) == [
        "Shed 1 request(s) past MAX_CONCURRENT_REQUESTS (1); the last: queue_full"
    ]


async def test_a_queued_request_runs_when_a_slot_frees_in_time(
    queued: Counter,
) -> None:
    """Waiters are let in, in order, or refused when their wait runs out."""
    app = Gated()
    async with capped(
        app, MAX_QUEUED_REQUESTS=2, REQUEST_QUEUE_TIMEOUT=60.0
    ) as middleware:
        first = Call(middleware)
        await app.admitted.reach(1)
        second = Call(middleware)
        await queued.reach(1)
        third = Call(middleware)
        await queued.reach(2)
        full = Call(middleware)
        assert await full.answered() == 503
        app.gates[0].set()
        await app.admitted.reach(2)
        assert queued.value == 1
        app.gates[1].set()
        await app.admitted.reach(3)
        app.gates[2].set()
        assert [await call.answered() for call in (first, second, third)] == [200] * 3

    async with capped(app, MAX_QUEUED_REQUESTS=1, REQUEST_QUEUE_TIMEOUT=0.01) as late:
        holder = Call(late)
        await app.admitted.reach(4)
        assert await Call(late).answered() == 503
        app.gates[3].set()
        assert await holder.answered() == 200


async def test_cancelled_requests_give_their_slot_or_place_back(
    queued: Counter,
) -> None:
    """A server cancels a request whose client left: in a slot, or waiting."""
    app = Gated()
    async with capped(
        app, MAX_QUEUED_REQUESTS=1, REQUEST_QUEUE_TIMEOUT=60.0
    ) as middleware:
        holder = Call(middleware)
        await app.admitted.reach(1)
        waiter = Call(middleware)
        await queued.reach(1)
        waiter.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter.task
        assert queued.value == 0
        holder.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder.task
        assert middleware.slots(1).held == 0
        nxt = Call(middleware)
        await app.admitted.reach(2)
        app.gates[1].set()
        assert await nxt.answered() == 200


async def test_the_deadline_frees_the_slot_of_a_request_it_cuts_off(
    queued: Counter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running or waiting, a request past its deadline gives everything back.

    The one cut off while it waited never got a slot: it is counted as shed.
    """
    shed: list[str] = []
    monkeypatch.setattr(shedding, "observe_shed", shed.append)
    app = Gated()
    given: list[deadlines.Deadline | None] = []
    with override_settings(
        REQUEST_TIMEOUT=60.0,
        MAX_CONCURRENT_REQUESTS=1,
        MAX_QUEUED_REQUESTS=1,
        REQUEST_QUEUE_TIMEOUT=60.0,
    ):
        middleware = ConcurrencyLimitMiddleware(app)

        async def keeping(scope: Scope, receive: Receive, send: Send) -> None:
            given.append(deadlines.current_deadline())
            await middleware(scope, receive, send)

        bounded = DeadlineMiddleware(keeping)
    holder = Call(bounded)
    await app.admitted.reach(1)
    waiter = Call(bounded)
    await queued.reach(1)
    for deadline in given:
        assert deadline is not None
        deadline.reset(0.0)  # Both pass now: one request runs, one waits.
    for call in (holder, waiter):
        assert await call.answered() == 503
        assert b'"code":"deadline_exceeded"' in call.sent[1]["body"]
    assert (middleware.slots(1).held, middleware.slots(1).waiting) == (0, 0)
    assert shed == [QUEUE_TIMEOUT]
    assert middleware.report.count == 0  # Logged at once: the first in a while.


async def test_a_streamed_response_gives_its_slot_back_when_it_starts() -> None:
    """Long-lived responses (exports, events) do not hold a slot."""
    app = Gated(stream=True)
    async with capped(app) as middleware:
        streaming = Call(middleware)
        await streaming.started.wait()
        assert middleware.slots(1).held == 0
        other = Call(middleware)
        await app.admitted.reach(2)
        app.gates[1].set()
        assert await other.answered() == 200
        app.gates[0].set()
        assert await streaming.answered() == 200


class Live:
    """Long-lived connections, as ``domainkit.http.live`` serves them.

    ``/events`` starts a response marked long-lived, as an event stream
    does, and a WebSocket connection opens; both end when the test sets
    ``gate``. Any other request waits until its deadline cuts it off.
    """

    def __init__(self) -> None:
        """Nothing open yet."""
        self.gate = asyncio.Event()
        self.left: list[float | None] = []
        self.opened = Counter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream, keep a socket open, or hang."""
        if scope["type"] == "websocket":
            self.left.append(deadlines.remaining())
            self.opened.add()
            await self.gate.wait()
            return
        if scope["path"] != "/events":
            await self.gate.wait()  # Never set before the deadline passes.
            return  # pragma: no cover - cut off first.
        scope["domainkit.long_lived"] = True
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        self.left.append(deadlines.remaining())
        await self.gate.wait()
        await send({"type": "http.response.body", "body": b"data: done\n\n"})


async def test_long_lived_connections_hold_no_slot_and_have_no_deadline() -> None:
    """An event stream and a WebSocket outlive REQUEST_TIMEOUT; others get in."""
    live = Live()
    with override_settings(
        REQUEST_TIMEOUT=0.05, MAX_CONCURRENT_REQUESTS=1, MAX_QUEUED_REQUESTS=0
    ):
        middleware = ConcurrencyLimitMiddleware(live)
        app = DeadlineMiddleware(middleware)
    events = Call(app, "/events")
    await events.started.wait()
    socket = asyncio.create_task(
        app({"type": "websocket", "path": "/socket"}, Call.receive, Call.send)  # type: ignore[arg-type]
    )
    await live.opened.reach(1)
    assert live.left == [None, None]
    assert middleware.slots(1).held == 0
    other = Call(app, "/work")  # Admitted, then cut off by its own deadline...
    assert await other.answered() == 503
    assert b'"code":"deadline_exceeded"' in other.sent[1]["body"]
    live.gate.set()  # ...while the stream and the socket, there first, go on.
    assert await events.answered() == 200
    assert events.sent[-1]["body"] == b"data: done\n\n"
    await socket


async def test_probes_websockets_and_no_cap_pass_through() -> None:
    """Exempt paths, other scope types, and MAX_CONCURRENT_REQUESTS None."""
    app = Gated()
    async with capped(app) as middleware:
        holder = Call(middleware)
        await app.admitted.reach(1)
        probe = Call(middleware, "/ready")
        await app.admitted.reach(2)
        app.gates[1].set()
        assert await probe.answered() == 200
        app.gates[0].set()
        await holder.answered()

    seen: list[str] = []

    async def record(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()  # What the server passed.
        seen.append(scope["type"])

    async with capped(record) as middleware:
        await middleware({"type": "websocket"}, Call.receive, Call.send)  # type: ignore[arg-type]
        await middleware({"type": "lifespan"}, Call.receive, Call.send)  # type: ignore[arg-type]
    async with capped(record, MAX_CONCURRENT_REQUESTS=None) as uncapped:
        await uncapped({"type": "http", "path": "/x"}, Call.receive, Call.send)  # type: ignore[arg-type]
    assert seen == ["websocket", "lifespan", "http"]


def test_shedding_is_logged_at_most_every_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first refusal, then a count once per LOG_INTERVAL."""
    caplog.set_level(logging.WARNING, LOGGER)
    report = ShedReport(100)
    with freeze_time("2026-01-01") as clock:
        report.shed(QUEUE_FULL)
        report.shed(QUEUE_FULL)
        clock.tick(LOG_INTERVAL - 1)
        report.shed(QUEUE_TIMEOUT)
        assert report.count == 2
        clock.tick(1)
        report.shed(QUEUE_TIMEOUT)

    assert messages(caplog) == [
        "Shed 1 request(s) past MAX_CONCURRENT_REQUESTS (100); the last: queue_full",
        "Shed 3 request(s) past MAX_CONCURRENT_REQUESTS (100); the last: queue_timeout",
    ]
    assert report.count == 0


async def test_each_event_loop_has_slots_of_its_own() -> None:
    """Slots hold futures of one loop; another loop gets others."""
    async with capped(Gated()) as middleware:
        here = middleware.slots(1)
        assert middleware.slots(1) is here
        other: list[Slots] = []

        async def take() -> None:
            other.append(middleware.slots(1))

        thread = threading.Thread(target=asyncio.run, args=(take(),))
        thread.start()
        thread.join()
    assert other[0] is not here


async def test_metrics_count_admitted_queued_and_shed() -> None:
    """Gauges go up and back down; refusals are counted by reason."""

    def sample(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    full = sample("http_requests_shed_total", reason=QUEUE_FULL)
    timed_out = sample("http_requests_shed_total", reason=QUEUE_TIMEOUT)
    admitted = sample("http_requests_admitted")
    app = Gated()
    async with capped(
        app, MAX_QUEUED_REQUESTS=1, REQUEST_QUEUE_TIMEOUT=0.01
    ) as middleware:
        holder = Call(middleware)
        await app.admitted.reach(1)
        assert sample("http_requests_admitted") == admitted + 1
        assert await Call(middleware).answered() == 503
        app.gates[0].set()
        await holder.answered()
    async with capped(app) as middleware:
        holder = Call(middleware)
        await app.admitted.reach(2)
        assert await Call(middleware).answered() == 503
        app.gates[1].set()
        await holder.answered()

    assert sample("http_requests_admitted") == admitted
    assert sample("http_requests_queued") == 0
    assert sample("http_requests_shed_total", reason=QUEUE_TIMEOUT) == timed_out + 1
    assert sample("http_requests_shed_total", reason=QUEUE_FULL) == full + 1


def test_deadlines_and_shedding_come_first_by_default() -> None:
    """Right after the request context: before hosts, bodies, and auth."""
    context = DEFAULT_MIDDLEWARE.index(
        "domainkit.http.middleware.RequestContextMiddleware"
    )
    assert DEFAULT_MIDDLEWARE[context + 1 : context + 3] == (
        DEADLINE_MIDDLEWARE,
        CONCURRENCY_MIDDLEWARE,
    )


# Requests waiting for their body --------------------------------------------------


class Body:
    """A client that sends its body chunk by chunk, when the test says."""

    def __init__(self) -> None:
        """Nothing sent yet."""
        self.chunks: asyncio.Queue[Message] = asyncio.Queue()
        self.waits = Counter()

    async def receive(self) -> Message:
        """The next chunk, once sent."""
        self.waits.add()
        return await self.chunks.get()

    def send(self, body: bytes, *, more: bool = False) -> None:
        """Send a chunk; the last unless ``more``."""
        self.chunks.put_nowait(
            {"type": "http.request", "body": body, "more_body": more}
        )


class Reader:
    """An app that reads the whole body, then answers 200 with what it read."""

    def __init__(self) -> None:
        """Nothing read yet."""
        self.admitted = Counter()
        self.read = Counter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read every chunk, then answer."""
        self.admitted.add()
        body = b""
        while True:
            message = await receive()
            body += message["body"]
            self.read.add()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})


def posting(app: ASGIApp, body: Body) -> asyncio.Task[list[Message]]:
    """Send a POST whose body comes from ``body``; the task returns the answer."""
    sent: list[Message] = []

    async def keep(message: Message) -> None:
        sent.append(message)

    async def run() -> list[Message]:
        scope = {"type": "http", "method": "POST", "path": "/items", "headers": []}
        await app(scope, body.receive, keep)
        return sent

    return asyncio.create_task(run())


async def test_a_request_waiting_for_its_body_holds_no_slot(
    admitted: Counter,
) -> None:
    """Clients that send headers, then their body slowly or never, hold no slot.

    One slot, no queue: while a request waits for its body, another is
    served, where it was refused 503 before.
    """
    reader, gated = Reader(), Gated()

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        await (reader if scope["method"] == "POST" else gated)(scope, receive, send)

    async with capped(route) as middleware:
        body = Body()
        slow = posting(middleware, body)
        await body.waits.reach(1)
        assert (middleware.slots(1).held, admitted.value) == (0, 0)  # Lent.
        other = Call(middleware)
        await gated.admitted.reach(1)
        gated.gates[0].set()
        assert await other.answered() == 200
        body.send(b"all")
        answer = await slow

    assert answer[0]["status"] == 200
    assert (middleware.slots(1).held, admitted.value) == (0, 0)


async def test_a_chunk_takes_a_slot_back_in_turn(
    admitted: Counter, queued: Counter
) -> None:
    """The slot is lent while the request waits, and taken back through the queue."""
    reader, gated = Reader(), Gated()

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        await (reader if scope["method"] == "POST" else gated)(scope, receive, send)

    async with capped(
        route, MAX_QUEUED_REQUESTS=1, REQUEST_QUEUE_TIMEOUT=60.0
    ) as middleware:
        body = Body()
        slow = posting(middleware, body)
        await reader.admitted.reach(1)
        await admitted.reach(0)  # Lent while it waits for its first chunk.
        other = Call(middleware)
        await gated.admitted.reach(1)
        body.send(b"one;", more=True)  # Its chunk waits for the slot, in turn.
        waits = asyncio.ensure_future(queued.reach(1))
        reads = asyncio.ensure_future(reader.read.reach(1))
        done, _ = await asyncio.wait(
            {waits, reads}, return_when=asyncio.FIRST_COMPLETED
        )
        reads.cancel()
        assert done == {waits}  # Queued: not read while another holds the slot.
        gated.gates[0].set()
        assert await other.answered() == 200
        await reader.read.reach(1)
        await admitted.reach(0)  # Lent again for the second chunk.
        body.send(b"two")
        answer = await slow

    assert answer[0]["status"] == 200
    assert answer[1]["body"] == b"one;two"
    assert (middleware.slots(1).held, admitted.value, queued.value) == (0, 0, 0)


async def test_a_chunk_that_finds_no_slot_is_refused_as_overloaded(
    admitted: Counter, caplog: pytest.LogCaptureFixture
) -> None:
    """As a new request would be: 503 overloaded, counted, at most one warning."""
    caplog.set_level(logging.WARNING, LOGGER)
    reader, gated = Reader(), Gated()

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        await (reader if scope["method"] == "POST" else gated)(scope, receive, send)

    async with capped(route) as middleware:
        body = Body()
        slow = posting(middleware, body)
        await reader.admitted.reach(1)
        await admitted.reach(0)
        holder = Call(middleware)
        await gated.admitted.reach(1)
        body.send(b"late")
        answer = await slow
        gated.gates[0].set()
        assert await holder.answered() == 200

    assert answer[0]["status"] == 503
    assert dict(answer[0]["headers"])[b"retry-after"] == b"1"
    assert b'"code":"overloaded"' in answer[1]["body"]
    assert reader.read.value == 0
    assert messages(caplog) == [
        "Shed 1 request(s) past MAX_CONCURRENT_REQUESTS (1); the last: queue_full"
    ]
    assert (middleware.slots(1).held, admitted.value) == (0, 0)


async def test_a_body_already_in_lends_nothing(admitted: Counter) -> None:
    """A chunk at hand is no wait; nor is waiting after the body, for a disconnect."""
    answer = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["body"] == b"all"
        listening = asyncio.ensure_future(receive())  # Waits for the disconnect.
        await answer.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        assert (await listening)["type"] == "http.disconnect"

    async with capped(app) as middleware:
        body = Body()
        body.send(b"all")
        task = posting(middleware, body)
        await body.waits.reach(2)  # The second receive() waits.
        assert (middleware.slots(1).held, admitted.value) == (1, 1)
        answer.set()
        body.chunks.put_nowait({"type": "http.disconnect"})
        assert (await task)[0]["status"] == 200

    assert (middleware.slots(1).held, admitted.value) == (0, 0)


async def test_a_check_for_a_disconnect_lends_nothing(admitted: Counter) -> None:
    """A receive() cancelled at once (Starlette's is_disconnected) is no wait."""
    checked = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        with anyio.CancelScope() as scope_:
            scope_.cancel()
            await receive()
        assert scope_.cancelled_caught
        checked.set()
        message = await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": message["body"]})

    async with capped(app) as middleware:
        body = Body()
        task = posting(middleware, body)
        await checked.wait()
        assert (middleware.slots(1).held, admitted.value) == (1, 1)
        await admitted.reach(0)  # Waiting for the body for real: lent.
        body.send(b"in")
        answer = await task

    assert answer[1]["body"] == b"in"
    assert (middleware.slots(1).held, admitted.value) == (0, 0)


async def test_a_response_started_while_a_chunk_waits_needs_no_slot(
    admitted: Counter, queued: Counter
) -> None:
    """Given one, or refused one, after the response started: nothing is held."""

    class Refusing(Slots):
        """Refuses a waiter when the test says, as if its wait ran out."""

        def __init__(self) -> None:
            super().__init__(limit=1, queue=1)
            self.refuse = asyncio.Event()

        async def acquire(self, wait: float) -> str | None:
            if self.held < self.limit:
                return await super().acquire(wait)
            queued.add(1)
            await self.refuse.wait()
            queued.add(-1)
            return QUEUE_TIMEOUT

    started, entered = asyncio.Event(), asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        reading = asyncio.ensure_future(receive())  # Waits for the body.
        await queued.reach(1)  # Its chunk came, and waits for a slot.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        assert (await reading)["body"] == b"late"
        await send({"type": "http.response.body", "body": b"done"})

    for refuse in (False, True):
        started.clear()
        entered.clear()
        slots = Refusing() if refuse else Slots(limit=1, queue=1)
        async with capped(app, MAX_QUEUED_REQUESTS=1) as middleware:
            assert middleware._slots.get(lambda: slots) is slots  # noqa: B023 - called at once.
            body = Body()
            task = posting(middleware, body)
            await entered.wait()
            await admitted.reach(0)
            assert await slots.acquire(0) is None  # Another request takes it.
            body.send(b"late")
            await started.wait()
            if isinstance(slots, Refusing):
                slots.refuse.set()
            else:
                slots.release()  # Handed to the chunk, after the response began.
            answer = await task
        slots.release() if refuse else None
        assert [message.get("status") for message in answer] == [200, None]
        assert answer[1]["body"] == b"done"
        assert (slots.held, admitted.value) == (0, 0)


async def test_a_chunk_after_the_response_started_takes_no_slot_back(
    admitted: Counter, queued: Counter
) -> None:
    """The response started while the body waited: the chunk waits for no slot."""
    started = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reading = asyncio.ensure_future(receive())  # Waits for the body.
        await admitted.reach(0)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        assert (await reading)["body"] == b"late"
        await send({"type": "http.response.body", "body": b"done"})

    async with capped(
        app, MAX_QUEUED_REQUESTS=1, REQUEST_QUEUE_TIMEOUT=60.0
    ) as middleware:
        slots = middleware.slots(1)
        body = Body()
        task = posting(middleware, body)
        await started.wait()
        assert await slots.acquire(0) is None  # Another request takes the slot.
        body.send(b"late")
        waiting = asyncio.ensure_future(queued.reach(1))
        done, _ = await asyncio.wait(
            {task, waiting}, return_when=asyncio.FIRST_COMPLETED
        )
        waiting.cancel()
        assert done == {task}  # Answered, never queued behind the other request.
        answer = await task
        slots.release()

    assert answer[1]["body"] == b"done"
    assert (slots.held, admitted.value, queued.value) == (0, 0, 0)


async def test_an_overload_raised_after_the_response_started_goes_on() -> None:
    """It cannot become a 503 any more."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise shedding.Overloaded

    async with capped(app) as middleware:
        call = Call(middleware)
        with pytest.raises(shedding.Overloaded):
            await call.task
        assert [m["type"] for m in call.sent] == ["http.response.start"]
        assert middleware.slots(1).held == 0


async def test_a_deadline_passing_while_the_body_waits_frees_everything(
    admitted: Counter,
) -> None:
    """Cut off while its slot is lent: 503, and nothing is held or owed."""
    given: list[deadlines.Deadline | None] = []
    reader = Reader()
    with override_settings(
        REQUEST_TIMEOUT=60.0, MAX_CONCURRENT_REQUESTS=1, MAX_QUEUED_REQUESTS=0
    ):
        middleware = ConcurrencyLimitMiddleware(reader)

        async def keeping(scope: Scope, receive: Receive, send: Send) -> None:
            given.append(deadlines.current_deadline())
            await middleware(scope, receive, send)

        bounded = DeadlineMiddleware(keeping)
    body = Body()
    task = posting(bounded, body)
    await reader.admitted.reach(1)
    await admitted.reach(0)
    [deadline] = given
    assert deadline is not None
    deadline.reset(0.0)
    answer = await task

    assert answer[0]["status"] == 503
    assert b'"code":"deadline_exceeded"' in answer[1]["body"]
    assert (middleware.slots(1).held, admitted.value) == (0, 0)
    assert await middleware.slots(1).acquire(0) is None


@pytest.mark.usefixtures("busy_routes")
async def test_the_application_serves_others_while_a_body_is_slow(
    admitted: Counter,
) -> None:
    """Through the default middleware, a body that waits holds no slot.

    Its chunk then finds the slot taken and no room in the queue: FastAPI
    answers the Overloaded raised while it read the body.
    """
    sent = asyncio.Event()

    async def slowly() -> AsyncIterator[bytes]:
        yield b'{"name":'
        await sent.wait()
        yield b'"widget"}'

    with override_settings(
        ROOT_ROUTER="domainkit_tests.deadline_routes.router",
        MAX_CONCURRENT_REQUESTS=1,
        MAX_QUEUED_REQUESTS=0,
    ):
        async with AsyncTestClient(get_asgi_application(), lifespan=False) as client:
            slow = asyncio.create_task(
                client.post("/echo", content=slowly(), headers=JSON)
            )
            await admitted.reach(1)
            await admitted.reach(0)
            assert (await client.get("/remaining")).status_code == 200
            holder = asyncio.create_task(client.get("/busy", params={"gate": "a"}))
            await deadline_routes.arrivals.reach(1)
            sent.set()
            refused = await slow
            deadline_routes.gates["a"].set()
            assert (await holder).status_code == 200

            sent.clear()
            fine = asyncio.create_task(
                client.post("/echo", content=slowly(), headers=JSON)
            )
            await admitted.reach(1)
            await admitted.reach(0)
            sent.set()
            assert (await fine).json() == {"name": "widget"}

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "1"
    assert refused.json() == {
        "detail": "The server is busy; try again shortly.",
        "code": "overloaded",
        "request_id": refused.headers["X-Request-ID"],
    }


async def test_exempt_paths_stay_exempt_behind_a_root_path() -> None:
    """Behind --root-path, a path starts with it: it is compared without it."""
    app = Gated()
    async with capped(app) as middleware:
        holder = Call(middleware)
        await app.admitted.reach(1)
        for admitted, (path, root) in enumerate(
            (("/api/ready", "/api"), ("/api/ready", "/api/"), ("/ready", "/api")),
            start=2,
        ):
            probe = Call(middleware, path, root_path=root)
            entered = asyncio.ensure_future(app.admitted.reach(admitted))
            done, _ = await asyncio.wait(
                {probe.task, entered}, return_when=asyncio.FIRST_COMPLETED
            )
            entered.cancel()
            assert done == {entered}  # Let in, not refused.
            app.gates[-1].set()
            assert await probe.answered() == 200
        for path, root in (("/api/work", "/api"), ("/api/ready", "")):
            assert await Call(middleware, path, root_path=root).answered() == 503
        app.gates[0].set()
        assert await holder.answered() == 200


@pytest.mark.parametrize(
    ("name", "highest"),
    [
        ("MAX_CONCURRENT_REQUESTS", 100_000),
        ("MAX_QUEUED_REQUESTS", 100_000),
        ("REQUEST_TIMEOUT", 3600.0),
        ("REQUEST_BODY_TIMEOUT", 3600.0),
    ],
)
def test_request_limits_have_upper_bounds(name: str, highest: float) -> None:
    """Generous, but bounded; the defaults stay well inside."""
    assert getattr(Settings.model_validate({name: highest}), name) == highest
    with pytest.raises(ValidationError, match=name):
        Settings.model_validate({name: highest + 1})


@pytest.fixture
def busy_routes() -> Iterator[None]:
    """The routes' gates and arrivals, fresh."""
    deadline_routes.gates.clear()
    deadline_routes.arrivals.value = 0
    yield
    for gate in deadline_routes.gates.values():
        gate.set()
    deadline_routes.gates.clear()


@pytest.mark.usefixtures("busy_routes")
async def test_the_application_sheds_with_its_error_body() -> None:
    """Through the default middleware: the 503 body names the request."""
    with override_settings(
        ROOT_ROUTER="domainkit_tests.deadline_routes.router",
        MAX_CONCURRENT_REQUESTS=1,
        MAX_QUEUED_REQUESTS=0,
    ):
        async with AsyncTestClient(get_asgi_application(), lifespan=False) as client:
            holder = asyncio.create_task(client.get("/busy", params={"gate": "a"}))
            await deadline_routes.arrivals.reach(1)
            refused = await client.get("/busy", params={"gate": "b"})
            ready = await client.get("/ready")
            deadline_routes.gates["a"].set()
            held = await holder

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "1"
    assert refused.json() == {
        "detail": "The server is busy; try again shortly.",
        "code": "overloaded",
        "request_id": refused.headers["X-Request-ID"],
    }
    assert ready.status_code == 200
    assert held.json() == {"gate": "a"}
