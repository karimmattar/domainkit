"""Column types that behave the same on every supported database."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import operators
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import BindParameter, ColumnElement
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.sql.selectable import FromClause
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import TypeDecorator, TypeEngine

from domainkit.money import Currency, currency

MAX_DIGITS: Final = 18
"""The most digits an ``ExactDecimal`` holds: any 18 fit SQLite's 64-bit integers."""
_SCALING: Final = frozenset({operators.mul, operators.truediv})
_COMPARING: Final = frozenset(
    {
        operators.eq,
        operators.ne,
        operators.lt,
        operators.le,
        operators.gt,
        operators.ge,
        operators.in_op,
        operators.not_in_op,
        operators.and_,  # The bounds of between().
    }
)
_LIKE_UNITS: Final = frozenset(
    {
        operators.add,
        operators.sub,
        operators.mod,
        operators.eq,
        operators.ne,
        operators.lt,
        operators.le,
        operators.gt,
        operators.ge,
    }
)
"""Operations whose operands must count the same units: cents with cents."""
_INT64: Final = 2**63
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

    class Comparator(TypeDecorator.Comparator[Decimal]):
        """Count both sides in the same minor units when they differ (SQLite).

        An amount added to, subtracted from, or compared with an amount of
        another scale, or with an integer column, is scaled up to the larger
        scale. PostgreSQL computes in ``NUMERIC`` and needs nothing.
        """

        type: "ExactDecimal"

        def operate(
            self,
            op: OperatorType,
            *other: Any,  # noqa: ANN401 - any operand.
            **kwargs: Any,  # noqa: ANN401 - SQLAlchemy's options.
        ) -> ColumnElement[Any]:
            """Rescale a column operand of another scale; then operate as usual."""
            scale = self.type.scale
            theirs = _scale_of(other[0]) if op in _LIKE_UNITS and other else None
            if theirs is None or theirs == scale:
                return super().operate(op, *other, **kwargs)
            common = ExactDecimal(MAX_DIGITS, max(scale, theirs))
            mine = _Rescaled(self.expr, common.scale - scale, common)
            return op(mine, _Rescaled(other[0], common.scale - theirs, common))

    comparator_factory = Comparator

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
        A value compared with the column may have any number of places
        (``price > Decimal("9.995")``); it is compared exactly.
        """
        if op is operators.floordiv:
            raise TypeError(
                "Floor-divide ExactDecimal amounts in Python: on SQLite the "
                "column holds minor units, and the result would differ."
            )
        if op in _SCALING:
            whole = isinstance(value, int) and not isinstance(value, bool)
            return Integer() if whole and op is operators.mul else _Factor()
        if op in _COMPARING:
            return _Compared(self.scale)
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
        amount = _decimal(value)
        if amount and amount.adjusted() >= self.precision - self.scale:
            raise ValueError(
                f"{amount} has more than {self.precision - self.scale} digits "
                "before the point."
            )
        places = amount.quantize(Decimal(1).scaleb(-self.scale))
        if places != amount:
            raise ValueError(f"{amount} has more than {self.scale} decimal places.")
        return places


def _decimal(value: object) -> Decimal:
    """``value`` as a finite Decimal; floats and booleans are refused."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise TypeError(
            f"ExactDecimal takes a Decimal or an int, not {type(value).__name__}"
            " (floats are inexact)."
        )
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError(f"ExactDecimal cannot hold {amount}.")
    return amount


def _scale_of(operand: object) -> int | None:
    """The scale of a column expression an amount meets: its own, 0 for integers.

    None for values, which their bind type converts, and other types.
    """
    if not isinstance(operand, ColumnElement) or isinstance(operand, BindParameter):
        return None
    if isinstance(operand.type, ExactDecimal):
        return operand.type.scale
    return 0 if isinstance(operand.type, Integer) else None


class _Rescaled(ColumnElement[Decimal]):
    """An amount counted in the minor units of a larger scale.

    On SQLite that multiplies it by ``10 ** places``; elsewhere the value is
    in major units already, and stays as it is.
    """

    inherit_cache = True
    _traverse_internals = [  # noqa: RUF012 - SQLAlchemy's declaration.
        ("amount", InternalTraversal.dp_clauseelement),
        ("places", InternalTraversal.dp_plain_obj),
        ("type", InternalTraversal.dp_type),
    ]

    def __init__(
        self, amount: ColumnElement[Any], places: int, type_: "ExactDecimal"
    ) -> None:
        """Scale ``amount`` up by ``places``, as an amount of ``type_``."""
        self.amount = amount
        self.places = places
        self.type = type_

    @property
    def _from_objects(self) -> list[FromClause]:
        """The tables of the amount, so queries select from them."""
        return self.amount._from_objects


@compiles(_Rescaled)
def _rescaled(element: _Rescaled, compiler: SQLCompiler, **kwargs: Any) -> str:  # noqa: ANN401
    """Major units already: the expression itself."""
    return compiler.process(element.amount.self_group(), **kwargs)


@compiles(_Rescaled, "sqlite")
def _rescaled_on_sqlite(
    element: _Rescaled,
    compiler: SQLCompiler,
    **kwargs: Any,  # noqa: ANN401 - the compiler's options.
) -> str:
    """Minor units: multiply up to the larger scale."""
    inner = compiler.process(element.amount.self_group(), **kwargs)
    return f"({inner} * {10**element.places})" if element.places else inner


class _Compared(TypeDecorator[Decimal]):
    """A value compared with an amount column, exactly, whatever its places.

    PostgreSQL compares ``NUMERIC`` values exactly. On SQLite the column
    holds whole minor units: a value that is whole in them is sent as an
    integer, and one that is not as a float that is not whole either, which
    lies between the same two integers and so compares the same.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, scale: int) -> None:
        """Compare with a column of ``scale`` places."""
        super().__init__()
        self.scale = scale

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Values pass through as they are on SQLite."""
        if dialect.name == "sqlite":
            return dialect.type_descriptor(_MinorUnits())
        return dialect.type_descriptor(Numeric())

    def process_bind_param(
        self, value: Decimal | int | None, dialect: Dialect
    ) -> Decimal | int | float | None:
        """The exact value; on SQLite, in minor units."""
        if value is None:
            return None
        amount = _decimal(value)
        if dialect.name != "sqlite":
            return amount
        minor = amount.scaleb(self.scale)
        if minor == minor.to_integral_value():
            # Past 64 bits it is beyond every amount; a float is near enough.
            return int(minor) if abs(minor) < _INT64 else float(minor)
        approximate = float(minor)
        if approximate.is_integer():
            raise ValueError(
                f"{amount} is too close to a whole number of minor units to "
                f"compare on SQLite; round it to {self.scale} places."
            )
        return approximate


class _MinorUnits(Numeric[Decimal]):
    """``NUMERIC`` storage whose values pass through as integers (SQLite)."""

    def bind_processor(self, dialect: Dialect) -> None:
        """Send integers as they are."""

    def result_processor(self, dialect: Dialect, coltype: object) -> None:
        """Read integers as they are, never through a float."""


class _Factor(TypeDecorator[Decimal]):
    """A fraction an amount is multiplied by, or a number it is divided by.

    The result takes the amount's type. On SQLite the amount is in minor
    units, and so is the result, which that type scales back; the factor
    is sent as a float, so a quotient is not truncated to a whole number.
    PostgreSQL computes in ``NUMERIC``, exactly.
    """

    impl = Numeric
    cache_ok = True

    def process_bind_param(
        self, value: Decimal | int | None, dialect: Dialect
    ) -> Decimal | float | None:
        """A float on SQLite; as it is elsewhere."""
        if value is None or dialect.name != "sqlite":
            return value
        return float(value)


class CurrencyCode(TypeDecorator[Currency]):
    """A currency, stored as its code (``"USD"``) and read back as the ``Currency``.

    Values are a ``Currency`` or a code; a code no currency has is refused
    (``UnknownCurrency``), on the way in and on the way out, so a
    registered currency must be registered before its rows are read.
    Comparisons and ``ORDER BY`` compare the codes.
    """

    impl = String(8)  # The longest code: three letters for ISO 4217, eight registered.
    cache_ok = True

    @property
    def python_type(self) -> type[Currency]:
        """``Currency``."""
        return Currency

    def process_bind_param(
        self, value: Currency | str | None, dialect: Dialect
    ) -> str | None:
        """The code of a known currency."""
        return None if value is None else currency(value).code

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> Currency | None:
        """The currency of the stored code."""
        return None if value is None else currency(value)
