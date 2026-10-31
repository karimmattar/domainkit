"""SEC-15b-3: reversing a reversal moves the money again, counted by no limit.

Limits count every entry the ledger books, except a reversal, which
"counts nothing, and releases what the entry it reverses counted"
(``postings.enforce``). ``reverse_entry`` accepts any entry, a reversal
too. So: pay 100 out of a wallet (counted); reverse it (released: the
window is empty again); reverse the reversal: 100 leaves the wallet a
second time, and nothing is counted, because its entry is a reversal and
the entry it reverses counted nothing. The wallet then pays another 100
inside the same day: 200 out against a daily 100, and the cycle repeats
for as much as the wallet holds.

The same path re-runs a large entry posted by an approved request with
no approval: the ledger books ``RR`` as a reversal, which approvals
never guards.

The test asserts the safe behaviour (after the reversal of the reversal,
the window holds 100, and another 100 is refused) and fails on
``d23a2ec``.
"""

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    ReverseEntry,
    post_entry,
    reverse_entry,
)
from domainkit.contrib.limits import LimitExceeded, Window, usage_of
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    NOON,
    books,
    egp,
    limit,
    period,
    spend,
    wallets,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]


async def test_a_reversed_reversal_counts_against_the_window() -> None:
    """Daily 100 out of the holder's wallets: 100, reversed, reversed again."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        paid = await post_entry(
            spend(wallet, till, egp("100"), "p-1"), AccountingUnitOfWork()
        )
        undone = await reverse_entry(
            ReverseEntry(entry_id=paid.id), AccountingUnitOfWork()
        )
        # Reversing the reversal pays the 100 out of the wallet again.
        await reverse_entry(ReverseEntry(entry_id=undone.id), AccountingUnitOfWork())
        (used,) = await usage_of("holder", "h-1")
        assert used.used == egp("100"), (
            f"100 left the wallet again and the window holds {used.used}"
        )
        with pytest.raises(LimitExceeded):
            await post_entry(
                spend(wallet, till, egp("100"), "p-2"), AccountingUnitOfWork()
            )
