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

The test asserts the safe behaviour and fails on ``d23a2ec``. The fix
refuses the reversal of a reversal in the ledger
(``reversal_not_reversible``): no money moves, the window is as the
reversal left it, and a payment of the window's 100 passes once, the
next being refused. (As first written, the test expected the reversal
of the reversal to pass and be counted; the ledger now books no such
entry, so it checks the refusal and that nothing left uncounted.)
"""

from decimal import Decimal

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    ReversalNotReversible,
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
        # Reversing the reversal would pay the 100 out of the wallet again.
        with pytest.raises(ReversalNotReversible) as refused:
            await reverse_entry(
                ReverseEntry(entry_id=undone.id), AccountingUnitOfWork()
            )
        assert refused.value.code == "reversal_not_reversible"
        async with AccountingUnitOfWork(readonly=True) as uow:
            assert (await uow.accounts.get(wallet.id)).balance == Decimal("5000")
        (used,) = await usage_of("holder", "h-1")
        assert used.used == egp("0"), f"the window holds {used.used}"
        await post_entry(spend(wallet, till, egp("100"), "p-2"), AccountingUnitOfWork())
        with pytest.raises(LimitExceeded):
            await post_entry(
                spend(wallet, till, egp("100"), "p-3"), AccountingUnitOfWork()
            )
