"""WebSocket limits: caps, the send queue, message rates, clocks, and re-checks."""

import logging
import math
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Final

import anyio
import anyio.lowlevel
import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from starlette.datastructures import State
from starlette.requests import HTTPConnection
from starlette.routing import Mount, Route
from starlette.testclient import WebSocketDenialResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from domainkit.clock import freeze_time, monotonic
from domainkit.conf import override_settings, settings
from domainkit.conf.global_settings import Settings
from domainkit.context import RequestContext, bind_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application, live, websockets
from domainkit.http.live import (
    LIMITS,
    REAUTHENTICATE,
    RULES,
    Limits,
    LiveConnection,
    LiveLimits,
    Slots,
    send_queue,
    sleep_until,
    stream_limits,
    websocket_limits,
)
from domainkit.http.middleware import utf8_size
from domainkit.http.websockets import (
    ConnectionClosed,
    WebSocketLimitsMiddleware,
    deployment_warnings,
    serves_websockets,
)
from domainkit.log import SECURITY_LOGGER
from domainkit.testing import TestClient

ROUTES: Final = "domainkit_tests.sample_live_routes.router"
NO_AUTH: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
CONNECT: Final[Message] = {"type": "websocket.connect"}
ACCEPT: Final[Message] = {"type": "websocket.accept"}

type MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client() -> Iterator[MakeClient]:
    """Clients of the live sample routes, without auth or a database."""
    with ExitStack() as stack:

        def make(
            *,
            client: tuple[str, int] = ("testclient", 50000),
            **overrides: Any,  # noqa: ANN401 - settings.
        ) -> TestClient:
            stack.enter_context(
                override_settings(
                    **{"ROOT_ROUTER": ROUTES, "MIDDLEWARE": NO_AUTH, **overrides}
                )
            )
            app = get_asgi_application()
            return stack.enter_context(TestClient(app, client=client))

        yield make


def text(socket: Any) -> str:  # noqa: ANN401 - a WebSocketTestSession.
    """The next text message of ``socket``."""
    received: str = socket.receive_text()
    return received


def closed_by(socket: Any) -> tuple[int, str]:  # noqa: ANN401
    """The code and reason ``socket`` is closed with next."""
    with pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_text()
    return closed.value.code, closed.value.reason


# Through the application -------------------------------------------------------------


def test_websockets_echo_under_the_limits(make_client: MakeClient) -> None:
    """A well-behaved client notices nothing."""
    client = make_client()

    with client.websocket_connect("/live/echo") as socket:
        for word in ("one", "two", "three"):
            socket.send_text(word)
            assert text(socket) == word


def test_an_idle_websocket_is_closed(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """No message either way for WEBSOCKET_IDLE_TIMEOUT: closed with 1001."""
    caplog.set_level(logging.INFO, logger="domainkit.http.websockets")
    client = make_client(WEBSOCKET_IDLE_TIMEOUT=30)

    with freeze_time() as clock, client.websocket_connect("/live/echo") as socket:
        clock.tick(29)
        socket.send_text("still here")
        assert text(socket) == "still here"
        clock.tick(29)
        socket.send_text("and here")
        assert text(socket) == "and here"
        clock.tick(30)
        assert closed_by(socket) == (1001, "Idle for too long.")

    assert [
        (r.name, r.levelname)
        for r in caplog.records
        if r.getMessage() == "Closed a WebSocket (1001): Idle for too long."
    ] == [("domainkit.http.websockets", "INFO")]


def test_a_websocket_ends_at_its_lifetime(make_client: MakeClient) -> None:
    """Busy or not, WEBSOCKET_MAX_LIFETIME closes it with 1001."""
    client = make_client(WEBSOCKET_MAX_LIFETIME=100, WEBSOCKET_IDLE_TIMEOUT=60)

    with freeze_time() as clock, client.websocket_connect("/live/echo") as socket:
        for _ in range(3):
            clock.tick(33)
            socket.send_text("busy")
            assert text(socket) == "busy"
        clock.tick(1)
        assert closed_by(socket) == (1001, "Connection lifetime reached.")


def test_a_route_sets_its_own_limits(make_client: MakeClient) -> None:
    """``LiveLimits`` on a route replaces the settings for its connections."""
    client = make_client()

    with freeze_time() as clock, client.websocket_connect("/live/short") as socket:
        socket.send_text("hi")
        assert text(socket) == "hi"
        clock.tick(5)
        assert closed_by(socket) == (1001, "Idle for too long.")
    with freeze_time() as clock, client.websocket_connect("/live/echo") as socket:
        clock.tick(5)
        socket.send_text("hi")
        assert text(socket) == "hi"


def test_too_many_messages_close_the_websocket(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Past WEBSOCKET_MESSAGE_RATE, per connection: 1008, logged as a security event."""
    client = make_client(WEBSOCKET_MESSAGE_RATE="3/minute")

    with (
        freeze_time() as clock,
        caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER),
        client.websocket_connect("/live/echo") as socket,
    ):
        for _ in range(3):
            socket.send_text("x")
            assert text(socket) == "x"
        clock.tick(20)  # One more message's worth.
        socket.send_text("x")
        assert text(socket) == "x"
        with client.websocket_connect("/live/echo") as other:
            other.send_text("its own count")
            assert text(other) == "its own count"
        socket.send_text("x")
        assert closed_by(socket) == (1008, "Too many messages.")

    assert [
        (r.name, r.levelname)
        for r in caplog.records
        if r.getMessage() == "Closed a WebSocket (1008): Too many messages."
    ] == [(SECURITY_LOGGER, "WARNING")]


def test_messages_are_not_limited_without_a_rate(make_client: MakeClient) -> None:
    """WEBSOCKET_MESSAGE_RATE = None counts nothing."""
    client = make_client(WEBSOCKET_MESSAGE_RATE=None)

    with freeze_time(), client.websocket_connect("/live/echo") as socket:
        for _ in range(50):
            socket.send_text("x")
            assert text(socket) == "x"


def test_open_websockets_are_capped_per_process(make_client: MakeClient) -> None:
    """Past LIVE_MAX_CONNECTIONS, a handshake gets 503 with Retry-After."""
    client = make_client(LIVE_MAX_CONNECTIONS=2, LIVE_MAX_CONNECTIONS_PER_CALLER=2)

    with (
        client.websocket_connect("/live/echo"),
        client.websocket_connect("/live/echo"),
        pytest.raises(WebSocketDenialResponse) as refused,
        client.websocket_connect("/live/echo"),
    ):
        pass

    assert refused.value.status_code == 503
    assert refused.value.json()["code"] == "too_many_connections"
    assert refused.value.json()["detail"] == (
        "Too many open connections; try again later."
    )
    assert 5 <= int(refused.value.headers["Retry-After"]) <= 15
    with client.websocket_connect("/live/echo") as socket:  # Slots came back.
        socket.send_text("again")
        assert text(socket) == "again"


def test_open_websockets_are_capped_per_caller(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Past LIVE_MAX_CONNECTIONS_PER_CALLER, that caller is refused; others are not."""
    client = make_client(LIVE_MAX_CONNECTIONS_PER_CALLER=1)
    other = TestClient(client.app, client=("192.0.2.7", 4000))
    other.portal = client.portal

    with client.websocket_connect("/live/echo"):
        with (
            pytest.raises(WebSocketDenialResponse) as refused,
            client.websocket_connect("/live/echo"),
        ):
            pass
        with other.websocket_connect("/live/echo") as socket:
            socket.send_text("mine")
            assert text(socket) == "mine"

    assert refused.value.status_code == 503
    assert (
        "Refused a live connection: LIVE_MAX_CONNECTIONS_PER_CALLER is reached"
        in caplog.messages
    )


# The send queue, raw -----------------------------------------------------------------


class Server:
    """The server's side of one raw WebSocket."""

    def __init__(self, *incoming: Message, extensions: object = None) -> None:
        """Deliver ``incoming`` after the connect; ``extensions`` go in the scope."""
        self.messages: list[Message] = [CONNECT, *incoming]
        self.sent: list[Message] = []
        self.more = anyio.Event()
        self.changed = anyio.Event()
        self.stuck: Callable[[Message], bool] = lambda message: False
        """Sends that never finish: a client that reads nothing."""
        self.gone: Callable[[Message], bool] = lambda message: False
        """Sends that fail as for a client that left."""
        self.scope: Scope = {
            "type": "websocket",
            "path": "/raw",
            "headers": [],
            "client": ("203.0.113.5", 1234),
            "app": SimpleNamespace(state=State()),
            "extensions": extensions or {},
        }

    async def receive(self) -> Message:
        """The next message from the client, waiting for more if there is none."""
        while not self.messages:
            await self.more.wait()
            self.more = anyio.Event()
        return self.messages.pop(0)

    def push(self, message: Message) -> None:
        """Let the client send ``message``."""
        self.messages.append(message)
        self.more.set()

    async def send(self, message: Message) -> None:
        """Take ``message``; stall or fail as the client is set up to."""
        if self.gone(message):
            raise OSError("client gone")
        self.sent.append(message)
        self.changed.set()
        if self.stuck(message):
            await anyio.sleep_forever()

    async def wait_sent(self, count: int) -> None:
        """Wait until the server has taken ``count`` messages."""
        while len(self.sent) < count:
            await self.changed.wait()
            self.changed = anyio.Event()

    async def serve(self, app: ASGIApp) -> None:
        """Serve ``app`` behind the middleware."""
        await WebSocketLimitsMiddleware(app)(self.scope, self.receive, self.send)


def sends(*texts: str) -> list[Message]:
    """``websocket.send`` messages of ``texts``."""
    return [{"type": "websocket.send", "text": value} for value in texts]


@pytest.mark.anyio
async def test_queued_messages_go_out_in_order_before_the_close() -> None:
    """The application's sends and close leave in order, after it returns."""
    server = Server()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        for value in ("a", "b", "c"):
            await send({"type": "websocket.send", "text": value})
        await send({"type": "websocket.close", "code": 1000})

    await server.serve(app)

    assert server.sent == [
        ACCEPT,
        *sends("a", "b", "c"),
        {"type": "websocket.close", "code": 1000},
    ]


@pytest.mark.anyio
async def test_a_client_that_stops_reading_is_closed_as_too_slow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sender waits while the queue is full; past the timeout: 1013, cancelled."""
    server = Server()
    server.stuck = lambda message: message["type"] == "websocket.send"
    queued: list[int] = []
    cancelled = anyio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        try:
            for number in range(10):
                await send({"type": "websocket.send", "text": str(number)})
                queued.append(number)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    with override_settings(LIVE_SEND_QUEUE=2, LIVE_SEND_TIMEOUT=0.01):
        await server.serve(app)

    assert queued == [0, 1, 2]  # One being sent, two waiting; the fourth waited.
    assert cancelled.is_set()
    assert server.sent == [
        ACCEPT,
        *sends("0"),
        {"type": "websocket.close", "code": 1013, "reason": "Client too slow."},
    ]
    assert [
        (r.name, r.levelname)
        for r in caplog.records
        if r.getMessage() == "Closed a WebSocket (1013): Client too slow."
    ] == [("domainkit.http.websockets", "WARNING")]


@pytest.mark.anyio
async def test_a_client_gone_mid_send_ends_the_application() -> None:
    """The server's OSError on a send: the application is cancelled, no close sent."""
    server = Server()
    server.gone = lambda message: message["type"] == "websocket.send"
    after: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        await send({"type": "websocket.send", "text": "lost"})
        await anyio.sleep_forever()
        after.append("never")

    await server.serve(app)

    assert server.sent == [ACCEPT]
    assert after == []


@pytest.mark.anyio
async def test_sends_after_the_limits_closed_the_socket_fail() -> None:
    """After the framework's close, receives say so, and sends raise an OSError."""
    server = Server(
        {"type": "websocket.receive", "text": "x"},
        {"type": "websocket.receive", "text": "y"},
    )
    raised: list[BaseException] = []
    disconnect = {"type": "websocket.disconnect", "code": 1008}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        assert await receive() == {"type": "websocket.receive", "text": "x"}
        assert [await receive(), await receive()] == [disconnect, disconnect]
        try:
            await send({"type": "websocket.send", "text": "late"})
        except ConnectionClosed as exc:
            raised.append(exc)

    with override_settings(WEBSOCKET_MESSAGE_RATE="1/minute"):
        await server.serve(app)

    assert [str(exc) for exc in raised] == ["Too many messages."]
    assert isinstance(raised[0], OSError)
    assert server.sent[-1] == {
        "type": "websocket.close",
        "code": 1008,
        "reason": "Too many messages.",
    }


@pytest.mark.anyio
async def test_a_send_after_the_applications_own_close_fails() -> None:
    """Once its close has gone out, the socket takes nothing more."""
    server = Server()
    raised: list[OSError] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        await send({"type": "websocket.close", "code": 1000})
        await server.wait_sent(2)
        try:
            await send({"type": "websocket.send", "text": "after"})
        except ConnectionClosed as exc:
            raised.append(exc)

    await server.serve(app)

    assert [str(exc) for exc in raised] == ["The WebSocket is closed."]
    assert server.sent == [ACCEPT, {"type": "websocket.close", "code": 1000}]


@pytest.mark.anyio
async def test_no_second_close_follows_the_applications_own() -> None:
    """A close the application queued is never followed by the framework's."""
    server = Server()
    server.stuck = lambda message: message["type"] == "websocket.close"

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        await send({"type": "websocket.close", "code": 1000})

    with override_settings(LIVE_SEND_TIMEOUT=0.01):
        await server.serve(app)

    assert server.sent == [ACCEPT, {"type": "websocket.close", "code": 1000}]


@pytest.mark.anyio
async def test_the_frameworks_close_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close the server never takes is given up after CLOSE_TIMEOUT."""
    monkeypatch.setattr(websockets, "CLOSE_TIMEOUT", 0.01)
    server = Server()
    server.stuck = lambda message: message["type"] != "websocket.accept"

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        await send({"type": "websocket.send", "text": "stuck"})
        await anyio.sleep_forever()

    with override_settings(LIVE_SEND_TIMEOUT=0.01):
        await server.serve(app)

    assert server.sent[-1]["type"] == "websocket.close"


@pytest.mark.anyio
async def test_a_client_leaving_is_not_a_message() -> None:
    """Its disconnect reaches the application as is, outside the message rate."""
    server = Server(
        {"type": "websocket.receive", "text": "x"},
        {"type": "websocket.disconnect", "code": 1000},
    )
    told: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        told.extend([await receive(), await receive()])

    with override_settings(WEBSOCKET_MESSAGE_RATE="1/minute"):
        await server.serve(app)

    assert told[1] == {"type": "websocket.disconnect", "code": 1000}
    assert server.sent == [ACCEPT]


@pytest.mark.anyio
async def test_route_limits_reach_the_middleware_through_a_copied_scope() -> None:
    """Middleware below may copy the scope: the route's limits still apply."""
    server = Server(
        {"type": "websocket.receive", "text": "x"},
        {"type": "websocket.receive", "text": "y"},
        {"type": "websocket.disconnect", "code": 1000},
    )

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        await LiveLimits(message_rate="1/minute")(HTTPConnection(scope))
        await echo_app(scope, receive, send)

    async def copying(scope: Scope, receive: Receive, send: Send) -> None:
        await route(dict(scope), receive, send)

    await server.serve(copying)  # The default rate would take both messages.

    assert server.sent[-1] == {
        "type": "websocket.close",
        "code": 1008,
        "reason": "Too many messages.",
    }
    assert sends("y")[0] not in server.sent


@pytest.mark.anyio
async def test_the_handshake_is_answered_directly() -> None:
    """A refusal before the accept is the application's, untouched."""
    server = Server()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "websocket.close", "code": 1008})

    await server.serve(app)

    assert server.sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.anyio
async def test_application_errors_are_raised_as_they_are() -> None:
    """Not wrapped in an exception group, so error handling still sees them."""
    server = Server()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send(ACCEPT)
        raise LookupError("the application's own")

    with pytest.raises(LookupError, match="the application's own"):
        await server.serve(app)


@pytest.mark.anyio
async def test_other_requests_pass_through() -> None:
    """HTTP requests take no slot and see no wrapping."""
    seen: list[tuple[Receive, Send]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((receive, send))

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        raise AssertionError

    await WebSocketLimitsMiddleware(app)({"type": "http"}, receive, send)

    assert seen == [(receive, send)]


@pytest.mark.anyio
@pytest.mark.parametrize("extensions", ["none", "other"])
async def test_a_full_process_closes_handshakes_it_cannot_answer(
    extensions: str,
) -> None:
    """Without the denial extension, a refused handshake is closed with 1013."""
    server = Server()
    if extensions == "none":
        del server.scope["extensions"]  # ASGI makes the key optional.
    else:
        server.scope["extensions"] = {"websocket.http.other": {}}
    slots = Slots(total=1, per_caller=1)
    slots.acquire("someone")
    server.scope["app"].state.live_slots = slots

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("A refused handshake reached the application.")

    await server.serve(app)

    assert server.sent == [{"type": "websocket.close", "code": 1013}]
    assert (slots.open, slots.callers) == (1, {"someone": 1})


# Clocks and re-checks, raw -----------------------------------------------------------


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Accept, then echo until the client leaves."""
    await receive()
    await send(ACCEPT)
    while (message := await receive())["type"] == "websocket.receive":
        await send({"type": "websocket.send", "text": message["text"]})


async def open_raw(server: Server, app: ASGIApp, tasks: Any) -> None:  # noqa: ANN401
    """Start serving ``app``, and wait until it has accepted."""
    tasks.start_soon(server.serve, app)
    await server.wait_sent(1)


class Answers:
    """A re-check's answers, one per call, and an event per call made."""

    def __init__(self, *answers: bool) -> None:
        """Answer ``answers`` in order."""
        self.answers = list(answers)
        self.asked: list[object] = []
        self.called = anyio.Event()

    async def __call__(self, asked: object) -> bool:
        """The next answer."""
        self.asked.append(asked)
        self.called.set()
        return self.answers.pop(0)

    async def wait(self, calls: int) -> None:
        """Wait until ``calls`` re-checks were made."""
        while len(self.asked) < calls:
            await self.called.wait()
            self.called = anyio.Event()


@pytest.mark.anyio
async def test_credentials_are_checked_again_while_the_socket_is_open(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every LIVE_REAUTH_INTERVAL; once they fail, closed with 1008."""
    monkeypatch.setattr("domainkit.http.live.random.uniform", lambda low, high: high)
    server = Server()
    reauthenticate = Answers(True, False)
    server.scope[REAUTHENTICATE] = reauthenticate
    with freeze_time() as clock, override_settings(LIVE_REAUTH_INTERVAL=60):
        async with anyio.create_task_group() as tasks:
            await open_raw(server, echo_app, tasks)
            clock.tick(59)  # Not yet: the spread is at its longest here.
            server.push({"type": "websocket.receive", "text": "still allowed"})
            await server.wait_sent(2)
            assert reauthenticate.asked == []
            clock.tick(1)  # Exactly the interval.
            await reauthenticate.wait(1)
            clock.tick(60)

    assert reauthenticate.asked == [server.scope, server.scope]
    assert server.sent == [
        ACCEPT,
        *sends("still allowed"),
        {"type": "websocket.close", "code": 1008, "reason": "Not authorized."},
    ]
    assert "Closed a WebSocket (1008): Not authorized." in caplog.messages


@pytest.mark.anyio
async def test_the_routes_rules_are_checked_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recorded access check that now refuses closes the socket with 1008."""
    server = Server()
    still_allowed = Answers(True, False)
    server.scope[RULES] = [still_allowed]
    with freeze_time() as clock, override_settings(LIVE_REAUTH_INTERVAL=10):
        async with anyio.create_task_group() as tasks:
            await open_raw(server, echo_app, tasks)
            clock.tick(10)
            await still_allowed.wait(1)
            clock.tick(10)

    assert [type(asked).__name__ for asked in still_allowed.asked] == [
        "HTTPConnection",
        "HTTPConnection",
    ]
    assert server.sent[-1]["code"] == 1008
    assert "An open connection's access rules no longer allow it" in caplog.messages


@pytest.mark.anyio
async def test_a_check_that_fails_closes_with_try_again_later(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken check confirms nothing: 1013, with the error logged."""
    server = Server()

    async def reauthenticate(scope: Scope) -> bool:
        raise ConnectionError("database down")

    server.scope[REAUTHENTICATE] = reauthenticate
    with freeze_time() as clock:
        async with anyio.create_task_group() as tasks:
            await open_raw(server, echo_app, tasks)
            clock.tick(60)

    assert server.sent[-1] == {
        "type": "websocket.close",
        "code": 1013,
        "reason": "Authorization not confirmed.",
    }
    failed = [r for r in caplog.records if r.getMessage().startswith("Could not")]
    assert [type(r.exc_info[1]) for r in failed if r.exc_info] == [ConnectionError]


@pytest.mark.anyio
async def test_a_check_that_hangs_is_cut_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Past REAUTH_TIMEOUT a check counts as failed: 1013."""
    monkeypatch.setattr(live, "REAUTH_TIMEOUT", 0.01)
    server = Server()

    async def reauthenticate(scope: Scope) -> bool:
        await anyio.sleep_forever()
        raise AssertionError

    server.scope[REAUTHENTICATE] = reauthenticate
    with freeze_time() as clock:
        async with anyio.create_task_group() as tasks:
            await open_raw(server, echo_app, tasks)
            clock.tick(60)

    assert server.sent[-1]["code"] == 1013
    assert "Re-checking an open connection took over 0s" in caplog.messages


@pytest.mark.anyio
async def test_a_check_in_flight_is_not_cut_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A client that leaves mid-check: the check runs on, to its own end."""
    monkeypatch.setattr(live, "REAUTH_TIMEOUT", 0.5)
    server = Server()
    started = anyio.Event()

    async def reauthenticate(scope: Scope) -> bool:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError

    server.scope[REAUTHENTICATE] = reauthenticate
    with freeze_time() as clock:
        async with anyio.create_task_group() as tasks:
            await open_raw(server, echo_app, tasks)
            clock.tick(60)
            await started.wait()
            server.push({"type": "websocket.disconnect", "code": 1000})

    # It ended at its own deadline, not when the client left.
    assert "Re-checking an open connection took over 0s" in caplog.messages
    assert server.sent[-1] == {
        "type": "websocket.close",
        "code": 1013,
        "reason": "Authorization not confirmed.",
    }


@pytest.mark.anyio
async def test_no_rechecks_without_an_interval() -> None:
    """LIVE_REAUTH_INTERVAL = None: the handshake's verdict stands."""
    server = Server()

    async def reauthenticate(scope: Scope) -> bool:
        raise AssertionError("checked")

    server.scope[REAUTHENTICATE] = reauthenticate
    with freeze_time() as clock, override_settings(LIVE_REAUTH_INTERVAL=None):
        async with anyio.create_task_group() as tasks:
            await open_raw(server, echo_app, tasks)
            clock.tick(299)
            server.push({"type": "websocket.receive", "text": "fine"})
            await server.wait_sent(2)
            server.push({"type": "websocket.disconnect", "code": 1000})

    assert server.sent == [ACCEPT, *sends("fine")]


@pytest.mark.anyio
async def test_the_first_reason_to_close_stands() -> None:
    """Two limits reached at once: the socket closes for the first."""
    server = Server()
    socket = websockets._Socket(
        WebSocketLimitsMiddleware(echo_app), server.scope, server.receive, server.send
    )

    async with anyio.create_task_group() as socket.tasks:
        socket.end(live.IDLE)
        socket.end(live.TOO_SLOW)

    assert socket.closure is live.IDLE


# The pieces --------------------------------------------------------------------------


def test_slots_count_per_process_and_per_caller() -> None:
    """Each cap refuses on its own; a caller with none left is forgotten."""
    slots = Slots(total=3, per_caller=2)

    assert [slots.acquire("a"), slots.acquire("a")] == [None, None]
    assert slots.acquire("a") == "LIVE_MAX_CONNECTIONS_PER_CALLER"
    assert slots.acquire("b") is None
    assert slots.acquire("c") == "LIVE_MAX_CONNECTIONS"
    slots.release("a")
    assert (slots.open, slots.callers) == (2, {"a": 1, "b": 1})
    slots.release("a")
    slots.release("b")
    assert (slots.open, slots.callers) == (0, {})


def test_connections_are_held_by_their_actor_or_address() -> None:
    """A user counts as itself, wherever it connects from; others by address."""
    scope = {"client": ("203.0.113.5", 1234)}
    context = RequestContext(request_id="r", client_ip=None, method="WS", path="/")

    assert live.caller_of(scope) == "ip:203.0.113.5"
    with bind_request(context):
        assert live.caller_of(scope) == "ip:203.0.113.5"
    with bind_request(replace(context, actor="42")):
        assert live.caller_of(scope) == "actor:42"


def test_the_applications_slots_are_made_once() -> None:
    """From the settings, kept in the application's state."""
    scope = {"app": SimpleNamespace(state=State())}

    with override_settings(LIVE_MAX_CONNECTIONS=7, LIVE_MAX_CONNECTIONS_PER_CALLER=3):
        slots = live.slots_for(scope)

    assert (slots.total, slots.per_caller) == (7, 3)
    assert live.slots_for(scope) is slots


def test_limits_come_from_settings_then_the_route() -> None:
    """Each kind reads its own settings and the shared ones; a route overrides."""
    defaults = Settings()
    scope: Scope = {}

    assert websocket_limits(scope) == Limits(
        max_lifetime=defaults.WEBSOCKET_MAX_LIFETIME,
        send_queue=64,
        send_timeout=10.0,
        reauth_interval=60.0,
        idle_timeout=300.0,
        message_rate=(20, 1.0),
    )
    assert stream_limits(scope) == Limits(
        max_lifetime=defaults.SSE_MAX_LIFETIME,
        send_queue=64,
        send_timeout=10.0,
        reauth_interval=60.0,
        keepalive=15.0,
    )
    overrides = LiveLimits(
        max_lifetime=9,
        send_queue=2,
        send_timeout=3,
        reauth_interval=None,
        idle_timeout=None,
        message_rate=None,
        keepalive=4,
    ).overrides
    scope[LIMITS] = overrides
    assert websocket_limits(scope) == Limits(9, 2, 3, None, None, None, None)
    assert stream_limits(scope) == Limits(9, 2, 3, None, keepalive=4)
    assert send_queue(scope) == 2
    scope[LIMITS] = LiveLimits(message_rate="5/minute").overrides
    assert websocket_limits(scope).message_rate == (5, 60.0)


def test_route_limits_keep_what_they_were_given() -> None:
    """Only the keys passed override; they are checked as the settings are."""
    assert LiveLimits(idle_timeout=5).overrides == {"idle_timeout": 5.0}
    assert LiveLimits(message_rate=None).overrides == {"message_rate": None}
    bad: list[dict[str, Any]] = [
        {"idle_timeout": 0},
        {"max_lifetime": -1},
        {"send_queue": 0},
        {"send_queue": 10_001},
        {"send_timeout": math.inf},
        {"reauth_interval": 0},
        {"keepalive": 0},
        {"message_rate": "often"},
        {"lifetime": 5},
    ]
    for overrides in bad:
        with pytest.raises(ImproperlyConfigured, match=r"^LiveLimits: "):
            LiveLimits(**overrides)


@pytest.mark.anyio
async def test_route_limits_reach_a_copied_scope() -> None:
    """The dependency updates the dictionary the middleware put in the scope."""
    shared: dict[str, Any] = {}
    copied = HTTPConnection({"type": "websocket", LIMITS: shared})
    fresh = HTTPConnection({"type": "http"})

    await LiveLimits(idle_timeout=7)(copied)
    await LiveLimits(max_lifetime=8)(copied)
    await LiveLimits(keepalive=9)(fresh)

    assert shared == {"idle_timeout": 7.0, "max_lifetime": 8.0}
    assert fresh.scope[LIMITS] == {"keepalive": 9.0}


def test_rechecks_are_spread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-check comes between three quarters of the interval and all of it."""
    bounds: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        bounds.append((low, high))
        return low

    monkeypatch.setattr("domainkit.http.live.random.uniform", uniform)
    with freeze_time():
        connection = LiveConnection({}, Limits(100, 1, 1, reauth_interval=40))
        assert connection.next_check == connection.opened + 30
    assert bounds == [(0.75, 1.0)]


def test_a_connection_looks_again_at_its_next_deadline() -> None:
    """The earliest of the lifetime, the next check, idleness, and keep-alive."""
    with freeze_time():
        limits = Limits(100, 1, 1, reauth_interval=None, idle_timeout=50, keepalive=20)
        connection = LiveConnection({}, limits)
        start = connection.opened
        assert connection.deadline() == start + 20
        connection.active += 40
        assert connection.deadline() == start + 60
        connection.next_check = start + 55
        assert connection.deadline() == start + 55
        connection.limits = Limits(100, 1, 1, reauth_interval=None, idle_timeout=50)
        assert connection.deadline() == start + 55
        connection.next_check = math.inf
        assert connection.deadline() == start + 90
        assert connection.expired(start + 89) is None
        assert connection.expired(start + 90) is live.IDLE
        assert connection.expired(start + 100) is live.LIFETIME


@pytest.mark.anyio
async def test_sleep_until_follows_a_frozen_clock() -> None:
    """Ticking a frozen clock ends the wait; a deadline passed returns at once."""
    with freeze_time() as clock:
        deadline = monotonic() + 30
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(sleep_until, deadline)
            await anyio.lowlevel.checkpoint()
            clock.tick(30)
        await sleep_until(deadline - 1)
    await sleep_until(monotonic())
    await sleep_until(monotonic() + 0.001)  # Real time, unfrozen.


@pytest.mark.anyio
async def test_sleep_until_sleeps_once_unless_the_clock_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One sleep to the deadline; under a frozen clock, steps no longer than one."""
    now = [10.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(live, "monotonic", lambda: now[0])
    monkeypatch.setattr(live, "anyio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(live, "FROZEN_STEP", 0.25)
    monkeypatch.setattr(live, "frozen", lambda: None)
    await sleep_until(10.5)
    assert (slept, now) == ([0.5], [10.5])

    slept.clear()
    monkeypatch.setattr(live, "frozen", lambda: object())
    await sleep_until(11.125)
    assert (slept, now) == ([0.25, 0.25, 0.125], [11.125])
    await sleep_until(11.0)
    assert slept == [0.25, 0.25, 0.125]


def test_text_is_measured_in_utf8_without_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASCII by its length; other text a chunk at a time, chunks never split."""
    monkeypatch.setattr("domainkit.http.middleware.UTF8_CHUNK", 3)
    samples = ["", "plain", "été", "\U0001f600" * 5 + "abc€", "€"]

    assert [utf8_size(sample) for sample in samples] == [
        len(sample.encode()) for sample in samples
    ]


# Deployment --------------------------------------------------------------------------


def websocket_app() -> FastAPI:
    """An application with a WebSocket route mounted under another."""
    inner = APIRouter()

    @inner.websocket("/socket")
    async def socket(websocket: WebSocket) -> None:
        """Nothing."""

    app = FastAPI()
    sub = FastAPI()
    sub.include_router(inner)
    app.mount("/sub", sub)
    return app


def test_websocket_routes_are_found_where_they_are_mounted() -> None:
    """WebSocket routes count, mounted or not; plain routes and apps do not."""

    async def page(scope: Scope, receive: Receive, send: Send) -> None:
        """Nothing."""

    assert serves_websockets(websocket_app().routes)
    assert not serves_websockets([Route("/page", page), Mount("/static", app=page)])
    assert not serves_websockets([])


def deployed(**overrides: Any) -> list[str]:  # noqa: ANN401
    """``check``'s WebSocket warnings for production settings."""
    with override_settings(ENVIRONMENT="production", **overrides):
        return deployment_warnings(websocket_app())


def test_check_warns_about_websocket_server_limits() -> None:
    """A server that reads larger messages than the limit, or a missing middleware."""
    middleware = [*settings.MIDDLEWARE]
    limited = {"MIDDLEWARE": middleware, "WEBSOCKET_SERVER_MAX_SIZE": 1_048_576}

    assert deployed(**limited) == []
    assert deployed(**{**limited, "MAX_REQUEST_BODY_SIZE": 2_000_000}) == []
    assert deployed(MIDDLEWARE=middleware) == [
        (
            "The server reads WebSocket messages of up to 16777216 bytes whole "
            "before MAX_REQUEST_BODY_SIZE (1048576) is checked; run uvicorn with "
            "--ws-max-size 1048576, and set WEBSOCKET_SERVER_MAX_SIZE to match."
        )
    ]
    assert deployed(MIDDLEWARE=middleware[:1], WEBSOCKET_SERVER_MAX_SIZE=1_048_577) == [
        (
            "The server reads WebSocket messages of up to 1048577 bytes whole "
            "before MAX_REQUEST_BODY_SIZE (1048576) is checked; run uvicorn with "
            "--ws-max-size 1048576, and set WEBSOCKET_SERVER_MAX_SIZE to match."
        ),
        (
            "WebSockets are served without "
            "domainkit.http.websockets.WebSocketLimitsMiddleware: no caps, send "
            "queue, message rate, idle or lifetime limits, or re-authorization. "
            "List it in MIDDLEWARE."
        ),
    ]


def test_check_is_quiet_without_websockets_or_outside_deployments() -> None:
    """Nothing to warn about: no WebSocket routes, or a local environment."""
    with override_settings(ENVIRONMENT="production", MIDDLEWARE=[]):
        assert deployment_warnings(FastAPI()) == []
    with override_settings(MIDDLEWARE=[]):
        assert deployment_warnings(websocket_app()) == []


def test_live_settings_are_checked() -> None:
    """A rate that does not read, or a caller's cap above the process's, is refused."""
    with pytest.raises(ValueError, match="rate"):
        Settings(WEBSOCKET_MESSAGE_RATE="lots")
    with pytest.raises(
        ValueError,
        match=(
            r"Value error, LIVE_MAX_CONNECTIONS_PER_CALLER cannot exceed "
            r"LIVE_MAX_CONNECTIONS \[type"
        ),
    ):
        Settings(LIVE_MAX_CONNECTIONS=5, LIVE_MAX_CONNECTIONS_PER_CALLER=6)
    assert Settings(LIVE_MAX_CONNECTIONS=5, LIVE_MAX_CONNECTIONS_PER_CALLER=5)


def test_deployments_are_warned_without_rechecks() -> None:
    """LIVE_REAUTH_INTERVAL = None keeps revoked tokens' sockets open."""
    key = settings.SECRET_KEY
    warning = (
        "LIVE_REAUTH_INTERVAL is None, so an open WebSocket or event stream keeps "
        "a revoked token or a removed permission until it closes; set it (60 is "
        "a minute)."
    )
    off = Settings(ENVIRONMENT="production", SECRET_KEY=key, LIVE_REAUTH_INTERVAL=None)
    on = Settings(ENVIRONMENT="production", SECRET_KEY=key)

    assert warning in off.deployment_warnings()
    assert warning not in on.deployment_warnings()
