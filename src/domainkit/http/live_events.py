"""Live committed events: domain events streamed to the clients that may see them.

``live_events`` hears every event the global ``event_bus`` publishes after a
commit, and hands it to the open connections that subscribed to its type
and whose access rule allows it. Over Server-Sent Events, or a WebSocket::

    @router.get(
        "/orders/events",
        dependencies=[Depends(Require(HasPermissions("shop.view_order")))],
    )
    async def order_events(request: Request) -> EventSourceResponse:
        return live_events.stream(request, OrderShipped, rule=IsCustomer())

    @router.websocket(
        "/orders/live",
        dependencies=[Depends(Require(HasPermissions("shop.view_order")))],
    )
    async def order_socket(websocket: WebSocket) -> None:
        await live_events.forward(websocket, OrderShipped, rule=IsCustomer())

The route's own rule decides who may connect; ``rule`` decides, event by
event, what each one sees: both ``has_permission`` and
``has_object_permission`` (with the event) must allow it, for the user as
of the last re-check (``LIVE_REAUTH_INTERVAL``). Pass ``AllowAny()`` to
send every event of those types to whoever may connect.

Each event goes out as its type's name, its ``event_id``, and its fields
as JSON, but those marked ``Personal``; ``render`` chooses other fields.
Each connection buffers at most ``LIVE_SEND_QUEUE`` events: one that falls
further behind is closed as too slow (1013), and reconnects.

Events reach the connections of this process only. Behind several
replicas, a client sees the events committed by the replica it is
connected to (ADR 0057).
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from functools import cache
from typing import Any, Final, Protocol

import anyio
from pydantic import TypeAdapter
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.ddd.personal import personal_fields
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.live import TOO_SLOW, Closed, Closure, send_queue
from domainkit.http.sse import EventSourceResponse, ServerSentEvent


class EventRule(Protocol):
    """An access rule (``domainkit.contrib.auth.access``) that sees each event."""

    async def has_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - the rule's user type.
    ) -> bool:
        """Whether ``user`` may see events at all."""
        ...

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - the rule's user type.
        obj: object,
    ) -> bool:
        """Whether ``user`` may see the event ``obj``."""
        ...


type Render = Callable[[DomainEvent], Mapping[str, Any]]
"""What a client is sent of an event, as JSON values."""


@cache
def _adapter(event_type: type[DomainEvent]) -> TypeAdapter[Any]:
    """Serializes events of ``event_type``."""
    return TypeAdapter(event_type)


@cache
def _personal(event_type: type[DomainEvent]) -> frozenset[str]:
    """The fields of ``event_type`` marked ``Personal``."""
    return frozenset(personal_fields(event_type))


def payload(event: DomainEvent) -> dict[str, Any]:
    """``event``'s fields as JSON values, but those marked ``Personal``."""
    event_type = type(event)
    data: dict[str, Any] = _adapter(event_type).dump_python(
        event, mode="json", exclude=set(_personal(event_type))
    )
    return data


class _Subscriber:
    """One connection's subscription: a bounded buffer, read in its own task."""

    def __init__(
        self,
        connection: HTTPConnection,
        types: tuple[type[DomainEvent], ...],
        rule: EventRule,
    ) -> None:
        """Buffer ``connection``'s events of ``types`` that ``rule`` may allow."""
        self.connection = connection
        self.types = types
        self.rule = rule
        self.size = send_queue(connection.scope)
        self.loop = asyncio.get_running_loop()
        self.buffer: deque[DomainEvent] = deque()
        self.overflowed = False
        self.ready = asyncio.Event()

    def offer(self, event: DomainEvent) -> None:
        """Buffer ``event`` if it is of a subscribed type, in the subscriber's loop."""
        if not isinstance(event, self.types):
            return
        if asyncio.get_running_loop() is self.loop:
            self._put(event)
            return
        with suppress(RuntimeError):  # The subscriber's loop has closed.
            self.loop.call_soon_threadsafe(self._put, event)

    def _put(self, event: DomainEvent) -> None:
        """Buffer ``event``; a full buffer marks the subscriber too slow."""
        if len(self.buffer) < self.size:
            self.buffer.append(event)
        else:
            self.overflowed = True
        self.ready.set()

    async def events(self) -> AsyncIterator[DomainEvent]:
        """The buffered events the rule allows, as they come.

        Raises:
            Closed: The subscriber fell too far behind (``TOO_SLOW``).
        """
        while True:
            if self.overflowed:
                raise Closed(TOO_SLOW)
            if not self.buffer:
                self.ready.clear()
                await self.ready.wait()
                continue
            event = self.buffer.popleft()
            if await self._allowed(event):
                yield event

    async def _allowed(self, event: DomainEvent) -> bool:
        """Whether the rule lets the connection's user see ``event``."""
        connection, rule = self.connection, self.rule
        user = connection.scope["user"]
        return await rule.has_permission(
            connection, user
        ) and await rule.has_object_permission(connection, user, event)


class LiveEvents:
    """Hands committed events to the connections subscribed to them."""

    def __init__(self, bus: EventBus | None = None) -> None:
        """Hear the events of ``bus`` (default: the global ``event_bus``)."""
        self.bus = event_bus if bus is None else bus
        self._subscribers: set[_Subscriber] = set()

    async def publish(self, event: DomainEvent) -> None:
        """Offer a committed event to every subscriber; never waits, never fails."""
        for subscriber in tuple(self._subscribers):
            subscriber.offer(event)

    @asynccontextmanager
    async def subscribe(
        self,
        connection: HTTPConnection,
        *types: type[DomainEvent],
        rule: EventRule,
    ) -> AsyncIterator[AsyncIterator[DomainEvent]]:
        """The events of ``types`` that ``rule`` lets ``connection`` see.

        ``async with live_events.subscribe(websocket, OrderShipped,
        rule=IsCustomer()) as events: async for event in events: ...``.
        Events committed while the block runs are heard; none before.

        Raises:
            ImproperlyConfigured: No types, or no ``AuthenticationMiddleware``.
        """
        _check(connection, types)
        self.bus.subscribe(DomainEvent, self.publish)
        subscriber = _Subscriber(connection, types, rule)
        self._subscribers.add(subscriber)
        try:
            yield subscriber.events()
        finally:
            self._subscribers.discard(subscriber)

    def stream(
        self,
        request: HTTPConnection,
        *types: type[DomainEvent],
        rule: EventRule,
        render: Render = payload,
        retry: int | None = None,
    ) -> EventSourceResponse:
        """An event stream of the events of ``types`` that ``rule`` allows.

        Each is an event named after its type, with its ``event_id`` as the
        id and ``render(event)`` as JSON data. Clients that reconnect miss
        what was committed meanwhile; a route that must not lose events
        replays them from ``last_event_id(request)`` first.

        Raises:
            ImproperlyConfigured: No types, or no ``AuthenticationMiddleware``.
        """
        _check(request, types)
        return EventSourceResponse(
            self._server_sent(request, types, rule, render), retry=retry
        )

    async def _server_sent(
        self,
        request: HTTPConnection,
        types: tuple[type[DomainEvent], ...],
        rule: EventRule,
        render: Render,
    ) -> AsyncIterator[ServerSentEvent]:
        """The subscription's events as Server-Sent Events."""
        async with self.subscribe(request, *types, rule=rule) as events:
            async for event in events:
                yield ServerSentEvent.json(
                    render(event), event=type(event).__name__, id=str(event.event_id)
                )

    async def forward(
        self,
        websocket: WebSocket,
        *types: type[DomainEvent],
        rule: EventRule,
        render: Render = payload,
    ) -> None:
        """Send ``websocket`` the events of ``types`` that ``rule`` allows.

        It accepts the connection if the route has not, then sends each
        event as JSON (``{"event": name, "id": event_id, "data": ...}``)
        until the client leaves. What the client sends is read and dropped.

        Raises:
            ImproperlyConfigured: No types, or no ``AuthenticationMiddleware``.
        """
        async with self.subscribe(websocket, *types, rule=rule) as events:
            if websocket.application_state is WebSocketState.CONNECTING:
                await websocket.accept()
            closure: Closure | None = None
            error: Exception | None = None
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(_until_disconnect, websocket, tasks.cancel_scope)
                try:
                    closure = await _send_all(websocket, events, render)
                except Exception as exc:  # noqa: BLE001 - raised below.
                    error = exc
                tasks.cancel_scope.cancel()
            if error is not None:
                raise error
            if closure is not None:
                await websocket.close(closure.code, closure.reason)


async def _send_all(
    websocket: WebSocket, events: AsyncIterator[DomainEvent], render: Render
) -> Closure | None:
    """Send ``events`` as JSON; the closure if the subscription ended the socket.

    A client that left while an event was on its way ends it quietly.
    """
    with suppress(WebSocketDisconnect):
        try:
            async for event in events:
                await websocket.send_json(
                    {
                        "event": type(event).__name__,
                        "id": str(event.event_id),
                        "data": render(event),
                    }
                )
        except Closed as closed:
            return closed.closure
    return None


def _check(connection: HTTPConnection, types: tuple[type[DomainEvent], ...]) -> None:
    """Refuse a subscription to nothing, or one nobody can be checked for.

    Raises:
        ImproperlyConfigured: No types, or no ``AuthenticationMiddleware``.
    """
    if not types:
        raise ImproperlyConfigured("Name the event types to subscribe to.")
    if "user" not in connection.scope:
        raise ImproperlyConfigured(
            "Live events are filtered per user: add domainkit.contrib.auth."
            "middleware.AuthenticationMiddleware to MIDDLEWARE."
        )


async def _until_disconnect(websocket: WebSocket, scope: anyio.CancelScope) -> None:
    """Read and drop what the client sends; cancel ``scope`` once it leaves."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass
    scope.cancel()


live_events: Final = LiveEvents()
"""The process's live events, fed by the global ``event_bus``."""
