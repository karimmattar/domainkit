"""The ledger's domain: accounts, journal entries and their postings, holds, periods.

Amounts are ``domainkit.money.Money`` wherever they come in or go out, and
``Decimal`` at the column's scale (``SCALE``) in the aggregates, with the
currency's code beside them, as the tables store them. An entry balances
per currency: each currency's debits equal its credits, or it is refused.

An account's ``balance`` is in its normal sign: what an asset or an expense
was debited more than credited, what a liability, equity, or income was
credited more than debited. ``held`` is what its active holds reserve, and
``available`` the balance less that.
"""

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import field
from datetime import date, datetime
from decimal import Context, Decimal, Inexact
from enum import StrEnum
from typing import Annotated, Final, NewType, Self
from uuid import UUID, uuid4

from domainkit import clock
from domainkit.ddd import (
    AggregateRoot,
    Digits,
    DomainEvent,
    Entity,
    MaxLength,
    ValueObject,
)
from domainkit.money import Currency, Money, currency
from domainkit.utils.decimals import decimal_text

from .exceptions import (
    AccountIsClosed,
    AccountNotEmpty,
    HoldNotActive,
    InsufficientFunds,
    InvalidAmount,
    InvalidEntry,
    PeriodIsClosed,
    PeriodsOverlap,
    UnbalancedEntry,
)

AccountId = NewType("AccountId", UUID)
EntryId = NewType("EntryId", UUID)
PostingId = NewType("PostingId", UUID)
HoldId = NewType("HoldId", UUID)
PeriodId = NewType("PeriodId", UUID)

PRECISION: Final = 18
"""Digits an amount column holds (``ExactDecimal(PRECISION, SCALE)``)."""
SCALE: Final = 4
"""Decimal places an amount column holds: the most a currency may have."""
ZERO: Final = Decimal(0)
EXACT: Final = Context(prec=PRECISION + 2, traps=[Inexact])
"""Puts a stored amount back at its currency's places, refusing to round."""
CODE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
"""An account's code: up to 32 letters, digits, ``.``, ``_``, or ``-``."""
SOURCE: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
"""Where entries come from (``payments``, ``billing.invoices``)."""
REFERENCE: Final = re.compile(r"[^\x00-\x1f\x7f]{1,128}")
"""An entry's or a hold's reference within its source: 1 to 128 printable characters."""
MAX_TEXT: Final = 255
"""The longest description, memo, or holder."""
MAX_NAME: Final = 200
MAX_PERIOD_NAME: Final = 64
"""The longest name of an account or a period."""


class AccountType(StrEnum):
    """What an account counts, which decides its normal balance."""

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"

    @property
    def normal_balance(self) -> "Side":
        """The side that increases it: debit for assets and expenses, else credit."""
        if self in (AccountType.ASSET, AccountType.EXPENSE):
            return Side.DEBIT
        return Side.CREDIT


class Side(StrEnum):
    """A posting's side."""

    DEBIT = "debit"
    CREDIT = "credit"

    @property
    def opposite(self) -> "Side":
        """The other side."""
        return Side.CREDIT if self is Side.DEBIT else Side.DEBIT


class AccountStatus(StrEnum):
    """Whether an account takes postings."""

    OPEN = "open"
    CLOSED = "closed"


class PeriodStatus(StrEnum):
    """Whether a period takes entries."""

    OPEN = "open"
    CLOSED = "closed"


class HoldStatus(StrEnum):
    """Where a hold is: reserving, or finished one of three ways."""

    ACTIVE = "active"
    CAPTURED = "captured"
    RELEASED = "released"
    EXPIRED = "expired"


def _now() -> datetime:
    """The current time in UTC (``domainkit.clock``)."""
    return clock.now()


def money(amount: Decimal, code: str) -> Money:
    """A stored ``amount`` of ``code`` as ``Money``, at the currency's places.

    Raises:
        decimal.Inexact: The amount has more places than its currency.
    """
    unit = currency(code)
    return Money.of(
        amount.quantize(Decimal(1).scaleb(-unit.minor_units), context=EXACT), unit
    )


def amount_text(amount: Decimal, code: str) -> str:
    """``amount`` of ``code`` as fixed-point text, at the currency's places."""
    return decimal_text(money(amount, code).amount)


def ledger_currency(code: str) -> Currency:
    """The currency of ``code``, if the ledger can hold its amounts exactly.

    Raises:
        UnknownCurrency: No currency has that code (``domainkit.money``).
        InvalidAmount: It has more minor units than the columns' ``SCALE``.
    """
    unit = currency(code)
    if unit.minor_units > SCALE:
        raise InvalidAmount("The ledger keeps at most four decimal places.")
    return unit


def _text(value: str, limit: int, error: str) -> str:
    """``value`` if it has at most ``limit`` characters, else ``InvalidEntry``."""
    if len(value) > limit:
        raise InvalidEntry(error)
    return value


class AccountOpened(DomainEvent):
    """An account was opened."""

    account_id: AccountId
    code: str
    type: AccountType
    currency: str


class AccountClosed(DomainEvent):
    """An account was closed: it takes no more postings or holds."""

    account_id: AccountId


class EntryPosted(DomainEvent):
    """A journal entry was posted; ``reverses`` names the entry it undoes."""

    entry_id: EntryId
    source: str
    reference: str
    booked_on: date
    reverses: EntryId | None = None


class HoldPlaced(DomainEvent):
    """An amount was reserved on an account."""

    hold_id: HoldId
    account_id: AccountId
    expires_at: datetime


class HoldFinished(DomainEvent):
    """A hold was captured into an entry, released, or expired (``status``)."""

    hold_id: HoldId
    account_id: AccountId
    status: HoldStatus
    entry_id: EntryId | None = None


class PeriodOpened(DomainEvent):
    """An accounting period was opened."""

    period_id: PeriodId
    name: str
    starts_on: date
    ends_on: date


class PeriodClosed(DomainEvent):
    """An accounting period was closed: no entry may be dated in it any more."""

    period_id: PeriodId


class Line(ValueObject):
    """One line of an entry to post: an amount on one side of one account."""

    account_id: AccountId
    side: Side
    amount: Money
    memo: str = ""

    @classmethod
    def debit(cls, account_id: AccountId, amount: Money, memo: str = "") -> Self:
        """Debit ``amount`` to the account."""
        return cls(account_id, Side.DEBIT, amount, memo)

    @classmethod
    def credit(cls, account_id: AccountId, amount: Money, memo: str = "") -> Self:
        """Credit ``amount`` to the account."""
        return cls(account_id, Side.CREDIT, amount, memo)


class Account(AggregateRoot[AccountId]):
    """An account of the chart: one currency, one type, open or closed.

    ``holder`` names the subject it is kept for, such as a customer's
    wallet; ``privacy export`` lists that subject's accounts. An account
    that ``may_go_negative`` is False refuses whatever would leave less
    than nothing available.
    """

    class Meta:
        """Its permissions: the defaults, and two of its own."""

        permissions = (
            ("close_account", "Can close account"),
            ("reconcile_account", "Can reconcile account"),
        )

    code: Annotated[str, MaxLength(32)]
    name: Annotated[str, MaxLength(MAX_NAME)]
    type: AccountType
    currency: Annotated[str, MaxLength(8)]
    status: AccountStatus = AccountStatus.OPEN
    holder: Annotated[str, MaxLength(MAX_TEXT)] | None = None
    may_go_negative: bool = True
    balance: Annotated[Decimal, Digits(PRECISION, SCALE)] = ZERO
    held: Annotated[Decimal, Digits(PRECISION, SCALE)] = ZERO
    opened_at: datetime = field(default_factory=_now)
    closed_at: datetime | None = None

    @classmethod
    def open(
        cls,
        code: str,
        name: str,
        type: AccountType,
        currency: str,
        *,
        holder: str | None = None,
        may_go_negative: bool = True,
    ) -> Self:
        """Open an account, recording ``AccountOpened``.

        Raises:
            InvalidEntry: The code, name, or holder is malformed.
            InvalidAmount: The currency is unknown, or has too many places.
        """
        if not CODE.fullmatch(code):
            raise InvalidEntry(
                "An account's code has up to 32 letters, digits, '.', '_', or '-'."
            )
        if not 1 <= len(name) <= MAX_NAME:
            raise InvalidEntry("An account's name has 1 to 200 characters.")
        if holder is not None and not 1 <= len(holder) <= MAX_TEXT:
            raise InvalidEntry("An account's holder has 1 to 255 characters.")
        unit = ledger_currency(currency)
        account = cls(
            id=AccountId(uuid4()),
            code=code,
            name=name,
            type=AccountType(type),
            currency=unit.code,
            holder=holder,
            may_go_negative=may_go_negative,
        )
        account.record(
            AccountOpened(
                account_id=account.id,
                code=code,
                type=account.type,
                currency=account.currency,
            )
        )
        return account

    @property
    def normal_balance(self) -> Side:
        """The side that increases it."""
        return self.type.normal_balance

    @property
    def available(self) -> Decimal:
        """The balance less what holds reserve."""
        return self.balance - self.held

    def _checked(self, amount: Money) -> Decimal:
        """``amount``'s value, if the account is open and of its currency."""
        if self.status is AccountStatus.CLOSED:
            raise AccountIsClosed("The account is closed.")
        if amount.currency.code != self.currency:
            raise InvalidEntry("An amount is not in its account's currency.")
        return amount.amount

    def post(self, side: Side, amount: Money) -> None:
        """Change the balance by ``amount`` on ``side``.

        Raises:
            AccountIsClosed: The account is closed.
            InvalidEntry: ``amount`` is in another currency.
            InsufficientFunds: It may not go negative, and less than nothing
                would be available.
        """
        value = self._checked(amount)
        balance = self.balance + (value if side is self.normal_balance else -value)
        if not self.may_go_negative and balance - self.held < ZERO:
            raise InsufficientFunds("The account does not have enough available.")
        self.balance = balance

    def reserve(self, amount: Money) -> None:
        """Hold ``amount`` back from what is available.

        Raises:
            AccountIsClosed: The account is closed.
            InvalidEntry: ``amount`` is in another currency.
            InsufficientFunds: It may not go negative and has less available.
        """
        value = self._checked(amount)
        if not self.may_go_negative and self.available < value:
            raise InsufficientFunds("The account does not have enough available.")
        self.held += value

    def unreserve(self, value: Decimal) -> None:
        """Make ``value`` available again: a hold finished."""
        self.held -= value

    def close(self) -> None:
        """Close the account, recording ``AccountClosed``.

        Raises:
            AccountIsClosed: It is closed already.
            AccountNotEmpty: It has a balance, or active holds.
        """
        if self.status is AccountStatus.CLOSED:
            raise AccountIsClosed("The account is closed.")
        if self.balance != ZERO or self.held != ZERO:
            raise AccountNotEmpty("Only an account with nothing on it can be closed.")
        self.status = AccountStatus.CLOSED
        self.closed_at = _now()
        self.record(AccountClosed(account_id=self.id))


class Posting(Entity[PostingId]):
    """One line of a posted entry: an amount on one side of one account."""

    class Meta:
        """Postings are read with their entry: no permissions of their own."""

        default_permissions = ()

    line: int
    account_id: AccountId
    side: Side
    amount: Annotated[Decimal, Digits(PRECISION, SCALE)]
    currency: Annotated[str, MaxLength(8)]
    booked_on: date
    memo: Annotated[str, MaxLength(MAX_TEXT)] = ""

    @property
    def money(self) -> Money:
        """The amount, as ``Money``."""
        return money(self.amount, self.currency)


def fingerprint(
    booked_on: date,
    description: str,
    lines: Sequence[Line],
    reverses: EntryId | None,
) -> str:
    """A digest of what an entry books, to tell a retry from another entry."""
    document = {
        "booked_on": booked_on.isoformat(),
        "description": description,
        "reverses": None if reverses is None else str(reverses),
        "lines": [
            [
                str(line.account_id),
                line.side.value,
                decimal_text(line.amount.amount),
                line.amount.currency.code,
                line.memo,
            ]
            for line in lines
        ],
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class Entry(AggregateRoot[EntryId]):
    """A posted journal entry: balanced lines, never changed once posted.

    ``source`` and ``reference`` name it where it came from, uniquely: an
    entry posted again with them is the same entry. A correction is
    another entry, the reversal (``reverses``), never a change to this one.
    """

    class Meta:
        """Its permissions: the defaults, and reversing."""

        verbose_name = "journal entry"
        permissions = (("reverse_entry", "Can reverse journal entry"),)

    source: Annotated[str, MaxLength(64)]
    reference: Annotated[str, MaxLength(128)]
    booked_on: date
    description: Annotated[str, MaxLength(MAX_TEXT)] = ""
    period_id: PeriodId | None = None
    reverses: EntryId | None = None
    fingerprint: Annotated[str, MaxLength(64)]
    posted_at: datetime = field(default_factory=_now)
    postings: list[Posting] = field(default_factory=list)

    @classmethod
    def draft(
        cls,
        source: str,
        reference: str,
        booked_on: date,
        lines: Sequence[Line],
        *,
        description: str = "",
        reverses: EntryId | None = None,
        max_lines: int,
    ) -> Self:
        """An entry of ``lines``, if they balance per currency; records ``EntryPosted``.

        Raises:
            InvalidEntry: The source, reference, description, a memo, or
                the number of lines is out of bounds.
            InvalidAmount: A line's amount is not more than zero.
            UnbalancedEntry: Debits and credits differ in some currency.
        """
        if not SOURCE.fullmatch(source):
            raise InvalidEntry(
                "A source has up to 64 lower-case letters, digits, '_', '.', ':', "
                "or '-'."
            )
        if not REFERENCE.fullmatch(reference):
            raise InvalidEntry("A reference has 1 to 128 printable characters.")
        _text(description, MAX_TEXT, "A description has at most 255 characters.")
        if not 2 <= len(lines) <= max_lines:
            raise InvalidEntry(
                "An entry has at least two lines, and at most ACCOUNTING.MAX_LINES."
            )
        sums: defaultdict[tuple[str, Side], Decimal] = defaultdict(Decimal)
        for line in lines:
            _text(line.memo, MAX_TEXT, "A memo has at most 255 characters.")
            if line.amount.amount <= ZERO:
                raise InvalidAmount("An amount is more than zero.")
            sums[line.amount.currency.code, line.side] += line.amount.amount
        for code in {code for code, _ in sums}:
            if sums[code, Side.DEBIT] != sums[code, Side.CREDIT]:
                raise UnbalancedEntry(
                    "The entry's debits and credits differ in a currency."
                )
        entry = cls(
            id=EntryId(uuid4()),
            source=source,
            reference=reference,
            booked_on=booked_on,
            description=description,
            reverses=reverses,
            fingerprint=fingerprint(booked_on, description, lines, reverses),
        )
        entry.postings = [
            Posting(
                id=PostingId(uuid4()),
                line=number,
                account_id=line.account_id,
                side=line.side,
                amount=line.amount.amount,
                currency=line.amount.currency.code,
                booked_on=booked_on,
                memo=line.memo,
            )
            for number, line in enumerate(lines, start=1)
        ]
        entry.record(
            EntryPosted(
                entry_id=entry.id,
                source=source,
                reference=reference,
                booked_on=booked_on,
                reverses=reverses,
            )
        )
        return entry

    def lines(self) -> list[Line]:
        """The entry's postings as lines, in order."""
        return [
            Line(posting.account_id, posting.side, posting.money, posting.memo)
            for posting in sorted(self.postings, key=lambda posting: posting.line)
        ]

    def reversal_lines(self) -> list[Line]:
        """Lines that undo this entry: each posting on the other side."""
        return [
            Line(line.account_id, line.side.opposite, line.amount, line.memo)
            for line in self.lines()
        ]


class Hold(AggregateRoot[HoldId]):
    """An amount reserved on an account until it is captured, released, or expires."""

    class Meta:
        """Its permissions: the defaults, and capturing and releasing."""

        permissions = (
            ("capture_hold", "Can capture hold"),
            ("release_hold", "Can release hold"),
        )

    account_id: AccountId
    amount: Annotated[Decimal, Digits(PRECISION, SCALE)]
    currency: Annotated[str, MaxLength(8)]
    source: Annotated[str, MaxLength(64)]
    reference: Annotated[str, MaxLength(128)]
    status: HoldStatus = HoldStatus.ACTIVE
    expires_at: datetime
    placed_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    entry_id: EntryId | None = None
    """The entry a capture posted."""

    @classmethod
    def place(
        cls,
        account: Account,
        amount: Money,
        source: str,
        reference: str,
        expires_at: datetime,
    ) -> Self:
        """Reserve ``amount`` on ``account`` until ``expires_at``; ``HoldPlaced``.

        Raises:
            InvalidEntry: The source or reference is malformed, or
                ``expires_at`` has passed.
            InvalidAmount: The amount is not more than zero.
            AccountIsClosed, InsufficientFunds: As ``Account.reserve``.
        """
        if not SOURCE.fullmatch(source):
            raise InvalidEntry(
                "A source has up to 64 lower-case letters, digits, '_', '.', ':', "
                "or '-'."
            )
        if not REFERENCE.fullmatch(reference):
            raise InvalidEntry("A reference has 1 to 128 printable characters.")
        if amount.amount <= ZERO:
            raise InvalidAmount("An amount is more than zero.")
        if expires_at <= _now():
            raise InvalidEntry("A hold expires in the future.")
        account.reserve(amount)
        hold = cls(
            id=HoldId(uuid4()),
            account_id=account.id,
            amount=amount.amount,
            currency=amount.currency.code,
            source=source,
            reference=reference,
            expires_at=expires_at,
        )
        hold.record(
            HoldPlaced(hold_id=hold.id, account_id=account.id, expires_at=expires_at)
        )
        return hold

    @property
    def money(self) -> Money:
        """The amount reserved, as ``Money``."""
        return money(self.amount, self.currency)

    def same_as(self, account_id: AccountId, amount: Money) -> bool:
        """Whether placing ``amount`` on ``account_id`` would make this hold."""
        return (self.account_id, self.money) == (account_id, amount)

    def finish(
        self, account: Account, status: HoldStatus, entry_id: EntryId | None = None
    ) -> None:
        """Give the amount back to ``account`` and end as ``status``.

        ``entry_id`` is the entry a capture posts, which ``HoldFinished``
        names; the capture sets ``entry_id`` once the entry is written.

        Raises:
            HoldNotActive: It finished already, or has expired and is
                captured.
        """
        if self.status is not HoldStatus.ACTIVE:
            raise HoldNotActive("The hold was captured, released, or has expired.")
        if status is HoldStatus.CAPTURED and self.expires_at <= _now():
            raise HoldNotActive("The hold was captured, released, or has expired.")
        account.unreserve(self.amount)
        self.status = status
        self.finished_at = _now()
        self.record(
            HoldFinished(
                hold_id=self.id,
                account_id=self.account_id,
                status=status,
                entry_id=entry_id,
            )
        )


class Period(AggregateRoot[PeriodId]):
    """Dates entries may be booked on, from ``starts_on`` to ``ends_on`` included."""

    class Meta:
        """Its permissions: the defaults, and closing."""

        permissions = (("close_period", "Can close period"),)

    name: Annotated[str, MaxLength(MAX_PERIOD_NAME)]
    starts_on: date
    ends_on: date
    status: PeriodStatus = PeriodStatus.OPEN
    closed_at: datetime | None = None

    @classmethod
    def open(cls, name: str, starts_on: date, ends_on: date) -> Self:
        """A new open period; records ``PeriodOpened``.

        Raises:
            InvalidEntry: The name is empty or too long.
            PeriodsOverlap: It ends before it starts.
        """
        if not 1 <= len(name) <= MAX_PERIOD_NAME:
            raise InvalidEntry("A period's name has 1 to 64 characters.")
        if ends_on < starts_on:
            raise PeriodsOverlap("A period ends on or after the day it starts.")
        period = cls(
            id=PeriodId(uuid4()), name=name, starts_on=starts_on, ends_on=ends_on
        )
        period.record(
            PeriodOpened(
                period_id=period.id, name=name, starts_on=starts_on, ends_on=ends_on
            )
        )
        return period

    def takes(self) -> None:
        """Refuse an entry dated in this period if it is closed.

        Raises:
            PeriodIsClosed: The period is closed.
        """
        if self.status is PeriodStatus.CLOSED:
            raise PeriodIsClosed("The entry's date is in a closed period.")

    def close(self) -> None:
        """Close the period, recording ``PeriodClosed``.

        Raises:
            PeriodIsClosed: It is closed already.
        """
        self.takes()
        self.status = PeriodStatus.CLOSED
        self.closed_at = _now()
        self.record(PeriodClosed(period_id=self.id))
