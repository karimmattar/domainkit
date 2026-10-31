"""SEC-15b-2: a hold's capture posts an entry over ENTRY_THRESHOLDS unapproved.

The docs exempt ``capture_hold`` from the large-posting guard because
"they post what an earlier hold ... already allowed". But nothing guards
``place_hold``: no approval allowed the hold. A caller who may place and
capture holds reserves any amount on one account, then captures it into
any ``counter_account_id`` it names at capture time. The capture's
entry moves the whole amount, far over the threshold, with no request
and no checker.

The test asserts the safe behaviour (the capture is refused as
``approval_required``, or posts nothing over the threshold) and fails on
``d23a2ec``.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from domainkit import clock
from domainkit.contrib.accounting import (
    CAPTURES,
    AccountingUnitOfWork,
    CaptureHold,
    PlaceHold,
    capture_hold,
    place_hold,
)
from domainkit.contrib.accounting.tables import entries, postings
from domainkit.contrib.approvals import ApprovalRequired
from domainkit.db import connections
from domainkit_tests.test_approvals import (  # noqa: F401 - the fixture.
    TODAY,
    books,
    project,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("project")]


async def test_a_capture_over_the_threshold_needs_approval() -> None:
    """Threshold 1,000 dollars; a hold of 50,000 is captured into another account."""
    cash, sales = await books()
    hold = await place_hold(
        PlaceHold(
            account_id=cash.id,
            amount=usd("50000.00"),
            source="cards",
            reference="h-1",
            expires_at=clock.now() + timedelta(hours=1),
        ),
        AccountingUnitOfWork(),
    )
    try:
        await capture_hold(
            CaptureHold(hold_id=hold.id, counter_account_id=sales.id, booked_on=TODAY),
            AccountingUnitOfWork(),
        )
    except ApprovalRequired:
        pass  # The safe behaviour.

    async with connections.engine().connect() as connection:
        moved = (
            await connection.execute(
                select(postings.c.amount)
                .join(entries, entries.c.id == postings.c.entry_id)
                .where(entries.c.source == CAPTURES, postings.c.side == "debit")
            )
        ).scalars().all()
    assert moved == [], (
        f"a capture posted {moved} dollars over the 1,000 threshold, unapproved"
    )
