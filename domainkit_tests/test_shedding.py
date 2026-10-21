"""Load shedding: a cap on requests at once, a short queue, and fast 503s."""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from prometheus_client import REGISTRY
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import deadlines
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
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


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Requests waiting for a slot, counted as the queue metric sees them."""
    count = Counter()
    monkeypatch.setattr(shedding, "observe_queued", count.add)
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


# The middleware -----------------------------------------------------------------


class Gated:
    """An ASGI app whose requests each wait for the test to let them finish."""

    def __init__(self, *, stream: bool = False) -> None:
        """Answer at once and then hold the body, if ``stream``."""
        self.stream = stream
        self.admitted = Counter()
        self.gates: list[asyncio.Event] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the admission, wait for the gate, answer."""
        gate = asyncio.Event()
        self.gates.append(gate)
        self.admitted.add()
        if self.stream:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        await gate.wait()
        if not self.stream:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})


class Call:
    """One request to an ASGI app, run as a task, with what it answered."""

    def __init__(self, app: ASGIApp, path: str = "/work") -> None:
        """Start the request."""
        self.sent: list[Message] = []
        self.started = asyncio.Event()
        scope = {"type": "http", "method": "GET", "path": path, "headers": []}

        async def run() -> None:
            await app(scope, self.receive, self.send)

        self.task = asyncio.create_task(run())

    async def receive(self) -> Message:
        """No body."""
        return {"type": "http.request", "body": b""}  # pragma: no cover

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
    queued: Counter,
) -> None:
    """Running or waiting, a request past its deadline gives everything back."""
    app = Gated()
    with override_settings(
        REQUEST_TIMEOUT=0.05,
        MAX_CONCURRENT_REQUESTS=1,
        MAX_QUEUED_REQUESTS=1,
        REQUEST_QUEUE_TIMEOUT=60.0,
    ):
        middleware = ConcurrencyLimitMiddleware(app)
        bounded = DeadlineMiddleware(middleware)
    holder = Call(bounded)
    await app.admitted.reach(1)
    waiter = Call(bounded)
    await queued.reach(1)
    assert await holder.answered() == 503
    assert await waiter.answered() == 503
    assert (middleware.slots(1).held, middleware.slots(1).waiting) == (0, 0)


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
