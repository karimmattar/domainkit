"""Fixtures: the bank, faked; a receiver served on 127.0.0.1; a clerk and staff.

Nothing leaves the machine. The bank's API is a function that every call
of ``domainkit.outbound.client`` goes to (``stub``), and the receiver is served
on a port of the test process (``LiveServer``), where the worker, run here
by ``drain``, delivers the webhooks over HTTP.
"""

from collections.abc import Iterator

import httpx2
import pytest
from domainkit.contrib.webhooks import new_secret
from domainkit.outbound.client import stub
from domainkit.testing import ClientFor, LiveServer, SettingsOverrides, TestClient

from receiver import Receiver


class FakeBank:
    """The bank's API: it takes each transfer once, keyed by its id."""

    def __init__(self) -> None:
        """A bank that answers, until ``down`` says otherwise."""
        self.calls: list[httpx2.Request] = []
        self.down = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one call: 201 and a reference made from the key, or 503."""
        self.calls.append(request)
        if self.down:
            return httpx2.Response(503)
        key = request.headers["Idempotency-Key"]
        return httpx2.Response(201, json={"reference": f"BANK-{key[:8]}"})

    @property
    def keys(self) -> list[str]:
        """The ``Idempotency-Key`` of each call, in order."""
        return [call.headers["Idempotency-Key"] for call in self.calls]


@pytest.fixture
def bank() -> Iterator[FakeBank]:
    """The bank, for every call the test's process makes."""
    fake = FakeBank()
    with stub(fake, everywhere=True):
        yield fake


@pytest.fixture
def clerk(client_for: ClientFor) -> TestClient:
    """A clerk who requests transfers and reads them."""
    return client_for("transfers.add_transfer", "transfers.view_transfer")


@pytest.fixture
def staff(client_for: ClientFor) -> TestClient:
    """Staff who manage the webhook subscriptions."""
    return client_for(
        "webhooks.add_subscription",
        "webhooks.view_subscription",
        "webhooks.change_subscription",
        is_staff=True,
    )


@pytest.fixture
def partner(settings: SettingsOverrides) -> Iterator[tuple[Receiver, str]]:
    """The receiving service, served on 127.0.0.1, and its URL.

    Webhooks go to ``https`` URLs only, unless ``DEBUG`` is on, as on a
    developer's machine.
    """
    settings.DEBUG = True
    receiver = Receiver(new_secret())  # The subscription's, once it is made.
    with LiveServer(receiver.app) as server:
        yield receiver, server.url


def subscribe(staff: TestClient, url: str) -> dict[str, str]:
    """Subscribe ``url`` to ``transfer.sent``; the subscription, with its secret."""
    made = staff.post(
        "/admin/webhooks/subscriptions",
        json={"url": url, "event_types": ["transfer.sent"], "description": "Partner"},
    )
    assert made.status_code == 201, made.json()
    subscription: dict[str, str] = made.json()
    return subscription


@pytest.fixture
def receiver(partner: tuple[Receiver, str], staff: TestClient) -> Receiver:
    """The receiver, subscribed, with the secret it was given."""
    receiver, url = partner
    receiver.secret = subscribe(staff, f"{url}/webhooks")["secret"]
    return receiver
