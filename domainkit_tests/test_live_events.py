"""Live committed events, and open connections re-authorized against the database."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, Final, cast
from uuid import UUID, uuid4

import anyio
import pytest
from starlette.datastructures import State
from starlette.requests import HTTPConnection
from starlette.types import Message, Scope
from starlette.websockets import WebSocket, WebSocketDisconnect

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth import (
    AbstractBaseUser,
    AllowAny,
    AuthenticationFailed,
    AuthUnitOfWork,
    BaseAuthentication,
    BasePermission,
    Credentials,
)
from domainkit.contrib.auth.events import UserCreated, UserPermissionGranted
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application, live
from domainkit.http.live import TOO_SLOW, Closed
from domainkit.http.live_events import (
    EventRule,
    LiveEvents,
    _send_all,
    _Subscriber,
    live_events,
    payload,
)
from domainkit.testing import TestClient
from domainkit.testing.clients import CarryingPortal
from domainkit_tests.conftest import FAST_HASHER, UseAuth
from domainkit_tests.sample_live_routes import VIEW_PRODUCT, OwnEvents

ROUTES: Final = "domainkit_tests.sample_live_routes.router"
LIVE_MIDDLEWARE: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]


class Someone:
    """A user who logged in, as the access rules see one."""

    is_authenticated = True
    is_active = True

    def __init__(self) -> None:
        """A user with an id of its own."""
        self.id = uuid4()


def connection(user: object | None = None) -> HTTPConnection:
    """A request from ``user``, through the authentication middleware."""
    return HTTPConnection({"type": "http", "headers": [], "user": user or Someone()})


def granted(
    user_id: UUID, permission: str = "shop.view_product"
) -> UserPermissionGranted:
    """A committed grant of ``permission`` to ``user_id``."""
    return UserPermissionGranted(user_id=user_id, permission=permission)


async def take(events: AsyncIterator[DomainEvent], count: int) -> list[DomainEvent]:
    """The next ``count`` events."""
    return [await anext(events) for _ in range(count)]


# The hub -----------------------------------------------------------------------------


def test_events_go_out_without_personal_fields() -> None:
    """A field marked ``Personal`` stays home unless ``render`` sends it."""
    event = UserCreated(user_id=UUID(int=7), username="alice")

    assert payload(event) == {
        "event_id": str(event.event_id),
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
        "user_id": "00000000-0000-0000-0000-000000000007",
    }


@pytest.mark.anyio
async def test_a_subscriber_hears_its_types_and_what_its_rule_allows() -> None:
    """Committed events of its types, about itself, in order; nothing else."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    mine, theirs = granted(me.id), granted(uuid4())

    async with hub.subscribe(
        connection(me), UserPermissionGranted, rule=OwnEvents()
    ) as events:
        for event in (UserCreated(user_id=me.id, username="me"), theirs, mine):
            await bus.publish(event)
        again = granted(me.id, "shop.add_product")
        await bus.publish(again)
        assert await take(events, 2) == [mine, again]

    assert hub._subscribers == set()


class Choosy(BasePermission):
    """Allows the request, the object, both, or neither."""

    def __init__(self, request: bool, obj: bool) -> None:
        """Answer ``request`` and ``obj``."""
        self.request, self.obj = request, obj

    async def has_permission(self, request: HTTPConnection, user: Any) -> bool:  # noqa: ANN401
        """The request's answer."""
        return self.request

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401
        obj: object,
    ) -> bool:
        """The object's answer."""
        return self.obj


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("request_allowed", "event_allowed"),
    [(True, True), (True, False), (False, True), (False, False)],
)
async def test_both_halves_of_a_rule_must_allow(
    request_allowed: bool, event_allowed: bool
) -> None:
    """``has_permission`` and ``has_object_permission`` each filter events."""
    me = Someone()
    rule = Choosy(request_allowed, event_allowed)
    subscriber = _Subscriber(connection(me), (DomainEvent,), rule)

    allowed = await subscriber._allowed(granted(me.id))

    assert allowed is (request_allowed and event_allowed)


@pytest.mark.anyio
async def test_a_subscriber_that_falls_behind_is_closed_as_too_slow() -> None:
    """More than LIVE_SEND_QUEUE events waiting: the subscription ends with 1013."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()

    with override_settings(LIVE_SEND_QUEUE=2):
        async with hub.subscribe(
            connection(me), DomainEvent, rule=AllowAny()
        ) as events:
            for _ in range(3):
                await bus.publish(granted(me.id))
            with pytest.raises(Closed) as closed:
                await anext(events)

    assert closed.value.closure is TOO_SLOW
    assert str(closed.value) == "Client too slow."


def test_a_subscription_names_types_and_needs_users() -> None:
    """Nothing to hear, or nobody to check, is a configuration error."""
    hub = LiveEvents()

    with pytest.raises(ImproperlyConfigured, match="Name the event types"):
        hub.stream(connection(), rule=AllowAny())
    anonymous = HTTPConnection({"type": "http", "headers": []})
    with pytest.raises(ImproperlyConfigured, match="AuthenticationMiddleware"):
        hub.stream(anonymous, UserCreated, rule=AllowAny())


@pytest.mark.anyio
async def test_a_subscription_is_checked_however_it_is_made() -> None:
    """``subscribe`` itself refuses no types, and connections without a user."""
    hub = LiveEvents(EventBus())
    anonymous = HTTPConnection({"type": "websocket", "headers": []})

    with pytest.raises(ImproperlyConfigured, match="Name the event types"):
        async with hub.subscribe(connection(), rule=AllowAny()):
            pass
    with pytest.raises(ImproperlyConfigured, match="AuthenticationMiddleware"):
        async with hub.subscribe(anonymous, UserCreated, rule=AllowAny()):
            pass
    assert hub._subscribers == set()


@pytest.mark.anyio
async def test_subscribing_hooks_the_bus_once() -> None:
    """The hub subscribes itself to every event of its bus, once."""
    bus = EventBus()
    hub = LiveEvents(bus)

    for _ in range(2):
        async with hub.subscribe(connection(), UserCreated, rule=AllowAny()):
            pass

    assert bus.handlers_for(DomainEvent) == [hub.publish]
    assert LiveEvents().bus is event_bus


@pytest.mark.anyio
async def test_events_from_another_event_loop_are_handed_over() -> None:
    """A commit in another thread's loop reaches the subscriber in its own."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    event = granted(me.id)

    async with hub.subscribe(connection(me), DomainEvent, rule=AllowAny()) as events:
        thread = threading.Thread(target=asyncio.run, args=(bus.publish(event),))
        thread.start()
        thread.join()
        assert await anext(events) is event


def test_subscribers_of_a_closed_loop_are_skipped() -> None:
    """Offering to a subscriber whose loop has ended does nothing."""
    hub = LiveEvents(EventBus())

    async def make() -> _Subscriber:
        return _Subscriber(connection(), (DomainEvent,), AllowAny())

    stale = asyncio.run(make())
    hub._subscribers.add(stale)
    asyncio.run(hub.publish(granted(uuid4())))

    assert list(stale.buffer) == []


class Hub(LiveEvents):
    """Live events that say when a connection has subscribed."""

    def __init__(self) -> None:
        """Hear a bus of its own."""
        super().__init__(EventBus())
        self.subscribed = anyio.Event()

    @asynccontextmanager
    async def subscribe(
        self,
        connection: HTTPConnection,
        *types: type[DomainEvent],
        rule: EventRule,
    ) -> AsyncIterator[AsyncIterator[DomainEvent]]:
        """Subscribe, then say so."""
        async with super().subscribe(connection, *types, rule=rule) as events:
            self.subscribed.set()
            yield events


@pytest.mark.anyio
async def test_live_events_as_server_sent_events() -> None:
    """Named after the type, with the event id, and the rendered fields as JSON."""
    hub = Hub()
    me = Someone()
    event = granted(me.id)
    sent: list[Message] = []
    arrived = anyio.Event()
    leave = anyio.Event()

    async def receive() -> Message:
        await leave.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal arrived
        sent.append(message)
        arrived.set()
        arrived = anyio.Event()

    request = connection(me)
    request.scope["app"] = SimpleNamespace(state=State())
    response = hub.stream(
        request,
        UserPermissionGranted,
        rule=OwnEvents(),
        render=lambda e: {"permission": getattr(e, "permission", None)},
        retry=1000,
    )
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(response, request.scope, receive, send)
        await hub.subscribed.wait()
        await hub.bus.publish(event)
        while len(sent) < 3:
            await arrived.wait()
        leave.set()

    assert sent[1]["body"] == b"retry: 1000\n\n"
    assert (
        sent[2]["body"]
        == (
            f"event: UserPermissionGranted\nid: {event.event_id}\n"
            'data: {"permission":"shop.view_product"}\n\n'
        ).encode()
    )
    assert hub._subscribers == set()


# Forwarding to a WebSocket, raw ------------------------------------------------------


class Socket:
    """A WebSocket after its handshake, as a route sees it."""

    def __init__(self, user: object) -> None:
        """Connecting, from ``user``."""
        self.to_app: list[Message] = [{"type": "websocket.connect"}]
        self.more = anyio.Event()
        self.sent: list[Message] = []
        self.arrived = anyio.Event()
        self.gone = False
        scope = {"type": "websocket", "path": "/raw", "headers": [], "user": user}
        self.websocket = WebSocket(scope, self.receive, self.send)

    async def receive(self) -> Message:
        """What the client sends next."""
        while not self.to_app:
            await self.more.wait()
            self.more = anyio.Event()
        return self.to_app.pop(0)

    def push(self, message: Message) -> None:
        """Let the client send ``message``."""
        self.to_app.append(message)
        self.more.set()

    async def send(self, message: Message) -> None:
        """Take what the route sends; fail once the client is gone."""
        if self.gone and message["type"] == "websocket.send":
            raise OSError("client gone")
        self.sent.append(message)
        self.arrived.set()

    async def wait_sent(self, count: int) -> None:
        """Wait until the route has sent ``count`` messages."""
        while len(self.sent) < count:
            await self.arrived.wait()
            self.arrived = anyio.Event()


@pytest.mark.anyio
async def test_forwarded_events_are_json_messages() -> None:
    """``forward`` accepts, then sends each allowed event until the client leaves."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    socket = Socket(me)
    event = granted(me.id)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            partial(hub.forward, socket.websocket, DomainEvent, rule=AllowAny())
        )
        await socket.wait_sent(1)
        socket.push({"type": "websocket.receive", "text": "ignored"})
        await bus.publish(event)
        await socket.wait_sent(2)
        socket.push({"type": "websocket.disconnect", "code": 1000})

    assert socket.sent[0]["type"] == "websocket.accept"
    assert socket.sent[1] == {
        "type": "websocket.send",
        "text": (
            f'{{"event":"UserPermissionGranted","id":"{event.event_id}",'
            f'"data":{{"event_id":"{event.event_id}",'
            f'"occurred_at":"{event.occurred_at.isoformat().replace("+00:00", "Z")}",'
            f'"user_id":"{me.id}","permission":"shop.view_product"}}}}'
        ),
    }
    assert hub._subscribers == set()


@pytest.mark.anyio
async def test_forward_takes_over_a_socket_the_route_accepted() -> None:
    """A route may accept first; ``forward`` then does not accept again."""
    bus = EventBus()
    hub = LiveEvents(bus)
    socket = Socket(Someone())

    async def route() -> None:
        await socket.websocket.accept()
        await hub.forward(socket.websocket, DomainEvent, rule=AllowAny())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(route)
        await socket.wait_sent(1)
        socket.push({"type": "websocket.disconnect", "code": 1000})

    assert [message["type"] for message in socket.sent] == ["websocket.accept"]


@pytest.mark.anyio
async def test_a_forwarded_socket_that_falls_behind_is_closed() -> None:
    """The subscription's overflow closes the socket with 1013 and the reason."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    socket = Socket(me)

    with override_settings(LIVE_SEND_QUEUE=1):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                partial(hub.forward, socket.websocket, DomainEvent, rule=AllowAny())
            )
            await socket.wait_sent(1)
            await bus.publish_all([granted(me.id), granted(me.id)])

    assert socket.sent[-1] == {
        "type": "websocket.close",
        "code": 1013,
        "reason": "Client too slow.",
    }


@pytest.mark.anyio
async def test_a_source_that_ends_ends_the_sending() -> None:
    """Sending stops, without a closure, when its events run out."""
    me = Someone()
    socket = Socket(me)
    event = granted(me.id)

    async def two() -> AsyncIterator[DomainEvent]:
        yield event
        yield event

    await socket.websocket.accept()
    closure = await _send_all(socket.websocket, two(), lambda e: {"n": 1})

    assert closure is None
    assert [message["text"] for message in socket.sent[1:]] == [
        f'{{"event":"UserPermissionGranted","id":"{event.event_id}","data":{{"n":1}}}}'
    ] * 2


@pytest.mark.anyio
async def test_a_client_gone_ends_forwarding_quietly() -> None:
    """A send that finds the client gone ends ``forward`` without an error."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    socket = Socket(me)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            partial(hub.forward, socket.websocket, DomainEvent, rule=AllowAny())
        )
        await socket.wait_sent(1)
        socket.gone = True
        await bus.publish(granted(me.id))

    assert [message["type"] for message in socket.sent] == ["websocket.accept"]
    assert hub._subscribers == set()


@pytest.mark.anyio
async def test_forwarding_errors_are_raised_as_they_are() -> None:
    """A failing ``render`` is the route's error, not an exception group."""
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    socket = Socket(me)

    def render(event: DomainEvent) -> Mapping[str, Any]:
        raise LookupError("cannot render")

    async def forward() -> None:
        await hub.forward(socket.websocket, DomainEvent, rule=AllowAny(), render=render)

    async def fail() -> None:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(forward)
            await socket.wait_sent(1)
            await bus.publish(granted(me.id))

    with pytest.raises(ExceptionGroup) as raised:
        await fail()

    assert [type(error) for error in raised.value.exceptions] == [LookupError]


# Through the application, with a database --------------------------------------------

type MakeClient = Callable[..., TestClient]


@pytest.fixture
def live_client(auth_project: str) -> Iterator[MakeClient]:
    """Clients of the live routes, with auth, on a migrated database."""
    with (
        override_settings(ROOT_ROUTER=ROUTES, MIDDLEWARE=LIVE_MIDDLEWARE),
        ExitStack() as stack,
    ):

        def make(**auth: Any) -> TestClient:  # noqa: ANN401 - AUTH keys.
            if auth:
                options = {"PASSWORD_HASHERS": [FAST_HASHER], **auth}
                stack.enter_context(override_settings(AUTH=options))
            return stack.enter_context(TestClient(get_asgi_application()))

        yield make


def run(client: TestClient, work: Callable[[], Any]) -> Any:  # noqa: ANN401
    """Run ``work`` in ``client``'s event loop, where its connections live."""
    return CarryingPortal(client._entered()).call(work)


async def change_user(username: str, change: Callable[[Any], Any]) -> None:
    """Apply ``change`` to the stored user ``username`` and commit."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        result = change(user)
        if asyncio.iscoroutine(result):
            await result
        await uow.commit()


def grant_to(username: str, *keys: str) -> Callable[[], Any]:
    """Work that grants ``keys`` to ``username``."""

    async def work() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            user.grant(*await uow.permissions.get_many(keys))
            await uow.commit()

    return work


def until_closed(socket: Any) -> tuple[int, str]:  # noqa: ANN401 - a test session.
    """Read ``socket`` until it is closed; the code and reason."""
    try:
        while True:
            socket.receive_json()
    except WebSocketDisconnect as closed:
        return closed.code, closed.reason


def test_users_see_their_own_committed_events_live(live_client: MakeClient) -> None:
    """A WebSocket hears the events about its user, as they commit; not others'."""
    client = live_client()
    alice = client.authenticated(username="alice")
    client.authenticated(username="bob")

    with alice.websocket_connect("/live/users") as socket:
        run(client, grant_to("bob", "shop.view_product"))
        run(client, grant_to("alice", "shop.add_product"))
        first = socket.receive_json()
        run(client, grant_to("alice", "shop.change_product"))
        second = socket.receive_json()

    assert (first["event"], first["data"]["permission"]) == (
        "UserPermissionGranted",
        "shop.add_product",
    )
    assert second["data"]["permission"] == "shop.change_product"
    assert live_events._subscribers == set()


def test_everyone_may_hear_what_a_public_route_sends(live_client: MakeClient) -> None:
    """``AllowAny`` on a public route; personal fields still stay home."""
    client = live_client()

    with client.websocket_connect("/live/everything") as socket:
        client.authenticated(username="carol")
        created = socket.receive_json()
        while created["event"] != "UserCreated":
            created = socket.receive_json()

    assert created["event"] == "UserCreated"
    assert "username" not in created["data"]


def test_a_flood_of_events_closes_a_slow_socket(live_client: MakeClient) -> None:
    """More events at once than LIVE_SEND_QUEUE: closed with 1013, to reconnect."""
    client = live_client()
    alice = client.authenticated(username="alice")

    with (
        override_settings(LIVE_SEND_QUEUE=2),
        alice.websocket_connect("/live/users") as socket,
    ):
        actions = ("add", "change", "delete")
        run(client, grant_to("alice", *(f"shop.{a}_product" for a in actions)))
        closed = until_closed(socket)

    assert closed == (1013, "Client too slow.")


@pytest.mark.parametrize(
    "revoke",
    ["logout", "deactivate", "remove permission"],
)
def test_an_open_websocket_loses_what_its_user_lost(
    live_client: MakeClient, revoke: str
) -> None:
    """A revoked token, a deactivated user, or a removed permission: 1008."""
    client = live_client()
    alice = client.authenticated(VIEW_PRODUCT, username="alice")

    async def deactivate() -> None:
        await change_user("alice", lambda user: user.set_active(False))

    async def remove() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            user.revoke(*await uow.permissions.get_many([VIEW_PRODUCT]))
            await uow.commit()

    with freeze_time() as clock, alice.websocket_connect("/live/products") as socket:
        socket.send_text("allowed")
        assert socket.receive_text() == "allowed"
        clock.tick(60)
        socket.send_text("still allowed")
        assert socket.receive_text() == "still allowed"
        if revoke == "logout":
            assert alice.post("/auth/logout").status_code == 204
        else:
            run(client, deactivate if revoke == "deactivate" else remove)
        clock.tick(60)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert (closed.value.code, closed.value.reason) == (1008, "Not authorized.")


def test_an_expired_token_closes_its_websocket(live_client: MakeClient) -> None:
    """A token that expires while the socket is open ends it at the next check."""
    client = live_client(TOKEN_TTL=timedelta(minutes=10))
    alice = client.authenticated(VIEW_PRODUCT, username="alice")

    with (
        freeze_time() as clock,
        override_settings(WEBSOCKET_IDLE_TIMEOUT=3600),
        alice.websocket_connect("/live/products") as socket,
    ):
        clock.tick(timedelta(minutes=9))
        socket.send_text("fresh")
        assert socket.receive_text() == "fresh"
        clock.tick(timedelta(minutes=2))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == 1008


def test_a_recheck_reloads_the_user(
    live_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between checks the socket sees its user as loaded; after one, as stored now."""
    checked = threading.Event()
    reauthorize = live.reauthorize

    async def recorded(scope: Scope) -> bool:
        allowed = await reauthorize(scope)
        checked.set()
        return allowed

    monkeypatch.setattr(live, "reauthorize", recorded)
    client = live_client()
    alice = client.authenticated(username="alice")

    with freeze_time() as clock, alice.websocket_connect("/live/whoami") as socket:
        socket.send_text("?")
        assert socket.receive_json() == {"may_view": False}
        run(client, grant_to("alice", VIEW_PRODUCT))
        socket.send_text("?")
        assert socket.receive_json() == {"may_view": False}
        clock.tick(60)
        assert checked.wait(timeout=60)  # A bound for a broken test, not a timing.
        socket.send_text("?")
        assert socket.receive_json() == {"may_view": True}


def test_anonymous_sockets_stay_anonymous(
    live_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public route's anonymous socket passes its checks."""
    checked = threading.Event()
    reauthorize = live.reauthorize

    async def recorded(scope: Scope) -> bool:
        allowed = await reauthorize(scope)
        checked.set()
        return allowed

    monkeypatch.setattr(live, "reauthorize", recorded)
    client = live_client()

    with freeze_time() as clock, client.websocket_connect("/live/echo") as socket:
        clock.tick(60)
        assert checked.wait(timeout=60)  # A bound for a broken test, not a timing.
        socket.send_text("still here")
        assert socket.receive_text() == "still here"


class Presenting(BaseAuthentication):
    """Credentials as the test sets them: a user, none, or a refusal."""

    def __init__(self, answer: Credentials | Exception | None) -> None:
        """Answer ``answer``."""
        self.answer = answer

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The set answer."""
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def credentials_of(user: object) -> Credentials:
    """Credentials naming ``user``."""
    return Credentials(cast("AbstractBaseUser", user), scheme="test")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("before", "now", "kept"),
    [
        ("alice", "alice", True),
        ("alice", "bob", False),
        ("alice", None, False),
        (None, "alice", False),
        (None, None, True),
        ("alice", "refused", False),
    ],
)
async def test_reauthentication_needs_the_same_principal(
    use_auth: UseAuth,
    caplog: pytest.LogCaptureFixture,
    before: str | None,
    now: str | None,
    kept: bool,
) -> None:
    """The credentials must still hold and name the user the socket opened with."""
    use_auth()
    users = {"alice": Someone(), "bob": Someone()}
    answer: Credentials | Exception | None = (
        AuthenticationFailed("Token expired.")
        if now == "refused"
        else None
        if now is None
        else credentials_of(users[now])
    )
    middleware = AuthenticationMiddleware(echo)
    middleware.authenticators = (Presenting(answer),)
    opened = None if before is None else credentials_of(users[before])
    scope: Scope = {"type": "websocket", "headers": [], "auth": opened, "user": "old"}

    assert await middleware.reauthenticate(scope) is kept

    if kept and now is not None:
        assert (scope["user"], scope["auth"]) == (users[now], answer)
    else:
        assert scope["user"] == "old"
    if now == "refused":
        assert (
            "Credentials of an open connection no longer hold: Token expired."
            in caplog.messages
        )
    elif not kept:
        assert (
            "An open connection's credentials no longer name its user"
            in caplog.messages
        )


async def echo(scope: Scope, receive: Any, send: Any) -> None:  # noqa: ANN401
    """An application that is never called."""


def test_live_streams_need_their_routes_rule(live_client: MakeClient) -> None:
    """The handshake of a live route is refused like any request without access."""
    client = live_client()
    bob = client.authenticated(username="bob")

    assert bob.get("/live/products/feed").status_code == 403
    assert client.get("/live/users/feed").status_code == 401
