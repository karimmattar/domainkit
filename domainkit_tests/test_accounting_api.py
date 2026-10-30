"""The ledger over HTTP: every route needs its permission, and refusals are problems."""

import ast
import csv
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from domainkit.conf import override_settings
from domainkit.contrib import accounting
from domainkit.ddd import DomainError
from domainkit.http import get_asgi_application
from domainkit.http.access import open_routes
from domainkit.i18n import gettext, override
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
from domainkit_tests.test_auth_http import create_user, grant

ACCOUNTING_API: Final = Shape(
    "accounting_api",
    "accounting",
    {
        "INSTALLED_DOMAINS": [
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.accounting",
            ALPHA,
        ],
        "ROOT_ROUTER": "domainkit_tests.sample_accounting_routes.router",
        "MIDDLEWARE": ["domainkit.http.locale.LocaleMiddleware", *MIDDLEWARE[:-1]],
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
        "ERROR_FORMAT": "problem+json",
    },
)
API: Final = "/accounting"
MODELS: Final = ("account", "entry", "hold", "period")
EVERY: Final = (
    *(f"accounting.{action}_{model}" for model in MODELS for action in ("view", "add")),
    "accounting.close_account",
    "accounting.reconcile_account",
    "accounting.reverse_entry",
    "accounting.capture_hold",
    "accounting.release_hold",
    "accounting.close_period",
)
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The ledger's API in a migrated project, behind token authentication."""
    with migrated(ACCOUNTING_API), TestClient(get_asgi_application()) as test_client:
        yield test_client


def user(name: str, *permissions: str) -> Headers:
    """A user granted ``permissions``; headers that authenticate them."""
    create_user(name)
    grant(name, *permissions)
    return token_for(name)


@pytest.fixture
def clerk(client: TestClient) -> Headers:
    """A user with every permission of the ledger."""
    return user("clerk", *EVERY)


def usd(amount: str) -> dict[str, str]:
    """An amount in dollars, as the API takes it."""
    return {"amount": amount, "currency": "USD"}


def open_books(client: TestClient, headers: Headers) -> dict[str, Any]:
    """A period and two accounts; their representations by name."""
    period = client.post(
        f"{API}/periods",
        json={"name": "2026-10", "starts_on": "2026-10-01", "ends_on": "2026-10-31"},
        headers=headers,
    )
    assert period.status_code == 201, period.json()
    cash = client.post(
        f"{API}/accounts",
        json={"code": "1000", "name": "Cash", "type": "asset", "currency": "USD"},
        headers=headers,
    )
    sales = client.post(
        f"{API}/accounts",
        json={"code": "4000", "name": "Sales", "type": "income", "currency": "USD"},
        headers=headers,
    )
    assert (cash.status_code, sales.status_code) == (201, 201), cash.json()
    return {"period": period.json(), "cash": cash.json(), "sales": sales.json()}


def sale(books: dict[str, Any], reference: str, amount: str) -> dict[str, Any]:
    """A cash sale, as the API takes it."""
    return {
        "source": "shop",
        "reference": reference,
        "booked_on": "2026-10-15",
        "lines": [
            {"account_id": books["cash"]["id"], "side": "debit", "amount": usd(amount)},
            {
                "account_id": books["sales"]["id"],
                "side": "credit",
                "amount": usd(amount),
            },
        ],
    }


def test_every_route_needs_a_rule(client: TestClient) -> None:
    """No route of the ledger is open: each declares its permission."""
    assert [
        route
        for route in open_routes(client.app)  # type: ignore[arg-type]
        if f" {API}/" in route
    ] == []


def test_entries_are_posted_once_and_move_balances(
    client: TestClient, clerk: Headers
) -> None:
    """A posted entry moves both accounts; posting it again changes nothing."""
    books = open_books(client, clerk)

    posted = client.post(
        f"{API}/entries", json=sale(books, "s-1", "10.50"), headers=clerk
    )
    again = client.post(
        f"{API}/entries", json=sale(books, "s-1", "10.50"), headers=clerk
    )

    assert posted.status_code == 201, posted.json()
    assert again.json()["id"] == posted.json()["id"]
    cash = client.get(f"{API}/accounts/{books['cash']['id']}", headers=clerk).json()
    assert cash["balance"] == usd("10.50")
    assert cash["available"] == usd("10.50")
    listed = client.get(f"{API}/entries", headers=clerk).json()
    assert [entry["reference"] for entry in listed["items"]] == ["s-1"]
    assert posted.json()["lines"][0]["amount"] == usd("10.50")


def test_refusals_are_problems_with_codes(client: TestClient, clerk: Headers) -> None:
    """Unbalanced, reused, and unknown: each answers with its status and code."""
    books = open_books(client, clerk)
    unbalanced = sale(books, "s-2", "1.00")
    unbalanced["lines"][1]["amount"] = usd("2.00")
    client.post(f"{API}/entries", json=sale(books, "s-3", "1.00"), headers=clerk)

    refused = client.post(f"{API}/entries", json=unbalanced, headers=clerk)
    reused = client.post(
        f"{API}/entries", json=sale(books, "s-3", "9.00"), headers=clerk
    )
    unknown = client.post(
        f"{API}/entries",
        json={**sale(books, "s-4", "1.00"), "booked_on": "2027-01-01"},
        headers=clerk,
    )

    assert refused.status_code == 422
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["code"] == "unbalanced_entry"
    assert (reused.status_code, reused.json()["code"]) == (409, "reference_reused")
    assert (unknown.status_code, unknown.json()["code"]) == (422, "no_open_period")


def test_a_permission_short_of_the_action_is_refused(client: TestClient) -> None:
    """Viewing does not post; posting without a token is not allowed at all."""
    viewer = user("viewer", "accounting.view_entry")

    assert client.get(f"{API}/entries", headers=viewer).status_code == 200
    assert client.post(f"{API}/entries", json={}, headers=viewer).status_code == 403
    assert client.get(f"{API}/entries").status_code == 401
    assert client.get(f"{API}/accounts", headers=viewer).status_code == 403


def test_reports_answer_in_json_and_csv(client: TestClient, clerk: Headers) -> None:
    """Trial balance and general ledger, each by ``Accept``, net zero."""
    books = open_books(client, clerk)
    client.post(f"{API}/entries", json=sale(books, "s-1", "10.50"), headers=clerk)
    period = books["period"]["id"]

    balance = client.get(f"{API}/periods/{period}/trial-balance", headers=clerk)
    balance_csv = client.get(
        f"{API}/periods/{period}/trial-balance", headers={**clerk, "Accept": "text/csv"}
    )
    ledger = client.get(f"{API}/periods/{period}/general-ledger", headers=clerk)
    ledger_csv = client.get(
        f"{API}/periods/{period}/general-ledger",
        headers={**clerk, "Accept": "text/csv"},
    )

    assert json.loads(balance.text)["totals"] == [
        {
            "currency": "USD",
            "opening": "0.00",
            "debits": "10.50",
            "credits": "10.50",
            "net": "0.00",
        }
    ]
    assert list(csv.reader(io.StringIO(balance_csv.text)))[-1][-1] == "0.00"
    assert json.loads(ledger.text)["totals"][0]["net"] == "0.00"
    assert ledger_csv.headers["content-type"].startswith("text/csv")
    assert list(csv.reader(io.StringIO(ledger_csv.text)))[-1][4] == "total"


def test_holds_are_placed_captured_and_released(
    client: TestClient, clerk: Headers
) -> None:
    """A hold lowers what is available; captured, it posts its entry."""
    books = open_books(client, clerk)
    client.post(f"{API}/entries", json=sale(books, "s-1", "100.00"), headers=clerk)
    wallet = books["cash"]["id"]
    hold = client.post(
        f"{API}/holds",
        json={
            "account_id": wallet,
            "amount": usd("30.00"),
            "source": "shop",
            "reference": "auth-1",
        },
        headers=clerk,
    )
    assert hold.status_code == 201, hold.json()
    held = client.get(f"{API}/accounts/{wallet}", headers=clerk).json()
    assert held["available"] == usd("70.00")

    with override_settings(ACCOUNTING={"REQUIRE_PERIOD": False}):
        captured = client.post(
            f"{API}/holds/{hold.json()['id']}/capture",
            json={"counter_account_id": books["sales"]["id"], "amount": usd("20.00")},
            headers=clerk,
        )

    assert captured.status_code == 200, captured.json()
    assert captured.json()["status"] == "captured"
    assert captured.json()["finished_at"] is not None
    entry = client.get(f"{API}/entries/{captured.json()['entry_id']}", headers=clerk)
    assert entry.json()["lines"][0]["amount"] == usd("20.00")
    after = client.get(f"{API}/accounts/{wallet}", headers=clerk).json()
    assert (after["balance"], after["available"]) == (usd("80.00"), usd("80.00"))
    released = client.post(f"{API}/holds/{hold.json()['id']}/release", headers=clerk)
    assert (released.status_code, released.json()["code"]) == (409, "hold_not_active")
    whole = client.post(
        f"{API}/holds",
        json={
            "account_id": wallet,
            "amount": usd("5.00"),
            "source": "shop",
            "reference": "auth-2",
        },
        headers=clerk,
    ).json()
    with override_settings(ACCOUNTING={"REQUIRE_PERIOD": False}):
        taken = client.post(
            f"{API}/holds/{whole['id']}/capture",
            json={"counter_account_id": books["sales"]["id"]},
            headers=clerk,
        )
    assert taken.status_code == 200, taken.json()
    after = client.get(f"{API}/accounts/{wallet}", headers=clerk).json()
    assert after["balance"] == usd("75.00")


def test_statements_are_imported_and_reconciled(
    client: TestClient, clerk: Headers
) -> None:
    """A CSV statement is imported once; reconciling it leaves its breaks."""
    books = open_books(client, clerk)
    client.post(f"{API}/entries", json=sale(books, "s-1", "10.50"), headers=clerk)
    account = books["cash"]["id"]
    statement = (
        "id,date,amount,currency,reference,description\n"
        "b-1,2026-10-15,10.50,USD,s-1,card sale\n"
        "b-2,2026-10-16,3.00,USD,,unknown\n"
    )
    sent = {**clerk, "Content-Type": "text/csv"}

    first = client.post(
        f"{API}/accounts/{account}/statements", content=statement, headers=sent
    )
    second = client.post(
        f"{API}/accounts/{account}/statements", content=statement, headers=sent
    )
    done = client.post(f"{API}/accounts/{account}/reconcile", headers=clerk)
    breaks = client.get(f"{API}/accounts/{account}/breaks", headers=clerk)

    assert first.json() == {"imported": 2, "skipped": 0}
    assert second.json() == {"imported": 0, "skipped": 2}
    assert done.json()["matched"] == {"reference": 1}
    assert [(b["kind"], b["amount"]) for b in breaks.json()] == [
        ("unmatched_line", usd("3.00"))
    ]
    (left,) = breaks.json()
    assert left["statement_line_id"] is not None
    assert left["posting_id"] is None
    assert done.json()["breaks"] == breaks.json()


def test_amounts_and_statements_that_are_not_are_refused(
    client: TestClient, clerk: Headers
) -> None:
    """More places than the currency, an unknown one, a statement not CSV or UTF-8."""
    books = open_books(client, clerk)
    inexact = sale(books, "s-1", "1.005")
    unknown = sale(books, "s-2", "1.00")
    unknown["lines"][0]["amount"]["currency"] = "XXX"
    account = books["cash"]["id"]

    refused = [
        client.post(f"{API}/entries", json=body, headers=clerk)
        for body in (inexact, unknown)
    ]
    as_json = client.post(
        f"{API}/accounts/{account}/statements", json={"id": "b-1"}, headers=clerk
    )
    latin = client.post(
        f"{API}/accounts/{account}/statements",
        content="id,date,amount,currency,reference\nb,2026-10-15,1,USD,caf\xe9\n".encode(
            "latin-1"
        ),
        headers={**clerk, "Content-Type": "text/csv; charset=latin-1"},
    )

    assert [(r.status_code, r.json()["code"]) for r in refused] == [
        (422, "invalid_amount"),
        (422, "invalid_amount"),
    ]
    assert (as_json.status_code, as_json.json()["code"]) == (422, "invalid_statement")
    assert as_json.json()["detail"] == "A statement is sent as text/csv."
    assert latin.json()["detail"] == "A statement is UTF-8 text."


def test_refusals_are_answered_in_the_callers_language(
    client: TestClient, clerk: Headers
) -> None:
    """A refusal's detail in Arabic; its code unchanged."""
    books = open_books(client, clerk)
    unbalanced = sale(books, "s-1", "1.00")
    unbalanced["lines"][1]["amount"] = usd("2.00")

    refused = client.post(
        f"{API}/entries", json=unbalanced, headers={**clerk, "Accept-Language": "ar"}
    )

    assert refused.json()["code"] == "unbalanced_entry"
    assert refused.json()["detail"] == "يختلف مدين القيد عن دائنه في إحدى العملات."


@pytest.mark.reads_own_source
def test_every_refusal_of_the_ledger_is_translated() -> None:
    """Each message a domain error of the app is raised with has its Arabic."""
    errors = {
        name
        for name in accounting.__all__
        if isinstance(getattr(accounting, name), type)
        and issubclass(getattr(accounting, name), DomainError)
    }
    messages = set()
    for path in Path(accounting.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in errors | {"_text"}
                and node.args
            ):
                last = node.args[-1]
                if isinstance(last, ast.Constant) and isinstance(last.value, str):
                    messages.add(last.value)
    with override("ar"):
        untranslated = sorted(
            message for message in messages if gettext(message) == message
        )
    assert len(messages) > 40
    assert untranslated == []


def test_entries_are_reversed_and_periods_and_accounts_closed(
    client: TestClient, clerk: Headers
) -> None:
    """A reversal is an entry; an empty account closes; a closed period refuses."""
    books = open_books(client, clerk)
    posted = client.post(
        f"{API}/entries", json=sale(books, "s-1", "2.00"), headers=clerk
    )
    entry = posted.json()["id"]

    reversal = client.post(
        f"{API}/entries/{entry}/reverse",
        json={"booked_on": "2026-10-16", "description": "refund"},
        headers=clerk,
    )
    closed_account = client.post(
        f"{API}/accounts/{books['cash']['id']}/close", headers=clerk
    )
    period = books["period"]["id"]
    closed = client.post(f"{API}/periods/{period}/close", headers=clerk)
    refused = client.post(
        f"{API}/entries", json=sale(books, "s-2", "1.00"), headers=clerk
    )

    assert reversal.status_code == 201, reversal.json()
    assert (reversal.json()["reverses"], reversal.json()["reference"]) == (entry, entry)
    assert closed_account.json()["status"] == "closed"
    assert closed.json()["status"] == "closed"
    assert (refused.status_code, refused.json()["code"]) == (422, "period_closed")
