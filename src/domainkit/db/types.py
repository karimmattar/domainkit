"""Column types that behave the same on every supported database."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import DateTime, Integer, Numeric
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import operators
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.types import TypeDecorator, TypeEngine

MAX_DIGITS: Final = 18
"""The most digits an ``ExactDecimal`` holds: any 18 fit SQLite's 64-bit integers."""
_SCALING: Final = frozenset({operators.mul, operators.truediv})
"""Operators whose other operand is a plain number, not an amount."""


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps, also on SQLite (which has no time zones).

    Naive datetimes are rejected rather than guessed; values come back aware
    and in UTC, so comparisons never mix naive and aware times.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Convert to UTC; SQLite stores it without the offset."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime needs a timezone-aware datetime.")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Return an aware UTC datetime."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ExactDecimal(TypeDecorator[Decimal]):
    """Fixed-point amounts that come back exactly as stored, also on SQLite.

    ``ExactDecimal(12, 2)`` holds up to 12 digits, 2 after the point.
    PostgreSQL stores it as ``NUMERIC(12, 2)``. SQLite has no decimal type
    and keeps ``NUMERIC`` values as floats, which lose digits after the 15th,
    so there it holds a whole number of minor units (cents): comparisons,
    ``ORDER BY``, ``sum``, ``min``, and ``max`` stay exact.

    Values are ``Decimal`` or ``int``. Floats are refused, being inexact, and
    so are values with more places or digits than the column holds: nothing
    is rounded on the way in.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        """Hold ``precision`` digits, ``scale`` of them after the point."""
        if not (0 <= scale <= precision and 1 <= precision <= MAX_DIGITS):
            raise ValueError(
                f"ExactDecimal needs 1 <= precision <= {MAX_DIGITS} and "
                f"0 <= scale <= precision, not precision={precision}, "
                f"scale={scale}."
            )
        super().__init__(precision=precision, scale=scale)
        self.precision = precision
        self.scale = scale

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """``NUMERIC`` everywhere; on SQLite it holds integers, read as they are."""
        if dialect.name == "sqlite":
            return dialect.type_descriptor(_MinorUnits(self.precision, self.scale))
        return dialect.type_descriptor(Numeric(self.precision, self.scale))

    def coerce_compared_value(
        self,
        op: OperatorType | None,
        value: Any,  # noqa: ANN401 - any Python value in an expression.
    ) -> TypeEngine[Any]:
        """Amounts compared, added, or subtracted; plain factors and divisors.

        ``price * 2`` doubles the price, so on SQLite the 2 must not become
        200 minor units. A fraction or a divisor is a ``_Factor``. Floor
        division, whose result is a count rather than an amount, is refused.
        """
        if op is operators.floordiv:
            raise TypeError(
                "Floor-divide ExactDecimal amounts in Python: on SQLite the "
                "column holds minor units, and the result would differ."
            )
        if op in _SCALING:
            whole = isinstance(value, int) and not isinstance(value, bool)
            return Integer() if whole and op is operators.mul else _Factor(self.scale)
        return self

    def process_bind_param(
        self, value: Decimal | int | None, dialect: Dialect
    ) -> Decimal | int | None:
        """Check the value fits exactly; on SQLite, count it in minor units."""
        if value is None:
            return None
        amount = self._exact(value)
        if dialect.name == "sqlite":
            return int(amount.scaleb(self.scale))
        return amount

    def process_result_value(
        self, value: Decimal | float | None, dialect: Dialect
    ) -> Decimal | None:
        """Return a ``Decimal``: with the column's places, unless computed.

        SQLite returns a float for a fractional product or a quotient; its
        shortest form is used, not every digit of the binary value.
        """
        if value is None:
            return None
        amount = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        if dialect.name == "sqlite":
            return amount.scaleb(-self.scale)
        return amount

    def _exact(self, value: object) -> Decimal:
        """``value`` as a Decimal with the column's places; refuse what would round."""
        if isinstance(value, bool) or not isinstance(value, Decimal | int):
            raise TypeError(
                f"ExactDecimal takes a Decimal or an int, not {type(value).__name__}"
                " (floats are inexact)."
            )
        amount = Decimal(value)
        if not amount.is_finite():
            raise ValueError(f"ExactDecimal cannot hold {amount}.")
        if amount and amount.adjusted() >= self.precision - self.scale:
            raise ValueError(
                f"{amount} has more than {self.precision - self.scale} digits "
                "before the point."
            )
        places = amount.quantize(Decimal(1).scaleb(-self.scale))
        if places != amount:
            raise ValueError(f"{amount} has more than {self.scale} decimal places.")
        return places


class _MinorUnits(Numeric[Decimal]):
    """``NUMERIC`` storage whose values pass through as integers (SQLite)."""

    def bind_processor(self, dialect: Dialect) -> None:
        """Send integers as they are."""

    def result_processor(self, dialect: Dialect, coltype: object) -> None:
        """Read integers as they are, never through a float."""


class _Factor(TypeDecorator[Decimal]):
    """A fraction an amount is multiplied by, or a number it is divided by.

    The result takes this type. On SQLite the amount is in minor units, and
    so is the result, which is scaled back; the factor is sent as a float,
    so a quotient is not truncated to a whole number. PostgreSQL computes
    in ``NUMERIC``, exactly.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, scale: int) -> None:
        """Scale results back by ``scale`` places on SQLite."""
        super().__init__()
        self.scale = scale

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Values pass through as they are on SQLite."""
        if dialect.name == "sqlite":
            return dialect.type_descriptor(_MinorUnits())
        return dialect.type_descriptor(Numeric())

    def process_bind_param(
        self, value: Decimal | int | None, dialect: Dialect
    ) -> Decimal | float | None:
        """A float on SQLite; as it is elsewhere."""
        if value is None or dialect.name != "sqlite":
            return value
        return float(value)

    def process_result_value(
        self, value: Decimal | float | None, dialect: Dialect
    ) -> Decimal | None:
        """The result as a ``Decimal``, from minor units on SQLite."""
        if value is None:
            return None
        amount = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        if dialect.name == "sqlite":
            return amount.scaleb(-self.scale)
        return amount
