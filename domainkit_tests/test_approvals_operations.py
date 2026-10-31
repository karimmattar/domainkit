"""Approvals for operators: the command, privacy export and erasure, and setup.

Commands run their own event loop, so these tests are synchronous and
reach the database through ``run_async``.
"""

import io
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Final

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
    requires_approval,
    submit_command,
)
from domainkit.contrib.approvals.rules import configured_rule
from domainkit.ddd import Command
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

    with pytest.raises(CommandError, match=r"^show needs a request's id\.$"):
        run("show")
    with pytest.raises(CommandError, match="needs a request's id"):
        run("show", "nope")
    with pytest.raises(CommandError, match="No request is"):
        run("show", str(uuid.uuid4()))
    with pytest.raises(CommandError, match="--limit"):
        run("list", "--limit", "0")


def test_privacy_exports_a_persons_requests_and_decisions(
    project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What they made, with its command, and what they decided: by time, then id.

    The ids are chosen so that neither their order nor the order the rows
    went in is the order of the export.
    """
    from domainkit.contrib.approvals import models

    ids = iter(uuid.UUID(int=number) for number in (2, 1, 3, 4, 5, 6, 30, 20, 10))
    monkeypatch.setattr(models, "uuid4", lambda: next(ids))
    now = clock.now()
    with clock.freeze_time(now):
        second, first = submitted("a", maker="subject"), submitted("b", maker="subject")
    with clock.freeze_time(now - timedelta(hours=1)):
        early = submitted("early", maker="subject")
    theirs, others, third = submitted("theirs"), submitted("others"), submitted("x")
    with clock.freeze_time(now - timedelta(minutes=1)):
        decide(third, "subject")  # The earliest decision, with the largest id.
    with clock.freeze_time(now):
        decide(theirs, "subject")
        decide(others, "subject", reject="No.")
    (export,) = on_export_subject.of("approvals")

    found = run_async(export.call("subject"))

    assert found == {
        "requests": [
            {
                "id": str(request.id),
                "action": "tests.act",
                "arguments": request.arguments,
                "fingerprint": request.fingerprint,
                "status": "pending",
                "reason": "",
                "submitted_at": request.submitted_at.isoformat(),
            }
            for request in (early, first, second)
        ],
        "decisions": [
            {
                "request_id": str(request.id),
                "verdict": verdict,
                "decided_at": when.isoformat(),
            }
            for request, verdict, when in (
                (third, "approve", now - timedelta(minutes=1)),
                (others, "reject", now),
                (theirs, "approve", now),
            )
        ],
    }
    assert run_async(export.call("nobody")) == {}


async def _stored() -> dict[str, ApprovalRequest]:
    """Every request, by id."""
    async with ApprovalsUnitOfWork() as uow:
        return {str(request.id): request for request in await uow.requests.filter()}


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
    decide(approved, "bob", reject="Bob's too.")
    rejected = submitted("rejected")
    decide(rejected, "subject", reject="Mine.")
    kept = submitted("kept")
    decide(kept, "bob", reject="Bob's.")
    bystander = submitted("bystander")  # Someone else's, pending: left alone.
    before = {key: request.version for key, request in run_async(_stored()).items()}
    (erase,) = on_erase_subject.of("approvals")

    with clock.freeze_time(clock.now()) as frozen:
        counts = run_async(erase.call("subject", "request"))

    assert counts == {"requests": 2, "cancelled": 1, "decisions": 2}
    assert run_async(erase.call("subject", "again")) == {}
    by_kind = run_async(_stored())
    first, second = by_kind[str(pending.id)], by_kind[str(decided.id)]
    assert (first.status, first.decided_at) == (RequestStatus.CANCELLED, frozen.now())
    assert (first.arguments, second.arguments) == ({}, {})
    assert first.maker == second.maker
    assert first.maker.startswith("erased:")
    assert second.reason == ""
    assert by_kind[str(rejected.id)].reason == ""
    assert by_kind[str(kept.id)].reason == "Bob's."
    assert by_kind[str(approved.id)].reason == "Bob's too."
    approvers = {each.approver for each in by_kind[str(approved.id)].decisions}
    assert approvers == {first.maker, "bob"}
    assert by_kind[str(approved.id)].maker == "maker"
    changed = {key: by_kind[key].version - version for key, version in before.items()}
    assert changed == {
        str(pending.id): 3,
        str(decided.id): 2,
        str(approved.id): 0,
        str(rejected.id): 1,
        str(kept.id): 0,
        str(bystander.id): 0,
    }
    assert by_kind[str(bystander.id)].status is RequestStatus.PENDING


def test_setup_refuses_thresholds_and_rules_that_cannot_work(
    project: str,
) -> None:
    """A threshold not of its currency, and an approve rule that is none."""
    for approvals, message in (
        ({"ENTRY_THRESHOLDS": {"USD": "1.001"}}, "threshold"),
        (
            {"APPROVE_RULE": "domainkit_tests.nowhere.Rule"},
            (
                r"^APPROVALS\.APPROVE_RULE 'domainkit_tests\.nowhere\.Rule' cannot be "
                r"imported: "
            ),
        ),
        (
            {"APPROVE_RULE": f"{__name__}.NOT_A_RULE"},
            (
                rf"^APPROVALS\.APPROVE_RULE '{__name__}\.NOT_A_RULE' is not an access "
                r"rule \(BasePermission\)\.$"
            ),
        ),
    ):
        registry.clear()
        with (
            override_settings(APPROVALS=approvals),
            pytest.raises(ImproperlyConfigured, match=message),
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


class Pair(Command):
    """A command of two fields, to show them in order."""

    b: str
    a: str


@requires_approval("tests.pair")
async def pair(command: Pair, uow: object) -> None:
    """Nothing: only shown."""


def test_the_command_lists_in_order_within_its_limit(project: str) -> None:
    """Oldest first, then by id; at most ``--limit`` (1 to 1000, 50 by default)."""
    now = clock.now()
    with clock.freeze_time(now):
        same = sorted((submitted(f"s-{n}") for n in range(50)), key=lambda r: r.id)
    with clock.freeze_time(now - timedelta(hours=1)):
        early = submitted("early")

    def ids(*args: str) -> list[str]:
        return [line.split()[0] for line in run("list", *args).splitlines()]

    assert ids() == [str(each.id) for each in (early, *same[:49])]
    assert ids("--limit", "1") == [str(early.id)]
    assert len(ids("--limit", "1000")) == 51
    with pytest.raises(CommandError, match=r"^--limit is from 1 to 1000\.$"):
        run("list", "--limit", "1001")
    with pytest.raises(SystemExit):
        run("list", "--status", "lost")
    with pytest.raises(SystemExit):
        run("lost")


def test_the_command_shows_decisions_in_order_and_keys_sorted(project: str) -> None:
    """Decisions by when they were taken; the command's keys in order."""
    guard(
        "tests.pair",
        pair,
        unit_of_work=object,
        policies=[Policy("tests.pairs", approvals=3, permission="a.b")],
        domain="alpha",
    )
    request = run_async(submit_command("tests.pair", Pair(b="2", a="1"), maker="m"))
    now = clock.now()
    with clock.freeze_time(now + timedelta(hours=1)):
        decide(request, "amy")
    with clock.freeze_time(now):
        decide(request, "zed")

    shown = run("show", str(request.id)).splitlines()

    assert shown[3] == '{"a": "1", "b": "2"}'
    assert [line.split()[1] for line in shown[4:]] == ["zed", "amy"]

    async def approvers() -> list[str]:
        async with ApprovalsUnitOfWork() as uow:
            found = await uow.requests.get(request.id)
        return [each.approver for each in found.decisions]

    assert run_async(approvers()) == ["zed", "amy"]
