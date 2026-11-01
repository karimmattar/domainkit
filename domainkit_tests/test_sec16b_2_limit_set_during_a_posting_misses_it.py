"""SEC-16b-2: a limit set while a posting is in flight never counts that posting.

PERF-26 stopped keeping the movements of subjects no window bounds, and
moved SEC-15b-5's promise ("a limit set mid-window counts what moved
before it") to ``count_earlier``, which reads the ledger's postings in
the transaction that sets the limit. Nothing orders the two: a posting
whose ``enforce`` ran before the limit existed records no usage (the
subject was unbounded), and commits after ``count_earlier`` read the
postings (it was not visible yet). Setting a limit takes no lock a
posting takes, nor the reverse. The holder's 900 is then in neither the
usage nor the count, and another 900 passes a daily 1,000.

Before PERF-26 every movement wrote its usage row, so the in-flight
posting was counted once it committed. A holder (or a fraud analyst's
"stop this customer now" limit, the docs' use case) racing a limit change
loses the very movement the limit was set to catch.

PostgreSQL only: SQLite runs one writer at a time. The test asserts the
safe behaviour (the second 900 is refused) and fails on ``675dfd6``.
"""

import asyncio

import pytest

from domainkit.contrib.accounting import AccountingUnitOfWork, post_entry
from domainkit.contrib.accounting.hooks import on_stage_entry
from domainkit.contrib.limits import LimitExceeded, Window
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    books,
    egp,
    limit,
    period,
    spend,
    wallets,
)

pytestmark = pytest.mark.anyio


async def test_a_limit_set_while_a_payout_commits_counts_that_payout(
    books: str,
) -> None:
    """900 staged; a daily 1,000 for the holder set; the 900 commits; another 900."""
    if books.startswith("sqlite"):
        pytest.skip("SQLite runs one writer at a time: no posting is in flight.")
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    staged, go = asyncio.Event(), asyncio.Event()

    async def pause(uow: object, entry: object, accounts: object) -> None:
        # Limits' ``enforce`` ran before this hook (domain order): it found
        # no limit, so it counted nothing. Hold the posting uncommitted.
        if getattr(entry, "reference", None) == "p-1":
            staged.set()
            await go.wait()

    on_stage_entry(pause, domain="alpha")
    first = asyncio.create_task(
        post_entry(spend(wallet, till, egp("900"), "p-1"), AccountingUnitOfWork())
    )
    await asyncio.wait_for(staged.wait(), 10)
    await asyncio.wait_for(
        limit(
            code="wallet.daily",
            subject="h-1",
            window=Window.DAY,
            max_amount=egp("1000"),
            time_zone="UTC",
        ),
        10,
    )
    go.set()
    await asyncio.wait_for(first, 10)
    with pytest.raises(LimitExceeded):
        await post_entry(spend(wallet, till, egp("900"), "p-2"), AccountingUnitOfWork())
