"""What SEC-15b hardened: captures, seals and run tokens, parties, gone policies.

On the project of ``test_approvals``: the ledger guarded over 1,000
dollars, two approvals by holders of ``accounting.add_entry``.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    CaptureHold,
    PlaceHold,
    ReverseEntry,
    capture_hold,
    place_hold,
    reverse_entry,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.approvals import (
    CAPTURES,
    ApprovalRequest,
    ApprovalRequired,
    ApprovalsUnitOfWork,
    CancelRequest,
    InterestedParty,
    InvalidArguments,
    Policy,
    PolicyGone,
    RejectRequest,
    RequestStatus,
    RequestTampered,
    approve_request,
    cancel_request,
    execute_request,
    expire_requests,
    guard,
    reject_request,
    requires_approval,
    submit_command,
)
from domainkit.contrib.approvals.guards import running
from domainkit.contrib.approvals.services import ApproveRequest, _fail_tampered
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import Command
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    TODAY,
    Act,
    approve,
    approved_act,
    books,
    count,
    guard_acts,
    project,
    refused,
    sale,
    status_of,
    stored,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


async def hold_of(account_id: Any, amount: str, reference: str) -> Any:  # noqa: ANN401
    """A hold of ``amount`` dollars on the account."""
    return await place_hold(
        PlaceHold(
            account_id=account_id,
            amount=usd(amount),
            source="cards",
            reference=reference,
            expires_at=clock.now() + timedelta(hours=1),
        ),
        AccountingUnitOfWork(),
    )


async def test_a_large_capture_runs_once_approved_and_a_small_one_at_once() -> None:
    """Over the threshold, a capture is a request like a posting; under, it runs.

    Its reversal (what a capture posted, undone) is not asked again.
    """
    cash, sales = await books()
    small, large = (
        await hold_of(cash.id, "10.00", "h-1"),
        await hold_of(cash.id, "5000.00", "h-2"),
    )
    await capture_hold(
        CaptureHold(hold_id=small.id, counter_account_id=sales.id, booked_on=TODAY),
        AccountingUnitOfWork(),
    )
    capture = CaptureHold(
        hold_id=large.id, counter_account_id=sales.id, booked_on=TODAY
    )
    with refused(ApprovalRequired):
        await capture_hold(capture, AccountingUnitOfWork())

    request = await submit_command(CAPTURES, capture, maker="maker")
    assert request.policy == "accounting.large_capture"
    await approve(request, "ann")
    await approve(request, "bob")
    await drain()

    assert (await stored(request.id)).status is RequestStatus.EXECUTED
    async with AccountingUnitOfWork() as uow:
        captured = await uow.holds.get(large.id)
        assert captured.entry_id is not None
    reversal = await reverse_entry(
        ReverseEntry(entry_id=captured.entry_id, booked_on=TODAY),
        AccountingUnitOfWork(),
    )
    assert reversal.reverses == captured.entry_id


async def test_an_approved_run_books_only_what_it_approved() -> None:
    """Running one approved command, another large entry is still refused."""
    cash, sales = await books()
    first, second = (
        await hold_of(cash.id, "5000.00", "h-3"),
        await hold_of(cash.id, "5000.00", "h-4"),
    )

    def capture(hold: Any) -> CaptureHold:  # noqa: ANN401
        return CaptureHold(
            hold_id=hold.id, counter_account_id=sales.id, booked_on=TODAY
        )

    with running(uuid.uuid4(), CAPTURES, capture(first)), refused(ApprovalRequired):
        await capture_hold(capture(second), AccountingUnitOfWork())
    entry = sale(cash, sales, "5000.00", "s-1")
    with (
        running(uuid.uuid4(), "accounting.post_entry", entry),
        refused(ApprovalRequired),
    ):
        await capture_hold(capture(second), AccountingUnitOfWork())
    assert await count(entries) == 0


async def test_a_request_changed_around_the_application_is_not_decided() -> None:
    """Approve, reject, cancel, and expiry refuse a row whose seal fails."""
    guard_acts()
    made = [
        await submit_command("tests.act", Act(kind=f"t-{n}"), maker="maker")
        for n in range(3)
    ]
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests)
            .where(requests.c.id.in_([each.id for each in made]))
            .values(required=5)  # Other approvals needed, without the key.
        )
    message = (
        r"^The request was changed outside the application; it is neither "
        r"decided nor run\.$"
    )
    with refused(RequestTampered, message):
        await approve(made[0], "ann")
    with refused(RequestTampered):
        await reject_request(
            RejectRequest(request_id=made[0].id, approver="ann", reason="No."),
            ApprovalsUnitOfWork(),
        )
    with refused(RequestTampered):
        await cancel_request(
            CancelRequest(request_id=made[0].id, maker="maker"), ApprovalsUnitOfWork()
        )
    with clock.freeze_time(clock.now() + timedelta(days=4)):
        assert await expire_requests() == 0
    assert [(await stored(each.id)).status for each in made] == [
        RequestStatus.PENDING
    ] * 3


async def test_expiry_stops_at_a_batch_of_tampered_requests(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A full batch it cannot expire ends the round: it does not loop on them."""
    from domainkit.contrib.approvals import services

    guard_acts()
    monkeypatch.setattr(services, "EXPIRE_BATCH", 1)
    made = await submit_command("tests.act", Act(kind="t"), maker="maker")
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests).where(requests.c.id == made.id).values(required=5)
        )
    with (
        clock.freeze_time(clock.now() + timedelta(days=4)),
        caplog.at_level(logging.WARNING, services.__name__),
    ):
        assert await expire_requests() == 0
    assert [record.getMessage() for record in caplog.records] == [
        f"Approval request {made.id} is tampered with"
    ]


async def test_only_its_own_token_runs_an_approved_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A message without its run token, or with another, runs nothing."""
    from domainkit.contrib.approvals import services

    guard_acts()
    request = await approved_act("token")
    with caplog.at_level(logging.WARNING, services.__name__):
        await execute_request(request.id)
        await execute_request(request.id, "0" * 32)
    assert await status_of(request) == (RequestStatus.APPROVED, "")
    assert len(caplog.records) == 2
    await _fail_tampered(request.id)  # Sealed: left as it is.
    assert await status_of(request) == (RequestStatus.APPROVED, "")
    await execute_request(request.id, request.run_token)
    assert await status_of(request) == (RequestStatus.EXECUTED, "")
    done = await stored(request.id)
    assert (done.run_token, done.sealed()) == (None, True)


class Pay(Command):
    """A payment to someone, who may not approve it."""

    payee: str


async def test_whoever_a_command_is_about_does_not_decide_it() -> None:
    """The guard's parties are refused, as approvers and as rejecters."""

    async def pay(command: Pay, uow: object) -> None:
        """Nothing: only decided."""

    guarded = requires_approval("tests.pay")(pay)
    guard(
        "tests.pay",
        guarded,
        unit_of_work=AccountingUnitOfWork,
        policies=[Policy("tests.pays", approvals=2, permission="a.b")],
        parties=lambda command: [command.payee],
        domain="alpha",
    )
    request = await submit_command("tests.pay", Pay(payee="ann"), maker="maker")
    with refused(InterestedParty, r"^Whoever the command is about cannot decide it\.$"):
        await approve(request, "ann")
    with refused(InterestedParty):
        await reject_request(
            RejectRequest(request_id=request.id, approver="ann", reason="Mine."),
            ApprovalsUnitOfWork(),
        )
    assert (await approve(request, "bob")).approvals == 1


async def test_a_request_whose_policy_is_gone_is_neither_decided_nor_run() -> None:
    """Gone since it was submitted: no fallback to other rules."""
    guard_acts()
    pending = await submit_command("tests.act", Act(kind="p"), maker="maker")
    registry.clear()
    setup()
    with refused(
        PolicyGone,
        r"^The request's guard or policy no longer exists; it is neither decided "
        r"nor run\.$",
    ):
        await approve(pending, "ann")
    with refused(PolicyGone):
        await reject_request(
            RejectRequest(request_id=pending.id, approver="ann", reason="No."),
            ApprovalsUnitOfWork(),
        )

    class Other(Command):
        number: int

    guard_acts(command=Other)
    with refused(InvalidArguments):
        await approve_request(
            ApproveRequest(
                request_id=pending.id, approver="ann", fingerprint=pending.fingerprint
            ),
            ApprovalsUnitOfWork(),
        )


async def test_the_approved_command_runs_as_its_maker() -> None:
    """Its events, and anything charged to the actor, are the maker's."""
    from domainkit.context import current_request

    seen: list[str | None] = []

    async def spy(command: Act, uow: object) -> None:
        """Record who acts."""
        context = current_request()
        seen.append(None if context is None else context.actor)

    guard(
        "tests.spy",
        requires_approval("tests.spy")(spy),
        unit_of_work=lambda: None,
        policies=[Policy("tests.spies", permission="a.b")],
        domain="alpha",
    )
    request = await submit_command("tests.spy", Act(kind="x"), maker="mia")
    done: ApprovalRequest = await approve(request, "ann")
    await execute_request(done.id, done.run_token)
    assert seen == ["mia"]


async def test_expiry_goes_on_past_requests_it_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In one round: one decided since, one tampered with, then one to expire."""
    from domainkit.contrib.approvals.uow import RequestRepository

    guard_acts()
    decided, tampered, due = [
        await submit_command("tests.act", Act(kind=kind), maker="maker")
        for kind in ("decided", "tampered", "due")
    ]
    await cancel_request(
        CancelRequest(request_id=decided.id, maker="maker"), ApprovalsUnitOfWork()
    )
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests).where(requests.c.id == tampered.id).values(required=5)
        )
    rounds = iter([[decided.id, tampered.id, due.id], []])

    async def found(self: RequestRepository, now: Any, limit: int) -> list[Any]:  # noqa: ANN401
        return next(rounds)

    monkeypatch.setattr(RequestRepository, "due", found)
    with clock.freeze_time(clock.now() + timedelta(days=4)):
        assert await expire_requests() == 1
    assert (await stored(due.id)).status is RequestStatus.EXPIRED


async def test_a_tampered_approved_request_fails_and_says_which(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run with its token after its row was changed: failed, and logged."""
    from domainkit.contrib.approvals import services

    guard_acts()
    request = await approved_act("t")
    async with connections.engine().begin() as connection:
        await connection.execute(
            update(requests).where(requests.c.id == request.id).values(required=5)
        )
    with caplog.at_level(logging.WARNING, services.__name__):
        await execute_request(request.id, request.run_token)
    assert await status_of(request) == (RequestStatus.FAILED, "request_tampered")
    assert [record.getMessage() for record in caplog.records] == [
        f"Approval request {request.id} is tampered with"
    ]
