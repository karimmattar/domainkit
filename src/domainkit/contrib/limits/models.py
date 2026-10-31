"""Limits on money movements, and the movements checked against them.

A ``Limit`` bounds the movements of one kind of subject (an account, a
holder, an actor, or a kind of the project's own), in one currency and one
direction: each movement's amount (``per_transaction``), and the sum and
the number of them in a window (``max_amount``, ``max_count``). Its
``subject`` names one subject it is for, or is ``""`` for every subject of
its kind; one for a subject replaces the one for every subject.

Windows are calendar days and months, which start at midnight in the
limit's ``time_zone``, and rolling ones: the last 24 hours, or 30 days.
Amounts are ``Money``, of at most four decimal places, as the ledger's.
"""

import re
from collections.abc import Mapping
from dataclasses import field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final, NewType, Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from domainkit import clock
from domainkit.ddd import AggregateRoot, DomainEvent, MaxLength, ValueObject
from domainkit.money import Currency, Money, currency

from .exceptions import InvalidLimit

LimitId = NewType("LimitId", UUID)

SCALE: Final = 4
"""The most decimal places an amount may have: the columns' scale."""
CODE: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
"""A limit's code: what it is, such as ``wallet.daily``."""
KIND: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
"""A kind of subject: ``account``, ``holder``, ``actor``, or the project's own."""
SUBJECT: Final = re.compile(r"[^\x00-\x1f\x7f]{1,255}")
"""One subject of a kind: 1 to 255 printable characters."""
MAX_NOTE: Final = 255
MAX_ZONE: Final = 64
ACCOUNT: Final = "account"
"""The kind of subject that is a ledger's account, by its id."""
HOLDER: Final = "holder"
"""The kind of subject that is the holder an account is kept for."""
ACTOR: Final = "actor"
"""The kind of subject that is who acts: the request's actor."""


def _now() -> datetime:
    """The current time in UTC (``domainkit.clock``)."""
    return clock.now()


class Direction(StrEnum):
    """Which movements of a subject a limit counts."""

    OUT = "out"
    """Money leaving: what lowers an account's balance."""
    IN = "in"
    """Money coming in: what raises it."""


class Window(StrEnum):
    """The time a limit's sum and count are taken over."""

    DAY = "day"
    """The calendar day, from midnight in the limit's time zone."""
    MONTH = "month"
    """The calendar month, from midnight of its first day in the limit's zone."""
    ROLLING_DAY = "rolling_day"
    """The last 24 hours."""
    ROLLING_MONTH = "rolling_month"
    """The last 30 days."""

    @property
    def rolling(self) -> bool:
        """Whether it reaches back from now: its start is not in it."""
        return self in (Window.ROLLING_DAY, Window.ROLLING_MONTH)

    def start(self, now: datetime, zone: str) -> datetime:
        """When the window that holds ``now`` started, in UTC.

        A calendar window holds its start (midnight); a rolling one does
        not: what was counted 24 hours ago has left the last 24 hours.

        Args:
            now: The present, with its zone (the database's clock).
            zone: The limit's time zone, for calendar windows.
        """
        if self is Window.ROLLING_DAY:
            return now - timedelta(days=1)
        if self is Window.ROLLING_MONTH:
            return now - timedelta(days=30)
        local = now.astimezone(ZoneInfo(zone))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if self is Window.MONTH:
            midnight = midnight.replace(day=1)
        return midnight.astimezone(UTC)


def limit_currency(code: str) -> Currency:
    """The currency of ``code``, if limits can hold its amounts exactly.

    Raises:
        UnknownCurrency: No currency has that code (``domainkit.money``).
        InvalidLimit: It has more minor units than ``SCALE``.
    """
    unit = currency(code)
    if unit.minor_units > SCALE:
        raise InvalidLimit("Limits keep amounts of at most four decimal places.")
    return unit


def check_kind(kind: str) -> str:
    """``kind`` if it names a kind of subject.

    Raises:
        InvalidLimit: It does not.
    """
    if not KIND.fullmatch(kind):
        raise InvalidLimit(
            "A kind of subject has up to 32 lower-case letters, digits, or '_', "
            "starting with a letter."
        )
    return kind


def check_subject(subject: str) -> str:
    """``subject`` if it names one subject.

    Raises:
        InvalidLimit: It does not.
    """
    if not SUBJECT.fullmatch(subject):
        raise InvalidLimit("A subject has 1 to 255 printable characters.")
    return subject


def check_zone(zone: str) -> str:
    """``zone`` if it names a time zone (``Africa/Cairo``).

    Raises:
        InvalidLimit: It does not.
    """
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise InvalidLimit(
            "The time zone is not one, such as 'Africa/Cairo'."
        ) from None
    return zone


class LimitSet(DomainEvent):
    """A limit was made or changed: what it allows, as it now stands."""

    limit_id: LimitId
    code: str
    kind: str
    direction: Direction
    currency: str
    subject: str
    """The subject it is for, an id (a holder's, an actor's); ``""``, every one."""
    per_transaction: Money | None
    window: Window | None
    max_amount: Money | None
    max_count: int | None
    time_zone: str
    enabled: bool


class LimitRemoved(DomainEvent):
    """A limit was removed: a default of the settings may apply again."""

    limit_id: LimitId
    code: str
    kind: str
    subject: str


class Limit(AggregateRoot[LimitId]):
    """What one kind of subject may move in one currency and direction.

    Its code, kind, direction, currency, and subject are its key: at most
    one limit has each, and they never change. Its bounds change
    (``change``): ``per_transaction``, the most one movement may be; and,
    over ``window``, ``max_amount`` and ``max_count``. At least one bound
    is set. A disabled limit bounds nothing, and still replaces a default
    of the settings with its key.
    """

    class Meta:
        """Django's four permissions: view, add, change, delete."""

        verbose_name = "limit"

    code: Annotated[str, MaxLength(64)]
    kind: Annotated[str, MaxLength(32)]
    direction: Direction
    currency: Annotated[str, MaxLength(8)]
    subject: Annotated[str, MaxLength(255)] = ""
    per_transaction: Money | None = None
    window: Window | None = None
    max_amount: Money | None = None
    max_count: int | None = None
    time_zone: Annotated[str, MaxLength(MAX_ZONE)] = "UTC"
    enabled: bool = True
    note: Annotated[str, MaxLength(MAX_NOTE)] = ""
    changed_at: datetime = field(default_factory=_now)

    @classmethod
    def define(
        cls,
        code: str,
        kind: str,
        currency: str,
        *,
        direction: Direction = Direction.OUT,
        subject: str = "",
        per_transaction: Money | None = None,
        window: Window | None = None,
        max_amount: Money | None = None,
        max_count: int | None = None,
        time_zone: str = "UTC",
        enabled: bool = True,
        note: str = "",
    ) -> Self:
        """A new limit; records ``LimitSet``.

        Raises:
            InvalidLimit: The code, kind, subject, or a bound is malformed,
                or no bound is set (``change``); the currency has more than
                ``SCALE`` places.
            UnknownCurrency: The currency is unknown.
        """
        if not CODE.fullmatch(code):
            raise InvalidLimit(
                "A limit's code has up to 64 lower-case letters, digits, '_', '.', "
                "':', or '-'."
            )
        if subject:
            check_subject(subject)
        limit = cls(
            id=LimitId(uuid4()),
            code=code,
            kind=check_kind(kind),
            direction=Direction(direction),
            currency=limit_currency(currency).code,
            subject=subject,
        )
        limit.change(
            per_transaction=per_transaction,
            window=window,
            max_amount=max_amount,
            max_count=max_count,
            time_zone=time_zone,
            enabled=enabled,
            note=note,
        )
        return limit

    def change(
        self,
        *,
        per_transaction: Money | None,
        window: Window | None,
        max_amount: Money | None,
        max_count: int | None,
        time_zone: str,
        enabled: bool,
        note: str,
    ) -> None:
        """Set the limit's bounds; records ``LimitSet``.

        Raises:
            InvalidLimit: No bound is set; an amount is not more than zero,
                or not in the limit's currency; a count is less than one; a
                window has neither ``max_amount`` nor ``max_count``, or one
                of them has no window; the zone or the note is wrong.
        """
        for amount in (per_transaction, max_amount):
            if amount is not None:
                self._check_amount(amount)
        if max_count is not None and max_count < 1:
            raise InvalidLimit("A limit's count is at least one.")
        bounded = max_amount is not None or max_count is not None
        if bounded != (window is not None):
            raise InvalidLimit(
                "A window needs a maximum amount or count, and they need a window."
            )
        if per_transaction is None and not bounded:
            raise InvalidLimit(
                "A limit sets an amount per transaction, or a maximum over a window."
            )
        if len(note) > MAX_NOTE:
            raise InvalidLimit("A limit's note has at most 255 characters.")
        self.per_transaction = per_transaction
        self.window = None if window is None else Window(window)
        self.max_amount = max_amount
        self.max_count = max_count
        self.time_zone = check_zone(time_zone)
        self.enabled = enabled
        self.note = note
        self.changed_at = _now()
        self.record(
            LimitSet(
                limit_id=self.id,
                code=self.code,
                kind=self.kind,
                direction=self.direction,
                currency=self.currency,
                subject=self.subject,
                per_transaction=per_transaction,
                window=self.window,
                max_amount=max_amount,
                max_count=max_count,
                time_zone=time_zone,
                enabled=enabled,
            )
        )

    def _check_amount(self, amount: Money) -> None:
        """Refuse an amount of another currency, or not more than zero.

        The limit's currency has at most ``SCALE`` places, so its amounts do.
        """
        if amount.currency.code != self.currency:
            raise InvalidLimit("A limit's amounts are in its currency.")
        if amount.amount <= 0:
            raise InvalidLimit("A limit's amounts are more than zero.")

    def remove(self) -> None:
        """Record that the limit is removed (``LimitRemoved``)."""
        self.record(
            LimitRemoved(
                limit_id=self.id, code=self.code, kind=self.kind, subject=self.subject
            )
        )


class Movement(ValueObject):
    """An amount of money moving, and the subjects whose limits it counts against.

    ``subjects`` maps a kind to the subject of that kind it moves for::

        Movement(
            Money.of("150.00", "EGP"),
            {"holder": str(user.id), "account": str(wallet.id)},
        )

    Each subject is checked against its kind's limits in the amount's
    currency and in ``direction``.
    """

    amount: Money
    subjects: Mapping[str, str]
    direction: Direction = Direction.OUT

    def __post_init__(self) -> None:
        """Refuse an amount not above zero, and no subjects or malformed ones.

        Raises:
            InvalidLimit: As said.
        """
        if self.amount.amount <= 0:
            raise InvalidLimit("A movement's amount is more than zero.")
        if not self.subjects:
            raise InvalidLimit("A movement names at least one subject.")
        for kind, subject in self.subjects.items():
            check_kind(kind)
            check_subject(subject)
