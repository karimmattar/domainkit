"""Server-Sent Events: encoding, the response, its bounds, and its end."""

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID

import anyio
import pytest
from starlette.datastructures import State
from starlette.requests import HTTPConnection
from starlette.types import Message, Scope

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.http import get_asgi_application, sse
from domainkit.http.live import GONE, LIFETIME, LONG_LIVED, REAUTHENTICATE, Slots
from domainkit.http.middleware import RequestBodyTooLarge
from domainkit.http.sse import (
    EventSourceResponse,
    ServerSentEvent,
    comment,
    last_event_id,
)
from domainkit.testing import TestClient

ROUTES: Final = "domainkit_tests.sample_live_routes.router"
STREAM_MIDDLEWARE: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]


# Encoding ----------------------------------------------------------------------------


def test_events_are_encoded_for_event_source() -> None:
    """Type, id, and retry first; each line of the data its own ``data:`` line."""
    event = ServerSentEvent("one\r\ntwo\rthree\nfour", event="tick", id="7", retry=500)

    assert event.encode() == (
        b"event: tick\nid: 7\nretry: 500\n"
        b"data: one\ndata: two\ndata: three\ndata: four\n\n"
    )
    assert ServerSentEvent().encode() == b"data: \n\n"
    assert ServerSentEvent("été", retry=0).encode() == (
        "retry: 0\ndata: été\n\n".encode()
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"event": "a\nb"}, "event has a line break"),
        ({"event": "a\rb"}, "event has a line break"),
        ({"id": "1\r\n"}, "id has a line break"),
        ({"id": "1\x00"}, "id has a NUL character"),
        ({"retry": -1}, "retry cannot be negative"),
    ],
)
def test_event_fields_cannot_break_the_stream(
    fields: dict[str, Any], message: str
) -> None:
    """A line break would start another field; a NUL makes the id ignored."""
    with pytest.raises(ValueError, match=rf"^A server-sent event's {message}\.$"):
        ServerSentEvent("data", **fields)


def test_json_events() -> None:
    """``json`` writes the value compactly, with UUIDs and dates as text."""
    value = {
        "id": UUID(int=1),
        "at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "name": "é",
    }

    event = ServerSentEvent.json(value, event="saved", id="x", retry=10)

    assert json.loads(event.data) == {
        "id": "00000000-0000-0000-0000-000000000001",
        "at": "2026-01-02T03:04:05Z",
        "name": "é",
    }
    assert " " not in event.data
    assert "é" in event.data
    assert (event.event, event.id, event.retry) == ("saved", "x", 10)


def test_comments() -> None:
    """Each line of a comment starts with a colon, which EventSource ignores."""
    assert comment("one\ntwo") == b": one\n: two\n\n"


@pytest.mark.parametrize(
    ("header", "found"),
    [
        (None, None),
        ("", None),
        ("41", "41"),
        ("x" * 256, "x" * 256),
        ("x" * 257, None),
        ("4\x011", None),
    ],
)
def test_the_last_event_id_is_read_back(header: str | None, found: str | None) -> None:
    """Absent, empty, too long, or with control characters: no id."""
    headers = [] if header is None else [(b"last-event-id", header.encode())]

    assert last_event_id(HTTPConnection({"type": "http", "headers": headers})) == found


def test_a_negative_retry_is_refused() -> None:
    """The browser cannot wait a negative time."""

    async def nothing() -> AsyncIterator[str]:
        yield "never"

    with pytest.raises(ValueError, match=r"^retry cannot be negative\.$"):
        EventSourceResponse(nothing(), retry=-5)


# Through the application -------------------------------------------------------------

type MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client() -> Iterator[MakeClient]:
    """Clients of the live sample routes, without auth or a database."""
    with ExitStack() as stack:

        def make(**overrides: Any) -> TestClient:  # noqa: ANN401 - settings.
            settings = {"ROOT_ROUTER": ROUTES, "MIDDLEWARE": STREAM_MIDDLEWARE}
            stack.enter_context(override_settings(**{**settings, **overrides}))
            return stack.enter_context(TestClient(get_asgi_application()))

        yield make


def test_an_event_stream_through_the_application(make_client: MakeClient) -> None:
    """The headers of an event stream, the retry, the events, and the end."""
    client = make_client()

    with client.stream("GET", "/live/count", params={"events": 2}) as response:
        body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-feed"] == "count"
    assert "content-length" not in response.headers
    assert body == (
        b"retry: 2000\n\n"
        b'event: number\nid: 1\ndata: {"n":1}\n\n'
        b'event: number\nid: 2\ndata: {"n":2}\n\n'
        b"data: done\n\n"
    )


def test_a_reconnecting_client_resumes_after_its_last_event(
    make_client: MakeClient,
) -> None:
    """The route reads ``Last-Event-ID`` and carries on from there."""
    client = make_client()

    response = client.get(
        "/live/count", params={"events": 1}, headers={"Last-Event-ID": "41"}
    )

    assert b'id: 42\ndata: {"n":42}' in response.content


def test_a_source_ends_the_stream_saying_why(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """``Closed`` from the source: a last comment with the reason, logged."""
    client = make_client()

    response = client.get("/live/count", params={"events": 0, "end": True})

    assert response.content.endswith(b"data: done\n\n: Client too slow.\n\n")
    assert "Ended an event stream: Client too slow." in caplog.messages


# Raw ---------------------------------------------------------------------------------


class Client:
    """The server's side of one raw event stream request."""

    def __init__(self) -> None:
        """A GET from an address of its own."""
        self.sent: list[Message] = []
        self.changed = anyio.Event()
        self.left = anyio.Event()
        """Set when the client disconnects."""
        self.requested = False
        self.stuck: Callable[[Message], bool] = lambda message: False
        self.gone: Callable[[Message], bool] = lambda message: False
        self.scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": "/raw",
            "headers": [],
            "client": ("203.0.113.5", 1234),
            "app": SimpleNamespace(state=State()),
        }

    async def receive(self) -> Message:
        """The request, then a disconnect once the client leaves."""
        if not self.requested:
            self.requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.left.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        """Take ``message``; stall or fail as the client is set up to."""
        if self.gone(message):
            raise OSError("client gone")
        self.sent.append(message)
        self.changed.set()
        if self.stuck(message):
            await anyio.sleep_forever()

    def body(self) -> bytes:
        """Everything sent in the body so far."""
        return b"".join(m["body"] for m in self.sent if m["type"].endswith(".body"))

    async def wait_for(self, part: bytes) -> None:
        """Wait until the body holds ``part``."""
        while part not in self.body():
            await self.changed.wait()
            self.changed = anyio.Event()

    async def serve(self, response: EventSourceResponse) -> None:
        """Answer with ``response``."""
        await response(self.scope, self.receive, self.send)


class Source:
    """An event source that waits between events, and says when it was closed."""

    def __init__(self, *events: str) -> None:
        """Yield ``events``, then wait for more until closed."""
        self.events = events
        self.closed = False

    async def __call__(self) -> AsyncIterator[str]:
        """The events, then nothing until the stream ends."""
        try:
            for event in self.events:
                yield event
            await anyio.sleep_forever()
        finally:
            self.closed = True


def ends_with(client: Client, body: bytes) -> bool:
    """Whether the last message ends the response with ``body``."""
    last = client.sent[-1]
    return last["body"] == body and last.get("more_body") is False


@pytest.mark.anyio
async def test_a_full_process_refuses_streams_with_503() -> None:
    """Past the caps: 503 with Retry-After, and the source is never read."""
    client = Client()
    slots = Slots(total=1, per_caller=1)
    slots.acquire("someone")
    client.scope["app"].state.live_slots = slots
    source = Source("never")

    await client.serve(EventSourceResponse(source()))

    start = client.sent[0]
    assert start["status"] == 503
    assert 5 <= int(dict(start["headers"])[b"retry-after"]) <= 15
    assert json.loads(client.body())["code"] == "too_many_connections"
    assert slots.open == 1


@pytest.mark.anyio
async def test_a_stream_ends_when_the_client_leaves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A disconnect stops the source at once; nothing more is sent."""
    caplog.set_level(logging.INFO, logger="domainkit.http.sse")
    client = Client()
    source = Source("first")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(client.serve, EventSourceResponse(source()))
        await client.wait_for(b"data: first\n\n")
        client.left.set()

    assert source.closed
    assert client.sent[-1] == {
        "type": "http.response.body",
        "body": b"data: first\n\n",
        "more_body": True,
    }
    assert client.scope[LONG_LIVED] is True
    assert client.scope["app"].state.live_slots.open == 0
    assert "Ended an event stream: Client gone." in caplog.messages


@pytest.mark.anyio
async def test_silence_sends_keepalive_comments() -> None:
    """After SSE_KEEPALIVE seconds without an event, a comment keeps proxies open."""
    client = Client()

    with freeze_time() as clock, override_settings(SSE_KEEPALIVE=15):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(client.serve, EventSourceResponse(Source("hi")()))
            await client.wait_for(b"data: hi\n\n")
            clock.tick(15)
            await client.wait_for(b": keep-alive\n\n")
            clock.tick(15)
            await client.wait_for(b": keep-alive\n\n: keep-alive\n\n")
            client.left.set()

    assert client.body() == b"data: hi\n\n: keep-alive\n\n: keep-alive\n\n"


@pytest.mark.anyio
async def test_a_stream_ends_at_its_lifetime() -> None:
    """SSE_MAX_LIFETIME ends it with a comment; the browser reconnects."""
    client = Client()
    source = Source()

    with freeze_time() as clock, override_settings(SSE_MAX_LIFETIME=600):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(client.serve, EventSourceResponse(source(), retry=100))
            await client.wait_for(b"retry: 100\n\n")
            clock.tick(600)

    assert source.closed
    assert ends_with(client, b": Connection lifetime reached.\n\n")


@pytest.mark.anyio
async def test_a_stream_whose_credentials_fail_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The re-check of the credentials ends it, saying only that."""
    client = Client()
    checked = anyio.Event()

    async def reauthenticate(scope: Scope) -> bool:
        checked.set()
        return False

    client.scope[REAUTHENTICATE] = reauthenticate
    with freeze_time() as clock:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(client.serve, EventSourceResponse(Source("a")()))
            await client.wait_for(b"data: a\n\n")
            clock.tick(60)

    assert checked.is_set()
    assert ends_with(client, b": Not authorized.\n\n")
    assert "Ended an event stream: Not authorized." in caplog.messages


@pytest.mark.anyio
async def test_a_client_that_stops_reading_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event that waits past LIVE_SEND_TIMEOUT ends the stream as too slow."""
    monkeypatch.setattr(sse, "CLOSE_TIMEOUT", 0.01)
    client = Client()
    client.stuck = lambda message: message["type"] == "http.response.body"
    source = Source("a", "b", "c")

    with override_settings(LIVE_SEND_TIMEOUT=0.01, LIVE_SEND_QUEUE=1):
        await client.serve(EventSourceResponse(source()))

    assert source.closed
    assert client.body() == b"data: a\n\n: Client too slow.\n\n"


@pytest.mark.anyio
async def test_a_client_gone_mid_send_ends_the_stream() -> None:
    """The server's OSError: the source is closed, and nothing more is sent."""
    client = Client()
    client.gone = lambda message: message["type"] == "http.response.body"
    source = Source("lost")

    await client.serve(EventSourceResponse(source()))

    assert source.closed
    assert [message["type"] for message in client.sent] == ["http.response.start"]


@pytest.mark.anyio
async def test_a_client_gone_before_the_start_is_not_streamed_to() -> None:
    """The source is closed unread, and nothing is raised."""
    client = Client()
    client.gone = lambda message: True
    source = Source("never")

    await client.serve(EventSourceResponse(source()))

    assert client.sent == []
    assert client.scope["app"].state.live_slots.open == 0


@pytest.mark.anyio
async def test_a_request_that_fails_to_read_ends_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused body (``receive`` raising) ends the stream, logged, not raised."""
    client = Client()
    client.requested = True

    async def refused() -> Message:
        raise RequestBodyTooLarge

    client.receive = refused  # type: ignore[method-assign]
    source = Source("a")

    await client.serve(EventSourceResponse(source()))

    assert source.closed
    assert "Ended an event stream whose request failed" in caplog.messages


@pytest.mark.anyio
async def test_keepalives_never_wait_or_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A comment is skipped while events wait, or once the source has ended."""
    called = anyio.Event()
    keepalive = sse._Stream._keepalive

    def recorded(self: Any) -> None:  # noqa: ANN401 - a _Stream.
        keepalive(self)
        called.set()

    monkeypatch.setattr(sse._Stream, "_keepalive", recorded)
    for events in (["a", "b", "c", "d"], ["a"]):
        client = Client()
        client.stuck = lambda message: message["type"] == "http.response.body"
        called = anyio.Event()

        async def finite(events: list[str] = events) -> AsyncIterator[str]:
            for event in events:
                yield event

        with freeze_time() as clock, override_settings(LIVE_SEND_QUEUE=1):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(client.serve, EventSourceResponse(finite()))
                await client.wait_for(b"data: a\n\n")
                clock.tick(15)
                await called.wait()
                client.left.set()

        assert client.body() == b"data: a\n\n"


@pytest.mark.anyio
async def test_source_errors_are_raised_as_they_are() -> None:
    """Not in an exception group; the stream's tasks have stopped first."""
    client = Client()

    async def failing() -> AsyncIterator[str]:
        yield "a"
        raise LookupError("the source's own")

    with pytest.raises(LookupError, match="the source's own"):
        await client.serve(EventSourceResponse(failing()))


@pytest.mark.anyio
async def test_the_first_reason_to_end_stands() -> None:
    """A stream ended twice ends for the first reason."""
    client = Client()
    stream = sse._Stream(
        EventSourceResponse(Source()()), client.scope, client.receive, client.send
    )

    async with anyio.create_task_group() as stream.tasks:
        stream.end(LIFETIME)
        stream.end(GONE)
    stream.outbox.close()
    stream.queued.close()

    assert stream.closure is LIFETIME


class Countdown:
    """An async iterable that is not a generator: nothing to close."""

    def __init__(self, count: int) -> None:
        """Count down from ``count``."""
        self.count = count

    def __aiter__(self) -> "Countdown":
        """Itself."""
        return self

    async def __anext__(self) -> ServerSentEvent:
        """The next number, until zero."""
        if self.count == 0:
            raise StopAsyncIteration
        self.count -= 1
        return ServerSentEvent(str(self.count))


@pytest.mark.anyio
async def test_any_async_iterable_is_a_source() -> None:
    """Sources without ``aclose`` stream too, and end the response when done."""
    client = Client()

    await client.serve(EventSourceResponse(Countdown(2)))

    assert client.body() == b"data: 1\n\ndata: 0\n\n"
    assert ends_with(client, b"")


@pytest.mark.anyio
async def test_a_source_that_fails_to_close_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its error is logged; the stream still ends."""
    client = Client()

    class Unclosable(Countdown):
        async def aclose(self) -> None:
            raise RuntimeError("cannot close either")

    await client.serve(EventSourceResponse(Unclosable(1)))

    failed = [r for r in caplog.records if r.getMessage().startswith("Closing")]
    assert [str(r.exc_info[1]) for r in failed if r.exc_info] == ["cannot close either"]
    assert ends_with(client, b"")
