"""The ledger's movements are counted as the posting commits (SEC-16b-2).

``enforce`` stages an entry's movements as it is booked; ``count_staged``
counts them on the commit, under a shared lock of each ledger kind that
``count_earlier`` takes alone on PostgreSQL.
"""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import timedelta

import pytest
from sqlalchemy import insert, select

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    AccountingUnitOfWork,
    Entry,
    post_entry,
)
from domainkit.contrib.accounting.ledger import REVERSAL_SOURCE, book, lock_accounts
from domainkit.contrib.limits import (
    InvalidLimit,
    LimitExceeded,
    LimitsUnitOfWork,
    Movement,
    MovementOutOfRange,
    RemoveLimit,
    SetLimit,
    Window,
    purge_usage,
    remove_limit,
    set_limit,
)
from domainkit.contrib.limits.postings import _staged
from domainkit.contrib.limits.tables import kept
from domainkit.db import connections
from domainkit.db.hooks import on_stage
from domainkit_tests.test_limits import (  # noqa: F401 - the fixture.
    NOON,
    TODAY,
    books,
    counted,
    egp,
    limit,
    period,
    refused,
    spend,
    wallets,
)

pytestmark = pytest.mark.anyio


async def test_a_limit_set_while_a_posting_commits_waits_for_it(
    books: str,  # noqa: F811 - the fixture.
) -> None:
    """The posting counted, found no limit, and holds the lock: the limit waits."""
    if books.startswith("sqlite"):
        pytest.skip("SQLite runs one writer at a time.")
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    counted, go = asyncio.Event(), asyncio.Event()

    async def pause(uow: object, events: Sequence[object], tasks: object) -> None:
        # After limits' ``count_staged`` (domain order), before the commit.
        if any(getattr(each, "reference", None) == "p-1" for each in events):
            counted.set()
            await go.wait()

    on_stage(pause, domain="alpha")
    first = asyncio.create_task(
        post_entry(spend(wallet, till, egp("900"), "p-1"), AccountingUnitOfWork())
    )
    await asyncio.wait_for(counted.wait(), 10)
    setting = asyncio.create_task(
        limit(code="wallet.daily", window=Window.DAY, max_amount=egp("1000"))
    )
    await asyncio.sleep(0.5)
    waited = not setting.done()
    go.set()
    await asyncio.wait_for(first, 10)
    await asyncio.wait_for(setting, 10)

    assert waited
    with refused(LimitExceeded):
        await post_entry(spend(wallet, till, egp("900"), "p-2"), AccountingUnitOfWork())


@pytest.mark.usefixtures("books")
async def test_what_a_rolled_back_transaction_staged_is_not_counted() -> None:
    """Staged, then rolled back: the next transaction of the session finds none."""
    async with AccountingUnitOfWork() as uow:
        await uow.session.execute(select(1))
        _staged(uow.session, create=True).append("first")  # type: ignore[arg-type]
        _staged(uow.session, create=True).append("second")  # type: ignore[arg-type]
        await uow.session.rollback()
        await uow.session.execute(select(1))
        after = _staged(uow.session)
        _staged(uow.session, create=True).append("third")  # type: ignore[arg-type]
        taken, again = _staged(uow.session), _staged(uow.session)

    assert (after, taken, again) == ([], ["third"], [])  # type: ignore[comparison-overlap]


@pytest.mark.usefixtures("books")
async def test_entries_booked_in_one_transaction_are_each_counted() -> None:
    """A reversal that gives back the payout, then another payout: both counted."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("1000"))
    paid = await post_entry(
        spend(wallet, till, egp("900"), "p-1"), AccountingUnitOfWork()
    )
    async with AccountingUnitOfWork() as uow:
        accounts = await lock_accounts(uow, [wallet.id, till.id])
        lines = paid.reversal_lines()
        undo = Entry.draft(
            REVERSAL_SOURCE,
            str(paid.id),
            TODAY,
            lines,
            reverses=paid.id,
            max_lines=len(lines),
        )
        again = Entry.draft(
            "payments",
            "p-2",
            TODAY,
            spend(wallet, till, egp("950"), "p-2").lines,
            max_lines=2,
        )
        await book(uow, undo, accounts)
        await book(uow, again, accounts)
        await uow.commit()

    with refused(LimitExceeded):  # 950 counted; the 900 given back.
        await post_entry(spend(wallet, till, egp("100"), "p-3"), AccountingUnitOfWork())


@pytest.mark.usefixtures("books")
async def test_a_movement_of_nothing_nobody_or_too_much_is_refused() -> None:
    """Zero, no subject, or past usage's columns: each refused as such."""
    with refused(InvalidLimit):
        Movement(egp("0"), {"holder": "h-1"})
    with refused(InvalidLimit):
        Movement(egp("1"), {})
    with refused(MovementOutOfRange):
        Movement(egp("100000000000000"), {"holder": "h-1"})
    assert Movement(egp("99999999999999"), {"holder": "h-1"}).amount


@pytest.mark.usefixtures("books")
async def test_the_marks_of_kept_reversals_are_purged_with_the_usage() -> None:
    """Older than ``KEEP_USAGE`` (35 days), a mark goes; one that old exactly stays."""
    old, newer = uuid.uuid4(), uuid.uuid4()
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(kept),
            [
                {"entry_id": old, "at": NOON},
                {"entry_id": newer, "at": NOON + timedelta(days=20)},
            ],
        )

    async def marks() -> set[uuid.UUID]:
        async with connections.engine().connect() as connection:
            return set(await connection.scalars(select(kept.c.entry_id)))

    with freeze_time(NOON + timedelta(days=35)):
        await purge_usage()
        exactly = await marks()
    with freeze_time(NOON + timedelta(days=36)):
        await purge_usage()
        later = await marks()

    assert (exactly, later) == ({old, newer}, {newer})


@pytest.mark.usefixtures("books")
async def test_removing_one_exemption_counts_back_for_its_subject_only() -> None:
    """Two holders exempt from the default; one exemption removed: one counted."""
    daily = {
        "DEFAULTS": [
            {
                "code": "wallet.daily",
                "kind": "holder",
                "currency": "EGP",
                "window": "day",
                "max_amount": "1000",
                "time_zone": "UTC",
            }
        ]
    }
    with override_settings(LIMITS=daily):
        await period()
        (first,), (till,) = await wallets("h-1", 1)
        (second,), (other,) = await wallets("h-2", 1)
        with freeze_time(NOON - timedelta(hours=3)):
            exemptions = [
                await set_limit(
                    SetLimit(
                        code="wallet.daily",
                        kind="holder",
                        currency="EGP",
                        subject=holder,
                        window=Window.DAY,
                        max_amount=egp("1000"),
                        enabled=False,
                    ),
                    LimitsUnitOfWork(),
                )
                for holder in ("h-1", "h-2")
            ]
            await post_entry(
                spend(first, till, egp("900"), "p-1"), AccountingUnitOfWork()
            )
            await post_entry(
                spend(second, other, egp("900"), "p-2"), AccountingUnitOfWork()
            )
        with freeze_time(NOON - timedelta(hours=1)):
            await remove_limit(
                RemoveLimit(limit_id=exemptions[0].id), LimitsUnitOfWork()
            )

    assert await counted() == 1
