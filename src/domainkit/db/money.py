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

from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import CheckConstraint, Column, Table, and_, or_
from sqlalchemy.orm import Mapper, composite
from sqlalchemy.sql.schema import SchemaItem

from domainkit.db.types import CurrencyCode, ExactDecimal
from domainkit.exceptions import ImproperlyConfigured
from domainkit.money import Currency, Money

__all__ = [
    "MoneyColumns",
]

MONEY: Final = "money"
"""The ``info`` key of a money column: ``(field, AMOUNT)`` or ``(field, CURRENCY)``."""
AMOUNT: Final = "amount"
CURRENCY: Final = "currency"
PRECISION: Final = 18
"""The digits of an amount column by default: as many as ``ExactDecimal`` holds."""
SCALE: Final = 4
"""The places of an amount column by default: the most an ISO 4217 currency has."""


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
            both = or_(
                and_(self.amount.is_(None), self.currency.is_(None)),
                and_(self.amount.is_not(None), self.currency.is_not(None)),
            )
            self.constraints = (CheckConstraint(both, name=f"{name}_money"),)

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
        properties[name] = composite(_money, columns[AMOUNT], columns[CURRENCY])
    return properties


def _money(amount: Decimal | None, currency: Currency | None) -> Money | None:
    """The money two columns hold; None where the amount is missing.

    A check keeps a currency from being stored without its amount.
    """
    return None if amount is None else Money(amount, currency)  # type: ignore[arg-type]


def money_columns(mapper: Mapper[Any], name: str) -> tuple[Column[Any], ...] | None:
    """The amount and currency columns of the money attribute ``name``; else None."""
    if name not in mapper.composites:
        return None
    columns = tuple(cast("list[Column[Any]]", mapper.composites[name].columns))
    return columns if columns[0].info.get(MONEY) is not None else None
