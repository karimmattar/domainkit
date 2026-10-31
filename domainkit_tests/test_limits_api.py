"""Limits over HTTP: routes need permissions, changes are audited, breaches 422."""

from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from domainkit.http import get_asgi_application
from domainkit.http.access import open_routes
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    Migrated,
    Shape,
)
from domainkit_tests.test_auth_admin import token_for
from domainkit_tests.test_auth_http import audited, create_user, grant

LIMITS_API: Final = Shape(
    "limits_api",
    "limits",
    {
        "INSTALLED_DOMAINS": [
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.audit",
            "domainkit.contrib.accounting",
            "domainkit.contrib.limits",
            ALPHA,
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_limits_routes.router",
        "MIDDLEWARE": ["domainkit.http.locale.LocaleMiddleware", *MIDDLEWARE[:-1]],
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
        "ERROR_FORMAT": "problem+json",
        "ACCOUNTING": {"REQUIRE_PERIOD": False},
    },
)
EVERY: Final = (
    *(f"limits.{action}_limit" for action in ("view", "add", "change", "delete")),
    "accounting.view_account",
    "accounting.add_account",
    "accounting.add_entry",
)
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """Limits and the ledger over HTTP in a migrated project, with the audit trail."""
    with migrated(LIMITS_API), TestClient(get_asgi_application()) as test_client:
        yield test_client


def user(name: str, *permissions: str) -> Headers:
    """A user granted ``permissions``; headers that authenticate them."""
    create_user(name)
    grant(name, *permissions)
    return token_for(name)


@pytest.fixture
def clerk(client: TestClient) -> Headers:
    """A user with every permission of limits and enough of the ledger's."""
    return user("clerk", *EVERY)


def egp(amount: str) -> dict[str, str]:
    """An amount in pounds, as the API takes it."""
    return {"amount": amount, "currency": "EGP"}


DAILY: Final = {
    "code": "wallet.daily",
    "kind": "holder",
    "currency": "EGP",
    "window": "day",
    "max_amount": egp("100"),
    "note": "As agreed with compliance",
}


def test_every_route_needs_a_rule(client: TestClient) -> None:
    """No route of the limits is open."""
    assert [
        route
        for route in open_routes(client.app)  # type: ignore[arg-type]
        if " /limits/" in route
    ] == []


def test_staff_make_change_list_and_remove_limits(
    client: TestClient, clerk: Headers
) -> None:
    """Each change answers with the limit, and the trail records who made it."""
    made = client.post("/limits/limits", json=DAILY, headers=clerk)
    assert made.status_code == 201, made.json()
    limit = made.json()
    assert limit["max_amount"] == {"amount": "100.00", "currency": "EGP"}
    assert (limit["window"], limit["subject"], limit["direction"]) == ("day", "", "out")
    assert limit["time_zone"] == "UTC"

    again = client.post("/limits/limits", json=DAILY, headers=clerk)
    assert again.status_code == 409
    assert again.json()["code"] == "limit_taken"

    changed = client.post(
        f"/limits/limits/{limit['id']}/change",
        json={"window": "month", "max_count": 10},
        headers=clerk,
    )
    assert changed.status_code == 200, changed.json()
    assert (changed.json()["window"], changed.json()["max_amount"]) == ("month", None)
    assert changed.json()["max_count"] == 10

    listed = client.get(
        "/limits/limits", params={"kind": "holder"}, headers=clerk
    ).json()
    assert [each["code"] for each in listed["items"]] == ["wallet.daily"]

    removed = client.delete(f"/limits/limits/{limit['id']}", headers=clerk)
    assert removed.status_code == 204
    gone = client.get(f"/limits/limits/{limit['id']}", headers=clerk)
    assert (gone.status_code, gone.json()["code"]) == (404, "limit_not_found")
    again_removed = client.delete(f"/limits/limits/{limit['id']}", headers=clerk)
    assert again_removed.json()["detail"] == "No limit has that id."

    changes = audited("limits.limit_set")
    assert [row["details"]["window"] for row in changes] == ["day", "month"]
    assert changes[0]["details"]["max_amount"] == {
        "amount": "100.00",
        "currency": "EGP",
    }
    assert changes[0]["details"]["subject"] == ""
    assert {row["actor"] for row in changes} == {
        row["actor"] for row in audited("limits.limit_removed")
    }
    raised = client.post(
        "/limits/limits",
        json={**DAILY, "subject": "h-1", "direction": "in"},
        headers=clerk,
    )
    assert (raised.json()["subject"], raised.json()["direction"]) == ("h-1", "in")


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (
            {**DAILY, "max_amount": {"amount": "1", "currency": "USD"}},
            422,
            "invalid_limit",
        ),
        ({**DAILY, "window": None}, 422, "invalid_limit"),
        ({**DAILY, "time_zone": "Nowhere/Never"}, 422, "invalid_limit"),
        ({**DAILY, "currency": "XYZ"}, 422, "unknown_currency"),
        ({**DAILY, "max_amount": egp("1.001")}, 422, "validation_error"),
        ({**DAILY, "code": "Upper"}, 422, "validation_error"),
        ({**DAILY, "max_count": 0}, 422, "validation_error"),
        ({**DAILY, "extra": 1}, 422, "validation_error"),
    ],
)
def test_a_limit_that_cannot_be_is_refused_with_its_code(
    client: TestClient, clerk: Headers, body: dict[str, Any], status: int, code: str
) -> None:
    """Problems with a stable code; nothing is made."""
    refused = client.post("/limits/limits", json=body, headers=clerk)

    assert (refused.status_code, refused.json()["code"]) == (status, code)
    assert refused.headers["content-type"] == "application/problem+json"
    assert client.get("/limits/limits", headers=clerk).json()["items"] == []


def test_each_route_needs_its_own_permission(client: TestClient) -> None:
    """Viewers read; only those allowed make, change, or remove."""
    viewer = user("viewer", "limits.view_limit")
    maker = user("maker", "limits.add_limit")
    made = client.post("/limits/limits", json=DAILY, headers=maker)
    assert made.status_code == 201, made.json()
    path = f"/limits/limits/{made.json()['id']}"

    assert client.get("/limits/limits", headers=maker).status_code == 403
    assert client.get(path, headers=viewer).status_code == 200
    assert client.post("/limits/limits", json=DAILY, headers=viewer).status_code == 403
    assert client.post(f"{path}/change", json={}, headers=viewer).status_code == 403
    assert client.delete(path, headers=viewer).status_code == 403
    assert client.get("/limits/limits").status_code == 401
    usage = client.get("/limits/usage", params={"kind": "holder", "subject": "h"})
    assert usage.status_code == 401
    assert (
        client.get(
            "/limits/usage", params={"kind": "holder", "subject": "h"}, headers=maker
        ).status_code
        == 403
    )


def test_a_breach_on_the_ledger_is_a_422_problem_and_usage_shows_it(
    client: TestClient, clerk: Headers
) -> None:
    """``limit_exceeded``, in the caller's language; ``GET /usage`` shows the window."""
    assert client.post("/limits/limits", json=DAILY, headers=clerk).status_code == 201
    accounts = {}
    for code, kind, holder in (("till", "asset", None), ("w-1", "liability", "h-1")):
        made = client.post(
            "/accounting/accounts",
            json={
                "code": code,
                "name": code,
                "type": kind,
                "currency": "EGP",
                "holder": holder,
            },
            headers=clerk,
        )
        assert made.status_code == 201, made.json()
        accounts[code] = made.json()["id"]

    def pay(reference: str, amount: str, **headers: str) -> Any:  # noqa: ANN401
        return client.post(
            "/accounting/entries",
            json={
                "source": "payments",
                "reference": reference,
                "booked_on": "2026-10-30",
                "lines": [
                    {
                        "account_id": accounts["w-1"],
                        "side": "debit",
                        "amount": egp(amount),
                    },
                    {
                        "account_id": accounts["till"],
                        "side": "credit",
                        "amount": egp(amount),
                    },
                ],
            },
            headers={**clerk, **headers},
        )

    assert pay("p-1", "60").status_code == 201
    over = pay("p-2", "60", **{"Accept-Language": "ar"})

    assert over.status_code == 422
    assert over.json()["code"] == "limit_exceeded"
    assert over.json()["detail"] == "المبلغ أكبر مما يسمح به حدّ خلال فترته."
    usage = client.get(
        "/limits/usage", params={"kind": "holder", "subject": "h-1"}, headers=clerk
    )
    assert usage.status_code == 200, usage.json()
    (only,) = usage.json()["items"]
    assert (only["code"], only["used"], only["count"], only["remaining"]) == (
        "wallet.daily",
        egp("60.00"),
        1,
        egp("40.00"),
    )
    assert (only["origin"], only["window"], only["subject"]) == ("stored", "day", "")
