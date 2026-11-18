"""Holds: an authorization reserves money, then a capture pays or a release frees."""

from domainkit.testing import TestClient

from tests.money import URL, balance, key, usd


def hold(customer: TestClient, wallet: str, amount: str) -> str:
    """Hold ``amount`` on the wallet; the hold's id."""
    held = customer.post(
        f"{URL}/wallets/{wallet}/holds", json={"amount": usd(amount)}, headers=key()
    )
    assert held.status_code == 201, held.json()
    hold_id: str = held.json()["id"]
    return hold_id


def test_a_capture_pays_part_of_a_hold_and_frees_the_rest(
    customer: TestClient, clerk: TestClient, wallet: str
) -> None:
    """Held, the money stays in the wallet but cannot be spent twice."""
    hold_id = hold(customer, wallet, "50.00")
    held = balance(customer, wallet)

    captured = clerk.post(
        f"{URL}/holds/{hold_id}/capture", json={"amount": usd("20.00")}
    )

    assert held == {"balance": "100.00", "held": "50.00", "available": "50.00"}
    assert captured.json()["status"] == "captured"
    assert balance(customer, wallet) == {
        "balance": "80.00",
        "held": "0.00",
        "available": "80.00",
    }


def test_a_release_moves_nothing(
    customer: TestClient, clerk: TestClient, wallet: str
) -> None:
    """A cancelled authorization gives the whole amount back."""
    hold_id = hold(customer, wallet, "30.00")

    released = clerk.post(f"{URL}/holds/{hold_id}/release")

    assert released.json()["status"] == "released"
    assert balance(customer, wallet) == {
        "balance": "100.00",
        "held": "0.00",
        "available": "100.00",
    }


def test_what_is_held_cannot_be_spent(customer: TestClient, wallet: str) -> None:
    """A payment past what is available is refused, the hold untouched."""
    hold(customer, wallet, "80.00")
    body = {"amount": usd("30.00"), "description": "Order 1043"}

    refused = customer.post(
        f"{URL}/wallets/{wallet}/payments", json=body, headers=key()
    )

    assert (refused.status_code, refused.json()["code"]) == (422, "insufficient_funds")
    assert balance(customer, wallet)["available"] == "20.00"
