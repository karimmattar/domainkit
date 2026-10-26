"""Compile domain queries (``domainkit.ddd.query``) to SQLAlchemy expressions.

Field names resolve only to the mapped columns of the aggregate's class, and
dotted names only through its mapped relationships (``EXISTS``). Values are
always bound parameters, typed by their column, so an ``ExactDecimal`` stays
exact and a value object is stored through its column type.

The compiled SQL keeps the semantics the in-memory repository has:

- A condition on a nullable column is also false where the column is NULL,
  so ``NOT`` negates exactly (two-valued logic, as in Python).
- ``NULLS LAST`` ascending, ``NULLS FIRST`` descending, on every database.
- Text lookups never use the pattern characters of the text they are given:
  on PostgreSQL they become ``LIKE`` with ``%``, ``_`` and the escape
  character escaped; on SQLite, whose ``LIKE`` ignores ASCII case, they use
  ``instr`` and ``substr``. Case-insensitive ones compare ``lower()`` of both.
"""

from collections.abc import Callable
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, String, and_, func, literal, not_, or_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper
from sqlalchemy.sql.elements import UnaryExpression
from sqlalchemy.sql.expression import type_coerce
from sqlalchemy.sql.schema import Column

from domainkit.ddd.fields import InvalidQuery, UnknownField
from domainkit.ddd.query import (
    CASE_FOLDED,
    ORDERING_LOOKUPS,
    And,
    Condition,
    Criteria,
    Lookup,
    Not,
    Or,
    Ordering,
)

__all__ = [
    "register_compiler",
]

ESCAPE: Final = "/"
"""The escape character of ``LIKE`` patterns."""
_PATTERNS: Final = {
    Lookup.CONTAINS: "%{}%",
    Lookup.ICONTAINS: "%{}%",
    Lookup.STARTSWITH: "{}%",
    Lookup.ISTARTSWITH: "{}%",
    Lookup.ENDSWITH: "%{}",
    Lookup.IENDSWITH: "%{}",
}


type Compiler = Callable[[Any, type, str], ColumnElement[bool]]
"""Compiles one kind of criteria: ``(criteria, model, dialect)`` to SQL."""
_COMPILERS: Final[dict[type[Criteria], Compiler]] = {}


def register_compiler(kind: type[Criteria], compiler: Compiler) -> None:
    """Compile criteria of ``kind`` (a ``Criteria`` of your own) with ``compiler``.

    It takes the criteria, bound to the model, the model, and the dialect's
    name, and returns a condition on the model's table, such as an
    ``EXISTS`` on another table. Values must be bound parameters.
    """
    _COMPILERS[kind] = compiler


def mapper_of(model: type) -> Mapper[Any]:
    """The mapper of ``model``.

    Raises:
        InvalidQuery: ``model`` is not mapped (``map_imperatively``).
    """
    mapper: object = sa_inspect(model, raiseerr=False)
    if not isinstance(mapper, Mapper):
        raise InvalidQuery(f"{model.__name__} is not mapped to a table.")
    return mapper


def compile_criteria(
    criteria: Criteria, model: type, dialect: str
) -> ColumnElement[bool]:
    """``criteria`` as a SQL condition on ``model``'s table, for ``dialect``.

    Raises:
        UnknownField: A name is not a mapped column or relationship.
        InvalidQuery: A dotted name passes through something else.
    """
    mapper = mapper_of(model)
    if isinstance(criteria, Condition):
        return _condition(
            criteria.field, criteria.lookup, criteria.value, mapper, dialect
        )
    if isinstance(criteria, And):
        return and_(*(compile_criteria(p, model, dialect) for p in criteria.parts))
    if isinstance(criteria, Or):
        return or_(*(compile_criteria(p, model, dialect) for p in criteria.parts))
    if isinstance(criteria, Not):
        return not_(compile_criteria(criteria.part, model, dialect))
    compiler = _COMPILERS.get(type(criteria))
    if compiler is not None:
        return compiler(criteria, model, dialect)
    raise TypeError(f"Cannot compile {type(criteria).__name__}.")


def compile_ordering(
    ordering: tuple[Ordering, ...], model: type
) -> list[UnaryExpression[Any]]:
    """``ORDER BY`` terms for ``ordering``; NULLs last ascending, first descending."""
    mapper = mapper_of(model)
    terms = []
    for item in ordering:
        column = column_of(mapper, item.field)
        term = column.desc() if item.descending else column.asc()
        if column.nullable:
            term = term.nulls_first() if item.descending else term.nulls_last()
        terms.append(term)
    return terms


def column_of(mapper: Mapper[Any], name: str) -> Column[Any]:
    """The table column of the mapped attribute ``name``.

    Raises:
        UnknownField: ``name`` is not a mapped column.
    """
    if name not in mapper.column_attrs:
        raise UnknownField(f"{mapper.class_.__name__}.{name} is not a mapped column.")
    column = mapper.column_attrs[name].columns[0]
    if not isinstance(column, Column):
        raise InvalidQuery(f"{mapper.class_.__name__}.{name} is not a table column.")
    return column


def _condition(
    name: str, lookup: Lookup, value: object, mapper: Mapper[Any], dialect: str
) -> ColumnElement[bool]:
    """One condition; a dotted name becomes ``EXISTS`` through the relationship."""
    head, _, rest = name.partition(".")
    if rest:
        if head not in mapper.relationships:
            raise UnknownField(
                f"{mapper.class_.__name__}.{head} is not a mapped relationship."
            )
        relationship = mapper.relationships[head]
        inner = _condition(rest, lookup, value, relationship.mapper, dialect)
        attribute = getattr(mapper.class_, head)
        exists = attribute.any(inner) if relationship.uselist else attribute.has(inner)
        return cast("ColumnElement[bool]", exists)
    column = column_of(mapper, head)
    expression = _compare(column, lookup, value, dialect)
    total = lookup is Lookup.ISNULL or (lookup is Lookup.EXACT and value is None)
    if column.nullable and not total:
        return and_(column.is_not(None), expression)
    return expression


def _compare(
    column: Column[Any],
    lookup: Lookup,
    value: object,
    dialect: str,
) -> ColumnElement[bool]:
    """The comparison itself, on a present value.

    ``True`` and ``False`` are bound like other values: SQLAlchemy would
    refuse ``>`` with them as literals.
    """
    if lookup in ORDERING_LOOKUPS:
        value = _bound_bools(value, column)
    match lookup:
        case Lookup.EXACT:
            return column.is_(None) if value is None else column == value
        case Lookup.ISNULL:
            return column.is_(None) if value else column.is_not(None)
        case Lookup.IN:
            return column.in_(cast("tuple[object, ...]", value))
        case Lookup.RANGE:
            low, high = cast("tuple[object, object]", value)
            return column.between(low, high)
        case Lookup.GT:
            return column > value
        case Lookup.GTE:
            return column >= value
        case Lookup.LT:
            return column < value
        case Lookup.LTE:
            return column <= value
        case _:
            return _text(column, lookup, str(value), dialect)


def _bound_bools(value: object, column: Column[Any]) -> object:
    """``value`` (or a range's two), with booleans as bound parameters."""
    if isinstance(value, tuple):
        return tuple(_bound_bools(each, column) for each in value)
    return literal(value, column.type) if isinstance(value, bool) else value


def _text(
    column: Column[Any], lookup: Lookup, value: str, dialect: str
) -> ColumnElement[bool]:
    """A text lookup: the value is matched as characters, never as a pattern."""
    folded = lookup in CASE_FOLDED
    text: ColumnElement[str] = type_coerce(column, String())
    piece: ColumnElement[str] = literal(value, String())
    if folded:
        text, piece = func.lower(text), func.lower(piece)
    if lookup is Lookup.IEXACT:
        return text == piece
    if dialect == "sqlite":
        return _sqlite_text(text, piece, lookup)
    escaped = (
        value.replace(ESCAPE, ESCAPE * 2)
        .replace("%", f"{ESCAPE}%")
        .replace("_", f"{ESCAPE}_")
    )
    pattern: ColumnElement[str] = literal(_PATTERNS[lookup].format(escaped), String())
    return text.like(func.lower(pattern) if folded else pattern, escape=ESCAPE)


def _sqlite_text(
    text: ColumnElement[str], piece: ColumnElement[str], lookup: Lookup
) -> ColumnElement[bool]:
    """Text lookups on SQLite, whose ``LIKE`` would ignore ASCII case."""
    if lookup in {Lookup.CONTAINS, Lookup.ICONTAINS}:
        return func.instr(text, piece) > 0
    if lookup in {Lookup.STARTSWITH, Lookup.ISTARTSWITH}:
        return func.substr(text, 1, func.length(piece)) == piece
    start = func.length(text) - func.length(piece) + 1
    return func.substr(text, start) == piece
