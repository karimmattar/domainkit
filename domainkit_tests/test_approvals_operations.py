"""Approvals for operators: the command, privacy export and erasure, and setup.

Commands run their own event loop, so these tests are synchronous and
reach the database through ``run_async``.
"""

import io
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_erase_subject, on_export_subject
from domainkit.conf import override_settings
from domainkit.contrib.accounting import AccountingUnitOfWork
from domainkit.contrib.approvals import (
    ApprovalRequest,
    ApprovalsUnitOfWork,
    ApproveRequest,
    Policy,
    RejectRequest,
    RequestStatus,
    approve_request,
    guard,
    guards,
    reject_request,
    submit_command,
)
from domainkit.contrib.approvals.rules import configured_rule
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import IsAuthenticated
from domainkit.management import CommandError, call_command, operators
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated
from domainkit_tests.test_approvals import PROJECT, Act, acting_guarded

ALLOW: Final = IsAuthenticated()
"""A rule instance ``APPROVE_RULE`` may name."""
NOT_A_RULE: Final = object()

type Action = tuple[str, str, dict[str, object]]


@pytest.fixture
def project(migrated: Migrated) -> Iterator[str]:
    """The project in a migrated database, the sample use case guarded."""
    with migrated(PROJECT) as url:
        guard(
            "tests.act",
            acting_guarded,
            unit_of_work=AccountingUnitOfWork,
            policies=[Policy("tests.acts", approvals=2, permission="a.b")],
            domain="alpha",
        )
        yield url


@pytest.fixture
def actions(monkeypatch: pytest.MonkeyPatch) -> list[Action]:
    """The operator actions the command records: ``(action, outcome, details)``."""
    found: list[Action] = []

    async def record(
        who: object, action: str, outcome: str, details: dict[str, object]
    ) -> None:
        found.append((action, outcome, details))

    monkeypatch.setattr(operators, "_record", record)
    return found


def run(*args: str) -> str:
    """``approvals`` with ``args``; what it printed."""
    output = io.StringIO()
    call_command("approvals", *args, "--operator", "tester", stdout=output)
    return output.getvalue()


def submitted(kind: str, maker: str = "maker") -> ApprovalRequest:
    """A pending request to act as ``kind``."""
    return run_async(submit_command("tests.act", Act(kind=kind), maker=maker))


def decide(request: ApprovalRequest, approver: str, *, reject: str = "") -> None:
    """``approver`` approves ``request``, or rejects it for ``reject``."""

    async def act() -> None:
        if reject:
            await reject_request(
                RejectRequest(request_id=request.id, approver=approver, reason=reject),
                ApprovalsUnitOfWork(),
            )
        else:
            await approve_request(
                ApproveRequest(
                    request_id=request.id,
                    approver=approver,
                    fingerprint=request.fingerprint,
                ),
                ApprovalsUnitOfWork(),
            )

    run_async(act())


def test_the_command_lists_shows_and_expires(
    project: str, actions: list[Action]
) -> None:
    """Pending requests by default; one in full; the overdue expired, and recorded."""
    first = submitted("first", maker="mak\x1b[2Jer")
    second = submitted("second")
    decide(second, "ann", reject="No.\x07")

    listed = run("list")
    assert str(first.id) in listed
    assert str(second.id) not in listed
    assert "mak\\u001b[2Jer" in listed
    assert str(second.id) in run("list", "--status", "rejected")
    assert run("list", "--status", "executed") == ""

    shown = run("show", str(second.id)).splitlines()
    assert shown[0] == f"{second.id}  tests.act  rejected"
    assert shown[1] == "maker maker, policy tests.acts, needs 2"
    assert shown[2] == f"fingerprint {second.fingerprint}"
    assert json.loads(shown[3]) == {"kind": "second"}
    assert shown[4].startswith("reject  ann  ")
    assert shown[5] == "reason: No.\\u0007"
    assert len(run("show", str(first.id)).splitlines()) == 4

    assert run("expire") == "Expired 0 request(s).\n"
    with clock.freeze_time(clock.now() + timedelta(days=3)):
        assert run("expire") == "Expired 1 request(s).\n"
    assert actions == [
        ("approvals.expire", "success", {"requests": 0}),
        ("approvals.expire", "success", {"requests": 1}),
    ]

    with pytest.raises(CommandError, match="needs a request's id"):
        run("show")
    with pytest.raises(CommandError, match="needs a request's id"):
        run("show", "nope")
    with pytest.raises(CommandError, match="No request is"):
        run("show", str(uuid.uuid4()))
    with pytest.raises(CommandError, match="--limit"):
        run("list", "--limit", "0")


def test_privacy_exports_a_persons_requests_and_decisions(project: str) -> None:
    """What they made, with its command, and what they decided."""
    made = submitted("mine", maker="subject")
    other = submitted("theirs")
    decide(other, "subject")
    (export,) = on_export_subject.of("approvals")

    found: dict[str, Any] = dict(run_async(export.call("subject")))

    assert [each["id"] for each in found["requests"]] == [str(made.id)]
    assert found["requests"][0]["arguments"] == {"kind": "mine"}
    assert found["requests"][0]["status"] == "pending"
    assert [each["request_id"] for each in found["decisions"]] == [str(other.id)]
    assert found["decisions"][0]["verdict"] == "approve"
    assert run_async(export.call("nobody")) == {}


def test_privacy_erasure_unlinks_a_person_and_keeps_the_record(project: str) -> None:
    """Their pending requests are cancelled, their commands and reasons blanked.

    Their id becomes one ``erased:`` id, so a request still shows that its
    maker and its approver were two people; what others wrote stays.
    """
    pending = submitted("pending", maker="subject")
    decided = submitted("decided", maker="subject")
    decide(decided, "ann", reject="Theirs.")
    approved = submitted("approved")
    decide(approved, "subject")
    rejected = submitted("rejected")
    decide(rejected, "subject", reject="Mine.")
    kept = submitted("kept")
    decide(kept, "bob", reject="Bob's.")
    (erase,) = on_erase_subject.of("approvals")

    counts = run_async(erase.call("subject", "request"))

    assert counts == {"requests": 2, "cancelled": 1, "decisions": 2}
    assert run_async(erase.call("subject", "again")) == {}

    async def read() -> list[ApprovalRequest]:
        async with ApprovalsUnitOfWork() as uow:
            return await uow.requests.filter()

    by_kind = {str(request.id): request for request in run_async(read())}
    first, second = by_kind[str(pending.id)], by_kind[str(decided.id)]
    assert first.status is RequestStatus.CANCELLED
    assert (first.arguments, second.arguments) == ({}, {})
    assert first.maker == second.maker
    assert first.maker.startswith("erased:")
    assert second.reason == ""
    assert by_kind[str(rejected.id)].reason == ""
    assert by_kind[str(kept.id)].reason == "Bob's."
    approvers = {each.approver for each in by_kind[str(approved.id)].decisions}
    assert approvers == {first.maker}
    assert by_kind[str(approved.id)].maker == "maker"


def test_setup_refuses_thresholds_and_rules_that_cannot_work(
    project: str,
) -> None:
    """A threshold not of its currency, and an approve rule that is none."""
    for approvals in (
        {"ENTRY_THRESHOLDS": {"USD": "1.001"}},
        {"APPROVE_RULE": "domainkit_tests.nowhere.Rule"},
        {"APPROVE_RULE": f"{__name__}.NOT_A_RULE"},
    ):
        registry.clear()
        with (
            override_settings(APPROVALS=approvals),
            pytest.raises(ImproperlyConfigured),
        ):
            setup()
    registry.clear()
    with override_settings(APPROVALS={"APPROVE_RULE": f"{__name__}.ALLOW"}):
        setup()
        assert configured_rule() is ALLOW
        assert list(guards()) == []  # No thresholds: postings are not guarded.
    registry.clear()
    rule_class = "domainkit.http.access.IsAuthenticated"
    with override_settings(APPROVALS={"APPROVE_RULE": rule_class}):
        setup()
        assert isinstance(configured_rule(), IsAuthenticated)
    registry.clear()
    setup()
