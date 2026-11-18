"""Fixtures: the books opened on a fixed day, a customer, and a clerk."""

import pytest
from domainkit.testing import ClientFor, FreezeTime, FrozenClock, TestClient

from domains.wallets.controllers import open_books
from tests.money import deposit

CLERK = (
    "accounting.add_entry",
    "accounting.capture_hold",
    "accounting.release_hold",
    "accounting.reverse_entry",
    "accounting.view_period",
)
"""What the back office may do: deposits, holds, refunds, and the reports."""


@pytest.fixture(autouse=True)
def clock(freeze_time: FreezeTime) -> FrozenClock:
    """Every entry is booked on 4 January 2027, in the period the books open."""
    return freeze_time("2027-01-04T10:00:00Z")


@pytest.fixture
def books(client: TestClient) -> None:
    """January's period, the bank, and the merchants' account, as seed opens them."""
    client.run(open_books)


@pytest.fixture
def customer(client_for: ClientFor) -> TestClient:
    """A customer: any logged-in user may open and use wallets of their own."""
    return client_for()


@pytest.fixture
def clerk(client_for: ClientFor) -> TestClient:
    """A clerk of the back office."""
    return client_for(*CLERK)


@pytest.fixture
def wallet(books: None, customer: TestClient, clerk: TestClient) -> str:
    """The customer's wallet, with 100.00 USD deposited."""
    opened = customer.post("/api/v1/wallets")
    assert opened.status_code == 201, opened.json()
    wallet_id: str = opened.json()["id"]
    deposit(clerk, wallet_id, "100.00")
    return wallet_id
