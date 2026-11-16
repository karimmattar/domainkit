"""Guards whose maker runs the approved command, and parties found by a coroutine.

A guard ``by_maker`` queues nothing once approved: the maker's own call,
made again, runs the service as the approved request, once (user report
A9). ``parties`` may be a coroutine function, which may read the database
(user report P3).
"""

import asyncio
from collections.abc import Iterable
from typing import Final
from uuid import UUID

import pytest

from domainkit.contrib.accounting import AccountingUnitOfWork, AccountType, OpenAccount
from domainkit.contrib.accounting import open_account as open_ledger_account
from domainkit.contrib.approvals import (
    ApprovalPending,
    ApprovalRequired,
    ApprovalsUnitOfWork,
    InterestedParty,
    MakerNotAllowed,
    Policy,
    RejectRequest,
    RequestStatus,
    current_run,
    execute_request,
    guard,
    guards,
    reject_request,
    requires_approval,
    submit_command,
)
from domainkit.contrib.approvals.guards import Running, running
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.messaging.testing import drain
from domainkit.ddd import Command
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    acting,
    approve,
    project,
    refused,
    stored,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]

PAYOUT: Final = "tests.payout"
seen: list[Running | None] = []


class Payout(Command):
    """Open an account for a payee: the act the maker runs once approved."""

    code: str
    payee: str = "nobody"


@requires_approval(PAYOUT)
async def payout(command: Payout, uow: AccountingUnitOfWork) -> None:
    """Open the account ``code``; note the run it was."""
    seen.append(current_run())
    await open_ledger_account(
        OpenAccount(
            code=command.code, name="P", type=AccountType.ASSET, currency="USD"
        ),
        uow,
    )


async def payees(command: Payout) -> Iterable[str]:
    """The payee, found as a lookup in the database would be: awaited."""
    await asyncio.sleep(0)
    return [command.payee]


def guard_payouts(permission: str | None = None) -> None:
    """Guard payouts: two approvals by holders of ``add_account``, run by the maker."""
    seen.clear()
    guard(
        PAYOUT,
        payout,
        unit_of_work=AccountingUnitOfWork,
        policies=[
            Policy("tests.payouts", approvals=2, permission="accounting.add_account")
        ],
        permission=permission,
        parties=payees,
        by_maker=True,
        domain="alpha",
    )


async def opened(code: str) -> bool:
    """Whether the account ``code`` exists."""
    async with AccountingUnitOfWork() as uow:
        return any(account.code == code for account in await uow.accounts.filter())


async def test_the_maker_runs_the_approved_command_with_the_same_call() -> None:
    """Submitted, found waiting, approved, run once by its maker, never the worker."""
    guard_payouts()
    with acting("ann"):
        with refused(ApprovalPending) as first:
            await payout(Payout(code="p-1"), AccountingUnitOfWork())
        with refused(ApprovalPending) as again:
            await payout(Payout(code="p-1"), AccountingUnitOfWork())
    request = await stored(first.value.request_id)  # type: ignore[attr-defined]
    await approve(request, "bob")
    approved = await approve(request, "carol")
    await drain()
    await execute_request(request.id, approved.run_token)
    waiting = await stored(request.id)
    with acting("dave"), refused(ApprovalPending) as other:
        await payout(Payout(code="p-1"), AccountingUnitOfWork())
    before = await opened("p-1")
    with acting("ann"):
        await payout(Payout(code="p-1"), AccountingUnitOfWork())
        with refused(ApprovalPending) as later:
            await payout(Payout(code="p-1"), AccountingUnitOfWork())

    assert again.value.request_id == request.id  # type: ignore[attr-defined]
    assert waiting.status is RequestStatus.APPROVED
    assert other.value.request_id != request.id  # type: ignore[attr-defined]
    assert before is False
    assert await opened("p-1")
    [run] = seen
    assert run is not None
    assert (run.request_id, run.maker, run.required) == (request.id, "ann", 2)
    assert (await stored(request.id)).status is RequestStatus.EXECUTED
    assert later.value.request_id not in (request.id, other.value.request_id)  # type: ignore[attr-defined]


async def test_no_one_acting_and_another_run_are_refused() -> None:
    """Without an actor there is no maker; inside another run, nothing is submitted."""
    guard_payouts()
    with refused(ApprovalRequired):
        await payout(Payout(code="p-2"), AccountingUnitOfWork())
    other = running(UUID(int=1), "tests.other", Payout(code="x"), "ann")
    with acting("ann"), other, refused(ApprovalRequired):
        await payout(Payout(code="p-2"), AccountingUnitOfWork())
    async with ApprovalsUnitOfWork() as uow:
        assert await uow.requests.filter() == []


async def test_a_maker_who_lost_the_permission_runs_nothing() -> None:
    """The maker is checked as a run checks them; the request stays approved."""
    guard_payouts(permission="accounting.add_account")
    async with AuthUnitOfWork() as uow:
        ann = await uow.users.create_user("ann", None)
        await uow.commit()
    request = await submit_command(PAYOUT, Payout(code="p-3"), maker=str(ann.id))
    await approve(request, "bob")
    await approve(request, "carol")

    with acting(str(ann.id)), refused(MakerNotAllowed):
        await payout(Payout(code="p-3"), AccountingUnitOfWork())

    assert (await stored(request.id)).status is RequestStatus.APPROVED
    assert not await opened("p-3")


async def test_parties_may_be_found_by_a_coroutine() -> None:
    """The payee neither approves nor rejects; plain ``parties_of`` says to await."""
    guard_payouts()
    command = Payout(code="p-4", payee="eve")
    request = await submit_command(PAYOUT, command, maker="ann")
    registered = guards()[PAYOUT]

    with refused(InterestedParty):
        await approve(request, "eve")
    with refused(InterestedParty):
        await reject_request(
            RejectRequest(request_id=request.id, approver="eve", reason="Mine."),
            ApprovalsUnitOfWork(),
        )
    with pytest.raises(ImproperlyConfigured, match="aparties_of"):
        registered.parties_of(command)

    assert await registered.aparties_of(command) == {"eve"}
    assert (await stored(request.id)).status is RequestStatus.PENDING
