"""``WebSocketLimitsMiddleware``: every WebSocket bounded, and authorized for life.

List it in ``MIDDLEWARE`` after ``AuthenticationMiddleware`` and
``RateLimitMiddleware``, so it counts connections per user and only once a
handshake passed the rate limit. Each WebSocket then:

- takes a slot under ``LIVE_MAX_CONNECTIONS`` and
  ``LIVE_MAX_CONNECTIONS_PER_CALLER``, or is refused (``domainkit.http.live``);
- sends through a queue of ``LIVE_SEND_QUEUE`` messages. A sender waits
  while it is full, and a client that takes longer than
  ``LIVE_SEND_TIMEOUT`` to receive one message is closed with 1013;
- may receive ``WEBSOCKET_MESSAGE_RATE`` messages, counted per connection
  in this process; one more closes it with 1008;
- is closed with 1001 after ``WEBSOCKET_IDLE_TIMEOUT`` seconds without a
  message either way, or ``WEBSOCKET_MAX_LIFETIME`` seconds in all;
- has its credentials and its route's access rules checked again every
  ``LIVE_REAUTH_INTERVAL`` seconds, and is closed with 1008 when they no
  longer hold.

When the framework closes a WebSocket, the application is cancelled, so no
handler outlives its connection, whatever it awaits. The close says why.
Keep-alive pings are the server's: uvicorn sends one every 20 seconds
(``--ws-ping-interval``) and drops a client that does not answer.
"""

import itertools
import logging
from collections.abc import Sequence
from contextlib import suppress
from typing import Final

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from starlette.routing import BaseRoute, Host, Mount, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.clock import monotonic
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.http.live import (
    GONE,
    LIMITS,
    NORMAL_ENDS,
    TOO_MANY_MESSAGES,
    Closure,
    LiveConnection,
    caller_of,
    refuse,
    slots_for,
    websocket_limits,
)
from domainkit.http.ratelimit import MemoryBackend
from domainkit.http.servers import limit_warnings, server_limits
from domainkit.log import SECURITY_LOGGER

__all__ = [
    "WebSocketLimitsMiddleware",
]

logger = logging.getLogger(__name__)
security_logger = logging.getLogger(SECURITY_LOGGER)

MIDDLEWARE: Final = "domainkit.http.websockets.WebSocketLimitsMiddleware"
CLOSE_TIMEOUT: Final = 5.0
"""Seconds the close of a WebSocket the framework ends may take to send."""


class ConnectionClosed(OSError):
    """The framework closed the WebSocket; nothing more can be sent on it.

    It is an ``OSError``, as ASGI asks of a send on a closed connection, so
    Starlette's ``WebSocket`` raises ``WebSocketDisconnect`` for it.
    """


class WebSocketLimitsMiddleware:
    """Cap, queue, rate-limit, time, and re-authorize every WebSocket.

    Other requests pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``; count every connection's messages in this process."""
        self.app = app
        self.limiter = MemoryBackend()
        """Counts messages per connection: a connection lives in one process."""
        self._ids = itertools.count()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve a WebSocket under its limits, if a slot is free."""
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        slots = slots_for(scope)
        caller = caller_of(scope)
        if (cap := slots.acquire(caller)) is not None:
            await refuse(scope, receive, send, cap, slots=slots)
            return
        try:
            await _Socket(self, scope, receive, send).serve()
        finally:
            slots.release(caller)

    def key(self) -> str:
        """A new connection's key in the message counter."""
        return f"ws:{next(self._ids)}"


class _Socket:
    """One WebSocket: its send queue, its clocks, and how it ended."""

    tasks: TaskGroup
    outbox: MemoryObjectSendStream[Message] | None = None
    """Where the application's messages wait, from the accept on."""
    queued: MemoryObjectReceiveStream[Message] | None = None
    """Where the writer takes them from."""
    connection: LiveConnection | None = None
    """The connection's clocks and limits, from the accept on."""
    closure: Closure | None = None
    """Why the framework ended the connection, if it did."""
    closing = False
    """Whether a close frame went to the server: no second one may follow."""

    def __init__(
        self,
        middleware: WebSocketLimitsMiddleware,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Serve ``scope`` with ``middleware``'s application and counter."""
        self.app = middleware.app
        self.limiter = middleware.limiter
        self.key = middleware.key()
        self.scope = scope
        self._receive = receive
        self._send = send
        self.drained = anyio.Event()

    async def serve(self) -> None:
        """Run the application; then close the connection if the limits ended it.

        Errors of the application are raised as they are, after the queue's
        tasks have stopped.
        """
        # A route's LiveLimits write here, even through a copy of the scope.
        self.scope.setdefault(LIMITS, {})
        error: Exception | None = None
        try:
            async with anyio.create_task_group() as self.tasks:
                try:
                    await self.app(self.scope, self.receive, self.send)
                    if self.outbox is not None:
                        self.outbox.close()
                        await self.drained.wait()
                except Exception as exc:  # noqa: BLE001 - raised once tasks stop.
                    error = exc
                self.tasks.cancel_scope.cancel()
        finally:
            for stream in (self.outbox, self.queued):
                if stream is not None:
                    stream.close()
        if error is not None:
            raise error
        await self._close()

    async def receive(self) -> Message:
        """The client's next message; a disconnect once the limits ended it."""
        if self.closure is not None:
            return {"type": "websocket.disconnect", "code": self.closure.code}
        message = await self._receive()
        connection = self.connection
        if message["type"] == "websocket.receive" and connection is not None:
            connection.active = monotonic()
            rate = connection.limits.message_rate
            if (
                rate is not None
                and not (await self.limiter.hit(self.key, *rate)).allowed
            ):
                self.end(TOO_MANY_MESSAGES)
                return {"type": "websocket.disconnect", "code": TOO_MANY_MESSAGES.code}
        return message

    async def send(self, message: Message) -> None:
        """Answer the handshake at once; queue what follows the accept.

        Raises:
            ConnectionClosed: The limits ended the connection, or it was closed.
        """
        if self.closure is not None:
            raise ConnectionClosed(self.closure.reason)
        if self.outbox is None:
            await self._send(message)
            if message["type"] == "websocket.accept":
                self._open()
            return
        try:
            await self.outbox.send(message)
        except anyio.BrokenResourceError:
            raise ConnectionClosed("The WebSocket is closed.") from None

    def _open(self) -> None:
        """Start the queue and the watch, under the route's limits."""
        limits = websocket_limits(self.scope)
        self.connection = connection = LiveConnection(self.scope, limits)
        self.outbox, self.queued = anyio.create_memory_object_stream[Message](
            limits.send_queue
        )
        self.tasks.start_soon(self._write, self.queued, connection)
        self.tasks.start_soon(connection.watch, self.end)

    async def _write(
        self, queued: MemoryObjectReceiveStream[Message], connection: LiveConnection
    ) -> None:
        """Hand the queued messages to the server, each within the send timeout."""
        try:
            async with queued:
                async for message in queued:
                    closing = message["type"] == "websocket.close"
                    self.closing = self.closing or closing
                    closure = await connection.deliver(self._send, message)
                    if closure is not None:
                        self.end(closure)
                        return
                    if closing:
                        return
        finally:
            self.drained.set()

    def end(self, closure: Closure) -> None:
        """End the connection with ``closure``: stop the application and the queue."""
        if self.closure is None:
            self.closure = closure
            level = logging.INFO if closure in NORMAL_ENDS else logging.WARNING
            log = security_logger if closure is TOO_MANY_MESSAGES else logger
            log.log(level, "Closed a WebSocket (%d): %s", closure.code, closure.reason)
        self.tasks.cancel_scope.cancel()

    async def _close(self) -> None:
        """Send the close frame of a connection the limits ended, if one can go."""
        closure = self.closure
        if closure is None or closure is GONE or self.closing:
            return
        message = {
            "type": "websocket.close",
            "code": closure.code,
            "reason": closure.reason,
        }
        with anyio.move_on_after(CLOSE_TIMEOUT, shield=True), suppress(OSError):
            await self._send(message)


def deployment_warnings(app: FastAPI) -> list[str]:
    """Weak WebSocket choices in staging and production, for ``check``.

    Only an application that serves WebSockets is warned.
    """
    if settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS or not serves_websockets(
        app.routes
    ):
        return []
    warnings = []
    if MIDDLEWARE not in settings.MIDDLEWARE:
        warnings.append(
            f"WebSockets are served without {MIDDLEWARE}: no caps, send queue, "
            "message rate, idle or lifetime limits, or re-authorization. List it "
            "in MIDDLEWARE."
        )
    return warnings


def server_warnings(app: FastAPI) -> list[str]:
    """What the server reads past ``MAX_REQUEST_BODY_SIZE``, for WebSocket apps.

    The server is found from this process's command line and environment
    (``domainkit.http.servers``). An application without WebSocket routes
    never reads a message, so it is not warned. A failure to tell is
    logged, never raised: a warning must not stop the application starting.
    """
    if not serves_websockets(app.routes):
        return []
    try:
        limits = server_limits()
    except Exception:
        logger.exception("Could not tell which server runs the application")
        return []
    return limit_warnings(settings.MAX_REQUEST_BODY_SIZE, limits)


def serves_websockets(routes: Sequence[BaseRoute]) -> bool:
    """Whether ``routes``, with included routers and mounts, take WebSockets."""
    for context in iter_route_contexts(routes):
        route = context.original_route
        if isinstance(route, WebSocketRoute):  # FastAPI's too.
            return True
        if isinstance(route, Mount | Host) and serves_websockets(route.routes):
            return True
    return False
