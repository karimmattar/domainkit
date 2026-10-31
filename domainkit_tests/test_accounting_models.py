"""The ledger's domain, without a database: accounts, entries, holds, periods."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, Inexact
from typing import Final

import pytest

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import (
    Account,
    AccountClosed,
    AccountId,
    AccountIsClosed,
    AccountNotEmpty,
    AccountOpened,
    AccountStatus,
    AccountType,
    Entry,
    EntryId,
    EntryPosted,
    Hold,
    HoldFinished,
    HoldNotActive,
    HoldPlaced,
    HoldStatus,
    InsufficientFunds,
    InvalidAmount,
    InvalidEntry,
    Line,
    Period,
    PeriodClosed,
    PeriodIsClosed,
    PeriodOpened,
    PeriodsOverlap,
    PeriodStatus,
    Side,
    UnbalancedEntry,
    models,
)
from domainkit.ddd import DomainError
from domainkit.money import (
    Money,
    UnknownCurrency,
    currencies_kept,
    register_currency,
)
from domainkit_tests.test_accounting import refused

NOW: Final = datetime(2026, 10, 30, 12, tzinfo=UTC)
DAY: Final = date(2026, 10, 30)


def usd(amount: str) -> Money:
    """``amount`` dollars."""
    return Money.of(amount, "USD")


def an_account(type: AccountType = AccountType.ASSET, **options: object) -> Account:
    """A new USD account."""
    account = Account.open("1000", "Cash", type, "USD", **options)  # type: ignore[arg-type]
    account.pull_events()
    return account


def ids() -> tuple[AccountId, AccountId]:
    """Two account ids."""
    return AccountId(uuid.uuid4()), AccountId(uuid.uuid4())


def draft(lines: list[Line], **options: object) -> Entry:
    """An entry of ``lines`` from ``tests``."""
    return Entry.draft(
        str(options.pop("source", "tests")),
        str(options.pop("reference", "r-1")),
        DAY,
        lines,
        max_lines=int(options.pop("max_lines", 100)),  # type: ignore[call-overload]
        **options,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("type", "normal"),
    [
        (AccountType.ASSET, Side.DEBIT),
        (AccountType.EXPENSE, Side.DEBIT),
        (AccountType.LIABILITY, Side.CREDIT),
        (AccountType.EQUITY, Side.CREDIT),
        (AccountType.INCOME, Side.CREDIT),
    ],
)
def test_each_type_has_its_normal_balance(type: AccountType, normal: Side) -> None:
    """Assets and expenses rise with debits; the others with credits."""
    assert type.normal_balance is normal
    assert an_account(type).normal_balance is normal
    assert normal.opposite is not normal
    assert normal.opposite.opposite is normal


def test_an_account_opens_with_nothing_on_it() -> None:
    """A new account is open, empty, and records ``AccountOpened``."""
    with freeze_time(NOW):
        account = Account.open(
            "1000", "Cash", AccountType.ASSET, "USD", holder="user-1"
        )
    (event,) = account.pull_events()
    assert isinstance(event, AccountOpened)
    assert (event.account_id, event.code, event.type, event.currency) == (
        account.id,
        "1000",
        AccountType.ASSET,
        "USD",
    )
    assert (account.status, account.balance, account.held) == (
        AccountStatus.OPEN,
        Decimal(0),
        Decimal(0),
    )
    assert (account.holder, account.may_go_negative, account.opened_at) == (
        "user-1",
        True,
        NOW,
    )
    assert account.closed_at is None
    assert isinstance(account.type, AccountType)


@pytest.mark.parametrize(
    ("code", "name", "holder", "message"),
    [
        ("", "Cash", None, "code"),
        ("-1", "Cash", None, "code"),
        ("a" * 33, "Cash", None, "code"),
        ("1 0", "Cash", None, "code"),
        ("1000", "", None, "name"),
        ("1000", "n" * 201, None, "name"),
        ("1000", "Cash", "", "holder"),
        ("1000", "Cash", "h" * 256, "holder"),
    ],
)
def test_malformed_accounts_are_refused(
    code: str, name: str, holder: str | None, message: str
) -> None:
    """Codes, names, and holders out of bounds."""
    with refused(InvalidEntry, match=f"account's {message}"):
        Account.open(code, name, AccountType.ASSET, "USD", holder=holder)


def test_accounts_at_their_bounds_open() -> None:
    """The longest code, name, and holder; codes of letters, digits, and marks."""
    account = Account.open(
        "Aa9._-" + "x" * 26, "n" * 200, AccountType.ASSET, "KWD", holder="h" * 255
    )
    assert (len(account.code), account.currency) == (32, "KWD")
    assert Account.open("1", "N", AccountType.ASSET, "CLF").currency == "CLF"


def test_currencies_the_ledger_cannot_hold_are_refused() -> None:
    """Unknown codes, and currencies of more than four places."""
    with pytest.raises(UnknownCurrency):
        Account.open("1000", "Cash", AccountType.ASSET, "XXX")
    with currencies_kept():
        register_currency("XBT", minor_units=8)
        with refused(InvalidAmount, match="at most four decimal places"):
            Account.open("1000", "Bitcoin", AccountType.ASSET, "XBT")


def test_stored_amounts_come_back_at_their_currencys_places() -> None:
    """Four stored places become the currency's; more digits are an error."""
    assert models.money(Decimal("10.5000"), "USD") == usd("10.50")
    assert models.amount_text(Decimal("-3.0000"), "KWD") == "-3.000"
    assert models.amount_text(Decimal("7.0000"), "JPY") == "7"
    with pytest.raises(Inexact):
        models.money(Decimal("1.0050"), "USD")


def test_postings_move_the_balance_by_side() -> None:
    """A debit raises an asset and lowers a liability; credits the reverse."""
    asset, liability = an_account(), an_account(AccountType.LIABILITY)

    asset.post(Side.DEBIT, usd("10"))
    asset.post(Side.CREDIT, usd("3"))
    liability.post(Side.CREDIT, usd("10"))
    liability.post(Side.DEBIT, usd("4"))

    assert (asset.balance, liability.balance) == (Decimal("7.00"), Decimal("6.00"))


def test_postings_are_refused_on_closed_accounts_and_in_other_currencies() -> None:
    """A closed account takes nothing; an account takes its own currency only."""
    account = an_account()
    with refused(InvalidEntry, match="not in its account's currency"):
        account.post(Side.DEBIT, Money.of("1", "EUR"))
    with refused(InvalidEntry, match="not in its account's currency"):
        account.reserve(Money.of("1", "EUR"))
    account.close()
    with refused(AccountIsClosed, match="account is closed"):
        account.post(Side.DEBIT, usd("1"))
    with refused(AccountIsClosed):
        account.reserve(usd("1"))


def test_an_account_that_may_not_go_negative_keeps_something_available() -> None:
    """Postings and holds may use what is available, and no more."""
    wallet = an_account(AccountType.LIABILITY, may_go_negative=False)
    wallet.post(Side.CREDIT, usd("10"))
    wallet.reserve(usd("4"))

    with refused(InsufficientFunds):
        wallet.post(Side.DEBIT, usd("6.01"))
    with refused(InsufficientFunds):
        wallet.reserve(usd("6.01"))
    wallet.post(Side.DEBIT, usd("6"))

    assert (wallet.balance, wallet.held, wallet.available) == (
        Decimal("4.00"),
        Decimal("4.00"),
        Decimal("0.00"),
    )
    wallet.unreserve(Decimal("4.00"))
    wallet.reserve(usd("4"))
    assert wallet.available == Decimal("0.00")


def test_an_account_that_may_go_negative_does() -> None:
    """By default an account may be overdrawn, held amounts included."""
    account = an_account()
    account.reserve(usd("5"))
    account.post(Side.CREDIT, usd("5"))
    assert (account.balance, account.available) == (Decimal("-5.00"), Decimal("-10.00"))


def test_only_an_empty_open_account_closes() -> None:
    """A balance or a hold keeps it open; once closed, it stays so."""
    account = an_account()
    account.post(Side.DEBIT, usd("1"))
    with refused(AccountNotEmpty):
        account.close()
    account.post(Side.CREDIT, usd("1"))
    account.reserve(usd("1"))
    with refused(AccountNotEmpty):
        account.close()
    account.unreserve(Decimal("1.00"))

    with freeze_time(NOW):
        account.close()

    assert (account.status, account.closed_at) == (AccountStatus.CLOSED, NOW)
    (event,) = account.pull_events()
    assert isinstance(event, AccountClosed)
    assert event.account_id == account.id
    with refused(AccountIsClosed):
        account.close()


def test_an_entry_balances_per_currency() -> None:
    """Each currency's debits equal its credits; the entry records ``EntryPosted``."""
    cash, sales = ids()
    entry = draft(
        [
            Line.debit(cash, usd("10"), "till"),
            Line.credit(sales, usd("10")),
            Line.debit(cash, Money.of("3", "EUR")),
            Line.credit(sales, Money.of("1", "EUR")),
            Line.credit(sales, Money.of("2", "EUR")),
        ],
        description="a sale",
    )

    assert [p.line for p in entry.postings] == [1, 2, 3, 4, 5]
    first = entry.postings[0]
    assert (first.account_id, first.side, first.amount, first.currency) == (
        cash,
        Side.DEBIT,
        Decimal("10.00"),
        "USD",
    )
    assert (first.memo, first.booked_on, first.money) == ("till", DAY, usd("10"))
    (event,) = entry.pull_events()
    assert isinstance(event, EntryPosted)
    assert (event.entry_id, event.source, event.reference, event.booked_on) == (
        entry.id,
        "tests",
        "r-1",
        DAY,
    )
    assert event.reverses is None
    assert (entry.description, entry.period_id, entry.reverses) == (
        "a sale",
        None,
        None,
    )


@pytest.mark.parametrize(
    "lines",
    [
        [("debit", "10", "USD"), ("credit", "9.99", "USD")],
        [("debit", "1", "USD"), ("credit", "1", "EUR")],
        [("debit", "1", "USD"), ("debit", "1", "USD")],
    ],
)
def test_an_unbalanced_entry_is_refused(lines: list[tuple[str, str, str]]) -> None:
    """Debits and credits that differ in any currency."""
    account = AccountId(uuid.uuid4())
    with refused(UnbalancedEntry, match="differ in a currency"):
        draft([Line(account, Side(s), Money.of(a, c)) for s, a, c in lines])


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"source": ""}, "source"),
        ({"source": "Tests"}, "source"),
        ({"source": "s" * 65}, "source"),
        ({"reference": ""}, "reference"),
        ({"reference": "r" * 129}, "reference"),
        ({"reference": "a\nb"}, "reference"),
        ({"description": "d" * 256}, "description"),
        ({"max_lines": 1}, "two lines"),
    ],
)
def test_malformed_entries_are_refused(
    options: dict[str, object], message: str
) -> None:
    """Sources, references, descriptions, and line counts out of bounds."""
    cash, sales = ids()
    with refused(InvalidEntry, match=message):
        draft([Line.debit(cash, usd("1")), Line.credit(sales, usd("1"))], **options)


def test_entries_at_their_bounds_are_drafted() -> None:
    """Two lines, the most lines, and the longest texts."""
    cash, sales = ids()
    lines = [Line.debit(cash, usd("2")), Line.credit(sales, usd("2"), "m" * 255)]
    longest = draft(
        lines,
        source="s" * 64,
        reference="r" * 128,
        description="d" * 255,
        max_lines=2,
    )
    assert len(longest.postings) == 2
    three = [Line.debit(cash, usd("2")), *[Line.credit(sales, usd("1"))] * 2]
    with refused(InvalidEntry, match="two lines"):
        draft(three, max_lines=2)
    with refused(InvalidEntry, match="two lines"):
        draft(lines[:1])
    assert len(draft(three, max_lines=3).postings) == 3
    assert draft(lines, source="a0_.:-z").source == "a0_.:-z"


def test_lines_are_positive_with_short_memos() -> None:
    """Zero and negative amounts, and long memos, are refused."""
    cash, sales = ids()
    for amount in ("0", "-1"):
        with refused(InvalidAmount, match="more than zero"):
            draft([Line.debit(cash, usd(amount)), Line.credit(sales, usd(amount))])
    with refused(InvalidEntry, match="memo"):
        draft([Line.debit(cash, usd("1"), "m" * 256), Line.credit(sales, usd("1"))])


def test_the_fingerprint_tells_entries_apart() -> None:
    """The same lines give the same digest; any change gives another."""
    cash, sales = ids()
    lines = [Line.debit(cash, usd("1")), Line.credit(sales, usd("1"))]
    same = draft(lines).fingerprint
    assert draft(lines, reference="other").fingerprint == same
    assert len(same) == 64
    others = {
        draft(lines, description="x").fingerprint,
        draft([Line.debit(cash, usd("2")), Line.credit(sales, usd("2"))]).fingerprint,
        draft([Line.debit(sales, usd("1")), Line.credit(cash, usd("1"))]).fingerprint,
        draft(
            [Line.debit(cash, usd("1"), "memo"), Line.credit(sales, usd("1"))]
        ).fingerprint,
        draft(lines, reverses=EntryId(uuid.uuid4())).fingerprint,
        models.fingerprint(date(2026, 10, 31), "", lines, None),
    }
    assert same not in others
    assert len(others) == 6


def test_a_reversal_puts_each_posting_on_the_other_side() -> None:
    """In the order of the entry's lines, whatever order they were loaded in."""
    cash, sales = ids()
    entry = draft([Line.debit(cash, usd("4"), "m"), Line.credit(sales, usd("4"))])
    entry.postings.reverse()

    assert entry.reversal_lines() == [
        Line.credit(cash, usd("4"), "m"),
        Line.debit(sales, usd("4")),
    ]


def test_a_hold_reserves_until_it_finishes() -> None:
    """Placed, it holds; released or expired, it gives the amount back."""
    account = an_account()
    account.post(Side.DEBIT, usd("10"))
    with freeze_time(NOW):
        hold = Hold.place(account, usd("4"), "shop", "h-1", NOW + timedelta(hours=1))
        (placed,) = hold.pull_events()
        assert isinstance(placed, HoldPlaced)
        assert (placed.hold_id, placed.account_id, placed.expires_at) == (
            hold.id,
            account.id,
            NOW + timedelta(hours=1),
        )
        assert (account.held, hold.money, hold.placed_at) == (
            Decimal("4.00"),
            usd("4"),
            NOW,
        )
        assert hold.same_as(account.id, usd("4"))
        assert not hold.same_as(account.id, usd("5"))
        assert not hold.same_as(AccountId(uuid.uuid4()), usd("4"))

        hold.finish(account, HoldStatus.RELEASED)

    assert (hold.status, hold.finished_at, account.held) == (
        HoldStatus.RELEASED,
        NOW,
        Decimal("0.00"),
    )
    (finished,) = hold.pull_events()
    assert isinstance(finished, HoldFinished)
    assert (finished.status, finished.entry_id) == (HoldStatus.RELEASED, None)
    with refused(HoldNotActive):
        hold.finish(account, HoldStatus.EXPIRED)


def test_an_expired_hold_is_not_captured_but_is_released() -> None:
    """At its expiry a hold can no longer be captured."""
    account = an_account()
    with freeze_time(NOW):
        hold = Hold.place(account, usd("4"), "shop", "h-1", NOW + timedelta(hours=1))
    with freeze_time(NOW + timedelta(hours=1)):
        with refused(HoldNotActive):
            hold.finish(account, HoldStatus.CAPTURED)
        hold.finish(account, HoldStatus.EXPIRED)
    assert hold.status is HoldStatus.EXPIRED


def test_a_hold_captured_before_its_expiry_names_its_entry() -> None:
    """Captured in time, the event names the entry it posted."""
    account, entry_id = an_account(), EntryId(uuid.uuid4())
    with freeze_time(NOW):
        hold = Hold.place(account, usd("4"), "shop", "h-1", NOW + timedelta(seconds=1))
        hold.pull_events()
        hold.finish(account, HoldStatus.CAPTURED, entry_id)
    (event,) = hold.pull_events()
    assert (event.status, event.entry_id) == (HoldStatus.CAPTURED, entry_id)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("source", "reference", "amount", "expires", "error", "message"),
    [
        ("Shop", "h", "1", 1, InvalidEntry, "source"),
        ("shop", "", "1", 1, InvalidEntry, "reference"),
        ("shop", "h", "0", 1, InvalidAmount, "more than zero"),
        ("shop", "h", "1", 0, InvalidEntry, "expires in the future"),
        ("shop", "h", "1", 366 * 86400 + 1, InvalidEntry, "within 366 days"),
    ],
)
def test_malformed_holds_are_refused(
    source: str,
    reference: str,
    amount: str,
    expires: int,
    error: type[DomainError],
    message: str,
) -> None:
    """Sources, references, amounts, and expiries out of bounds."""
    account = an_account()
    with freeze_time(NOW), refused(error, match=message):
        Hold.place(
            account, usd(amount), source, reference, NOW + timedelta(seconds=expires)
        )
    assert account.held == Decimal(0)


def test_a_hold_lasts_366_days_at_most_and_its_expiry_has_a_time_zone() -> None:
    """The longest hold is placed; a naive expiry is refused, no crash (SEC-14b-3)."""
    account = an_account()
    with freeze_time(NOW):
        longest = Hold.place(account, usd("1"), "shop", "h", NOW + timedelta(days=366))
        with refused(InvalidEntry, match="needs a time zone"):
            Hold.place(account, usd("1"), "shop", "n", datetime(2027, 1, 1))  # noqa: DTZ001
    assert longest.expires_at == NOW + timedelta(days=366)
    assert account.held == Decimal(1)


def test_a_period_opens_and_closes_once() -> None:
    """A closed period takes no entries, and does not close again."""
    with freeze_time(NOW):
        period = Period.open("2026-10", date(2026, 10, 1), date(2026, 10, 31))
        (opened,) = period.pull_events()
        period.takes()
        period.close()
    assert isinstance(opened, PeriodOpened)
    assert (opened.period_id, opened.name, opened.starts_on, opened.ends_on) == (
        period.id,
        "2026-10",
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert (period.status, period.closed_at) == (PeriodStatus.CLOSED, NOW)
    (closed,) = period.pull_events()
    assert isinstance(closed, PeriodClosed)
    assert closed.period_id == period.id
    with refused(PeriodIsClosed, match="closed period"):
        period.takes()
    with refused(PeriodIsClosed):
        period.close()


def test_malformed_periods_are_refused() -> None:
    """A name of 1 to 64 characters; it ends on or after its first day."""
    one_day = Period.open("p" * 64, DAY, DAY)
    assert one_day.starts_on == one_day.ends_on
    for name in ("", "p" * 65):
        with refused(InvalidEntry, match="period's name"):
            Period.open(name, DAY, DAY)
    with refused(PeriodsOverlap, match="ends on or after"):
        Period.open("p", DAY, DAY - timedelta(days=1))
