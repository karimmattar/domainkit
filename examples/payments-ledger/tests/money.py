"""Requests that move money, as a client sends them."""

from typing import Any
from uuid import uuid4

from domainkit.testing import TestClient

URL = "/api/v1"


def usd(amount: str) -> dict[str, str]:
    """``amount`` US dollars, as JSON."""
    return {"amount": amount, "currency": "USD"}


def key() -> dict[str, str]:
    """A new ``Idempotency-Key``: one per operation, sent again only on a retry."""
    return {"Idempotency-Key": str(uuid4())}


def deposit(clerk: TestClient, wallet_id: str, amount: str) -> None:
    """Book ``amount`` that reached the bank for the wallet."""
    booked = clerk.post(
        f"{URL}/wallets/{wallet_id}/deposits",
        json={"amount": usd(amount)},
        headers=key(),
    )
    assert booked.status_code == 201, booked.json()


def balance(customer: TestClient, wallet_id: str) -> dict[str, Any]:
    """The wallet's balance, held, and available amounts, as text."""
    read = customer.get(f"{URL}/wallets/{wallet_id}")
    assert read.status_code == 200, read.json()
    return {
        name: read.json()[name]["amount"] for name in ("balance", "held", "available")
    }
