"""The webhooks admin API and the ``webhooks`` command."""

import io
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth import open_routes
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.contrib.webhooks import queue_deliveries
from domainkit.contrib.webhooks.admin import (
    NewSubscriptionOut,
    SecretOut,
    SubscriptionIn,
    SubscriptionOut,
    SubscriptionPatch,
)
from domainkit.contrib.webhooks.models import (
    SubscriptionChanged,
    SubscriptionCreated,
    SubscriptionDeleted,
    SubscriptionSecretRotated,
    secret_box,
)
from domainkit.contrib.webhooks.tables import FAILED, deliveries, subscriptions
from domainkit.db import connections
from domainkit.ddd import DomainEvent, event_bus
from domainkit.http import get_asgi_application
from domainkit.management import CommandError, call_command, operators
from domainkit.management.base import run_async
from domainkit.management.operators import Operator
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    SHOP,
)
from domainkit_tests.test_auth_admin import staff
from domainkit_tests.test_auth_http import create_user, grant
from domainkit_tests.test_messaging import FAST

ADMIN = "/admin/webhooks"
ALL = (
    "webhooks.view_subscription",
    "webhooks.add_subscription",
    "webhooks.change_subscription",
    "webhooks.delete_subscription",
)
NEW = {"url": "https://hooks.example.com/in/T0K3N", "event_types": ["invoice.paid"]}

type Headers = dict[str, str]


@contextmanager
def project(
    database_url: str, tmp_path: Path, *, idempotency: bool = False
) -> Iterator[TestClient]:
    """A migrated project with auth, messaging, and webhooks; the admin mounted.

    With ``idempotency``, the idempotency app and its middleware too.
    """
    registry.clear()
    extra = ["domainkit.contrib.idempotency"] if idempotency else []
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.messaging",
            "domainkit.contrib.webhooks",
            *extra,
            SHOP,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.sample_webhook_routes.router",
        MIDDLEWARE=MIDDLEWARE if idempotency else MIDDLEWARE[:-1],
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
        MESSAGING={**FAST, "VISIBILITY_TIMEOUT": 60},
    ):
        setup()
        call_command("makemigrations", "-m", "webhooks", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with TestClient(get_asgi_application()) as test_client:
            yield test_client
    registry.clear()


@pytest.fixture
def client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    """The project, without idempotency keys."""
    with project(database_url, tmp_path) as test_client:
        yield test_client


@pytest.fixture
def keyed_client(database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    """The project, with idempotency keys."""
    with project(database_url, tmp_path, idempotency=True) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> Headers:
    """Staff with every webhooks permission."""
    return staff("hooks-admin", *ALL)


@pytest.fixture
def recorded() -> Iterator[list[DomainEvent]]:
    """Events published while the test runs."""
    events: list[DomainEvent] = []

    async def keep(event: DomainEvent) -> None:
        events.append(event)

    event_bus.subscribe(DomainEvent, keep)
    yield events
    event_bus.unsubscribe(DomainEvent, keep)


def create(client: TestClient, headers: Headers, **body: Any) -> dict[str, Any]:  # noqa: ANN401
    """Create a subscription; return the answer's JSON."""
    response = client.post(
        f"{ADMIN}/subscriptions", json={**NEW, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_every_route_is_guarded(client: TestClient) -> None:
    """Deny by default holds: no admin route is open."""
    assert open_routes(client.app) == []  # type: ignore[arg-type]


def test_staff_create_subscriptions_and_see_the_secret_once(
    client: TestClient,
    admin: Headers,
    recorded: list[DomainEvent],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The secret comes with the 201, and in no listing, read, or log."""
    with caplog.at_level(logging.DEBUG, "domainkit"):  # Drivers log SQL values.
        created = create(client, admin, description="billing", active=False)
    secret = created.pop("secret")
    listed = client.get(f"{ADMIN}/subscriptions", headers=admin)
    read = client.get(f"{ADMIN}/subscriptions/{created['id']}", headers=admin)

    assert secret.startswith("whsec_")
    assert created == {
        "id": created["id"],
        "url": NEW["url"],
        "event_types": ["invoice.paid"],
        "active": False,
        "description": "billing",
        "created_at": created["created_at"],
        "rotating": False,
        "owner": None,
    }
    assert listed.json() == [created]
    assert read.json() == created
    for text in (listed.text, read.text, caplog.text):
        assert secret not in text
    (event,) = [e for e in recorded if isinstance(e, SubscriptionCreated)]
    assert (event.host, event.event_types) == ("hooks.example.com", ("invoice.paid",))
    assert "T0K3N" not in repr(recorded)
    body = SubscriptionIn.model_validate(NEW)
    for model in (
        body,
        SubscriptionPatch(url=NEW["url"]),
        SubscriptionOut.model_validate(created),
        NewSubscriptionOut.model_validate({**created, "secret": secret}),
        SecretOut(secret=secret, previous_valid_until=None),
    ):
        assert "T0K3N" not in repr(model)
        assert secret not in repr(model)
    assert body.url == NEW["url"]


def test_subscriptions_are_paged(client: TestClient, admin: Headers) -> None:
    """Pages by limit and offset, oldest first, within bounds."""
    ids = [create(client, admin)["id"] for _ in range(3)]

    page = client.get(f"{ADMIN}/subscriptions?limit=2&offset=1", headers=admin)

    assert [item["id"] for item in page.json()] == ids[1:]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=10001"):
        response = client.get(f"{ADMIN}/subscriptions?{query}", headers=admin)
        assert response.status_code == 422


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"url": "http://hooks.example.com/"}, "unsafe_webhook_url"),
        ({"url": "https://10.0.0.8/"}, "unsafe_webhook_url"),
        ({"url": "https://[::ffff:169.254.169.254]/"}, "unsafe_webhook_url"),
        ({"url": "https://me:pw@hooks.example.com/"}, "unsafe_webhook_url"),
        ({"url": "https://localhost/"}, "unsafe_webhook_url"),
        ({"url": "https://hooks.example.com/" + "a" * 2048}, "validation_error"),
        ({"event_types": []}, "validation_error"),
        ({"event_types": ["x"] * 51}, "validation_error"),
        ({"event_types": ["a" * 129]}, "validation_error"),
        ({"event_types": ["bad type"]}, "invalid_webhook_event_types"),
        ({"description": "d" * 256}, "validation_error"),
        ({"secret": "whsec_mine"}, "validation_error"),
    ],
)
def test_bad_subscriptions_are_refused(
    client: TestClient, admin: Headers, body: dict[str, Any], code: str
) -> None:
    """Unsafe URLs, malformed event types, and unknown fields get 422."""
    response = client.post(
        f"{ADMIN}/subscriptions", json={**NEW, **body}, headers=admin
    )

    assert (response.status_code, response.json()["code"]) == (422, code)
    if "url" in body:
        assert body["url"] not in response.text
    assert client.get(f"{ADMIN}/subscriptions", headers=admin).json() == []


def test_staff_change_rotate_and_delete_subscriptions(
    client: TestClient, admin: Headers, recorded: list[DomainEvent]
) -> None:
    """PATCH what is given; rotate shows the new secret; delete removes it."""
    created = create(client, admin)
    one = f"{ADMIN}/subscriptions/{created['id']}"

    changed = client.patch(
        one,
        json={"url": "https://new.example.com/", "event_types": ["*"], "active": False},
        headers=admin,
    )
    refused = client.patch(one, json={"url": "https://127.0.0.1/"}, headers=admin)
    unknown = client.patch(one, json={"secret": "x"}, headers=admin)
    rotated = client.post(f"{one}/rotate-secret", headers=admin)
    after = client.get(one, headers=admin)
    deleted = client.delete(one, headers=admin)
    gone = client.get(one, headers=admin)

    assert changed.json()["url"] == "https://new.example.com/"
    assert (changed.json()["event_types"], changed.json()["active"]) == (["*"], False)
    assert (refused.status_code, refused.json()["code"]) == (422, "unsafe_webhook_url")
    assert unknown.status_code == 422
    secret = rotated.json()["secret"]
    assert secret.startswith("whsec_")
    assert secret != created["secret"]
    assert rotated.json()["previous_valid_until"] is not None
    assert after.json()["rotating"] is True
    assert secret not in after.text
    assert deleted.status_code == 204
    assert (gone.status_code, gone.json()["code"]) == (
        404,
        "webhook_subscription_not_found",
    )
    kinds = [type(e) for e in recorded]
    assert kinds == [
        SubscriptionCreated,
        SubscriptionChanged,
        SubscriptionSecretRotated,
        SubscriptionDeleted,
    ]
    assert recorded[1].fields == ("url", "event_types", "active")  # type: ignore[attr-defined]
    # The trail can tell where events went before and after (DATA-06).
    assert (recorded[1].host, recorded[1].previous_host) == (  # type: ignore[attr-defined]
        "new.example.com",
        "hooks.example.com",
    )


def test_missing_subscriptions_are_404(client: TestClient, admin: Headers) -> None:
    """Every subscription endpoint answers 404 for an unknown id."""
    one = f"{ADMIN}/subscriptions/{uuid.uuid4()}"
    for response in (
        client.get(one, headers=admin),
        client.patch(one, json={}, headers=admin),
        client.post(f"{one}/rotate-secret", headers=admin),
        client.delete(one, headers=admin),
        client.get(f"{one}/deliveries", headers=admin),
    ):
        assert (response.status_code, response.json()["code"]) == (
            404,
            "webhook_subscription_not_found",
        )


def test_only_staff_with_the_permission_get_in(client: TestClient) -> None:
    """Anonymous callers, non-staff, and staff lacking the permission are refused."""
    viewer = staff("viewer", "webhooks.view_subscription")
    create_user("plain")
    grant("plain", *ALL)
    plain = staff("plain-staff", *ALL, is_staff=False)
    root = staff("root", is_superuser=True)

    assert client.get(f"{ADMIN}/subscriptions").status_code == 401
    assert client.get(f"{ADMIN}/subscriptions", headers=viewer).status_code == 200
    assert (
        client.post(f"{ADMIN}/subscriptions", json=NEW, headers=viewer).status_code
        == 403
    )
    assert client.get(f"{ADMIN}/subscriptions", headers=plain).status_code == 403
    created = create(client, root)
    one = f"{ADMIN}/subscriptions/{created['id']}"
    assert client.post(f"{one}/rotate-secret", headers=viewer).status_code == 403
    assert client.delete(one, headers=viewer).status_code == 403
    assert client.patch(one, json={}, headers=viewer).status_code == 403
    delivery = f"{ADMIN}/deliveries/{uuid.uuid4()}"
    assert client.get(delivery, headers=viewer).status_code == 404  # Allowed.
    assert client.post(f"{delivery}/replay", headers=viewer).status_code == 403
    assert client.get(f"{ADMIN}/deliveries/{uuid.uuid4()}").status_code == 401


def queue(event_type: str = "invoice.paid", key: str = "k") -> None:
    """Queue a delivery of an event to its subscribers."""

    async def run() -> None:
        await queue_deliveries(
            event_type, {"invoice": 7}, key=key, occurred_at=datetime.now(UTC)
        )

    run_async(run())


def fail_deliveries() -> None:
    """Mark every delivery failed."""

    async def run() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(deliveries).values(status=FAILED, finished_at=datetime.now(UTC))
            )

    run_async(run())


def test_staff_inspect_and_replay_deliveries(
    client: TestClient, admin: Headers
) -> None:
    """Deliveries by subscription, one with its body and log, and replays."""
    created = create(client, admin)
    queue(key="a")
    queue(key="b")
    listed = client.get(
        f"{ADMIN}/subscriptions/{created['id']}/deliveries?limit=1", headers=admin
    )
    (item,) = listed.json()
    one = f"{ADMIN}/deliveries/{item['id']}"
    detail = client.get(one, headers=admin)
    pending = client.post(f"{one}/replay", headers=admin)
    none_failed = client.get(
        f"{ADMIN}/subscriptions/{created['id']}/deliveries?status=failed", headers=admin
    )
    fail_deliveries()
    failed = client.get(
        f"{ADMIN}/subscriptions/{created['id']}/deliveries?status=failed", headers=admin
    )
    replayed = client.post(f"{one}/replay", headers=admin)
    missing = f"{ADMIN}/deliveries/{uuid.uuid4()}"

    assert item["webhook_id"] == f"msg_{uuid.UUID(item['id']).hex}"
    assert (item["status"], item["event_type"], item["attempts"]) == (
        "pending",
        "invoice.paid",
        0,
    )
    assert detail.json()["payload"]["data"] == {"invoice": 7}
    assert detail.json()["log"] == []
    assert (pending.status_code, pending.json()) == (202, {"replayed": False})
    assert none_failed.json() == []
    assert len(failed.json()) == 2
    assert replayed.json() == {"replayed": True}
    for response in (
        client.get(missing, headers=admin),
        client.post(f"{missing}/replay", headers=admin),
    ):
        assert (response.status_code, response.json()["code"]) == (
            404,
            "webhook_delivery_not_found",
        )
    bad = client.get(
        f"{ADMIN}/subscriptions/{created['id']}/deliveries?status=lost", headers=admin
    )
    assert bad.status_code == 422


def run(*args: str) -> str:
    """Run a command; return its output."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


def test_the_command_lists_and_replays(
    client: TestClient, admin: Headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subscriptions, deliveries (filtered), and replay by id or --failed.

    Each replay is recorded as its operator's action (SEC-r2-2); listing
    records nothing.
    """
    recorded: list[tuple[str, str, str, dict[str, object]]] = []

    async def record(
        who: Operator, action: str, outcome: str, details: dict[str, object]
    ) -> None:
        recorded.append((who.actor, action, outcome, details))

    monkeypatch.setattr(operators, "_record", record)
    monkeypatch.setenv("DOMAINKIT_OPERATOR", "mallory")
    created = create(client, admin)
    create(
        client,
        admin,
        url="https://hooks.example.com?token=T0K3N",
        active=False,
        event_types=["a", "b"],
    )
    queue(key="a")
    queue(key="b")

    lines = run("webhooks", "subscriptions").splitlines()
    assert lines[0] == f"{created['id']}  active    hooks.example.com  invoice.paid"
    assert lines[1].endswith("  inactive  hooks.example.com  a,b")
    assert "T0K3N" not in run("webhooks", "subscriptions")
    listed = run("webhooks", "deliveries", "--limit", "1").splitlines()
    assert len(listed) == 1
    assert "pending    invoice.paid  x0  -  " in listed[0]
    assert run("webhooks", "deliveries", "--status", "failed") == ""
    assert run("webhooks", "deliveries", "--subscription", str(uuid.uuid4())) == ""

    first = listed[0].split()[0]
    assert run("webhooks", "replay", first) == "Queued 0 delivery(ies) again.\n"
    fail_deliveries()
    assert run("webhooks", "replay", f"msg_{uuid.UUID(first).hex}") == (
        "Queued 1 delivery(ies) again.\n"
    )
    assert run("webhooks", "replay", "--failed", "--subscription", created["id"]) == (
        "Queued 1 delivery(ies) again.\n"
    )
    for args, error in (
        (("replay",), "delivery ids or --failed, not both"),
        (("replay", first, "--failed"), "delivery ids or --failed, not both"),
        (("replay", "nope"), "'nope' is not an id"),
        (("deliveries", "--limit", "0"), "--limit must be from 1 to 1000"),
        (("deliveries", "--limit", "1001"), "--limit must be from 1 to 1000"),
    ):
        with pytest.raises(CommandError, match=error):
            run("webhooks", *args)
    replayed = ("operator:mallory", "webhooks.replay", "success")
    assert recorded == [
        (*replayed, {"asked": 1, "deliveries": 0}),
        (*replayed, {"asked": 1, "deliveries": 1}),
        (*replayed, {"asked": 1, "deliveries": 1}),
    ]


def test_subscription_rows_hold_no_extra_secrets(
    client: TestClient, admin: Headers
) -> None:
    """The secret column holds the current secret only, encrypted, until a rotation."""
    created = create(client, admin)

    async def read() -> Any:  # noqa: ANN401
        async with connections.engine().connect() as connection:
            return (await connection.execute(select(subscriptions))).one()

    row = run_async(read())
    assert row.previous_secret is None
    assert created["secret"].encode() not in row.secret
    assert secret_box().decrypt(row.secret, associated=row.id.bytes) == (
        created["secret"].encode(),
        True,
    )


NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}


def stored_bodies() -> list[bytes | None]:
    """The bodies the idempotency table holds."""

    async def read() -> list[bytes | None]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(idempotency_keys.c.body))
            return [row.body for row in rows]

    return run_async(read())


def test_a_new_subscription_is_never_kept_for_replay(keyed_client: TestClient) -> None:
    """Its answer holds the secret: no-store, so a keyed retry gets 409, not it."""
    headers = {**staff("hooks-admin", *ALL), "Idempotency-Key": "create-1"}

    first = keyed_client.post(f"{ADMIN}/subscriptions", json=NEW, headers=headers)
    retry = keyed_client.post(f"{ADMIN}/subscriptions", json=NEW, headers=headers)

    assert first.status_code == 201
    assert {name: first.headers[name] for name in NO_STORE} == NO_STORE
    assert (retry.status_code, retry.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    secret = first.json()["secret"]
    assert secret not in retry.text
    assert stored_bodies() == [None]
    assert secret.encode() not in repr(stored_bodies()).encode()


def test_a_rotated_secret_is_never_kept_for_replay(keyed_client: TestClient) -> None:
    """Rotating answers no-store too; a keyed retry gets 409, not the secret."""
    admin = staff("hooks-admin", *ALL)
    created = keyed_client.post(f"{ADMIN}/subscriptions", json=NEW, headers=admin)
    rotate = f"{ADMIN}/subscriptions/{created.json()['id']}/rotate-secret"
    headers = {**admin, "Idempotency-Key": "rotate-1"}

    first = keyed_client.post(rotate, headers=headers)
    retry = keyed_client.post(rotate, headers=headers)

    assert first.status_code == 200
    assert {name: first.headers[name] for name in NO_STORE} == NO_STORE
    assert (retry.status_code, retry.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    assert first.json()["secret"] not in retry.text
    assert stored_bodies() == [None]


def test_other_answers_may_be_kept(client: TestClient, admin: Headers) -> None:
    """Only answers with a secret opt out; the rest keep the default headers."""
    created = create(client, admin)

    read = client.get(f"{ADMIN}/subscriptions/{created['id']}", headers=admin)

    assert "pragma" not in read.headers
