"""Transfers: told to the bank and to subscribers once committed, retried."""

import io
from typing import Any

import pytest
from domainkit.contrib.messaging.testing import drain
from domainkit.management import call_command
from domainkit.testing import TestClient

from domains.transfers.domain import Transfer
from receiver import Receiver
from tests.conftest import FakeBank, subscribe

URL = "/api/v1/transfers"


def transfer(amount: str) -> dict[str, Any]:
    """A transfer of ``amount`` euros to a supplier, as JSON."""
    return {
        "payee": "Acme Supplies Ltd",
        "iban": "DE89370400440532013000",
        "amount": {"amount": amount, "currency": "EUR"},
    }


def test_a_transfer_is_told_once_it_commits(
    client: TestClient, clerk: TestClient, bank: FakeBank, receiver: Receiver
) -> None:
    """The request only commits; the worker calls the bank, then signs the webhook."""
    created = clerk.post(URL, json=transfer("250.00"))
    sent_in_the_request = list(bank.calls)

    client.run(drain)  # The worker, here.

    transfer_id = created.json()["id"]
    assert (created.status_code, sent_in_the_request) == (201, [])
    assert bank.keys == [transfer_id]
    read = clerk.get(f"{URL}/{transfer_id}").json()
    assert (read["status"], read["bank_reference"]) == (
        "sent",
        f"BANK-{transfer_id[:8]}",
    )
    [event] = receiver.events.values()
    assert event["type"] == "transfer.sent"
    assert event["data"] == {
        "transfer_id": transfer_id,
        "amount": "250.00",
        "currency": "EUR",
        "bank_reference": f"BANK-{transfer_id[:8]}",
    }


def test_nothing_is_told_of_a_transfer_that_did_not_commit(
    client: TestClient, clerk: TestClient, bank: FakeBank, receiver: Receiver
) -> None:
    """A refused transfer leaves nothing in the outbox."""
    refused = clerk.post(URL, json=transfer("0.00"))

    client.run(drain)

    assert (refused.status_code, refused.json()["code"]) == (422, "invalid_transfer")
    assert (bank.calls, receiver.events) == ([], {})


def test_a_failing_bank_is_tried_again_then_left_for_an_operator(
    client: TestClient, clerk: TestClient, bank: FakeBank, receiver: Receiver
) -> None:
    """Each attempt waits longer; the last one dead-letters the message.

    ``messages retry`` sends it again once the bank is back, with the same
    key, so the bank moves the money once.
    """
    bank.down = True
    transfer_id = clerk.post(URL, json=transfer("250.00")).json()["id"]

    client.run(drain, delayed=True)  # Every retry made due at once.
    dead = io.StringIO()
    call_command("messages", "list", "dead", stdout=dead)
    bank.down = False
    call_command("messages", "retry", "all", stdout=io.StringIO())
    client.run(drain)

    assert len(bank.calls) == 4  # Three attempts (the test settings'), and the retry.
    assert set(bank.keys) == {transfer_id}
    assert "transfers.send_to_bank" in dead.getvalue()
    assert clerk.get(f"{URL}/{transfer_id}").json()["status"] == "sent"
    assert len(receiver.events) == 1


def test_a_worker_that_dies_before_its_commit_moves_the_money_once(
    client: TestClient,
    clerk: TestClient,
    bank: FakeBank,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bank answered, then the commit never came: the message runs again.

    The bank is asked twice under the same key, and moves the money once;
    the subscribers hear of the transfer once.
    """
    mark_sent, crashes = Transfer.mark_sent, [RuntimeError("The worker died.")]

    def dies_once(sent: Transfer, bank_reference: str) -> None:
        if crashes:
            raise crashes.pop()
        mark_sent(sent, bank_reference)

    monkeypatch.setattr(Transfer, "mark_sent", dies_once)
    transfer_id = clerk.post(URL, json=transfer("250.00")).json()["id"]

    client.run(drain, delayed=True)

    assert bank.keys == [transfer_id, transfer_id]
    assert clerk.get(f"{URL}/{transfer_id}").json()["status"] == "sent"
    assert len(receiver.events) == 1


def test_a_failed_delivery_is_tried_again_then_replayed(
    client: TestClient,
    clerk: TestClient,
    staff: TestClient,
    bank: FakeBank,
    partner: tuple[Receiver, str],
) -> None:
    """A receiver that answers 404 fails each attempt; once fixed, staff replay it."""
    receiver, url = partner
    made = subscribe(staff, f"{url}/old-path")
    receiver.secret = made["secret"]
    subscription = f"/admin/webhooks/subscriptions/{made['id']}"
    clerk.post(URL, json=transfer("250.00"))

    client.run(drain, delayed=True)
    [failed] = staff.get(f"{subscription}/deliveries").json()
    staff.patch(subscription, json={"url": f"{url}/webhooks"})
    replayed = staff.post(f"/admin/webhooks/deliveries/{failed['id']}/replay")
    client.run(drain)

    assert (failed["status"], failed["attempts"]) == ("failed", 3)
    assert replayed.status_code == 202, replayed.json()
    assert list(receiver.events) == [f"msg_{failed['id'].replace('-', '')}"]
