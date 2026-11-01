"""FIN-88: one person reverses an entry two others had to approve.

``APPROVALS.ENTRY_THRESHOLDS`` makes an entry over its amount wait for two
approvers; the ledger's ``on_stage_entry`` check then refuses any booking
over it that no approved request makes, so a capture or a crafted command
cannot get round it. Reversals are let through
(``builtin.check_booking``: "reversals post what an entry posted before,
and are left to the limits"), and limits *release* what the reversed entry
counted. So whoever holds ``accounting.reverse_entry`` alone books the
same large amount the other way: a customer's approved 5,000 payout,
paid out of the bank already, is credited back to their account, and its
limits are freed for them to draw it again. A reversal moves as much
money as the entry it undoes, and needs the approval that entry needed.
"""

import pytest

from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    ReverseEntry,
    post_entry,
    reverse_entry,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.approvals import (
    ApprovalPending,
    ApprovalRequired,
    RequestStatus,
    submit_command,
)
from domainkit.contrib.approvals.builtin import POST_ENTRY
from domainkit.contrib.messaging.testing import drain
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    TODAY,
    acting,
    approve,
    books,
    count,
    project,
    sale,
    stored,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


async def test_reversing_an_entry_over_the_threshold_needs_its_approval() -> None:
    """The approved 5,000 entry is not undone on one person's say."""
    cash, sales = await books()
    large = sale(cash, sales, "5000.00", "big")
    with acting("maker"):
        request = await submit_command(POST_ENTRY, large, maker="maker")
    with acting("ann"):
        await approve(request, "ann")
    with acting("bob"):
        await approve(request, "bob")
    await drain()
    assert (await stored(request.id)).status is RequestStatus.EXECUTED
    async with AccountingUnitOfWork(readonly=True) as uow:
        posted = await uow.entries.by_reference("shop", "big")
    assert posted is not None

    with (
        acting("mallory"),
        pytest.raises((ApprovalRequired, ApprovalPending)),
    ):
        await reverse_entry(
            ReverseEntry(entry_id=posted.id, booked_on=TODAY), AccountingUnitOfWork()
        )

    assert await count(entries, entries.c.reverses == posted.id) == 0


async def test_a_small_entry_is_still_reversed_at_once() -> None:
    """Under the threshold, a reversal needs no one else (control for the above)."""
    cash, sales = await books()
    small = await post_entry(
        sale(cash, sales, "10.00", "small"), AccountingUnitOfWork()
    )

    with acting("clerk"):
        reversal = await reverse_entry(
            ReverseEntry(entry_id=small.id, booked_on=TODAY), AccountingUnitOfWork()
        )

    assert reversal.reverses == small.id
