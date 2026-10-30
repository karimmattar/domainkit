"""The ledger's JSON: amounts, lines, and the representations of its aggregates.

An amount is ``{"amount": "10.50", "currency": "USD"}``, its amount always
text, never a JSON number: a number would pass through a float on its way.
The same shape comes in (``MoneyIn``) and goes out (``MoneyOut``), at the
currency's places.
"""

from datetime import date, datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from domainkit.money import Money

from .exceptions import InvalidAmount
from .models import (
    MAX_TEXT,
    Account,
    AccountStatus,
    AccountType,
    Entry,
    Hold,
    HoldStatus,
    Line,
    Period,
    PeriodStatus,
    Posting,
    Side,
    amount_text,
    ledger_currency,
)

AmountText = Annotated[
    str,
    Field(
        pattern=r"^-?\d{1,14}(\.\d{1,4})?$",
        description="Fixed-point text, such as 10.50; never a JSON number.",
        examples=["10.50"],
    ),
]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{2,7}$", examples=["USD"])]


class MoneyIn(BaseModel):
    """An amount and its currency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: AmountText
    currency: CurrencyCode

    def to_money(self) -> Money:
        """As ``Money``.

        Raises:
            InvalidAmount: The currency is unknown, or the amount has more
                places than it.
        """
        unit = ledger_currency(self.currency)
        try:
            return Money.of(self.amount, unit)
        except ValueError:
            raise InvalidAmount(
                "The amount has more places than its currency."
            ) from None


class MoneyOut(BaseModel):
    """An amount and its currency, the amount at the currency's places."""

    amount: str
    currency: str

    @classmethod
    def of(cls, amount: object, currency: str) -> Self:
        """``amount`` (a stored ``Decimal``) of ``currency``."""
        return cls(amount=amount_text(amount, currency), currency=currency)  # type: ignore[arg-type]


class LineIn(BaseModel):
    """A line of an entry to post."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    side: Side
    amount: MoneyIn
    memo: str = Field(default="", max_length=MAX_TEXT)

    @classmethod
    def of(cls, line: Line) -> Self:
        """``line``, to send in a message."""
        return cls(
            account_id=line.account_id,
            side=line.side,
            amount=MoneyIn(
                amount=amount_text(line.amount.amount, line.amount.currency.code),
                currency=line.amount.currency.code,
            ),
            memo=line.memo,
        )

    def to_line(self) -> Line:
        """As a ``Line``.

        Raises:
            InvalidAmount: As ``MoneyIn.to_money``.
        """
        from .models import AccountId

        return Line(
            AccountId(self.account_id), self.side, self.amount.to_money(), self.memo
        )


class AccountOut(BaseModel):
    """An account, its balance in its normal sign, and what is available."""

    id: UUID
    code: str
    name: str
    type: AccountType
    normal_balance: Side
    currency: str
    status: AccountStatus
    holder: str | None
    may_go_negative: bool
    balance: MoneyOut
    held: MoneyOut
    available: MoneyOut
    opened_at: datetime
    closed_at: datetime | None
    version: int

    @classmethod
    def of(cls, account: Account) -> Self:
        """Describe ``account``."""
        code = account.currency
        return cls(
            id=account.id,
            code=account.code,
            name=account.name,
            type=account.type,
            normal_balance=account.normal_balance,
            currency=code,
            status=account.status,
            holder=account.holder,
            may_go_negative=account.may_go_negative,
            balance=MoneyOut.of(account.balance, code),
            held=MoneyOut.of(account.held, code),
            available=MoneyOut.of(account.available, code),
            opened_at=account.opened_at,
            closed_at=account.closed_at,
            version=account.version,
        )


class PostingOut(BaseModel):
    """One line of a posted entry."""

    line: int
    account_id: UUID
    side: Side
    amount: MoneyOut
    memo: str

    @classmethod
    def of(cls, posting: Posting) -> Self:
        """Describe ``posting``."""
        return cls(
            line=posting.line,
            account_id=posting.account_id,
            side=posting.side,
            amount=MoneyOut.of(posting.amount, posting.currency),
            memo=posting.memo,
        )


class EntryOut(BaseModel):
    """A posted journal entry and its lines."""

    id: UUID
    source: str
    reference: str
    booked_on: date
    description: str
    period_id: UUID | None
    reverses: UUID | None
    posted_at: datetime
    lines: list[PostingOut]

    @classmethod
    def of(cls, entry: Entry) -> Self:
        """Describe ``entry``."""
        return cls(
            id=entry.id,
            source=entry.source,
            reference=entry.reference,
            booked_on=entry.booked_on,
            description=entry.description,
            period_id=entry.period_id,
            reverses=entry.reverses,
            posted_at=entry.posted_at,
            lines=[
                PostingOut.of(posting)
                for posting in sorted(entry.postings, key=lambda posting: posting.line)
            ],
        )


class HoldOut(BaseModel):
    """A hold and where it is."""

    id: UUID
    account_id: UUID
    amount: MoneyOut
    source: str
    reference: str
    status: HoldStatus
    expires_at: datetime
    placed_at: datetime
    finished_at: datetime | None
    entry_id: UUID | None
    version: int

    @classmethod
    def of(cls, hold: Hold) -> Self:
        """Describe ``hold``."""
        return cls(
            id=hold.id,
            account_id=hold.account_id,
            amount=MoneyOut.of(hold.amount, hold.currency),
            source=hold.source,
            reference=hold.reference,
            status=hold.status,
            expires_at=hold.expires_at,
            placed_at=hold.placed_at,
            finished_at=hold.finished_at,
            entry_id=hold.entry_id,
            version=hold.version,
        )


class PeriodOut(BaseModel):
    """An accounting period."""

    id: UUID
    name: str
    starts_on: date
    ends_on: date
    status: PeriodStatus
    closed_at: datetime | None
    version: int

    @classmethod
    def of(cls, period: Period) -> Self:
        """Describe ``period``."""
        return cls(
            id=period.id,
            name=period.name,
            starts_on=period.starts_on,
            ends_on=period.ends_on,
            status=period.status,
            closed_at=period.closed_at,
            version=period.version,
        )
