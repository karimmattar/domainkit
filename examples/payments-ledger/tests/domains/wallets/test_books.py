"""The books: a refund is a reversal, and the trial balance balances."""

from datetime import date, timedelta

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    OpenPeriod,
    open_period,
    verify_ledger,
)
from domainkit.testing import ClientFor, FrozenClock, TestClient

from domains.wallets.controllers import BANK, MERCHANTS, house_account, open_books
from tests.conftest import CLERK
from tests.money import URL, balance, deposit, key, usd

BOOKS = "/api/accounting"


def test_the_trial_balance_balances(
    customer: TestClient, clerk: TestClient, wallet: str
) -> None:
    """A deposit, a payment and its refund, and a capture: every currency nets 0."""
    body = {"amount": usd("30.00"), "description": "Order 1042"}
    paid = customer.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=key())
    held = customer.post(
        f"{URL}/wallets/{wallet}/holds", json={"amount": usd("50.00")}, headers=key()
    )
    clerk.post(
        f"{URL}/holds/{held.json()['id']}/capture", json={"amount": usd("20.00")}
    )
    refund = clerk.post(f"{BOOKS}/entries/{paid.json()['entry_id']}/reverse", json={})
    [january] = [
        period
        for period in clerk.get(f"{BOOKS}/periods").json()["items"]
        if period["name"] == "2027-01"
    ]

    report = clerk.get(f"{BOOKS}/periods/{january['id']}/trial-balance")

    assert refund.status_code == 201, refund.json()
    assert balance(customer, wallet)["balance"] == "80.00"
    closing = {row["code"]: row["closing"] for row in report.json()["accounts"]}
    assert closing["1000"] == "100.00"  # The bank: the deposit.
    assert closing["2200"] == "-20.00"  # Owed to the merchants: the capture.
    assert [total["net"] for total in report.json()["totals"]] == ["0.00"]
    assert clerk.run(verify_ledger) == []


def test_payments_go_on_into_the_next_month(
    books: None, client_for: ClientFor, clock: FrozenClock
) -> None:
    """The books open a month ahead: a payment on the 1st finds its period.

    The worker runs ``open_books`` each day, which keeps the month after
    the current one open too.
    """
    clock.move_to("2027-01-31T23:59:00Z")
    customer, clerk = client_for(), client_for(*CLERK)
    wallet = customer.post(f"{URL}/wallets").json()["id"]
    deposit(clerk, wallet, "100.00")
    body = {"amount": usd("30.00"), "description": "Order 1042"}

    january = customer.post(
        f"{URL}/wallets/{wallet}/payments", json=body, headers=key()
    )
    clock.tick(timedelta(minutes=2))  # 1 February.
    february = customer.post(
        f"{URL}/wallets/{wallet}/payments", json=body, headers=key()
    )
    clerk.run(open_books)  # The worker's run on the 1st opens March.

    assert (january.status_code, february.status_code) == (201, 201), february.json()
    periods = clerk.get(f"{BOOKS}/periods").json()["items"]
    assert sorted(period["name"] for period in periods) == [
        "2027-01",
        "2027-02",
        "2027-03",
    ]


def test_the_books_open_beside_a_period_of_the_operators(
    client: TestClient, clerk: TestClient
) -> None:
    """A period from the 15th to the 14th reaches both months: it is left.

    The daily run opens no period over it, fails nothing, and still opens
    the business's accounts.
    """
    mid_month = OpenPeriod(
        name="2027-01-15", starts_on=date(2027, 1, 15), ends_on=date(2027, 2, 14)
    )
    client.run(open_period, mid_month, AccountingUnitOfWork())

    client.run(open_books)
    client.run(open_books)  # The next day's run.

    periods = clerk.get(f"{BOOKS}/periods").json()["items"]
    assert [period["name"] for period in periods] == ["2027-01-15"]
    assert client.run(house_account, BANK) != client.run(house_account, MERCHANTS)
