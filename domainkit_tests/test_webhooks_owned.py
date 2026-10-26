"""Owned webhook subscriptions: the self-service API, routing, DNS shares."""

import asyncio
import io
import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from domainkit import lifecycle, setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.auth import AuthUnitOfWork, open_routes
from domainkit.contrib.webhooks import delivery as delivery_module
from domainkit.contrib.webhooks import expose, queue_deliveries
from domainkit.contrib.webhooks.admin import router as admin_router
from domainkit.contrib.webhooks.models import (
    MAX_OWNER,
    Subscription,
    SubscriptionCreated,
)
from domainkit.contrib.webhooks.owned import owned_router, user_owner
from domainkit.contrib.webhooks.owned import router as owned
from domainkit.contrib.webhooks.tables import FAILED, SUCCEEDED, deliveries
from domainkit.contrib.webhooks.uow import WebhooksUnitOfWork
from domainkit.ddd import BusinessRuleViolation, DomainEvent
from domainkit.http import get_asgi_application, guard
from domainkit.lifecycle import Phase
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    SHOP,
)
from domainkit_tests.test_auth_admin import staff
from domainkit_tests.test_messaging import FAST
from domainkit_tests.test_webhooks import Receiver
from domainkit_tests.test_webhooks_admin import ALL, recorded
from domainkit_tests.test_webhooks_delivery import (
    HungDns,
    hooks,
    rows,
    subscribe,
    webhooks,
)

__all__ = ["hooks", "recorded", "webhooks"]  # Fixtures used here.

MINE: Final = "/me/webhooks"
NEW: Final = {"url": "https://hooks.example.com/in/T0K3N", "event_types": ["*"]}


def __getattr__(name: str) -> APIRouter:
    """``router`` (``ROOT_ROUTER``), built when the application is: both APIs.

    Built each time, not at import, so the routes are the code under test
    now (mutation testing changes it between imports and runs).
    """
    if name != "router":
        raise AttributeError(name)
    built = APIRouter()
    built.include_router(owned_router(), prefix=MINE)
    built.include_router(
        owned_router(lambda user: "tenant-1"), prefix="/tenant/webhooks"
    )
    built.include_router(admin_router, prefix="/admin/webhooks")
    return built


def test_the_module_router_is_the_default_one() -> None:
    """``owned.router`` has the routes ``owned_router()`` builds, for the user's id."""
    fresh = owned_router()

    assert [(r.path, r.methods) for r in owned.routes] == [  # type: ignore[attr-defined]
        (r.path, r.methods)  # type: ignore[attr-defined]
        for r in fresh.routes
    ]
    assert owned.tags == fresh.tags == ["webhooks"]


type Headers = dict[str, str]


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    """A migrated project with auth, messaging, and webhooks; both APIs mounted."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.messaging",
            "domainkit.contrib.webhooks",
            SHOP,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER=f"{__name__}.router",
        MIDDLEWARE=MIDDLEWARE[:-1],
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
        MESSAGING={**FAST, "VISIBILITY_TIMEOUT": 60},
        WEBHOOKS={"MAX_SUBSCRIPTIONS_PER_OWNER": 2},
    ):
        setup()
        call_command("makemigrations", "-m", "owned", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with TestClient(get_asgi_application()) as test_client:
            yield test_client
    registry.clear()


def user(name: str) -> Headers:
    """A user without staff rights or permissions; their headers."""
    return staff(name, is_staff=False)


def user_id(username: str) -> str:
    """The id of ``username``, as text."""

    async def find() -> str:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(username)
        assert found is not None
        return str(found.id)

    return run_async(find())


def create(client: TestClient, headers: Headers, prefix: str = MINE) -> Any:  # noqa: ANN401 - a response.
    """Subscribe through the self-service API."""
    return client.post(f"{prefix}/subscriptions", json=NEW, headers=headers)


def test_no_owned_route_is_open(client: TestClient) -> None:
    """Every route needs an authenticated user: 401 without one."""
    assert open_routes(client.app) == []  # type: ignore[arg-type]
    assert client.get(f"{MINE}/subscriptions").status_code == 401
    assert client.post(f"{MINE}/subscriptions", json=NEW).status_code == 401


def test_users_manage_their_own_subscriptions(
    client: TestClient, recorded: list[DomainEvent]
) -> None:
    """Create, list, read, change, rotate, and delete: each owned by the user."""
    ann = user("ann")
    created = create(client, ann)
    body = created.json()
    one = f"{MINE}/subscriptions/{body['id']}"
    changed = client.patch(one, json={"description": "mine"}, headers=ann)
    rotated = client.post(f"{one}/rotate-secret", headers=ann)
    listed = client.get(f"{MINE}/subscriptions", headers=ann)
    log = client.get(f"{one}/deliveries", headers=ann)
    failed = client.get(f"{one}/deliveries?status=failed", headers=ann)
    staff_view = client.get(
        f"/admin/webhooks/subscriptions/{body['id']}",
        headers=staff("boss", *ALL),
    )
    deleted = client.delete(one, headers=ann)

    owner = user_id("ann")
    assert created.status_code == 201
    assert body["secret"].startswith("whsec_")
    assert created.headers["Cache-Control"] == "no-store"
    assert (body["owner"], body["url"]) == (owner, NEW["url"])
    assert changed.json()["description"] == "mine"
    assert rotated.json()["secret"].startswith("whsec_")
    assert rotated.headers["Cache-Control"] == "no-store"
    assert [each["id"] for each in listed.json()] == [body["id"]]
    assert (log.status_code, log.json()) == (200, [])
    assert (failed.status_code, failed.json()) == (200, [])
    assert staff_view.json()["owner"] == owner
    assert deleted.status_code == 204
    assert client.get(one, headers=ann).status_code == 404
    (event,) = [e for e in recorded if isinstance(e, SubscriptionCreated)]
    assert (event.owner, event.host) == (owner, "hooks.example.com")


def test_another_owners_subscriptions_do_not_exist(client: TestClient) -> None:
    """Bob cannot see, change, rotate, delete, or list Ann's: 404, as for none."""
    ann, bob = user("ann"), user("bob")
    theirs = create(client, ann).json()["id"]
    staffs = client.post(
        "/admin/webhooks/subscriptions", json=NEW, headers=staff("boss", *ALL)
    ).json()["id"]
    for subscription in (theirs, staffs):
        one = f"{MINE}/subscriptions/{subscription}"
        answers = [
            client.get(one, headers=bob),
            client.patch(one, json={"active": False}, headers=bob),
            client.post(f"{one}/rotate-secret", headers=bob),
            client.get(f"{one}/deliveries", headers=bob),
            client.delete(one, headers=bob),
        ]
        assert [a.status_code for a in answers] == [404] * 5
        assert {a.json()["code"] for a in answers} == {"webhook_subscription_not_found"}
    assert client.get(f"{MINE}/subscriptions", headers=bob).json() == []
    assert client.get(f"{MINE}/subscriptions/{theirs}", headers=ann).status_code == 200


def test_an_owner_has_at_most_its_share_of_subscriptions(client: TestClient) -> None:
    """``MAX_SUBSCRIPTIONS_PER_OWNER`` (2 here) per owner; another owner has its own."""
    ann, bob = user("ann"), user("bob")
    made = [create(client, ann).status_code for _ in range(3)]
    refused = create(client, ann)

    assert made == [201, 201, 422]
    assert refused.json()["code"] == "webhook_subscription_limit"
    assert create(client, bob).status_code == 201


def test_owners_can_be_named_otherwise(client: TestClient) -> None:
    """``owned_router(owner=...)``: Ann and Bob share their tenant's subscriptions."""
    ann, bob = user("ann"), user("bob")
    made = create(client, ann, "/tenant/webhooks").json()

    shared = client.get("/tenant/webhooks/subscriptions", headers=bob).json()

    assert made["owner"] == "tenant-1"
    assert [each["id"] for each in shared] == [made["id"]]
    assert client.get(f"{MINE}/subscriptions", headers=ann).json() == []


def test_owners_are_checked() -> None:
    """An owner is 1 to 255 characters; a user's is its id."""
    for bad in ("", "x" * (MAX_OWNER + 1)):
        with pytest.raises(BusinessRuleViolation, match="1 to 255 characters"):
            Subscription.create("https://h.example/", "h.example", ["*"], owner=bad)

    class Someone:
        id = uuid.UUID(int=7)

    assert user_owner(Someone()) == str(uuid.UUID(int=7))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("length", "status"), [(0, 422), (1, 200), (MAX_OWNER, 200), (MAX_OWNER + 1, 422)]
)
def test_owners_named_by_a_router_are_bounded(
    client: TestClient, length: int, status: int
) -> None:
    """An owner function giving no owner, or one too long, answers 422."""
    named = APIRouter()
    named.include_router(owned_router(lambda user: "o" * length), prefix="/named")
    client.app.include_router(named)  # type: ignore[attr-defined]

    answer = client.get("/named/subscriptions", headers=user("ann"))

    assert answer.status_code == status
    if status == 422:
        assert answer.json()["detail"] == (
            "A subscription's owner has 1 to 255 characters."
        )


class Paid(DomainEvent):
    """An invoice of a customer was paid."""

    customer: str | None


async def owned_subscription(url: str, owner: str | None) -> uuid.UUID:
    """Store an active subscription to every event; return its id."""
    subscription, _ = Subscription.create(url, "127.0.0.1", ["*"], owner=owner)
    async with WebhooksUnitOfWork() as uow:
        await uow.subscriptions.add(subscription)
        await uow.commit()
    return subscription.id


@pytest.mark.anyio
async def test_owned_subscriptions_get_their_owners_events_only(
    hooks: Receiver,
) -> None:
    """Staff's get every event; an owner's only its own, never an unowned one."""
    expose(Paid, "invoice.paid", owner=lambda event: event.customer)
    staffs = await owned_subscription(hooks.url("/staff"), None)
    anns = await owned_subscription(hooks.url("/ann"), "ann")
    bobs = await owned_subscription(hooks.url("/bob"), "bob")

    for customer in ("ann", "bob", None):
        await delivery_module.fan_out(Paid(customer=customer))
    await queue_deliveries(
        "invoice.paid", {}, key="direct", occurred_at=datetime.now(UTC)
    )

    got: dict[uuid.UUID, int] = {}
    for row in await rows(deliveries):
        got[row.subscription_id] = got.get(row.subscription_id, 0) + 1
    assert got == {staffs: 4, anns: 1, bobs: 1}


@pytest.mark.anyio
async def test_one_owners_hung_hosts_share_one_quarter_of_the_lookups(
    hooks: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight subscriptions of one owner whose DNS never answers hold 4 of 16 threads.

    The 0.9.1 residual: an owner could hold every thread for a first round
    of slow lookups with eight subscriptions. Now its lookups are the
    owner's, a quarter of the threads: the other four of its deliveries
    are refused at once, and staff's delivery goes through.
    """
    dns = HungDns(hung=4)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(guard, "resolver", guard.GuardResolver())
    saved = dict(lifecycle._registered[Phase.RESOURCES])
    hung = [
        await owned_subscription(f"http://s{i}.hang.test:{hooks.port}/hook", "tenant")
        for i in range(8)
    ]
    victim, _ = await subscribe(hooks.url(host="victim.test"), "*")
    await queue_deliveries(
        "note.written", {}, key="k", occurred_at=datetime.now(UTC), owner="tenant"
    )
    by_subscription = {row.subscription_id: row.id for row in await rows(deliveries)}
    queued_by = RequestContext("req-1", None, "TASK", "webhooks.deliver")
    try:
        with bind_request(queued_by):
            waiting = [
                asyncio.create_task(delivery_module.deliver(by_subscription[s]))
                for s in hung
            ]
            assert await asyncio.to_thread(dns.all_hung.wait, 30)
            owners = dict(guard.resolver._owners)
            await delivery_module.deliver(by_subscription[victim.id])
        dns.release.set()
        await asyncio.gather(*waiting)
    finally:
        dns.release.set()
        await guard.resolver.close()
        lifecycle._registered[Phase.RESOURCES].clear()
        lifecycle._registered[Phase.RESOURCES].update(saved)

    statuses = {row.subscription_id: row.status for row in await rows(deliveries)}
    assert owners == {"webhook-owner:tenant": 4}
    assert statuses[victim.id] == SUCCEEDED
    assert [statuses[s] for s in hung] == [FAILED] * 8
    assert dns.waiting == 4


@pytest.mark.anyio
async def test_erasing_an_owner_deletes_its_subscriptions(hooks: Receiver) -> None:
    """Its subscriptions go, audited as deletions; their deliveries are blanked."""
    anns = await owned_subscription(hooks.url("/ann"), "ann")
    another = await owned_subscription(hooks.url("/ann-2"), "ann")
    staffs = await owned_subscription(hooks.url("/staff"), None)
    await queue_deliveries(
        "invoice.paid",
        {"total": 1},
        key="k",
        occurred_at=datetime.now(UTC),
        owner="ann",
    )

    erased = await delivery_module.erase_subject("ann")

    async with WebhooksUnitOfWork() as uow:
        left = [each.id for each in await uow.subscriptions.page(limit=10)]
    by_subscription = {row.subscription_id: row for row in await rows(deliveries)}
    assert erased == {"deliveries": 2, "cancelled": 2, "subscriptions": 2}
    assert left == [staffs]
    assert by_subscription[anns].body == by_subscription[another].body == ""
    assert by_subscription[staffs].body != ""
