"""Server-Sent Events: a response that streams events to a browser's ``EventSource``.

A route returns an ``EventSourceResponse`` over an async iterator of
events. Guard it with an access rule like any route::

    @router.get("/prices", dependencies=[Depends(Require())])
    async def prices(request: Request) -> EventSourceResponse:
        async def events() -> AsyncIterator[ServerSentEvent]:
            async for price in price_feed(after=last_event_id(request)):
                yield ServerSentEvent.json(price, event="price", id=str(price.id))

        return EventSourceResponse(events())

The stream is bounded as a WebSocket is (``domainkit.http.live``): it takes
a slot under ``LIVE_MAX_CONNECTIONS`` and ``LIVE_MAX_CONNECTIONS_PER_CALLER``
(or gets 503 with ``Retry-After``); at most ``LIVE_SEND_QUEUE`` events wait
for a client, which is dropped if one takes over ``LIVE_SEND_TIMEOUT``
seconds to receive; it ends after ``SSE_MAX_LIFETIME`` seconds, and when
the credentials or the route's rules no longer hold. It sends a comment
after ``SSE_KEEPALIVE`` seconds of silence, so proxies keep it open, and
stops as soon as the client disconnects. A stream the framework ends
finishes with a comment that says why; the browser reconnects after
``retry`` milliseconds, sending ``Last-Event-ID``.
"""

import json
import logging
import re
from collections.abc import AsyncIterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Self

import anyio
from pydantic_core import to_jsonable_python
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from domainkit.http.live import (
    GONE,
    LONG_LIVED,
    NORMAL_ENDS,
    Closed,
    Closure,
    LiveConnection,
    caller_of,
    refuse,
    slots_for,
    stream_limits,
)

logger = logging.getLogger(__name__)

MEDIA_TYPE: Final = "text/event-stream"
MAX_EVENT_ID: Final = 256
"""Longest ``Last-Event-ID`` read back; a longer one is ignored."""
CLOSE_TIMEOUT: Final = 5.0
"""Seconds the end of a stream, and of its source, may take."""
KEEPALIVE: Final = b": keep-alive\n\n"
_LINE_BREAK: Final = re.compile(r"\r\n|\r|\n")
"""What ends a line in an event stream: CRLF, CR, or LF."""


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    """One event of a stream: its data, and optionally a type, id, and retry.

    ``data`` may hold several lines; each becomes a ``data:`` line. The
    ``event`` type and the ``id`` cannot hold line breaks, which would
    start another field.
    """

    data: str = ""
    event: str | None = None
    """The event's type; the browser dispatches it under this name."""
    id: str | None = None
    """Sent back by a reconnecting browser in ``Last-Event-ID``."""
    retry: int | None = None
    """Milliseconds the browser waits before reconnecting."""

    def __post_init__(self) -> None:
        """Refuse fields that would break the stream.

        Raises:
            ValueError: A type or id with a line break, an id with NUL, or a
                negative retry.
        """
        for name, value in (("event", self.event), ("id", self.id)):
            if value is not None and _LINE_BREAK.search(value):
                raise ValueError(f"A server-sent event's {name} has a line break.")
        if self.id is not None and "\0" in self.id:
            raise ValueError("A server-sent event's id has a NUL character.")
        if self.retry is not None and self.retry < 0:
            raise ValueError("A server-sent event's retry cannot be negative.")

    @classmethod
    def json(
        cls,
        value: object,
        *,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
    ) -> Self:
        """An event whose data is ``value`` in JSON (UUIDs, dates as text)."""
        data = json.dumps(
            to_jsonable_python(value), separators=(",", ":"), ensure_ascii=False
        )
        return cls(data, event=event, id=id, retry=retry)

    def encode(self) -> bytes:
        """The event as it goes on the wire, in UTF-8."""
        lines = [
            f"{name}: {value}"
            for name, value in (
                ("event", self.event),
                ("id", self.id),
                ("retry", self.retry),
            )
            if value is not None
        ]
        lines += [f"data: {line}" for line in _LINE_BREAK.split(self.data)]
        return ("\n".join(lines) + "\n\n").encode()


def comment(text: str) -> bytes:
    """``text`` as a comment, which ``EventSource`` ignores."""
    return "".join(f": {line}\n" for line in _LINE_BREAK.split(text)).encode() + b"\n"


def last_event_id(connection: HTTPConnection) -> str | None:
    """The id of the last event a reconnecting client saw (``Last-Event-ID``).

    None when the header is absent, empty, longer than ``MAX_EVENT_ID``, or
    holds control characters.
    """
    value = connection.headers.get("last-event-id")
    if not value or len(value) > MAX_EVENT_ID or not value.isprintable():
        return None
    return value


type Event = ServerSentEvent | str
"""What a stream yields: an event, or text sent as one event's data."""


class EventSourceResponse(Response):
    """Stream ``events`` as Server-Sent Events (``text/event-stream``).

    ``retry`` (milliseconds) is sent first, and tells the browser how long
    to wait before it reconnects. A source may end the stream by raising
    ``Closed``, saying why. ``headers`` replace the defaults of the same
    name, whatever their case. Background tasks run once the stream ends.
    """

    media_type = MEDIA_TYPE

    def __init__(
        self,
        events: AsyncIterable[Event],
        *,
        retry: int | None = None,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Stream ``events`` with ``status_code`` and ``headers``."""
        if retry is not None and retry < 0:
            raise ValueError("retry cannot be negative.")
        self.events = events
        self.retry = retry
        self.status_code = status_code
        self.background = None
        given = {name.lower(): value for name, value in (headers or {}).items()}
        self.init_headers(
            {"cache-control": "no-store", "x-accel-buffering": "no", **given}
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream the events, if a slot is free, until a side ends the stream."""
        scope[LONG_LIVED] = True
        slots = slots_for(scope)
        caller = caller_of(scope)
        if (cap := slots.acquire(caller)) is not None:
            await refuse(scope, receive, send, cap, slots=slots)
        else:
            try:
                await _Stream(self, scope, receive, send).serve()
            finally:
                slots.release(caller)
        if self.background is not None:
            await self.background()


class _Stream:
    """One event stream: its queue, its clocks, and how it ended."""

    closure: Closure | None = None
    """Why the stream was ended early, if it was."""

    def __init__(
        self, response: EventSourceResponse, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Stream ``response`` to the client of ``scope``."""
        self.response = response
        self._receive = receive
        self._send = send
        limits = stream_limits(scope)
        self.connection = LiveConnection(scope, limits)
        self.outbox, self.queued = anyio.create_memory_object_stream[bytes](
            limits.send_queue
        )
        self.drained = anyio.Event()

    async def serve(self) -> None:
        """Start the response, stream until it ends, then finish it.

        Errors of the source are raised as they are, once the stream's
        tasks have stopped. A client gone before the start is not streamed to.
        """
        start: Message = {
            "type": "http.response.start",
            "status": self.response.status_code,
            "headers": self.response.raw_headers,
        }
        error: Exception | None = None
        try:
            await self._send(start)
        except OSError:
            await _close_source(self.response.events)
            return
        else:
            async with anyio.create_task_group() as self.tasks:
                self.tasks.start_soon(self._listen)
                self.tasks.start_soon(self._write)
                self.tasks.start_soon(self.connection.watch, self.end, self._keepalive)
                try:
                    await self._produce()
                except Exception as exc:  # noqa: BLE001 - raised once tasks stop.
                    error = exc
                self.tasks.cancel_scope.cancel()
        finally:
            self.outbox.close()
            self.queued.close()
        if error is not None:
            raise error
        await self._finish()

    async def _produce(self) -> None:
        """Queue the source's events, waiting while the queue is full.

        Then let the queue drain: a source that ended the stream (``Closed``)
        still has the events it gave sent first.
        """
        events = self.response.events
        try:
            if self.response.retry is not None:
                await self.outbox.send(f"retry: {self.response.retry}\n\n".encode())
            async for item in events:
                event = (
                    item if isinstance(item, ServerSentEvent) else ServerSentEvent(item)
                )
                await self.outbox.send(event.encode())
        except Closed as closed:
            self._record(closed.closure)
        finally:
            await _close_source(events)
        self.outbox.close()
        await self.drained.wait()

    async def _write(self) -> None:
        """Send the queued events, each within the send timeout."""
        try:
            async with self.queued:
                async for chunk in self.queued:
                    message: Message = {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                    closure = await self.connection.deliver(self._send, message)
                    if closure is not None:
                        self.end(closure)
                        return
        finally:
            self.drained.set()

    async def _listen(self) -> None:
        """End the stream when the client disconnects, or sends what is refused.

        A body over ``MAX_REQUEST_BODY_SIZE``, say, makes ``receive`` raise.
        """
        try:
            while (await self._receive())["type"] != "http.disconnect":
                pass
        except Exception:
            logger.warning("Ended an event stream whose request failed", exc_info=True)
        self.end(GONE)

    def _keepalive(self) -> None:
        """Queue a keep-alive comment, unless events already wait."""
        with suppress(anyio.WouldBlock, anyio.ClosedResourceError):
            self.outbox.send_nowait(KEEPALIVE)

    def end(self, closure: Closure) -> None:
        """End the stream with ``closure`` now: stop the source and the queue."""
        self._record(closure)
        self.tasks.cancel_scope.cancel()

    def _record(self, closure: Closure) -> None:
        """Keep and log why the stream ends, unless it was ending already."""
        if self.closure is None:
            self.closure = closure
            level = logging.INFO if closure in NORMAL_ENDS else logging.WARNING
            logger.log(level, "Ended an event stream: %s", closure.reason)

    async def _finish(self) -> None:
        """Send the end of the response, with why it ended early, if it did."""
        closure = self.closure
        if closure is GONE:
            return
        body = b"" if closure is None else comment(closure.reason)
        message: Message = {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
        with anyio.move_on_after(CLOSE_TIMEOUT, shield=True), suppress(OSError):
            await self._send(message)


async def _close_source(events: AsyncIterable[Event]) -> None:
    """Close an async generator source, so its ``finally`` blocks run now."""
    aclose = getattr(events, "aclose", None)
    if aclose is None:
        return
    with anyio.move_on_after(CLOSE_TIMEOUT, shield=True):
        try:
            await aclose()
        except Exception:
            logger.exception("Closing an event stream's source failed")
