"""Money in tables: an amount column and a currency column, one attribute.

``Money`` (``domainkit.money``) is stored in two columns, so the amount
stays a number the database compares, orders, and sums, and the currency
a code beside it::

    accounts = Table(
        "billing_accounts",
        metadata,
        Column("id", Uuid, primary_key=True),
        *MoneyColumns("balance"),        # balance NUMERIC(18, 4), balance_currency
        Column("version", Integer, nullable=False),
    )

The amount column is ``ExactDecimal``, so it is exact on SQLite too; its
scale must hold the places of every currency stored in it (4 by default,
the most an ISO 4217 currency has). ``table_for`` derives the same columns
for a field hinted ``Money`` (``Annotated[Money, Digits(18, 2)]`` to size
them), and ``map_imperatively`` maps the pair to the one attribute, so the
aggregate sees ``Money`` and queries see its parts (ADR 0115).
"""

from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import (
    CheckConstraint,
    Column,
    ColumnElement,
    Table,
    and_,
    event,
    not_,
    or_,
)
from sqlalchemy.orm import Composite, Mapper, composite
from sqlalchemy.sql import operators
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.sql.schema import SchemaItem

from domainkit.db.types import CurrencyCode, ExactDecimal
from domainkit.exceptions import DomainkitError, ImproperlyConfigured
from domainkit.money import AmountOutOfRange, Currency, Money

__all__ = [
    "IncompleteMoney",
    "MoneyColumns",
    "money_check",
]

MONEY: Final = "money"
"""The ``info`` key of a money column: ``(field, AMOUNT)`` or ``(field, CURRENCY)``."""
AMOUNT: Final = "amount"
CURRENCY: Final = "currency"
PRECISION: Final = 18
"""The digits of an amount column by default: as many as ``ExactDecimal`` holds."""
SCALE: Final = 4
"""The places of an amount column by default: the most an ISO 4217 currency has."""
CHECK: Final = "money_check"
"""The ``info`` key of a pair's both-or-neither check: the field's name."""
_EQUALITY: Final = frozenset({operators.eq, operators.ne})


class IncompleteMoney(ValueError, DomainkitError):
    """A row holds an amount without its currency, or a currency without its amount.

    The pair's check keeps that from being stored (``money_check``); a row
    written before the check, or around it, is refused when read.
    """


def money_check(
    name: str, amount: Column[Any], currency: Column[Any]
) -> CheckConstraint:
    """The check that the money field ``name``'s two columns are both set or neither.

    ``MoneyColumns`` and ``table_for`` add it to a pair that may be missing,
    and ``makemigrations`` writes it when it adds such a pair to a table
    that exists.
    """
    both = or_(
        and_(amount.is_(None), currency.is_(None)),
        and_(amount.is_not(None), currency.is_not(None)),
    )
    return CheckConstraint(both, name=f"{name}_money", info={CHECK: name})


class MoneyColumns:
    """The two columns of a money field: ``<name>`` and ``<name>_currency``.

    Pass them to a ``Table`` (``*MoneyColumns("total")``). ``<name>`` holds
    the amount in an ``ExactDecimal(precision, scale)``, and
    ``<name>_currency`` the currency's code (``CurrencyCode``). A nullable
    pair also gets a check that both are missing or neither is.
    ``map_imperatively`` maps them to the attribute ``<name>``.
    """

    def __init__(
        self,
        name: str,
        precision: int = PRECISION,
        scale: int = SCALE,
        *,
        nullable: bool = False,
    ) -> None:
        """Describe the columns of the money field ``name``.

        Raises:
            ValueError: ``precision`` or ``scale`` is out of ``ExactDecimal``'s bounds.
        """
        self.name = name
        self.amount: Column[Decimal] = Column(
            name,
            ExactDecimal(precision, scale),
            nullable=nullable,
            info={MONEY: (name, AMOUNT)},
        )
        self.currency: Column[Currency] = Column(
            f"{name}_{CURRENCY}",
            CurrencyCode(),
            nullable=nullable,
            info={MONEY: (name, CURRENCY)},
        )
        self.constraints: tuple[CheckConstraint, ...] = ()
        if nullable:
            self.constraints = (money_check(name, self.amount, self.currency),)

    def __iter__(self) -> Iterator[SchemaItem]:
        """The amount column, the currency column, and the check if nullable."""
        yield self.amount
        yield self.currency
        yield from self.constraints


def money_properties(table: Table, given: Mapping[str, Any]) -> dict[str, Any]:
    """The mapper properties of ``table``'s money columns, those not ``given``.

    Each pair is mapped to one ``Money`` attribute; its columns to private
    attributes (``_total_amount``, ``_total_currency``), so the field's
    name is the money's.

    Raises:
        ImproperlyConfigured: A money column has no partner.
    """
    pairs: dict[str, dict[str, Column[Any]]] = {}
    for column in table.columns:
        if (marked := column.info.get(MONEY)) is not None:
            name, part = marked
            pairs.setdefault(name, {})[part] = column
    properties: dict[str, Any] = {}
    for name, columns in pairs.items():
        if name in given:
            continue
        if set(columns) != {AMOUNT, CURRENCY}:
            raise ImproperlyConfigured(
                f"{table.name}.{name} has one money column of two; make both "
                f"with MoneyColumns({name!r})."
            )
        properties[f"_{name}_{AMOUNT}"] = columns[AMOUNT]
        properties[f"_{name}_{CURRENCY}"] = columns[CURRENCY]
        properties[name] = composite(
            _money,
            columns[AMOUNT],
            columns[CURRENCY],
            comparator_factory=MoneyComparator,
        )
    return properties


def guard_money(class_: type, table: Table, names: Iterable[str]) -> None:
    """Refuse, as it is set, money the attribute's amount column cannot hold.

    Each of ``names`` is a money attribute of ``class_``, mapped to a pair
    of ``table``'s columns. Set to an amount with more digits before the
    point, or more places, than its ``ExactDecimal`` holds, it raises
    ``AmountOutOfRange`` and keeps its value: a 422 where the use case
    computed it, not a failed flush.
    """
    for column in table.columns:
        marked = column.info.get(MONEY)
        if (
            marked is not None
            and marked[0] in names
            and marked[1] == AMOUNT
            and isinstance(column.type, ExactDecimal)
        ):
            event.listen(getattr(class_, marked[0]), "set", _Guard(column))


class _Guard:
    """The ``set`` listener of one money attribute: its amount must fit its column."""

    def __init__(self, column: Column[Any]) -> None:
        """Guard ``column``, an ``ExactDecimal``."""
        self.column = column
        self.type = cast("ExactDecimal", column.type)

    def __call__(
        self,
        target: object,
        value: object,
        old: object,
        initiator: object,
    ) -> None:
        """Refuse ``value`` if it is money its column cannot hold.

        Raises:
            AmountOutOfRange: It has more digits before the point, or more
                places, than the column.
        """
        if not isinstance(value, Money):
            return
        try:
            self.type._exact(value.amount)
        except ValueError:
            kind = self.type
            raise AmountOutOfRange(
                f"{self.column.table.name}.{self.column.name} holds {kind.scale} "
                f"decimal places, {kind.precision - kind.scale} digits before the "
                f"point: this {value.currency.code} amount does not fit."
            ) from None


class MoneyComparator(Composite.Comparator[Money]):
    """Compare a mapped money attribute as money: within its currency.

    ``Wallet.balance == m`` is both columns equal, ``!=`` its negation
    (true of no money), ``Wallet.balance < m`` the currency equal and the
    amount less, as ``domainkit.ddd.query`` and ``Money`` compare;
    ``== None`` is a missing amount. Without it, SQLAlchemy would compare
    each column on its own: ``balance < 100 AND balance_currency < 'USD'``
    (SEC-14a-2).
    """

    def _compare(
        self,
        operator: OperatorType,
        other: Any,  # noqa: ANN401 - money, or None.
    ) -> ColumnElement[bool]:
        """The comparison of both columns with ``other``.

        Raises:
            TypeError: ``other`` is not money (None only with ``==`` and ``!=``).
        """
        amount, code = self._comparable_elements
        if other is None and operator in _EQUALITY:
            missing = amount.is_(None)
            return missing if operator is operators.eq else not_(missing)
        if not isinstance(other, Money):
            raise TypeError(f"Money compares with Money, not {type(other).__name__}.")
        same = code == other.currency
        equal = and_(same, amount == other.amount)
        if operator is operators.eq:
            return equal
        if operator is operators.ne:  # As in Python: no money is other money.
            return or_(amount.is_(None), not_(equal))
        return and_(same, operator(amount, other.amount))


def _money(amount: Decimal | None, currency: Currency | None) -> Money | None:
    """The money two columns hold; None where both are missing.

    Raises:
        IncompleteMoney: One of the two is missing.
    """
    if amount is None and currency is None:
        return None
    if amount is None or currency is None:
        raise IncompleteMoney(
            "A row holds an amount without its currency, or a currency without "
            "its amount; add the pair's check (money_check) and repair the row."
        )
    return Money(amount, currency)


def money_columns(mapper: Mapper[Any], name: str) -> tuple[Column[Any], ...] | None:
    """The amount and currency columns of the money attribute ``name``; else None."""
    if name not in mapper.composites:
        return None
    columns = tuple(cast("list[Column[Any]]", mapper.composites[name].columns))
    return columns if columns[0].info.get(MONEY) is not None else None
