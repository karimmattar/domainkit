"""Payouts over the threshold: another person approves the exact request."""

from domainkit.contrib.audit.testing import audit_entries
from domainkit.contrib.messaging.testing import Outbox
from domainkit.testing import TestClient

from tests.people import APPROVER, MAKER, REQUESTS, URL, Person


def test_a_large_payout_runs_once_another_person_approves_it(
    client: TestClient, maker: Person, approver: Person, outbox: Outbox
) -> None:
    """Submitted and approved, each act confirmed; the worker sends it as the maker."""
    request = maker.submit("25000.00")

    approved = approver.approve(request)
    outbox.run_queued()  # The worker, here: it runs the approved command.

    assert request["status"] == "pending"
    assert approved.json()["status"] == "approved"
    assert approver.api.get(f"{REQUESTS}/{request['id']}").json()["status"] == (
        "executed"
    )
    assert [item["payee"] for item in maker.api.get(URL).json()["items"]] == [
        "Acme Supplies Ltd"
    ]
    trail = [
        (entry.action, entry.actor)
        for entry in client.run(audit_entries)
        if entry.action.startswith(("approvals.", "payouts.", "auth.action"))
    ]
    assert trail == [
        ("auth.action_authorized", maker.id),  # The maker confirmed the request,
        ("approvals.request_submitted", maker.id),  # submitted it,
        ("auth.action_authorization_used", maker.id),  # and so spent the act.
        ("auth.action_authorized", approver.id),  # The approver confirmed,
        ("approvals.approval_given", approver.id),
        ("approvals.request_approved", approver.id),  # the one approval needed,
        ("auth.action_authorization_used", approver.id),
        ("payouts.payout_sent", maker.id),  # and the worker sent it, as the maker.
        ("approvals.request_executed", maker.id),
    ]


def test_the_maker_never_approves_their_own_payout(client: TestClient) -> None:
    """Even holding the approvers' permission: asking to approve is refused."""
    both = Person(client, *MAKER, *APPROVER)
    request = both.submit("25000.00")

    asked = both.api.post(
        f"{REQUESTS}/{request['id']}/approve/authorization",
        json={"fingerprint": request["fingerprint"]},
    )

    assert (asked.status_code, asked.json()["code"]) == (403, "maker_cannot_decide")


def test_an_approval_is_for_the_request_its_approver_confirmed(
    maker: Person, approver: Person
) -> None:
    """Confirmed for one request, an authorization approves no other."""
    first, second = maker.submit("25000.00"), maker.submit("30000.00")
    act = f"{REQUESTS}/{first['id']}/approve"
    headers = approver.confirm(act, {"fingerprint": first["fingerprint"]})

    elsewhere = approver.api.post(
        f"{REQUESTS}/{second['id']}/approve",
        json={"fingerprint": second["fingerprint"]},
        headers=headers,
    )

    assert (elsewhere.status_code, elsewhere.json()["code"]) == (
        403,
        "action_not_authorized",
    )
    assert approver.api.get(f"{REQUESTS}/{second['id']}").json()["status"] == "pending"


def test_a_rejected_payout_is_never_sent(
    maker: Person, approver: Person, outbox: Outbox
) -> None:
    """One rejection ends the request, with its reason; nothing runs."""
    request = maker.submit("25000.00")

    rejected = approver.api.post(
        f"{REQUESTS}/{request['id']}/reject", json={"reason": "No such invoice."}
    )
    outbox.run_queued()

    assert (rejected.json()["status"], rejected.json()["reason"]) == (
        "rejected",
        "No such invoice.",
    )
    assert maker.api.get(URL).json()["items"] == []


def test_the_maker_withdraws_a_request(maker: Person, approver: Person) -> None:
    """Cancelled by its maker, a request can no longer be approved."""
    request = maker.submit("25000.00")

    cancelled = maker.api.post(
        f"{REQUESTS}/{request['id']}/cancel", json={"reason": "Paid by cheque."}
    )
    late = approver.api.post(
        f"{REQUESTS}/{request['id']}/approve/authorization",
        json={"fingerprint": request["fingerprint"]},
    )

    assert cancelled.json()["status"] == "cancelled"
    assert (late.status_code, late.json()["code"]) == (409, "request_not_pending")
