"""SEC-15b-5: a limit set during its window ignores what moved earlier in it.

The docs: "with the ledger installed, every entry it books is counted",
and a daily limit bounds "the sum ... of movements in a window". But
``consume`` writes a usage row only for a subject some windowed limit
already bounds (``_windowed``); an entry booked while none does leaves
no row. So the limit an operator sets at 11:00 to stop a holder (the
docs' "raise or lower one customer's") does not see the 900 the holder
moved at 10:00: another 900 passes against a daily 1,000. The same
holds after an exemption for one subject (a disabled limit) is lifted,
and when a ``per_transaction``-only limit gains a window.

The test asserts the safe behaviour (the second 900 is refused) and
fails on ``d23a2ec``.
"""

from datetime import UTC, datetime

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry
from domainkit.contrib.limits import LimitExceeded, Window
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    books,
    egp,
    limit,
    period,
    spend,
    wallets,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]


async def test_a_daily_limit_set_at_eleven_counts_what_moved_at_ten() -> None:
    """900 at 10:00; a daily 1,000 for the holder at 11:00; another 900 at noon."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    with freeze_time(datetime(2026, 10, 30, 10, tzinfo=UTC)):
        await post_entry(spend(wallet, till, egp("900"), "p-1"), AccountingUnitOfWork())
    with freeze_time(datetime(2026, 10, 30, 11, tzinfo=UTC)):
        await limit(
            code="wallet.daily",
            subject="h-1",
            window=Window.DAY,
            max_amount=egp("1000"),
            time_zone="UTC",
        )
    with (
        freeze_time(datetime(2026, 10, 30, 12, tzinfo=UTC)),
        pytest.raises(LimitExceeded),
    ):
        await post_entry(spend(wallet, till, egp("900"), "p-2"), AccountingUnitOfWork())
