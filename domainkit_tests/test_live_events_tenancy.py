"""Live events and tenants: each connection hears its own tenant's commits (ADR 0183).

An event keeps the tenant it was committed in, through the fan-out between
processes too, and reaches only the connections served as that tenant. A
subscription with ``per_tenant=False`` hears every tenant's, its rule alone
deciding. The audit's reproducer (A28-SEC-01) is the first test.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any, Final
from uuid import uuid4

import anyio
import pytest
from fastapi import APIRouter, Depends, WebSocket
from sqlalchemy import text
from starlette.datastructures import State
from starlette.types import Message

from domainkit.cache import caches
from domainkit.conf import override_settings, settings
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.events import UserPermissionGranted
from domainkit.db import connections
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.ddd.tenancy import no_tenant
from domainkit.http import get_asgi_application
from domainkit.http import live_events as live_events_module
from domainkit.http.access import AllOf, AllowAny, Require
from domainkit.http.live import TOO_SLOW, Closed
from domainkit.http.live_events import LiveEvents, live_events
from domainkit.tenancy import HasMemberPermissions, InTenant, all_tenants, tenant
from domainkit.testing import TestClient
from domainkit_tests.conftest import FAST_HASHER
from domainkit_tests.support.waiting import until
from domainkit_tests.test_live_events import (
    LIVE_MIDDLEWARE,
    Hub,
    Someone,
    connection,
    granted,
    take,
)
from domainkit_tests.test_live_fanout import (
    Recorded,
    processes,
    redis_url,
    stop_listening,
)

__all__ = ["member_permissions", "redis_url", "router"]

VIEW: Final = "shop.view_order"
MEMBERS: Final = {"alice": {"a"}, "bob": {"b"}, "carol": {"a", "b"}}
TENANCY: Final = {
    "HEADER": "X-Tenant",
    "REQUIRED": False,
    "MEMBER_PERMISSIONS": f"{__name__}.member_permissions",
}
IN_TENANT: Final = AllOf(InTenant(), HasMemberPermissions(VIEW))
"""The framework's own tenancy rules: a member of the tenant served, holding VIEW."""


def member_permissions(user: Any, tenant_id: str) -> set[str] | None:  # noqa: ANN401 - any user.
    """Each user holds VIEW in their tenants, and is no member elsewhere."""
    name = str(getattr(user, "name", None) or getattr(user, "username", ""))
    return {VIEW} if tenant_id in MEMBERS.get(name, ()) else None


class Member(Someone):
    """A user known by name, as ``member_permissions`` looks them up."""

    def __init__(self, name: str) -> None:
        """A user with an id of its own, and ``name``."""
        super().__init__()
        self.name = name


async def committed_in(
    tenant_id: str | None, bus: EventBus, *events: DomainEvent
) -> None:
    """Publish ``events`` as a commit made in ``tenant_id`` (None: no tenant) does."""
    with no_tenant() if tenant_id is None else tenant(tenant_id):
        await bus.publish_all(events)


# The hub ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_member_of_one_tenant_never_hears_anothers_commits() -> None:
    """The audit's reproducer: Bob, of tenant b, hears b's event and not a's.

    He subscribes with the framework's own rules, which ask only about the
    connection's tenant: the event's tenant decides before the rule.
    """
    bus = EventBus()
    hub = LiveEvents(bus)
    bob = Member("bob")
    secret = granted(uuid4(), "tenant-a-secret")
    own = granted(uuid4(), "tenant-b-news")

    with override_settings(TENANCY=TENANCY), tenant("b"):
        async with hub.subscribe(
            connection(bob), UserPermissionGranted, rule=IN_TENANT
        ) as events:
            await asyncio.create_task(committed_in("a", bus, secret))
            await asyncio.create_task(committed_in("b", bus, own))
            async with asyncio.timeout(2):
                assert await anext(events) is own


@pytest.mark.anyio
async def test_events_of_no_tenant_reach_only_connections_served_as_none() -> None:
    """No tenant is a tenant of its own: neither side hears the other's commits."""
    bus = EventBus()
    hub = LiveEvents(bus)
    plain, of_a = granted(uuid4(), "no-tenant"), granted(uuid4(), "tenant-a")
    later = granted(uuid4(), "tenant-a-later")

    async with hub.subscribe(connection(), DomainEvent, rule=AllowAny()) as none:
        with tenant("a"):
            async with hub.subscribe(connection(), DomainEvent, rule=AllowAny()) as a:
                await committed_in("a", bus, of_a)
                await committed_in(None, bus, plain)
                await committed_in("a", bus, later)
                async with asyncio.timeout(2):
                    assert await take(a, 2) == [of_a, later]
                    assert await anext(none) is plain


@pytest.mark.anyio
async def test_a_subscription_says_when_it_hears_every_tenant() -> None:
    """``per_tenant=False``: every tenant's commits, and none's; the rule decides.

    ``all_tenants()`` is about rows: a subscription made inside it hears its
    own tenant's events alone.
    """
    bus = EventBus()
    hub = LiveEvents(bus)
    me = Someone()
    mine = [granted(me.id, f"in-{name}") for name in ("a", "b", "none")]
    theirs = granted(uuid4())

    with tenant("b"):
        async with hub.subscribe(
            connection(me), UserPermissionGranted, rule=AllowAny(), per_tenant=False
        ) as events:
            for tenant_id, event in zip(("a", "b", None), mine, strict=True):
                await committed_in(tenant_id, bus, event)
            async with asyncio.timeout(2):
                assert await take(events, 3) == mine

    with all_tenants():
        async with hub.subscribe(
            connection(me), UserPermissionGranted, rule=AllowAny()
        ) as events:
            await committed_in("a", bus, theirs)
            own = granted(me.id)
            await committed_in(None, bus, own)
            async with asyncio.timeout(2):
                assert await anext(events) is own


@pytest.mark.anyio
async def test_another_tenants_commits_never_make_a_subscriber_too_slow() -> None:
    """Tenant a's burst never reaches b's buffers, so it cannot close b's socket.

    a's own subscriber falls behind on the burst and is closed: the bound
    still holds within a tenant.
    """
    bus = EventBus()
    hub = LiveEvents(bus)
    burst = [granted(uuid4()) for _ in range(40)]
    mine = granted(uuid4(), "tenant-b")

    with override_settings(LIVE_SEND_QUEUE=2), tenant("b"):
        async with hub.subscribe(connection(), DomainEvent, rule=AllowAny()) as b:
            (subscriber,) = hub._subscribers
            with tenant("a"):
                async with hub.subscribe(
                    connection(), DomainEvent, rule=AllowAny()
                ) as a:
                    await bus.publish_all(burst)
                    with pytest.raises(Closed) as closed:
                        await anext(a)
            assert not subscriber.intake
            assert not subscriber.overflowed
            await bus.publish(mine)
            async with asyncio.timeout(2):
                assert await anext(b) is mine

    assert closed.value.closure is TOO_SLOW


@pytest.mark.anyio
async def test_a_stream_hears_the_tenant_its_route_served() -> None:
    """An event stream sent as tenant b, as the middleware sends it, gets b's."""
    hub = Hub()
    me = Someone()
    sent: list[Message] = []
    arrived = anyio.Event()
    leave = anyio.Event()
    of_a, of_b = granted(me.id, "tenant-a"), granted(me.id, "tenant-b")

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
        rule=AllowAny(),
        render=lambda event: {"permission": getattr(event, "permission", None)},
    )

    async def served_as_b() -> None:
        with tenant("b"):
            await response(request.scope, receive, send)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(served_as_b)
        await hub.subscribed.wait()
        await committed_in("a", hub.bus, of_a)
        await committed_in("b", hub.bus, of_b)
        while len(sent) < 2:
            await arrived.wait()
        leave.set()

    assert [each.get("body") for each in sent[1:]] == [
        (
            f"event: UserPermissionGranted\nid: {of_b.event_id}\n"
            'data: {"permission":"tenant-b"}\n\n'
        ).encode()
    ]


# Across processes -------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_fanout_says_which_tenant_committed_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``n`` names the tenant; a message of no tenant names none, as 2.8.0 sent."""
    bus = EventBus()
    hub = LiveEvents(bus)
    hub.share(UserPermissionGranted)
    recorded = Recorded()
    monkeypatch.setattr(live_events_module, "backend", lambda: recorded)
    of_a, plain = granted(uuid4()), granted(uuid4())

    await committed_in("a", bus, of_a)
    await committed_in(None, bus, plain)

    async def both() -> bool:
        return sum(len(batch) for batch in recorded.sent) == 2

    await until(both, "the two messages were not sent")
    first, second = (json.loads(each) for batch in recorded.sent for each in batch)
    assert (first["n"], first["d"]["event_id"]) == ("a", str(of_a.event_id))
    assert set(second) == {"o", "t", "d"}


@pytest.mark.anyio
async def test_a_process_hands_what_it_hears_to_the_tenant_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each tenant's subscribers get their own; a bad tenant is dropped, and said."""
    caplog.set_level(logging.WARNING, logger="domainkit.http.live_events")
    _, here, _, there = processes()
    loop = asyncio.get_running_loop()
    key = live_events_module._key(UserPermissionGranted)

    def message(event: UserPermissionGranted, **more: object) -> str:
        data = live_events_module._adapter(type(event)).dump_python(event, mode="json")
        return json.dumps({"o": here.origin, "t": key, "d": data, **more})

    of_a, of_b, plain = granted(uuid4()), granted(uuid4()), granted(uuid4())
    async with there.subscribe(connection(), DomainEvent, rule=AllowAny()) as none:
        with tenant("b"):
            async with there.subscribe(connection(), DomainEvent, rule=AllowAny()) as b:
                for wrong in ("not a tenant!", 7, ["b"]):
                    there._heard(message(granted(uuid4()), n=wrong), loop)
                there._heard(message(of_a, n="a"), loop)
                there._heard(message(of_b, n="b"), loop)
                there._heard(message(plain), loop)
                async with asyncio.timeout(2):
                    assert await anext(b) == of_b
                    assert await anext(none) == plain
                assert all(
                    not each.intake and not each.allowed for each in there._subscribers
                )

    dropped = [each for each in caplog.messages if "Dropped a live event" in each]
    assert len(dropped) == 3


async def across_tenants(
    here_bus: EventBus, there: LiveEvents, listening: Callable[[], Awaitable[bool]]
) -> None:
    """A commit in tenant a reaches a's subscribers in the other process, not b's."""
    alice, bob = Member("alice"), Member("bob")
    of_a, of_b = granted(alice.id, "tenant-a"), granted(bob.id, "tenant-b")
    rule = AllowAny()
    with tenant("a"):
        in_a = there.subscribe(connection(alice), UserPermissionGranted, rule=rule)
        heard_in_a = await in_a.__aenter__()
    try:
        with tenant("b"):
            async with there.subscribe(
                connection(bob), UserPermissionGranted, rule=rule
            ) as heard_in_b:
                await until(listening, "nobody listens on the channel")
                await committed_in("a", here_bus, of_a)
                await committed_in("b", here_bus, of_b)
                async with asyncio.timeout(5):
                    assert await anext(heard_in_a) == of_a
                    assert await anext(heard_in_b) == of_b
                assert all(
                    not each.intake and not each.allowed for each in there._subscribers
                )
    finally:
        await in_a.__aexit__(None, None, None)


@pytest.fixture
def redis_fanout(redis_url: str) -> Iterator[None]:
    """``LIVE_FANOUT`` through a Redis cache of the test's own."""
    with override_settings(
        CACHES={
            "default": {"URL": "memory://"},
            "live": {"URL": redis_url, "KEY_PREFIX": "test"},
        },
        LIVE_FANOUT={"BACKEND": "redis", "ALIAS": "live"},
    ):
        yield


@pytest.mark.anyio
@pytest.mark.usefixtures("redis_fanout")
async def test_each_tenant_hears_its_own_across_processes_by_redis() -> None:
    """Through Redis, the tenant travels with the event."""
    here_bus, here, _, there = processes()
    client = caches["live"].store.client  # type: ignore[attr-defined]

    async def listening() -> bool:
        counts = await client.pubsub_numsub(settings.LIVE_FANOUT.CHANNEL)
        return bool(counts and counts[0][1] >= 1)

    try:
        await across_tenants(here_bus, there, listening)
    finally:
        await stop_listening(here, there)
        await caches.close_all()


@pytest.mark.anyio
async def test_each_tenant_hears_its_own_across_processes_by_postgres(
    database_url: str,
) -> None:
    """Through PostgreSQL's notifications, the tenant travels with the event."""
    if not database_url.startswith("postgresql"):
        pytest.skip("set DOMAINKIT_TEST_POSTGRES_URL")
    with override_settings(
        DATABASES={"default": {"URL": database_url}},
        LIVE_FANOUT={"BACKEND": "postgres"},
    ):
        here_bus, here, _, there = processes()

        async def listening() -> bool:
            async with connections.engine().connect() as checking:
                count = await checking.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE query LIKE 'LISTEN %domainkit_live%'"
                    )
                )
            return bool(count)

        try:
            await across_tenants(here_bus, there, listening)
        finally:
            await stop_listening(here, there)
            await connections.close_all()


# Over HTTP --------------------------------------------------------------------------

router = APIRouter()
router.include_router(auth_router, prefix="/auth")


@router.websocket("/live/orders", dependencies=[Depends(Require())])
async def orders(websocket: WebSocket) -> None:
    """The orders of the tenant served, to its members who may view them."""
    await live_events.forward(websocket, UserPermissionGranted, rule=IN_TENANT)


@router.websocket("/live/every-order", dependencies=[Depends(Require())])
async def every_order(websocket: WebSocket) -> None:
    """Every tenant's orders, to a member of the tenant served (on purpose)."""
    await live_events.forward(
        websocket, UserPermissionGranted, rule=IN_TENANT, per_tenant=False
    )


@pytest.fixture
def tenant_client(auth_project: str) -> Iterator[TestClient]:
    """A client of the routes above, with ``TenantMiddleware`` after authentication."""
    authenticated = LIVE_MIDDLEWARE.index(
        "domainkit.contrib.auth.middleware.AuthenticationMiddleware"
    )
    middleware = list(LIVE_MIDDLEWARE)
    middleware.insert(authenticated + 1, "domainkit.tenancy.TenantMiddleware")
    with (
        override_settings(
            ROOT_ROUTER=f"{__name__}.router",
            MIDDLEWARE=middleware,
            TENANCY=TENANCY,
            AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
        ),
        TestClient(get_asgi_application()) as client,
    ):
        yield client


def commit(client: TestClient, tenant_id: str, permission: str) -> None:
    """Publish a committed event in ``tenant_id``, in the client's event loop."""

    async def work() -> None:
        await committed_in(tenant_id, event_bus, granted(uuid4(), permission))

    client.run(work)


def test_a_socket_served_as_a_tenant_hears_that_tenants_commits(
    tenant_client: TestClient,
) -> None:
    """Bob's socket in b gets b's order; a's, committed first, never comes."""
    bob = tenant_client.authenticated(username="bob")

    with bob.websocket_connect("/live/orders", headers={"X-Tenant": "b"}) as socket:
        commit(tenant_client, "a", "a-order")
        commit(tenant_client, "b", "b-order")
        heard = socket.receive_json()

    assert heard["data"]["permission"] == "b-order"
    assert live_events._subscribers == set()


def test_a_member_of_two_tenants_hears_the_one_served(
    tenant_client: TestClient,
) -> None:
    """Carol, of a and b, connected as a, hears a's commits only."""
    carol = tenant_client.authenticated(username="carol")

    with carol.websocket_connect("/live/orders", headers={"X-Tenant": "a"}) as socket:
        commit(tenant_client, "b", "b-order")
        commit(tenant_client, "a", "a-order")
        heard = socket.receive_json()

    assert heard["data"]["permission"] == "a-order"


def test_a_socket_that_hears_every_tenant_says_so(tenant_client: TestClient) -> None:
    """``per_tenant=False``: a's commit reaches b's member too, as the route chose."""
    bob = tenant_client.authenticated(username="bob")

    with bob.websocket_connect(
        "/live/every-order", headers={"X-Tenant": "b"}
    ) as socket:
        commit(tenant_client, "a", "a-order")
        heard = socket.receive_json()

    assert heard["data"]["permission"] == "a-order"
