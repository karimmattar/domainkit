"""SEC-16b-9: a limit set after an approved reversal frees the payout it undid.

FIN-88's fix: "an approved reversal is counted against limits and frees
nothing", so "undoing a payout two people approved does not free the
holder's limit to draw it again" (ADR 0123, amendment). ``enforce`` does
that while the holder is bounded. But since PERF-26 an unbounded holder
keeps no usage, and a limit set later counts the morning from the
ledger (``count_earlier``), which skips every entry that was reversed,
and every reversal: the approved 2,000 payout and its approved reversal
both vanish, and the daily limit set at eleven starts from nothing. The
holder draws the 2,000 again, within the limit, exactly the outcome
FIN-88 was fixed to prevent; only the order (limit after reversal)
differs.

The test asserts the safe behaviour (the second payout is refused) and
fails on ``675dfd6``.
"""

import uuid
from datetime import UTC, datetime

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    AccountType,
    Line,
    PostEntry,
    ReverseEntry,
    post_entry,
    reverse_entry,
)
from domainkit.contrib.approvals import POST_ENTRY, REVERSE_ENTRY
from domainkit.contrib.approvals.guards import running
from domainkit.contrib.limits import (
    LimitExceeded,
    LimitsUnitOfWork,
    SetLimit,
    Window,
    set_limit,
)
from domainkit_tests.test_limits import TODAY, account, egp, period, spend
from domainkit_tests.test_limits_earlier import (  # noqa: F401 - the fixture.
    MORNING,
    approving,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("approving")]
ELEVEN = datetime(2026, 10, 30, 11, tzinfo=UTC)
NOON = datetime(2026, 10, 30, 12, tzinfo=UTC)


async def test_a_limit_set_after_an_approved_reversal_counts_the_payout() -> None:
    """2,000 paid and reversed on approval at nine; daily 2,500 at eleven; 2,000."""
    await period()
    till = await account("till")
    wallet = await account("wallet", AccountType.LIABILITY, holder="h-1")
    fund = PostEntry(
        source="topups",
        reference="f-1",
        booked_on=TODAY,
        lines=(Line.debit(till.id, egp("5000")), Line.credit(wallet.id, egp("5000"))),
    )
    payout = spend(wallet, till, egp("2000"), "p-1")
    with freeze_time(MORNING):
        with running(uuid.uuid4(), POST_ENTRY, fund, "maker", 2):
            await post_entry(fund, AccountingUnitOfWork())
        with running(uuid.uuid4(), POST_ENTRY, payout, "maker", 2):
            paid = await post_entry(payout, AccountingUnitOfWork())
        undo = ReverseEntry(entry_id=paid.id, booked_on=TODAY)
        with running(uuid.uuid4(), REVERSE_ENTRY, undo, "maker", 2):
            await reverse_entry(undo, AccountingUnitOfWork())
    with freeze_time(ELEVEN):
        await set_limit(
            SetLimit(
                code="daily",
                kind="holder",
                currency="EGP",
                window=Window.DAY,
                max_amount=egp("2500"),
                time_zone="UTC",
            ),
            LimitsUnitOfWork(),
        )
    again = spend(wallet, till, egp("2000"), "p-2")
    with freeze_time(NOON), pytest.raises(LimitExceeded):
        with running(uuid.uuid4(), POST_ENTRY, again, "maker", 2):
            await post_entry(again, AccountingUnitOfWork())
