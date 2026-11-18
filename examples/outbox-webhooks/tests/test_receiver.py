"""The receiver keeps each signed delivery once, and refuses every other."""

import time

import pytest
from domainkit.contrib.webhooks import new_secret, sign
from domainkit.testing import TestClient

from receiver import Receiver

BODY = b'{"type":"transfer.sent","timestamp":"2027-01-04T10:00:00Z","data":{}}'


def signed(secret: str, body: bytes, *, age: int = 0) -> dict[str, str]:
    """The headers a delivery of ``body`` carries, signed ``age`` seconds ago."""
    timestamp = int(time.time()) - age
    return {
        "webhook-id": "msg_2b7f",
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(secret, "msg_2b7f", timestamp, body),
    }


@pytest.fixture
def receiver() -> Receiver:
    """A receiver that holds its subscription's secret."""
    return Receiver(new_secret())


def test_a_signed_delivery_is_kept_once(receiver: Receiver) -> None:
    """Delivered twice under one webhook-id, it is acted on once."""
    with TestClient(receiver.app) as partner:
        first = partner.post(
            "/webhooks", content=BODY, headers=signed(receiver.secret, BODY)
        )
        again = partner.post(
            "/webhooks", content=BODY, headers=signed(receiver.secret, BODY)
        )

    assert (first.status_code, again.status_code) == (204, 204)
    assert list(receiver.events) == ["msg_2b7f"]


@pytest.mark.parametrize(
    ("secret", "body", "age"),
    [
        (new_secret(), BODY, 0),  # Signed by someone else.
        (None, BODY.replace(b"{}", b'{"amount":"9999.00"}'), 0),  # Changed on the way.
        (None, BODY, 600),  # Signed ten minutes ago: a replay.
    ],
)
def test_a_delivery_that_does_not_hold_is_refused(
    receiver: Receiver, secret: str | None, body: bytes, age: int
) -> None:
    """Answered 400, and kept nowhere."""
    headers = signed(secret or receiver.secret, BODY, age=age)

    with TestClient(receiver.app) as partner:
        refused = partner.post("/webhooks", content=body, headers=headers)
        unsigned = partner.post("/webhooks", content=BODY)

    assert (refused.status_code, unsigned.status_code) == (400, 400)
    assert receiver.events == {}
