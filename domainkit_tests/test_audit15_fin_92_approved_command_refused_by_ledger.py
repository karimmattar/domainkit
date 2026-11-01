"""FIN-92: an approved command that books a large entry is refused by the ledger.

A bank wires its own maker-checker use case (a payout, a large
transfer, as the fintech profile's ``payments.make_transfer``) to the
books: the service, once approved, posts its entry. With
``ENTRY_THRESHOLDS`` set, as the profile sets them, the ledger's own
guard then asks for *another* approval: ``check(ENTRIES, command)`` only
lets through the approved command of ``accounting.post_entry``, and the
booking check only an approved ``PostEntry`` or ``CaptureHold``. The
payout two people approved fails (``approval_required``), so guards do
not compose: the project must submit every large posting twice, or turn
the ledger's guard off. The run of an approved request should satisfy
the ledger's threshold for the entries it books (its policy asked at
least as many approvers), or the docs should say how the two meet.
"""

import pytest

from domainkit.contrib.accounting import (
    Account,
    AccountingUnitOfWork,
    Line,
    PostEntry,
    post_entry,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.approvals import (
    Policy,
    RequestStatus,
    guard,
    requires_approval,
    submit_command,
)
from domainkit.contrib.messaging.testing import drain
from domainkit.ddd import Command
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    TODAY,
    acting,
    approve,
    books,
    count,
    project,
    stored,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


class Payout(Command):
    """Pay ``amount`` dollars out of the till: a project's guarded use case."""

    reference: str
    amount: str
    till: str
    bank: str


@requires_approval("tests.payout")
async def payout(command: Payout, uow: AccountingUnitOfWork) -> None:
    """Book the payout, once two people approved it."""
    amount = usd(command.amount)
    await post_entry(
        PostEntry(
            source="payouts",
            reference=command.reference,
            booked_on=TODAY,
            lines=(
                Line.debit(command.bank, amount),  # type: ignore[arg-type]
                Line.credit(command.till, amount),  # type: ignore[arg-type]
            ),
        ),
        uow,
    )


async def test_a_payout_two_people_approved_is_booked() -> None:
    """Approved under the project's policy, its entry is booked, once."""
    till, bank = await books()
    guard(
        "tests.payout",
        payout,
        unit_of_work=AccountingUnitOfWork,
        policies=[
            Policy("tests.payouts", approvals=2, permission="accounting.add_entry")
        ],
        domain="alpha",
    )
    command = Payout(reference="p-1", amount="5000.00", till=_id(till), bank=_id(bank))
    with acting("maker"):
        request = await submit_command("tests.payout", command, maker="maker")
    with acting("ann"):
        await approve(request, "ann")
    with acting("bob"):
        await approve(request, "bob")
    await drain()

    done = await stored(request.id)
    assert (done.status, done.reason) == (RequestStatus.EXECUTED, "")
    assert await count(entries, entries.c.reference == "p-1") == 1


def _id(account: Account) -> str:
    """The account's id, as the command holds it."""
    return str(account.id)
