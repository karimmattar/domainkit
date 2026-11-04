"""Compile domain queries (``domainkit.ddd.query``) to SQLAlchemy expressions.

Field names resolve only to the mapped columns of the aggregate's class, and
dotted names only through its mapped relationships (``EXISTS``). Values are
always bound parameters, typed by their column, so an ``ExactDecimal`` stays
exact and a value object is stored through its column type.

The compiled SQL keeps the semantics the in-memory repository has:

- A condition on a nullable column is also false where the column is NULL,
  so ``NOT`` negates exactly (two-valued logic, as in Python).
- ``NULLS LAST`` ascending, ``NULLS FIRST`` descending, on every database.
- A money attribute (``MoneyColumns``) is its two columns: conditions on
  money come bound as conditions on ``total.amount`` and ``total.currency``
  (``domainkit.ddd.query``), which name them; ``isnull`` looks at the
  amount, and ordering sorts by currency, then amount.
- An encrypted column (``domainkit.db.encrypted``) holds ciphertext: a
  condition may ask whether it is None, and, with a blind index, whether
  it equals (or is in) values; anything else, and ordering by it, is
  refused.
- Text lookups never use the pattern characters of the text they are given:
  on PostgreSQL they become ``LIKE`` with ``%``, ``_`` and the escape
  character escaped; on SQLite, whose ``LIKE`` ignores ASCII case, they use
  ``instr`` and ``substr``. Case-insensitive ones compare ``lower()`` of both.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import (
    ColumnElement,
    String,
    and_,
    bindparam,
    func,
    literal,
    not_,
    or_,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import UnaryExpression
from sqlalchemy.sql.expression import type_coerce
from sqlalchemy.sql.schema import Column

from domainkit.db.encrypted import EncryptedType
from domainkit.db.encrypted import lookup as lookup_
from domainkit.db.money import money_columns
from domainkit.ddd.fields import MONEY_PARTS, InvalidQuery, UnknownField
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
    Within,
)

if TYPE_CHECKING:
    from domainkit.ddd.places import Point

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
    if isinstance(criteria, Within):
        return _within(criteria.relation, criteria.criteria, mapper, dialect)
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
        money = money_columns(mapper, item.field)
        columns = reversed(money) if money else [column_of(mapper, item.field)]
        for column in columns:  # Money: the currency, then the amount.
            if isinstance(column.type, EncryptedType):
                raise InvalidQuery(
                    f"{mapper.class_.__name__}.{item.field} is encrypted: its "
                    "ciphertexts do not sort as its values do."
                )
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
        from domainkit.utils.text import did_you_mean

        raise UnknownField(
            f"{mapper.class_.__name__}.{name} is not a mapped column"
            f"{did_you_mean(name, mapper.column_attrs.keys())}."
        )
    column = mapper.column_attrs[name].columns[0]
    if not isinstance(column, Column):
        raise InvalidQuery(f"{mapper.class_.__name__}.{name} is not a table column.")
    return column


def _condition(
    name: str, lookup: Lookup, value: object, mapper: Mapper[Any], dialect: str
) -> ColumnElement[bool]:
    """One condition; a dotted name becomes ``EXISTS`` through the relationship."""
    head, _, rest = name.partition(".")
    if (money := money_columns(mapper, head)) is not None:
        column = _money_column(money, rest)
    elif rest:
        return _through(
            mapper,
            head,
            lambda child: _condition(rest, lookup, value, child, dialect),
        )
    else:
        column = column_of(mapper, head)
    if isinstance(column.type, EncryptedType):
        return _encrypted(column, lookup, value)
    expression = _compare(column, lookup, value, dialect)
    total = lookup is Lookup.ISNULL or (lookup is Lookup.EXACT and value is None)
    if column.nullable and not total:
        return and_(column.is_not(None), expression)
    return expression


def _encrypted(
    column: Column[Any], lookup: Lookup, value: object
) -> ColumnElement[bool]:
    """A condition on an encrypted column: None or not, or equality by blind index.

    Raises:
        InvalidQuery: Any other lookup, or equality without a blind index.
    """
    if lookup is Lookup.ISNULL:
        return column.is_(None) if value else column.is_not(None)
    if lookup is Lookup.EXACT and value is None:
        return column.is_(None)
    if lookup in {Lookup.EXACT, Lookup.IN}:
        values = cast("tuple[object, ...]", value) if lookup is Lookup.IN else (value,)
        return lookup_(column, values)
    raise InvalidQuery(
        f"{column.table.name}.{column.name} is encrypted: a query can ask only "
        "whether it is None, or, with a blind index, whether it equals a value."
    )


def _within(
    name: str, criteria: Criteria, mapper: Mapper[Any], dialect: str
) -> ColumnElement[bool]:
    """``criteria`` on one child entity at the relation ``name``: one ``EXISTS``."""
    head, _, rest = name.partition(".")
    if rest:
        return _through(
            mapper, head, lambda child: _within(rest, criteria, child, dialect)
        )
    return _through(
        mapper,
        head,
        lambda child: compile_criteria(criteria, child.class_, dialect),
    )


def _through(
    mapper: Mapper[Any],
    name: str,
    inner: Callable[[Mapper[Any]], ColumnElement[bool]],
) -> ColumnElement[bool]:
    """``EXISTS`` a row related through ``name`` that meets ``inner`` of its mapper.

    Raises:
        UnknownField: ``name`` is not a mapped relationship.
    """
    if name not in mapper.relationships:
        from domainkit.utils.text import did_you_mean

        raise UnknownField(
            f"{mapper.class_.__name__}.{name} is not a mapped relationship"
            f"{did_you_mean(name, mapper.relationships.keys())}."
        )
    relationship = mapper.relationships[name]
    condition = inner(relationship.mapper)
    attribute = getattr(mapper.class_, name)
    exists = (
        attribute.any(condition) if relationship.uselist else attribute.has(condition)
    )
    return cast("ColumnElement[bool]", exists)


def _money_column(money: tuple[Column[Any], ...], part: str) -> Column[Any]:
    """The column of a money attribute's part: the amount, unless the currency.

    Bound criteria name only ``amount`` and ``currency`` (``resolve``), or
    no part for ``isnull``.
    """
    return money[1] if part == MONEY_PARTS[1] else money[0]


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
        case Lookup.SEARCH:
            return _search(column, str(value), dialect)
        case Lookup.WITHIN:
            from domainkit.db.places import within

            point, meters = cast("tuple[Point, float]", value)
            return within(column, point, meters, dialect)
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
        case Lookup.REGEX | Lookup.IREGEX:
            text: ColumnElement[str] = type_coerce(column, String())
            pattern, flags = str(value), None
            if lookup is Lookup.IREGEX:
                # SQLite's REGEXP is Python's re.search, and SQLAlchemy drops
                # flags there: ignore case with an inline flag instead.
                if dialect == "sqlite":
                    pattern = f"(?i){pattern}"
                else:
                    flags = "i"
            return text.regexp_match(literal(pattern, String()), flags=flags)
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


def _search(column: Column[Any], words: str, dialect: str) -> ColumnElement[bool]:
    """Every word: PostgreSQL's full-text search, else each word's characters."""
    if dialect == "postgresql":
        document = func.to_tsvector("simple", type_coerce(column, String()))
        query = func.websearch_to_tsquery("simple", literal(words, String()))
        return cast("ColumnElement[bool]", document.op("@@")(query))
    parts = [_text(column, Lookup.ICONTAINS, word, dialect) for word in words.split()]
    return and_(*parts)


def contains_text(column: Column[Any], piece: str, dialect: str) -> ColumnElement[bool]:
    """Whether ``column`` holds ``piece``, as characters, in ``dialect``'s SQL."""
    return _text(column, Lookup.CONTAINS, piece, dialect)


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


@dataclass(frozen=True, slots=True)
class Slot:
    """Where a value was in criteria made into a shape (``shape_of``).

    ``kind`` is the value's type (an ``in``'s, its values' types): the
    parameter's type comes from the column and it, as a literal's would.
    """

    index: int
    kind: object


_BOUND_LOOKUPS: Final = frozenset(
    {Lookup.EXACT, Lookup.IN, Lookup.GT, Lookup.GTE, Lookup.LT, Lookup.LTE}
)
"""Lookups whose value a bound parameter carries just as the value itself."""


def shape_of(
    criteria: Criteria, mapper: Mapper[Any]
) -> tuple[Criteria, list[object]] | None:
    """``criteria`` with each value a ``Slot``, and the values in slot order.

    Two queries of one shape compile to one statement: made once, with
    bound parameters, it keeps its cache key, which SQLAlchemy would walk
    a new statement for on each execution. Only what a bound
    parameter means exactly as its value does takes a slot: ``exact``,
    ``in``, and the comparisons, of a value that is not None nor a bool, on
    a column of the model's own table that is not money nor encrypted,
    joined by ``&``, ``|``, and ``~``; ``isnull`` keeps its value in the
    shape. None for anything else, which compiles as it always did.
    """
    values: list[object] = []
    shaped = _shaped(criteria, mapper, values)
    return None if shaped is None else (shaped, values)


def _shaped(
    criteria: Criteria, mapper: Mapper[Any], values: list[object]
) -> Criteria | None:
    """``shape_of``'s work on one node; None if it takes no slot."""
    if isinstance(criteria, (And, Or)):
        parts = [_shaped(part, mapper, values) for part in criteria.parts]
        if any(part is None for part in parts):
            return None
        return type(criteria)(tuple(cast("list[Criteria]", parts)))
    if isinstance(criteria, Not):
        part = _shaped(criteria.part, mapper, values)
        return None if part is None else Not(part)
    if not isinstance(criteria, Condition) or not _plain(criteria.field, mapper):
        return None
    value, lookup = criteria.value, criteria.lookup
    if lookup is Lookup.ISNULL and isinstance(value, bool):
        return criteria
    if lookup not in _BOUND_LOOKUPS or value is None or isinstance(value, bool):
        return None
    kind: object = type(value)
    if lookup is Lookup.IN:
        if not isinstance(value, tuple) or not value or None in value:
            return None
        kind = frozenset(map(type, value))
        value = list(value)
    values.append(value)
    slot = Slot(len(values) - 1, kind)
    return Condition(criteria.field, lookup, slot, criteria.path)


def _plain(field: str, mapper: Mapper[Any]) -> bool:
    """Whether ``field`` is a column of the table that is not encrypted.

    A money attribute is no mapped column: its parts are its two columns,
    which bound criteria name with a dot.
    """
    if "." in field or field not in mapper.column_attrs:
        return False
    column = mapper.column_attrs[field].columns[0]
    return isinstance(column, Column) and not isinstance(column.type, EncryptedType)


_COMPARED: Final = {
    Lookup.EXACT: operators.eq,
    Lookup.IN: operators.in_op,
    Lookup.GT: operators.gt,
    Lookup.GTE: operators.ge,
    Lookup.LT: operators.lt,
    Lookup.LTE: operators.le,
}
"""The operator SQLAlchemy types a literal by, for each lookup a slot takes."""


def with_parameters(
    criteria: Criteria, mapper: Mapper[Any], values: list[object]
) -> Criteria:
    """A shape's criteria with each ``Slot`` a bound parameter, ``p<index>``.

    Each parameter has the type SQLAlchemy gives the literal it stands for
    (the column's ``coerce_compared_value`` of the operator and ``values``'
    value), so it binds as that literal would.
    """
    if isinstance(criteria, (And, Or)):
        parts = (with_parameters(part, mapper, values) for part in criteria.parts)
        return type(criteria)(tuple(parts))
    if isinstance(criteria, Not):
        return Not(with_parameters(criteria.part, mapper, values))
    assert isinstance(criteria, Condition)  # noqa: S101 - shape_of made only these.
    slot, lookup = criteria.value, criteria.lookup
    if not isinstance(slot, Slot):
        return criteria
    column = column_of(mapper, criteria.field)
    value = values[slot.index]
    type_ = column.type.coerce_compared_value(_COMPARED[lookup], value)
    parameter: object = bindparam(
        f"p{slot.index}", type_=type_, expanding=lookup is Lookup.IN
    )
    return Condition(criteria.field, lookup, parameter, criteria.path)
