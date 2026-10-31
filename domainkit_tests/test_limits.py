"""Limits under concurrency, and on the ledger's posting path.

Each runs on SQLite, and on PostgreSQL when ``DOMAINKIT_TEST_POSTGRES_URL``
is set (CI sets it): there the fifty postings really run at once, each
holding its own two accounts' locks, so only the subject's row orders
them.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy import func, select

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.accounting import (
    Account,
    AccountingUnitOfWork,
    AccountType,
    CaptureHold,
    Entry,
    InsufficientFunds,
    Line,
    OpenAccount,
    OpenPeriod,
    PlaceHold,
    PostEntry,
    ReverseEntry,
    capture_hold,
    open_account,
    open_period,
    place_hold,
    post_entry,
    reverse_entry,
    verify_ledger,
)
from domainkit.contrib.accounting.tables import entries
from domainkit.contrib.limits import (
    Direction,
    LimitExceeded,
    LimitsUnitOfWork,
    SetLimit,
    TransactionLimitExceeded,
    Window,
    set_limit,
    usage_of,
)
from domainkit.contrib.limits.tables import usage
from domainkit.db import connections
from domainkit.ddd import DomainError
from domainkit.i18n import gettext, override
from domainkit.money import Money
from domainkit_tests.conftest import ALPHA, Migrated, Shape

pytestmark = pytest.mark.anyio

ACCOUNTING: Final = "domainkit.contrib.accounting"
LIMITS: Final = "domainkit.contrib.limits"
LIMITED: Final = Shape(
    "limits", "limits", {"INSTALLED_DOMAINS": [ACCOUNTING, LIMITS, ALPHA]}
)
"""The ledger with limits, migrated once per run."""
TODAY: Final = date(2026, 10, 30)
NOON: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
ATTEMPTS: Final = 50
WALLETS: Final = 5


@contextmanager
def refused[E: DomainError](
    error: type[E], match: str | None = None
) -> Iterator[pytest.ExceptionInfo[E]]:
    """``pytest.raises(error, match=match)``, its message one the catalogs translate."""
    with pytest.raises(error, match=match) as raised:
        yield raised
    message = str(raised.value)
    with override("ar"):
        assert gettext(message) != message, message


@pytest.fixture
async def books(migrated: Migrated) -> AsyncIterator[str]:
    """The ledger with limits in a migrated database of the test's own; its URL."""
    with migrated(LIMITED) as url:
        yield url
        await connections.close_all()


def egp(amount: str) -> Money:
    """``amount`` Egyptian pounds."""
    return Money.of(amount, "EGP")


async def account(
    code: str, type: AccountType = AccountType.ASSET, holder: str | None = None
) -> Account:
    """Open an account ``code`` in pounds."""
    return await open_account(
        OpenAccount(
            code=code,
            name=f"Account {code}",
            type=type,
            currency="EGP",
            holder=holder,
            may_go_negative=holder is None,
        ),
        AccountingUnitOfWork(),
    )


async def period() -> None:
    """Open October."""
    await open_period(
        OpenPeriod(name="2026-10", starts_on=date(2026, 10, 1), ends_on=TODAY),
        AccountingUnitOfWork(),
    )


async def wallets(holder: str, count: int) -> tuple[list[Account], list[Account]]:
    """``count`` wallets of ``holder``, each funded with 5,000, and a till for each."""
    held, tills = [], []
    for number in range(count):
        till = await account(f"till-{holder}-{number}")
        wallet = await account(
            f"wallet-{holder}-{number}", AccountType.LIABILITY, holder=holder
        )
        await post_entry(
            PostEntry(
                source="topups",
                reference=f"{holder}-{number}",
                booked_on=TODAY,
                lines=(
                    Line.debit(till.id, egp("5000")),
                    Line.credit(wallet.id, egp("5000")),
                ),
            ),
            AccountingUnitOfWork(),
        )
        held.append(wallet)
        tills.append(till)
    return held, tills


def spend(wallet: Account, till: Account, amount: Money, reference: str) -> PostEntry:
    """Pay ``amount`` out of ``wallet`` (a liability: debit), into ``till``."""
    return PostEntry(
        source="payments",
        reference=reference,
        booked_on=TODAY,
        lines=(Line.debit(wallet.id, amount), Line.credit(till.id, amount)),
    )


async def limit(**fields: object) -> None:
    """Set the limit ``fields`` describe, in pounds, for holders by default."""
    await set_limit(
        SetLimit(**{"kind": "holder", "currency": "EGP", **fields}),  # type: ignore[arg-type]
        LimitsUnitOfWork(),
    )


async def counted() -> int:
    """Movements counted and not released."""
    async with connections.engine().connect() as connection:
        found = await connection.scalar(
            select(func.count()).select_from(usage).where(usage.c.released_at.is_(None))
        )
    return int(found or 0)


async def test_fifty_payments_at_once_pass_exactly_what_the_limit_allows(
    books: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Fifty payments of 25 against a daily 1,000: exactly forty pass, ten are refused.

    The holder's five wallets each lock only their own accounts, so the
    ledger's locks do not order the payments (ADR 0123's design B let up
    to 1,139.25 through); the holder's row does. None deadlocks or is
    retried, and the books still balance.
    """
    pooled: dict[str, object] = {"POOL_SIZE": 25, "MAX_OVERFLOW": 30}
    if books.startswith("sqlite"):  # One writer at a time: wait as long as it takes.
        pooled = {"CONNECT_ARGS": {"timeout": 60}}
    with override_settings(DATABASES={"default": {"URL": books, **pooled}}):
        await connections.close_all()
        await period()
        held, tills = await wallets("h-1", WALLETS)
        await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("1000"))
        start = asyncio.Event()

        async def pay(number: int) -> Entry | str:
            await start.wait()
            wallet, till = held[number % WALLETS], tills[number % WALLETS]
            try:
                return await post_entry(
                    spend(wallet, till, egp("25"), f"p-{number}"),
                    AccountingUnitOfWork(),
                )
            except LimitExceeded as exc:
                return exc.code

        # The database's clock decides the day on PostgreSQL: no frozen clock.
        with caplog.at_level(logging.INFO, "domainkit.ddd.uow"):
            running = [asyncio.create_task(pay(number)) for number in range(ATTEMPTS)]
            await asyncio.sleep(0)
            start.set()
            done = await asyncio.wait_for(asyncio.gather(*running), 60)
            (used,) = await usage_of("holder", "h-1")

        posted = [each for each in done if isinstance(each, Entry)]
        assert len(posted) == 40
        assert [each for each in done if not isinstance(each, Entry)] == [
            "limit_exceeded"
        ] * 10
        assert (used.used, used.count, used.remaining) == (egp("1000"), 40, egp("0"))
        assert not [r for r in caplog.records if "lost a race" in r.getMessage()]
        assert await verify_ledger() == []
        async with connections.engine().connect() as connection:
            booked = await connection.scalar(
                select(func.count())
                .select_from(entries)
                .where(entries.c.source == "payments")
            )
        assert booked == 40
        await connections.close_all()


@pytest.mark.usefixtures("books")
async def test_a_refused_posting_books_nothing_and_counts_nothing() -> None:
    """Over the limit: the entry is not booked, no balance moves, nothing counts."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        await post_entry(spend(wallet, till, egp("60"), "p-1"), AccountingUnitOfWork())
        with refused(LimitExceeded, "more than a limit allows") as raised:
            await post_entry(
                spend(wallet, till, egp("60"), "p-2"), AccountingUnitOfWork()
            )

        assert (raised.value.limit, raised.value.window) == ("wallet.daily", "day")
        async with AccountingUnitOfWork(readonly=True) as uow:
            assert (await uow.accounts.get(wallet.id)).balance == Decimal("4940.0000")
            assert await uow.entries.by_reference("payments", "p-2") is None
        assert await counted() == 1
        # What is left fits.
        await post_entry(spend(wallet, till, egp("40"), "p-3"), AccountingUnitOfWork())
    assert await counted() == 2


@pytest.mark.usefixtures("books")
async def test_a_reversal_gives_back_what_its_entry_counted() -> None:
    """Reversed, a payment no longer counts: the window takes another as large."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        paid = await post_entry(
            spend(wallet, till, egp("100"), "p-1"), AccountingUnitOfWork()
        )
        with refused(LimitExceeded):
            await post_entry(
                spend(wallet, till, egp("1"), "p-2"), AccountingUnitOfWork()
            )

        await reverse_entry(ReverseEntry(entry_id=paid.id), AccountingUnitOfWork())
        # The reversal moved money back in: nothing it counts, under "in".
        assert await counted() == 0
        await post_entry(spend(wallet, till, egp("100"), "p-3"), AccountingUnitOfWork())
        (used,) = await usage_of("holder", "h-1")
    assert (used.used, used.count) == (egp("100"), 1)


@pytest.mark.usefixtures("books")
async def test_a_posting_refused_after_counting_releases_what_it_counted() -> None:
    """A posting the ledger refuses after the limits counted it rolls the count back."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("10000"))
    with freeze_time(NOON):
        with pytest.raises(InsufficientFunds):
            await post_entry(
                spend(wallet, till, egp("6000"), "p-1"), AccountingUnitOfWork()
            )
        assert await counted() == 0
        await post_entry(
            spend(wallet, till, egp("5000"), "p-2"), AccountingUnitOfWork()
        )
    assert await counted() == 1


@pytest.mark.usefixtures("books")
async def test_captures_count_and_holds_do_not() -> None:
    """A hold moves nothing; its capture is a posting, and is counted."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="wallet.daily", window=Window.DAY, max_amount=egp("100"))
    with freeze_time(NOON):
        hold = await place_hold(
            PlaceHold(
                account_id=wallet.id, amount=egp("150"), source="cards", reference="a-1"
            ),
            AccountingUnitOfWork(),
        )
        assert await counted() == 0
        with refused(LimitExceeded):
            await capture_hold(
                CaptureHold(hold_id=hold.id, counter_account_id=till.id),
                AccountingUnitOfWork(),
            )
        captured = await capture_hold(
            CaptureHold(hold_id=hold.id, counter_account_id=till.id, amount=egp("80")),
            AccountingUnitOfWork(),
        )
    assert captured.entry_id is not None
    assert await counted() == 1


@pytest.mark.usefixtures("books")
async def test_each_subject_of_a_posting_is_counted_its_own_way() -> None:
    """The account, its holder, and the actor; in and out; per transaction."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="topups", direction=Direction.IN, window=Window.DAY, max_count=1)
    await limit(code="account.each", kind="account", per_transaction=egp("500"))
    await limit(
        code="actor.daily", kind="actor", window=Window.DAY, max_amount=egp("700")
    )
    with freeze_time(NOON):
        with refused(
            TransactionLimitExceeded, "more than one movement may be"
        ) as raised:
            await post_entry(
                spend(wallet, till, egp("501"), "p-1"), AccountingUnitOfWork()
            )
        assert (raised.value.code, raised.value.limit) == (
            "transaction_limit_exceeded",
            "account.each",
        )
        # One movement in a day: a second top-up is refused.
        for reference in ("t-1", "t-2"):
            topup = PostEntry(
                source="topups",
                reference=reference,
                booked_on=TODAY,
                lines=(Line.debit(till.id, egp("1")), Line.credit(wallet.id, egp("1"))),
            )
            if reference == "t-1":
                await post_entry(topup, AccountingUnitOfWork())
                continue
            with refused(LimitExceeded, "as many of these as a limit allows"):
                await post_entry(topup, AccountingUnitOfWork())
        with bind_request(RequestContext("r-1", None, "POST", "/", actor="u-1")):
            await post_entry(
                spend(wallet, till, egp("400"), "p-2"), AccountingUnitOfWork()
            )
            with refused(LimitExceeded):
                await post_entry(
                    spend(wallet, till, egp("400"), "p-3"), AccountingUnitOfWork()
                )
        # Without a request there is no actor to count.
        await post_entry(spend(wallet, till, egp("400"), "p-4"), AccountingUnitOfWork())
        (actor,) = await usage_of("actor", "u-1")
    assert (actor.used, actor.count) == (egp("400"), 1)


@pytest.mark.usefixtures("books")
async def test_an_entrys_postings_to_one_account_count_as_one_movement() -> None:
    """Three postings to a wallet: one movement of their sum."""
    await period()
    (wallet,), (till,) = await wallets("h-1", 1)
    await limit(code="each", per_transaction=egp("100"))
    lines = (
        Line.debit(wallet.id, egp("40")),
        Line.debit(wallet.id, egp("40")),
        Line.debit(wallet.id, egp("40")),
        Line.credit(till.id, egp("120")),
    )
    with refused(TransactionLimitExceeded):
        await post_entry(
            PostEntry(source="payments", reference="p", booked_on=TODAY, lines=lines),
            AccountingUnitOfWork(),
        )
