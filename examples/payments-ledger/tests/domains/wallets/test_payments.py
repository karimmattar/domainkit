"""Payments: a retried request books once, and a wallet is never overdrawn."""

import asyncio
from uuid import UUID, uuid4

import pytest
from domainkit.money import Money
from domainkit.testing import AsyncClientFor, ClientFor, TestClient

from domains.wallets import controllers
from tests.money import URL, balance, key, usd

pytestmark = pytest.mark.anyio


def test_a_retried_payment_books_once(customer: TestClient, wallet: str) -> None:
    """The same request with the same key gets the first answer, replayed."""
    body = {"amount": usd("30.00"), "description": "Order 1042"}
    sent = key()

    first = customer.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=sent)
    retry = customer.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=sent)

    assert (first.status_code, retry.status_code) == (201, 201)
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()
    assert balance(customer, wallet)["balance"] == "70.00"


def test_the_books_book_a_key_once_after_its_answer_is_gone(
    customer: TestClient, wallet: str
) -> None:
    """Past the key's lifetime the route runs again; the entry is the first one.

    Its reference is the wallet and the key, so the ledger returns what it
    booked under them instead of booking again.
    """
    sent, amount = uuid4(), Money.of("30.00", "USD")

    async def pay_twice() -> list[object]:
        account = await controllers.find_wallet(UUID(wallet))
        return [
            (await controllers.pay(account, amount, sent, "Order 1042")).id
            for _ in range(2)
        ]

    first, again = customer.run(pay_twice)

    assert first == again
    assert balance(customer, wallet)["balance"] == "70.00"


def test_money_moves_only_with_a_key(customer: TestClient, wallet: str) -> None:
    """Without an Idempotency-Key, a client could not retry safely: refused."""
    body = {"amount": usd("30.00"), "description": "Order 1042"}

    refused = customer.post(f"{URL}/wallets/{wallet}/payments", json=body)

    assert (refused.status_code, refused.json()["code"]) == (
        400,
        "idempotency_key_required",
    )
    assert balance(customer, wallet)["balance"] == "100.00"


async def test_two_payments_at_once_never_overdraw(
    async_client_for: AsyncClientFor, db: None
) -> None:
    """Of two payments of 70.00 from 100.00 sent together, one is refused."""
    await controllers.open_books()
    customer = await async_client_for()
    clerk = await async_client_for("accounting.add_entry")
    wallet = (await customer.post(f"{URL}/wallets")).json()["id"]
    await clerk.post(
        f"{URL}/wallets/{wallet}/deposits",
        json={"amount": usd("100.00")},
        headers=key(),
    )
    body = {"amount": usd("70.00"), "description": "Order 1042"}

    answers = await asyncio.gather(
        customer.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=key()),
        customer.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=key()),
    )

    assert sorted((each.status_code, each.json().get("code")) for each in answers) == [
        (201, None),
        (422, "insufficient_funds"),
    ]
    read = (await customer.get(f"{URL}/wallets/{wallet}")).json()
    assert (read["balance"]["amount"], read["available"]["amount"]) == (
        "30.00",
        "30.00",
    )


def test_a_wallet_is_its_holders_alone(
    customer: TestClient, client_for: ClientFor, wallet: str
) -> None:
    """Another customer's wallet is not found, to read or to pay from."""
    stranger = client_for()
    body = {"amount": usd("30.00"), "description": "Order 1042"}

    read = stranger.get(f"{URL}/wallets/{wallet}")
    paid = stranger.post(f"{URL}/wallets/{wallet}/payments", json=body, headers=key())

    assert (read.status_code, paid.status_code) == (404, 404)
    assert balance(customer, wallet)["balance"] == "100.00"
