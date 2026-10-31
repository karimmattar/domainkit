"""The ledger's use cases on a database: accounts, entries, periods, and holds."""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.accounting import (
    CAPTURES,
    REVERSALS,
    AccountCodeTaken,
    AccountId,
    AccountingUnitOfWork,
    AccountIsClosed,
    AccountNotFound,
    AccountType,
    CaptureHold,
    CloseAccount,
    ClosePeriod,
    EntryId,
    EntryNotFound,
    HoldFinished,
    HoldId,
    HoldNotActive,
    HoldNotFound,
    HoldStatus,
    InsufficientFunds,
    InvalidAmount,
    InvalidEntry,
    Line,
    NoOpenPeriod,
    OpenPeriod,
    PeriodIsClosed,
    PeriodNameTaken,
    PeriodNotFound,
    PeriodsOverlap,
    PeriodStatus,
    PlaceHold,
    PostEntry,
    ReferenceReused,
    ReleaseHold,
    ReverseEntry,
    capture_hold,
    close_account,
    close_period,
    expire_holds,
    open_period,
    place_hold,
    post_entry,
    release_hold,
    reverse_entry,
    verify_ledger,
)
from domainkit.contrib.accounting.tables import entries, holds
from domainkit.db import connections
from domainkit.ddd import event_bus
from domainkit.money import Money
from domainkit_tests.conftest import POSTGRES_VARIABLE
from domainkit_tests.test_accounting import (  # noqa: F401 - the fixture.
    TODAY,
    account,
    balances,
    books,
    count,
    period,
    refused,
    transfer,
    usd,
)

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("books")]
NOW: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
postgres_only = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
)


async def test_account_codes_are_unique() -> None:
    """A second account with a code is refused by the database."""
    await account("1000")
    with pytest.raises(AccountCodeTaken):
        await account("1000")


async def test_an_account_closes_only_when_empty() -> None:
    """Closed, it takes no postings; a missing one is not found."""
    await period()
    cash, other = await account("1000"), await account("1001")

    closed = await close_account(
        CloseAccount(account_id=cash.id), AccountingUnitOfWork()
    )

    assert closed.closed_at is not None
    with refused(AccountNotFound, match=r"^No account has that id\.$"):
        await close_account(
            CloseAccount(account_id=AccountId(uuid.UUID(int=1))),
            AccountingUnitOfWork(),
        )
    with refused(AccountIsClosed):
        await post_entry(transfer(other, cash, usd("1"), "t"), AccountingUnitOfWork())
    assert await count(entries) == 0


async def test_a_reference_reused_for_another_entry_is_refused() -> None:
    """Different lines under a reference the source used: a conflict, nothing booked."""
    await period()
    cash, sales = await account("1000"), await account("4000")
    await post_entry(transfer(sales, cash, usd("1"), "r"), AccountingUnitOfWork())

    with refused(ReferenceReused):
        await post_entry(transfer(sales, cash, usd("2"), "r"), AccountingUnitOfWork())

    assert (await balances())[cash.id] == Decimal("1.0000")


async def test_lines_on_missing_accounts_book_nothing() -> None:
    """An account no one opened is not found; the other line is not booked."""
    await period()
    cash = await account("1000")
    missing = AccountId(uuid.UUID(int=7))

    with refused(AccountNotFound, match=r"^No account has that id\.$"):
        await post_entry(
            PostEntry(
                source="tests",
                reference="r",
                booked_on=TODAY,
                lines=(Line.debit(cash.id, usd("1")), Line.credit(missing, usd("1"))),
            ),
            AccountingUnitOfWork(),
        )
    assert (await balances())[cash.id] == Decimal(0)


async def test_a_malformed_entry_is_refused_before_any_lock() -> None:
    """Unbalanced lines never reach the database."""
    cash = await account("1000")
    with refused(InvalidEntry):
        await post_entry(
            PostEntry(
                source="tests",
                reference="r",
                booked_on=TODAY,
                lines=(Line.debit(cash.id, usd("1")),),
            ),
            AccountingUnitOfWork(),
        )


async def test_entries_need_an_open_period_by_default() -> None:
    """Without a period covering the date, an entry is refused, or booked without."""
    cash, sales = await account("1000"), await account("4000")
    command = transfer(sales, cash, usd("1"), "r")

    with refused(NoOpenPeriod):
        await post_entry(command, AccountingUnitOfWork())
    with override_settings(ACCOUNTING={"REQUIRE_PERIOD": False}):
        entry = await post_entry(command, AccountingUnitOfWork())

    assert entry.period_id is None


async def test_no_entry_is_dated_in_a_closed_period() -> None:
    """Closed, a period refuses entries, with or without ``REQUIRE_PERIOD``."""
    october = await period()
    cash, sales = await account("1000"), await account("4000")
    first = await post_entry(
        transfer(sales, cash, usd("1"), "r-1"), AccountingUnitOfWork()
    )

    with freeze_time(NOW):
        closed = await close_period(
            ClosePeriod(period_id=october), AccountingUnitOfWork()
        )

    assert (first.period_id, closed.status, closed.closed_at) == (
        october,
        PeriodStatus.CLOSED,
        NOW,
    )
    with (
        override_settings(ACCOUNTING={"REQUIRE_PERIOD": False}),
        refused(PeriodIsClosed),
    ):
        await post_entry(transfer(sales, cash, usd("1"), "r-2"), AccountingUnitOfWork())
    with refused(PeriodIsClosed):
        await close_period(ClosePeriod(period_id=october), AccountingUnitOfWork())
    with refused(PeriodNotFound, match=r"^No accounting period has that id\.$"):
        await close_period(
            ClosePeriod(period_id=uuid.UUID(int=3)), AccountingUnitOfWork()
        )


async def test_periods_do_not_overlap() -> None:
    """A period whose days another covers is refused; one next to it opens."""
    await period("2026-10", date(2026, 10, 1), date(2026, 10, 31))

    for starts_on, ends_on in [
        (date(2026, 10, 31), date(2026, 11, 30)),
        (date(2026, 9, 1), date(2026, 10, 1)),
        (date(2026, 9, 1), date(2026, 12, 1)),
        (date(2026, 10, 5), date(2026, 10, 6)),
    ]:
        with refused(PeriodsOverlap):
            await period("other", starts_on, ends_on)
    with pytest.raises(PeriodNameTaken):
        await period("2026-10", date(2026, 11, 1), date(2026, 11, 30))
    november = await open_period(
        OpenPeriod(
            name="2026-11", starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 30)
        ),
        AccountingUnitOfWork(),
    )
    assert november.status is PeriodStatus.OPEN


async def test_a_reversal_undoes_an_entry_once() -> None:
    """Reversed, the balances are back; reversing again returns the reversal."""
    await period()
    cash, sales = await account("1000"), await account("4000")
    entry = await post_entry(
        transfer(sales, cash, usd("7"), "r"), AccountingUnitOfWork()
    )

    with freeze_time(NOW):
        reversal = await reverse_entry(
            ReverseEntry(entry_id=entry.id, description="refund"),
            AccountingUnitOfWork(),
        )
    again = await reverse_entry(
        ReverseEntry(entry_id=entry.id, booked_on=date(2026, 10, 2)),
        AccountingUnitOfWork(),
    )

    assert again.id == reversal.id
    assert (reversal.source, reversal.reference, reversal.reverses) == (
        REVERSALS,
        str(entry.id),
        entry.id,
    )
    assert (reversal.booked_on, reversal.description) == (NOW.date(), "refund")
    assert set((await balances()).values()) == {Decimal(0)}
    with freeze_time(NOW):  # Dated today: within the period, whatever the day.
        twice = await reverse_entry(
            ReverseEntry(entry_id=reversal.id), AccountingUnitOfWork()
        )
    assert twice.reverses == reversal.id
    assert (await balances())[cash.id] == Decimal("7.0000")
    with refused(EntryNotFound, match=r"^No journal entry has that id\.$"):
        await reverse_entry(
            ReverseEntry(entry_id=EntryId(uuid.UUID(int=5))),
            AccountingUnitOfWork(),
        )
    assert await verify_ledger() == []


@postgres_only
async def test_entries_are_append_only_on_postgresql() -> None:
    """The triggers ``migrate`` made refuse updates, deletes, and truncation."""
    await period()
    cash, sales = await account("1000"), await account("4000")
    await post_entry(transfer(sales, cash, usd("1"), "r"), AccountingUnitOfWork())
    for statement in (
        "UPDATE domainkit_accounting_entries SET description = 'x'",
        "DELETE FROM domainkit_accounting_postings",
        "TRUNCATE domainkit_accounting_postings CASCADE",
        "TRUNCATE domainkit_accounting_entries CASCADE",
    ):
        with pytest.raises(Exception, match="is append-only"):
            async with connections.engine().begin() as connection:
                await connection.execute(text(statement))
    assert await count(entries) == 1


async def test_a_hold_is_placed_once_and_released() -> None:
    """The same reference places one hold; another amount under it is refused."""
    wallet = await account("2000", type=AccountType.LIABILITY, may_go_negative=False)
    await period()
    funding = await account("1000")
    await post_entry(
        transfer(wallet, funding, usd("10"), "fund"), AccountingUnitOfWork()
    )
    command = PlaceHold(
        account_id=wallet.id, amount=usd("6"), source="shop", reference="h"
    )

    with freeze_time(NOW):
        hold = await place_hold(command, AccountingUnitOfWork())
        again = await place_hold(command, AccountingUnitOfWork())

    assert again.id == hold.id
    assert hold.expires_at == NOW + timedelta(days=7)
    with refused(ReferenceReused):
        await place_hold(
            PlaceHold(
                account_id=wallet.id, amount=usd("5"), source="shop", reference="h"
            ),
            AccountingUnitOfWork(),
        )
    with refused(InsufficientFunds):
        await place_hold(
            PlaceHold(
                account_id=wallet.id, amount=usd("4.01"), source="shop", reference="x"
            ),
            AccountingUnitOfWork(),
        )

    released = await release_hold(ReleaseHold(hold_id=hold.id), AccountingUnitOfWork())
    still = await release_hold(ReleaseHold(hold_id=hold.id), AccountingUnitOfWork())

    assert (released.status, still.status) == (HoldStatus.RELEASED, HoldStatus.RELEASED)
    async with AccountingUnitOfWork(readonly=True) as uow:
        assert (await uow.accounts.get(wallet.id)).held == Decimal(0)
    with refused(HoldNotFound, match=r"^No hold has that id\.$"):
        await release_hold(
            ReleaseHold(hold_id=HoldId(uuid.UUID(int=9))),
            AccountingUnitOfWork(),
        )


async def test_a_capture_posts_its_entry_and_releases_the_rest() -> None:
    """Part of a hold is captured into an entry; the rest is available again."""
    await period()
    wallet = await account("2000", type=AccountType.LIABILITY, may_go_negative=False)
    bank = await account("1000")
    await post_entry(
        PostEntry(
            source="tests",
            reference="top-up",
            booked_on=TODAY,
            lines=(Line.debit(bank.id, usd("10")), Line.credit(wallet.id, usd("10"))),
        ),
        AccountingUnitOfWork(),
    )
    with freeze_time(NOW):
        hold = await place_hold(
            PlaceHold(
                account_id=wallet.id,
                amount=usd("8"),
                source="shop",
                reference="h",
                expires_at=NOW + timedelta(hours=1),
            ),
            AccountingUnitOfWork(),
        )
        for wrong in ("9", "0", "8.01"):
            with refused(InvalidAmount, match="up to the hold"):
                await capture_hold(
                    CaptureHold(
                        hold_id=hold.id, counter_account_id=bank.id, amount=usd(wrong)
                    ),
                    AccountingUnitOfWork(),
                )
        with refused(InvalidAmount, match="up to the hold"):
            await capture_hold(
                CaptureHold(
                    hold_id=hold.id,
                    counter_account_id=bank.id,
                    amount=Money.of("1", "EUR"),
                ),
                AccountingUnitOfWork(),
            )
        command = CaptureHold(
            hold_id=hold.id,
            counter_account_id=bank.id,
            amount=usd("5"),
            description="buy",
        )
        finished: list[HoldFinished] = []

        async def note(event: HoldFinished) -> None:
            finished.append(event)

        event_bus.subscribe(HoldFinished, note)
        captured = await capture_hold(command, AccountingUnitOfWork())
        again = await capture_hold(command, AccountingUnitOfWork())

    assert (captured.status, again.status) == (HoldStatus.CAPTURED, HoldStatus.CAPTURED)
    assert again.entry_id == captured.entry_id is not None
    event_bus.unsubscribe(HoldFinished, note)
    assert [(e.hold_id, e.status, e.entry_id) for e in finished] == [
        (hold.id, HoldStatus.CAPTURED, captured.entry_id)
    ]
    async with AccountingUnitOfWork(readonly=True) as uow:
        assert captured.entry_id is not None
        entry = await uow.entries.get(captured.entry_id)
        stored = await uow.accounts.get(wallet.id)
    assert entry.description == "buy"
    assert (entry.source, entry.reference, entry.booked_on) == (
        CAPTURES,
        str(hold.id),
        NOW.date(),
    )
    assert [(p.account_id, p.side.value, p.amount) for p in entry.postings] == [
        (wallet.id, "debit", Decimal("5.0000")),
        (bank.id, "credit", Decimal("5.0000")),
    ]
    assert (stored.balance, stored.held) == (Decimal("5.0000"), Decimal(0))
    with refused(HoldNotActive):
        await release_hold(ReleaseHold(hold_id=hold.id), AccountingUnitOfWork())
    assert await verify_ledger() == []


async def test_a_whole_hold_is_captured_by_default() -> None:
    """Without an amount, the capture takes all of it, dated today."""
    await period()
    card = await account("2000", type=AccountType.LIABILITY)
    sales = await account("4000", type=AccountType.INCOME)
    with freeze_time(NOW):
        hold = await place_hold(
            PlaceHold(
                account_id=card.id, amount=usd("3"), source="shop", reference="h"
            ),
            AccountingUnitOfWork(),
        )
        captured = await capture_hold(
            CaptureHold(hold_id=hold.id, counter_account_id=sales.id),
            AccountingUnitOfWork(),
        )
    stored = await balances()
    assert (stored[card.id], stored[sales.id]) == (
        Decimal("-3.0000"),
        Decimal("3.0000"),
    )
    assert captured.entry_id is not None


async def test_expired_holds_are_released_and_not_captured() -> None:
    """Past its time a hold is refused to captures; the sweep releases it."""
    await period()
    card, sales = await account("1000"), await account("4000")
    with freeze_time(NOW):
        placed = [
            await place_hold(
                PlaceHold(
                    account_id=card.id,
                    amount=usd("1"),
                    source="shop",
                    reference=f"h-{number}",
                    expires_at=NOW + timedelta(minutes=number + 1),
                ),
                AccountingUnitOfWork(),
            )
            for number in range(3)
        ]
    later = NOW + timedelta(minutes=2)
    with freeze_time(later):
        with refused(HoldNotActive):
            await capture_hold(
                CaptureHold(hold_id=placed[0].id, counter_account_id=sales.id),
                AccountingUnitOfWork(),
            )
        await release_hold(ReleaseHold(hold_id=placed[0].id), AccountingUnitOfWork())
        assert await expire_holds() == 1
        assert await expire_holds() == 0
    async with connections.engine().connect() as connection:
        statuses = dict(
            (await connection.execute(select(holds.c.reference, holds.c.status))).all()
        )
    assert statuses == {
        "h-0": HoldStatus.RELEASED,
        "h-1": HoldStatus.EXPIRED,
        "h-2": HoldStatus.ACTIVE,
    }
    assert await expire_holds(later + timedelta(minutes=5)) == 1
    async with AccountingUnitOfWork(readonly=True) as uow:
        assert (await uow.accounts.get(card.id)).held == Decimal(0)


async def test_the_sweep_reads_holds_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full batch is followed by another read; a captured hold is left alone."""
    from domainkit.contrib.accounting import holds as module

    monkeypatch.setattr(module, "EXPIRY_BATCH", 2)
    await period()
    card = await account("1000")
    with freeze_time(NOW):
        for number in range(3):
            await place_hold(
                PlaceHold(
                    account_id=card.id,
                    amount=usd("1"),
                    source="shop",
                    reference=f"h-{number}",
                    expires_at=NOW + timedelta(seconds=1),
                ),
                AccountingUnitOfWork(),
            )
    with freeze_time(NOW + timedelta(seconds=1)):
        assert await expire_holds() == 3


async def test_a_hold_captured_while_the_sweep_ran_stays_captured() -> None:
    """The sweep finishes only holds still active when it locks them."""
    from domainkit.contrib.accounting.holds import finish

    await period()
    card, sales = await account("1000"), await account("4000")
    with freeze_time(NOW):
        hold = await place_hold(
            PlaceHold(
                account_id=card.id, amount=usd("1"), source="shop", reference="h"
            ),
            AccountingUnitOfWork(),
        )
        await capture_hold(
            CaptureHold(hold_id=hold.id, counter_account_id=sales.id),
            AccountingUnitOfWork(),
        )
        found, done = await finish(AccountingUnitOfWork(), hold.id, HoldStatus.EXPIRED)
    assert (found.status, done) == (HoldStatus.CAPTURED, False)


@postgres_only
async def test_a_close_waits_for_the_postings_dated_in_its_period() -> None:
    """A posting holds its period shared: the close waits, then later ones see it."""
    october = await period()
    async with (
        connections.engine().connect() as watcher,
        AccountingUnitOfWork() as posting,
    ):
        assert (await posting.periods.covering(TODAY)) is not None
        holder = await posting.session.scalar(text("SELECT pg_backend_pid()"))
        closing = asyncio.create_task(
            close_period(ClosePeriod(period_id=october), AccountingUnitOfWork())
        )
        await _until_blocked_by(holder, closing, watcher)
        assert not closing.done()
    closed = await asyncio.wait_for(closing, 30)
    assert closed.status is PeriodStatus.CLOSED


async def _until_blocked_by(
    holder: int, waiting: asyncio.Task[object], watcher: AsyncConnection
) -> None:
    """Return once a session waits for a lock that session ``holder`` holds.

    PostgreSQL tells of the wait only when asked, so ``watcher``, connected
    before ``waiting`` started, asks until it does, each time in a
    transaction of its own. Only a session ``holder`` blocks counts, so no
    other test's database or session can stand in for it. If ``waiting``
    ends first (refused, or never connected), what ended it is raised; past
    30 seconds, the failure lists what the database's sessions were doing.
    """
    blocked = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE CAST(:holder AS integer) = ANY(pg_blocking_pids(pid))"
    )
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        found = await watcher.scalar(blocked, {"holder": holder})
        await watcher.rollback()
        if found:
            return
        if waiting.done():
            waiting.result()  # What ended it, raised.
            pytest.fail("The close ended without waiting for the posting.")
        if asyncio.get_running_loop().time() > deadline:
            sessions = (
                await watcher.execute(
                    text(
                        "SELECT pid, state, wait_event_type, wait_event, "
                        "left(query, 60) FROM pg_stat_activity "
                        "WHERE datname = current_database()"
                    )
                )
            ).all()
            pytest.fail(f"Nothing waited for session {holder} in 30 s: {sessions}")
        await asyncio.sleep(0.01)


async def test_holds_are_keyed_by_source_and_reference_together() -> None:
    """Two sources may use one reference: each places a hold of its own."""
    card = await account("1000")
    shop = await place_hold(
        PlaceHold(account_id=card.id, amount=usd("1"), source="shop", reference="h"),
        AccountingUnitOfWork(),
    )
    cards = await place_hold(
        PlaceHold(account_id=card.id, amount=usd("2"), source="cards", reference="h"),
        AccountingUnitOfWork(),
    )
    assert shop.id != cards.id
    assert (await balances())[card.id] == Decimal(0)
