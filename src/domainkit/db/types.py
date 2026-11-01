"""Column types that behave the same on every supported database."""

import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    DefaultClause,
    Integer,
    Numeric,
    String,
    Table,
    TextClause,
    literal,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql import operators
from sqlalchemy.sql.compiler import DDLCompiler, SQLCompiler
from sqlalchemy.sql.elements import BindParameter, ColumnElement
from sqlalchemy.sql.operators import OperatorType
from sqlalchemy.sql.selectable import FromClause
from sqlalchemy.sql.visitors import InternalTraversal
from sqlalchemy.types import TypeDecorator, TypeEngine

from domainkit.money import Currency, currency

STORED_DIGITS: Final = 18
"""The most digits an ``ExactDecimal`` holds: any 18 fit SQLite's 64-bit integers.

Fewer than ``domainkit.money.MAX_DIGITS`` (38), the most ``Money`` holds in
memory: a stored amount has at most ``precision - scale`` digits before its
point, so a money column refuses (``AmountOutOfRange``) what money takes.
"""
FACTOR_PLACES: Final = 9
"""The most places of a number an amount is multiplied or divided by in SQL.

On SQLite such a factor is sent as a whole number of billionths, so the
product of two exact integers is what the database divides back.
"""
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
            """Scale by a factor, or rescale an operand of another scale; else as usual.

            A number an amount is multiplied or divided by, given as a value
            or as a bound parameter, is a factor, never an amount: on SQLite
            it must not be counted in minor units.

            Raises:
                TypeError: The operand of a product or a quotient is an
                    amount too (``ExactDecimal``): the result means nothing
                    for money, and on SQLite both would count in minor units.
            """
            if op in _SCALING and other:
                if _is_amount(other[0]):
                    raise TypeError(
                        "An amount (ExactDecimal) is multiplied or divided by a "
                        "number, not by another amount: give the factor as a "
                        "plain Decimal or an untyped bindparam, or an integer or "
                        "numeric column."
                    )
                factor = _factor(op, other[0])
                if factor is not None:
                    return _Scaling(self.expr, factor, op, self.type)
            scale = self.type.scale
            theirs = _scale_of(other[0]) if op in _LIKE_UNITS and other else None
            if theirs is None or theirs == scale:
                return super().operate(op, *other, **kwargs)
            common = ExactDecimal(STORED_DIGITS, max(scale, theirs))
            mine = _Rescaled(self.expr, common.scale - scale, common)
            return op(mine, _Rescaled(other[0], common.scale - theirs, common))

        def reverse_operate(
            self,
            op: OperatorType,
            other: Any,  # noqa: ANN401 - any operand.
            **kwargs: Any,  # noqa: ANN401 - SQLAlchemy's options.
        ) -> ColumnElement[Any]:
            """``factor * amount`` is ``amount * factor``; the rest as usual."""
            if op is operators.mul and (factor := _factor(op, other)) is not None:
                return _Scaling(self.expr, factor, op, self.type)
            return super().reverse_operate(op, other, **kwargs)

    comparator_factory = Comparator

    def __init__(self, precision: int, scale: int) -> None:
        """Hold ``precision`` digits, ``scale`` of them after the point."""
        if not (0 <= scale <= precision and 1 <= precision <= STORED_DIGITS):
            raise ValueError(
                f"ExactDecimal needs 1 <= precision <= {STORED_DIGITS} and "
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


def _is_amount(operand: object) -> bool:
    """Whether ``operand`` is an expression of an amount: typed ``ExactDecimal``."""
    return isinstance(operand, ColumnElement) and isinstance(operand.type, ExactDecimal)


def _factor(op: OperatorType, operand: object) -> ColumnElement[Any] | None:
    """``operand`` as a factor of an amount, if it is one; else None.

    A value or an untyped bound parameter is a ``_Factor``; so is a whole
    number a divisor, while a whole multiplier stays an integer (exact in
    minor units). An integer column divides as it is. Anything else (an
    amount column, a typed parameter) is left to SQLAlchemy.
    """
    if isinstance(operand, BindParameter):
        null = operand.type._isnull  # SQLAlchemy's flag of an untyped parameter.
        return operand._with_binary_element_type(_Factor()) if null else None
    if isinstance(operand, ColumnElement):
        divides = op is operators.truediv and isinstance(operand.type, Integer)
        return operand if divides else None
    whole = isinstance(operand, int) and not isinstance(operand, bool)
    if whole and op is operators.mul:
        return None
    return literal(operand, _Factor())


class _Scaling(ColumnElement[Decimal]):
    """An amount multiplied or divided by a factor, as the amount's type.

    PostgreSQL computes it in ``NUMERIC``, exactly. On SQLite the amount is
    minor units and a bound factor billionths (``FACTOR_PLACES``): the two
    integers are multiplied exactly, then divided back, so a result of 15
    significant digits or fewer reads back exactly. An integer column
    divides the amount as a real number, never by integer division.
    """

    inherit_cache = True
    _traverse_internals = [  # noqa: RUF012 - SQLAlchemy's declaration.
        ("amount", InternalTraversal.dp_clauseelement),
        ("factor", InternalTraversal.dp_clauseelement),
        ("dividing", InternalTraversal.dp_plain_obj),
        ("type", InternalTraversal.dp_type),
    ]

    def __init__(
        self,
        amount: ColumnElement[Any],
        factor: ColumnElement[Any],
        op: OperatorType,
        type_: "ExactDecimal",
    ) -> None:
        """Scale ``amount`` by ``factor`` (``op`` is ``mul`` or ``truediv``)."""
        self.amount = amount
        self.factor = factor
        self.dividing = op is operators.truediv
        self.type = type_

    @property
    def _from_objects(self) -> list[FromClause]:
        """The tables of both operands."""
        return [*self.amount._from_objects, *self.factor._from_objects]


@compiles(_Scaling)
def _scaling(element: _Scaling, compiler: SQLCompiler, **kwargs: Any) -> str:  # noqa: ANN401
    """``amount * factor`` or ``amount / factor``, in ``NUMERIC``."""
    amount = compiler.process(element.amount.self_group(), **kwargs)
    factor = compiler.process(element.factor.self_group(), **kwargs)
    sign = "/" if element.dividing else "*"
    return f"({amount} {sign} {factor})"


@compiles(_Scaling, "sqlite")
def _scaling_on_sqlite(
    element: _Scaling,
    compiler: SQLCompiler,
    **kwargs: Any,  # noqa: ANN401 - the compiler's options.
) -> str:
    """Minor units times billionths, divided back; or divided as reals."""
    amount = compiler.process(element.amount.self_group(), **kwargs)
    factor = compiler.process(element.factor.self_group(), **kwargs)
    unit = 10**FACTOR_PLACES if isinstance(element.factor.type, _Factor) else 1
    if element.dividing:
        return f"(({amount} * {unit}) / ({factor} * 1.0))"
    return f"(({amount} * {factor}) / {unit}.0)"


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
    """A number an amount is multiplied or divided by: never an amount itself.

    It has at most ``FACTOR_PLACES`` places, refused past them rather than
    rounded. On SQLite it is sent as a whole number of billionths (see
    ``_Scaling``); elsewhere as the exact ``Decimal``.
    """

    impl = Numeric
    cache_ok = True

    def process_bind_param(
        self, value: Decimal | int | None, dialect: Dialect
    ) -> Decimal | int | None:
        """The exact factor; on SQLite, in billionths.

        Raises:
            TypeError: A float, a ``bool``, or not a number.
            ValueError: Not finite, or more than ``FACTOR_PLACES`` places.
        """
        if value is None:
            return None
        factor = _decimal(value)
        if factor.as_tuple().exponent < -FACTOR_PLACES:  # type: ignore[operator]
            raise ValueError(
                f"A factor of an amount has at most {FACTOR_PLACES} decimal "
                "places in SQL; round it first, or compute in Python."
            )
        if dialect.name == "sqlite":
            return int(factor.scaleb(FACTOR_PLACES))
        return factor


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


# SQL written around an amount, on SQLite ------------------------------------

_DEFAULT_LITERAL: Final = re.compile(
    r"\s*\(?\s*'?\s*(-?[0-9]+(?:\.[0-9]+)?)\s*'?\s*\)?\s*"
)
"""A server default that is a plain number: ``1.50``, ``'1.50'``, ``(0)``."""
_SQL_TOKEN: Final = re.compile(
    r"(?P<string>'(?:[^']|'')*')"  # Left as it is.
    r'|(?P<name>(?:(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)\.)?'  # Its table's.
    r'(?:"(?P<quoted>(?:[^"]|"")*)"|(?P<bare>[A-Za-z_][A-Za-z0-9_$]*)))'
    r"|(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<other>\s+|.)",
    re.DOTALL,
)
"""A check's SQL, token by token: strings, names, numbers, and the rest."""
_JOINS: Final = frozenset({"and", "or"})
"""The words that join a check's comparisons (``BETWEEN``'s ``AND`` aside)."""
_FACTORS: Final = frozenset("*/%")
"""SQL operators whose operand is a factor, not an amount: refused beside one."""


def stored_scale(column: Column[Any]) -> int | None:
    """The places an amount column of ``column``'s name holds, if it is one.

    A revision declares the column as plain ``NUMERIC`` (``render_item``):
    its table in ``domainkit.db.metadata`` says whether it is an amount.
    """
    if isinstance(column.type, ExactDecimal):
        return column.type.scale
    from domainkit.db.orm import metadata  # orm imports types.

    table = column.table if isinstance(column.table, Table) else None
    known = None if table is None else metadata.tables.get(table.fullname)
    if known is None or column.name not in known.c:
        return None
    kind = known.c[column.name].type
    return kind.scale if isinstance(kind, ExactDecimal) else None


def default_in_minor_units(
    column: Column[Any], specification: str, compiler: DDLCompiler
) -> str:
    """``specification`` with a plain-number default of an amount in minor units.

    On SQLite the column holds minor units, so ``DEFAULT 1.50`` would read
    back as 0.015: it is written ``DEFAULT 150`` (DATA-15).

    Raises:
        CompileError: The default has more places than the column.
    """
    scale = stored_scale(column)
    default = column.server_default
    if not scale or not isinstance(default, DefaultClause):
        return specification
    given = default.arg if isinstance(default.arg, str) else None
    if isinstance(default.arg, TextClause):
        given = default.arg.text
    found = None if given is None else _DEFAULT_LITERAL.fullmatch(given)
    if found is None:
        return specification
    minor = Decimal(found[1]).scaleb(scale)
    if minor != minor.to_integral_value():
        raise CompileError(
            f"{column.name}'s default has more than {scale} decimal places."
        )
    written = compiler.get_column_default_string(column)
    for form in (f"DEFAULT ({written})", f"DEFAULT {written}"):
        if form in specification:
            return specification.replace(form, f"DEFAULT {int(minor)}", 1)
    return specification  # pragma: no cover - SQLite writes one of the two.


@compiles(CreateColumn, "sqlite")
def _create_column_on_sqlite(
    element: CreateColumn,
    compiler: DDLCompiler,
    **kwargs: Any,  # noqa: ANN401 - the compiler's options.
) -> str:
    """A column of ``CREATE TABLE``, its amount's default in minor units."""
    specification = cast("str", compiler.visit_create_column(element, **kwargs))  # type: ignore[no-untyped-call]
    return default_in_minor_units(element.element, specification, compiler)


@compiles(CheckConstraint, "sqlite")
def _check_on_sqlite(
    constraint: CheckConstraint,
    compiler: DDLCompiler,
    **kwargs: Any,  # noqa: ANN401 - the compiler's options.
) -> str:
    """A check written as SQL, comparing amounts in exact minor units.

    Each comparison of the check that names an amount column is counted in
    whole minor units of the largest scale it names: a column of a smaller
    scale is multiplied up (``fee * 10``), and each number is written in
    those units (``cap <= 100`` with a scale of 2 is ``cap <= 10000``), so
    ``CHECK (cap <= 100)`` caps the column at 100.00, as on PostgreSQL
    (DATA-15), and amounts are compared as integers, never as floats
    (SEC-16c-1). A comparison without an amount is left as it is. Checks
    built as expressions bind their values through the column's type
    already.

    Raises:
        CompileError: A comparison of an amount holds a number with more
            places than its unit, another column, or ``*``, ``/``, ``%``.
    """
    visit = getattr(compiler, f"visit_{constraint.__visit_name__}")
    written: str = visit(constraint, **kwargs)
    table = constraint.table if isinstance(constraint.parent, Table) else None
    if table is None and isinstance(constraint.parent, Column):
        table = constraint.parent.table
    if not isinstance(constraint.sqltext, TextClause) or not isinstance(table, Table):
        return written
    scales = {
        column.name.lower(): scale
        for column in table.columns
        if (scale := stored_scale(column)) is not None
    }
    if not scales:
        return written
    columns = {column.name.lower() for column in table.columns}
    inner = compiler.sql_compiler.process(
        constraint.sqltext, include_table=False, literal_binds=True
    )
    counted = "".join(
        _in_minor_units(comparison, scales, columns, constraint)
        for comparison in _comparisons(inner)
    )
    return written.replace(f"CHECK ({inner})", f"CHECK ({counted})", 1)


def _name(token: re.Match[str]) -> str | None:
    """The column a name token names, lowered; None for any other token."""
    if token["quoted"] is not None:
        return token["quoted"].replace('""', '"').lower()
    bare = token["bare"]
    return None if bare is None else bare.lower()


def _comparisons(sql: str) -> list[list[re.Match[str]]]:
    """``sql``'s tokens, cut before each ``AND`` and ``OR`` that joins comparisons.

    The ``AND`` of ``BETWEEN ... AND ...`` stays inside its comparison.
    """
    parts: list[list[re.Match[str]]] = [[]]
    between = False
    for token in _SQL_TOKEN.finditer(sql):
        word = token["bare"].lower() if token["bare"] and not token["quoted"] else None
        if word == "between":
            between = True
        elif word in _JOINS and not (between and word == "and"):
            parts.append([])
        elif word == "and":
            between = False
        parts[-1].append(token)
    return parts


def _in_minor_units(
    tokens: list[re.Match[str]],
    scales: Mapping[str, int],
    columns: Collection[str],
    constraint: CheckConstraint,
) -> str:
    """One comparison's SQL, in minor units if it names an amount.

    Raises:
        CompileError: See ``_check_on_sqlite``.
    """
    names = [
        name
        for token in tokens
        if (name := _name(token)) is not None and name in columns
    ]
    amounts = [scales[name] for name in names if name in scales]
    if not amounts:
        return "".join(token[0] for token in tokens)
    where = f"{constraint.name or 'A check'}"
    if any(name not in scales for name in names) or any(
        token["other"] in _FACTORS for token in tokens
    ):
        raise CompileError(
            f"{where} compares an amount with another column, or multiplies or "
            "divides one: on SQLite amounts are minor units, so write such a "
            "check as an expression of the columns, or keep it in Python."
        )
    unit = max(amounts)
    written: list[str] = []
    for token in tokens:
        name = _name(token)
        if name is not None and name in scales and scales[name] < unit:
            written.append(f"({token[0]} * {10 ** (unit - scales[name])})")
        elif token["number"] is not None:
            minor = Decimal(token["number"]).scaleb(unit)
            if minor != minor.to_integral_value():
                raise CompileError(
                    f"{where} holds {token['number']}, which has more than "
                    f"{unit} decimal places: no amount it compares has them."
                )
            written.append(str(int(minor)))
        else:
            written.append(token[0])
    return "".join(written)
