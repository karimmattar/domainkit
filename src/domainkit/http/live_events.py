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
send every event of those types to whoever may connect. The principal is
read as ``current_user`` reads it: credentials limited to scopes are
refused on a route that declares no rule that checks scopes
(``SCOPED_CREDENTIALS``), a ``Public`` one included, before any event.

Each event goes out as its type's name, its ``event_id``, and its fields
as JSON, but its ``actor`` and those marked ``Personal``; ``render``
chooses other fields.
Each connection buffers at most ``LIVE_SEND_QUEUE`` events its rule allows:
one that falls further behind is closed as too slow (1013), and
reconnects. Events it may not see never count against it, however many
one commit publishes; they wait for the rule in a buffer of their own,
``INTAKE_FACTOR`` times as large, so memory stays bounded.

Events reach the connections of this process, unless ``LIVE_FANOUT``
names a backend (ADR 0143): then the event types a domain shares in its
``ready()`` (``live_events.share(OrderShipped)``) reach the connections
of every process, whichever committed them, the worker included.
Without it, a client behind several replicas sees the events committed
by the replica it is connected to (ADR 0057).
"""

import asyncio
import json
import logging
import threading
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from functools import cache
from typing import Any, Final, Protocol
from weakref import WeakKeyDictionary

import anyio
from pydantic import TypeAdapter, ValidationError
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS, LiveFanoutConfig
from domainkit.db.uow import MESSAGING
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.ddd.events import inherits_actor
from domainkit.ddd.personal import personal_fields
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import check_scopes_bounded, current_user
from domainkit.http.live import TOO_SLOW, Closed, Closure, live_routes, send_queue
from domainkit.http.live_fanout import Sender, backend
from domainkit.http.servers import server_processes
from domainkit.http.sse import EventSourceResponse, ServerSentEvent
from domainkit.tasks import supervise

__all__ = [
    "fanout_warnings",
    "live_events",
]

logger = logging.getLogger(__name__)

INTAKE_FACTOR: Final = 16
"""Events of its types, allowed or not, that a connection may hold before
its rule has seen them: this many times its send queue. One more is too
slow too, as events would be lost unchecked."""


class EventRule(Protocol):
    """An access rule (``domainkit.http.access``) that sees each event."""

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
def _withheld(event_type: type[DomainEvent]) -> frozenset[str]:
    """The fields of ``event_type`` marked ``Personal``, and its ``actor``.

    Who acted (ADR 0147) is not a client's to see: which staff member, or
    that staff act as the user.
    """
    actor = {"actor"} if inherits_actor(event_type) else set()
    return frozenset(personal_fields(event_type)) | actor


def payload(event: DomainEvent) -> dict[str, Any]:
    """``event``'s fields as JSON values, but its actor and its ``Personal`` ones."""
    event_type = type(event)
    data: dict[str, Any] = _adapter(event_type).dump_python(
        event, mode="json", exclude=set(_withheld(event_type))
    )
    return data


class _Subscriber:
    """One connection's subscription: bounded buffers, read in its own task.

    Events of the subscribed types wait in ``intake`` until the reader asks
    for the next one; it then checks all that came against the rule, and
    keeps those allowed in ``allowed``. Too slow is judged on what the
    connection may see: more than ``size`` allowed events waiting. The
    intake has a bound of its own, ``INTAKE_FACTOR`` times larger.
    """

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
        """Allowed events that may wait for the reader; one more is too slow."""
        self.intake_size = self.size * INTAKE_FACTOR
        """Events that may wait for the rule; one more is too slow."""
        self.loop = asyncio.get_running_loop()
        self.intake: deque[DomainEvent] = deque()
        """Events of the subscribed types the rule has not seen yet."""
        self.allowed: deque[DomainEvent] = deque()
        """Events the rule allowed, not yet taken by the reader."""
        self.overflowed = False
        """Whether the intake was full, so events were lost unchecked."""
        self.ready = asyncio.Event()

    def offer(self, event: DomainEvent) -> None:
        """Take ``event`` in if it is of a subscribed type, in the subscriber's loop."""
        if not isinstance(event, self.types):
            return
        if asyncio.get_running_loop() is self.loop:
            self._put(event)
            return
        with suppress(RuntimeError):  # The subscriber's loop has closed.
            self.loop.call_soon_threadsafe(self._put, event)

    def _put(self, event: DomainEvent) -> None:
        """Take ``event`` in; a full intake marks the subscriber too slow."""
        if len(self.intake) < self.intake_size:
            self.intake.append(event)
        else:
            self.overflowed = True
        self.ready.set()

    async def events(self) -> AsyncIterator[DomainEvent]:
        """The events the rule allows, in order, as they come.

        Raises:
            Closed: The subscriber fell too far behind (``TOO_SLOW``).
        """
        while True:
            await self._check_intake()
            if self.allowed:
                yield self.allowed.popleft()
            elif not self.intake:  # Events that came during the checks go next.
                self.ready.clear()
                await self.ready.wait()

    async def _check_intake(self) -> None:
        """Check the events taken in so far, and keep those the rule allows.

        Raises:
            Closed: ``TOO_SLOW``: the intake overflowed, or more than ``size``
                allowed events wait.
        """
        if self.overflowed:
            raise Closed(TOO_SLOW)
        # Only those here now, so a steady stream cannot hold the next one back.
        for _ in range(len(self.intake)):
            event = self.intake.popleft()
            if await self._allowed(event):
                self.allowed.append(event)
                if len(self.allowed) > self.size:
                    raise Closed(TOO_SLOW)

    async def _allowed(self, event: DomainEvent) -> bool:
        """Whether the rule lets the connection's user see ``event``.

        The user is read through ``current_user``, which holds scoped
        credentials to the route's rules (decided once, at ``subscribe``).
        """
        connection, rule = self.connection, self.rule
        user = current_user(connection)
        return await rule.has_permission(
            connection, user
        ) and await rule.has_object_permission(connection, user, event)


def _key(event_type: type[DomainEvent]) -> str:
    """How processes name a shared event type to each other: ``module.QualName``."""
    return f"{event_type.__module__}.{event_type.__qualname__}"


class LiveEvents:
    """Hands committed events to the connections subscribed to them."""

    def __init__(self, bus: EventBus | None = None) -> None:
        """Hear the events of ``bus`` (default: the global ``event_bus``)."""
        self.bus = event_bus if bus is None else bus
        self.origin = uuid.uuid4().hex
        """Marks what this process sends, so it never hears its own events twice."""
        self._subscribers: set[_Subscriber] = set()
        self._lock = threading.Lock()
        """Guards the subscribers: commits in other threads publish too."""
        self._shared: dict[str, type[DomainEvent]] = {}
        self._senders: WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[LiveFanoutConfig, Sender]
        ] = WeakKeyDictionary()
        self._listeners: WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[LiveFanoutConfig, asyncio.Task[None]]
        ] = WeakKeyDictionary()
        self._unshared_told: set[type[DomainEvent]] = set()

    @property
    def shared(self) -> tuple[type[DomainEvent], ...]:
        """The event types that go to every process (``share``)."""
        return tuple(self._shared.values())

    def share(self, *types: type[DomainEvent]) -> None:
        """Send these committed events to every process's subscribers (ADR 0143).

        Call it in ``DomainConfig.ready()``: every process loads the same
        domains, so each knows what it sends and what it reads back. Only
        the types named go, not their subclasses. It does nothing more
        until ``LIVE_FANOUT`` names a backend.

        Raises:
            ImproperlyConfigured: Two types of one ``module.QualName``.
        """
        for event_type in types:
            key = _key(event_type)
            known = self._shared.get(key)
            if known is not None and known is not event_type:
                raise ImproperlyConfigured(
                    f"Two event types are named {key!r}: processes could not "
                    "tell them apart."
                )
            self._shared[key] = event_type
        # Heard again at each share: the bus may be cleared (tests restore it).
        self.bus.subscribe(DomainEvent, self._send_out)

    async def publish(self, event: DomainEvent) -> None:
        """Offer a committed event to every subscriber; never waits, never fails.

        It then yields to the event loop once, so subscribers in this loop
        take each event of a commit as it comes, not all of them at once.
        """
        if self._offer(event):
            await asyncio.sleep(0)

    def _offer(
        self, event: DomainEvent, loop: asyncio.AbstractEventLoop | None = None
    ) -> bool:
        """Offer ``event`` to the subscribers (of ``loop``); whether there were any."""
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            if loop is None or subscriber.loop is loop:
                subscriber.offer(event)
        return bool(subscribers)

    async def _send_out(self, event: DomainEvent) -> None:
        """Hand a committed shared event to the loop's sender (``LIVE_FANOUT``)."""
        event_type = type(event)
        key = _key(event_type)
        if self._shared.get(key) is not event_type:
            return
        config = settings.LIVE_FANOUT
        fanout = backend()
        if fanout is None:
            return
        data = _adapter(event_type).dump_python(event, mode="json")
        message = json.dumps(
            {"o": self.origin, "t": key, "d": data}, separators=(",", ":")
        )
        senders = self._senders.setdefault(asyncio.get_running_loop(), {})
        sender = senders.get(config)
        if sender is None:
            sender = senders[config] = Sender(fanout)
        sender.put(message, event_type.__name__)

    def _listen(self, types: tuple[type[DomainEvent], ...]) -> None:
        """Hear the other processes' events in this loop, once (``LIVE_FANOUT``)."""
        config = settings.LIVE_FANOUT
        fanout = backend()
        if fanout is None:
            return
        for event_type in types:
            if event_type not in self._unshared_told and (
                self._shared.get(_key(event_type)) is not event_type
            ):
                self._unshared_told.add(event_type)
                logger.warning(
                    "Live events of %s reach the commits of this process only: "
                    "share them in a domain's ready() (live_events.share)",
                    event_type.__qualname__,
                )
        loop = asyncio.get_running_loop()
        listening = self._listeners.setdefault(loop, {})
        task = listening.get(config)
        if task is None or task.done():

            def heard(message: str) -> None:
                self._heard(message, loop)

            listening[config] = supervise(
                lambda: fanout.listen(heard), name="domainkit.live.listen"
            )

    def _heard(self, message: str, loop: asyncio.AbstractEventLoop) -> None:
        """Offer an event another process sent to ``loop``'s subscribers."""
        try:
            sent = json.loads(message)
            if sent.get("o") == self.origin:
                return  # Its subscribers had it when it committed.
            event_type = self._shared[sent["t"]]
            event = _adapter(event_type).validate_python(sent["d"])
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            ValidationError,
        ) as exc:
            logger.warning(
                "Dropped a live event another process sent: this one cannot read "
                "it (%s: %s)",
                type(exc).__name__,
                exc,
            )
            return
        self._offer(event, loop)

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
            PermissionDenied: Credentials limited to scopes, on a route that
                declares no rule that checks scopes (``SCOPED_CREDENTIALS =
                "refuse"``); the refusal is audited.
        """
        _check(connection, types)
        await check_scopes_bounded(connection)
        subscriber = _Subscriber(connection, types, rule)
        with self._lock:
            # Hear the bus, once: it ignores repeats. Not just at the first
            # subscription, as a bus may be cleared (tests restore theirs).
            self.bus.subscribe(DomainEvent, self.publish)
            self._subscribers.add(subscriber)
        self._listen(types)
        try:
            yield subscriber.events()
        finally:
            with self._lock:
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
            PermissionDenied: Credentials limited to scopes, on a route that
                declares no rule that checks scopes: refused here, with 403,
                before the stream starts.
        """
        _check(request, types)
        current_user(request)  # Scoped credentials, before the 200 is sent.
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
            PermissionDenied: Credentials limited to scopes, on a route that
                declares no rule that checks scopes, before it accepts.
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


def fanout_warnings(routes: Sequence[BaseRoute] = ()) -> list[str]:
    """``live.W003``: events that stay in process where several commit them.

    Where deployed, with no ``LIVE_FANOUT``: domains share event types, so
    that every process's clients see them; or, among ``routes``, live
    routes (WebSockets, event streams) are served while more than one
    process commits events: the server's worker processes
    (``servers.server_processes``), or the worker of
    ``domainkit.contrib.messaging``. A client then sees only the events its
    own process committed (user report L1). One process alone, the server
    not found, or no live route is not warned. A failure to tell is logged,
    never raised: the application logs these when it starts.
    """
    if (
        settings.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS
        or settings.LIVE_FANOUT.BACKEND is not None
    ):
        return []
    if live_events.shared:
        names = ", ".join(sorted(each.__qualname__ for each in live_events.shared))
        warning = (
            f"Domains share live events ({names}), and LIVE_FANOUT names no "
            "backend: a client sees only those its own process committed, and "
            'none the worker did. Set LIVE_FANOUT = {"BACKEND": "postgres"} (or '
            '"redis").'
        )
        return [warning]
    served = live_routes(routes)
    committers = _committers() if served else []
    if not committers:
        return []
    shown = ", ".join(served[:3])
    if len(served) > 3:
        shown += f", and {len(served) - 3} more"
    warning = (
        f"Live routes ({shown}) are served, events are committed by "
        f"{' and by '.join(committers)}, and LIVE_FANOUT names no backend: a "
        "client sees only the events its own process committed. Share the "
        "types clients watch (live_events.share) and set LIVE_FANOUT = "
        '{"BACKEND": "postgres"} (or "redis"); silence live.W003 if these routes '
        "stream no committed events."
    )
    return [warning]


def _committers() -> list[str]:
    """What commits events in processes other than a client's, if anything."""
    found = []
    try:
        processes = server_processes()
    except Exception:
        logger.exception("Could not tell how many processes the server starts")
        processes = None
    if processes is not None and processes > 1:
        found.append(f"the server's {processes} worker processes")
    if registry.is_installed(MESSAGING):
        found.append("the messaging worker")
    return found
