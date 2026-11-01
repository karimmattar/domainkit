"""SEC-16b-1: ``POST /accounts`` still opens accounts that may go negative.

FIN-97 flipped ``may_go_negative`` to ``False`` in ``Account.open`` and
``OpenAccount``, and the upgrade guide says so for ``POST /accounts``
too. But the route's body model, ``AccountIn`` (``accounting/api.py``),
still defaults the field to ``True``: a customer's wallet opened over
HTTP without the flag is overdrawn without refusal, the ledger creating
credit nobody granted, which is FIN-97 on the path the audit named.

The test asserts the safe behaviour (the account is opened unable to go
negative, and a payout past its balance is refused) and fails on
``675dfd6``.
"""

from fastapi.testclient import TestClient

from domainkit_tests.test_accounting_api import (  # noqa: F401 - the fixtures.
    API,
    Headers,
    clerk,
    client,
    open_books,
    usd,
)


def test_an_account_opened_over_http_without_the_flag_may_not_go_negative(
    client: TestClient, clerk: Headers
) -> None:
    """A wallet opened with no ``may_go_negative``: a payout past it is refused."""
    books = open_books(client, clerk)
    wallet = client.post(
        f"{API}/accounts",
        json={
            "code": "2100",
            "name": "Customer wallet",
            "type": "liability",
            "currency": "USD",
            "holder": "customer-1",
        },
        headers=clerk,
    )
    assert wallet.status_code == 201, wallet.json()
    assert wallet.json()["may_go_negative"] is False
    payout = client.post(
        f"{API}/entries",
        json={
            "source": "payouts",
            "reference": "p-1",
            "booked_on": "2026-10-15",
            "lines": [
                {"account_id": wallet.json()["id"], "side": "debit", "amount": usd("500")},
                {"account_id": books["cash"]["id"], "side": "credit", "amount": usd("500")},
            ],
        },
        headers=clerk,
    )
    assert payout.status_code == 422, payout.json()
    assert payout.json()["code"] == "insufficient_funds"
