"""The books: a refund is a reversal, and the trial balance balances."""

from domainkit.contrib.accounting import verify_ledger
from domainkit.testing import TestClient

from tests.money import URL, balance, key, usd

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
    (january,) = clerk.get(f"{BOOKS}/periods").json()["items"]

    report = clerk.get(f"{BOOKS}/periods/{january['id']}/trial-balance")

    assert refund.status_code == 201, refund.json()
    assert balance(customer, wallet)["balance"] == "80.00"
    closing = {row["code"]: row["closing"] for row in report.json()["accounts"]}
    assert closing["1000"] == "100.00"  # The bank: the deposit.
    assert closing["2200"] == "-20.00"  # Owed to the merchants: the capture.
    assert [total["net"] for total in report.json()["totals"]] == ["0.00"]
    assert clerk.run(verify_ledger) == []
