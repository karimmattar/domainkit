"""Queries over aggregates: criteria, ordering, and specifications.

A repository answers queries built from field expressions::

    from domainkit.ddd import F, Selection

    late = (F("status") == "open") & (F("due") < today)
    invoices = await uow.invoices.filter(Selection(late, ordering=["-due"], limit=20))
    count = await uow.invoices.count(late)

Conditions compose with ``&``, ``|``, and ``~``. A dotted name reaches into
child entities: ``F("lines.sku") == "A-1"`` holds when some line has that
sku. ``Specification`` names a rule, so the domain can test an aggregate
against it and a repository can find the aggregates that meet it.

Every repository evaluates a query the same way; the in-memory one in
Python, the SQL one in the database:

- A condition on a missing value (None, or no child entity) is false, and
  ``~`` negates exactly: ``~(F("stock") > 5)`` includes rows without stock.
- ``F("x") == None`` is ``F("x").isnull()``.
- None sorts after every value in ascending order, and first in descending.
- Results come in the query's ordering, then by id, so the order is total.
- Text lookups compare characters; the ``i`` lookups fold case with
  ``str.lower`` (``lower()`` in SQL, which folds only ASCII on SQLite).
- Values are checked against the field's type hint: an amount compares
  exactly, a datetime must be aware, an enum field takes a member or its
  value. Single-field value objects compare by what they wrap.

``Selection`` says which aggregates to load. It was called ``Query`` before
0.9.2, and that name still works here; ``domainkit.ddd.Query`` is the base of
CQRS query messages, a different thing.
"""

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, NoReturn, Self, cast

from domainkit.ddd.fields import (
    FieldInfo,
    InvalidQuery,
    UnknownField,
    fields_of,
    primitive,
)

__all__ = [
    "Criteria",
    "F",
    "Ordering",
    "Query",
    "RowLock",
    "Selection",
    "Specification",
]

ID: Final = "id"
"""The field every ordering ends with, so the order is total."""


class Lookup(StrEnum):
    """How a condition compares a field with its value (Django's lookups)."""

    EXACT = "exact"
    IEXACT = "iexact"
    CONTAINS = "contains"
    ICONTAINS = "icontains"
    STARTSWITH = "startswith"
    ISTARTSWITH = "istartswith"
    ENDSWITH = "endswith"
    IENDSWITH = "iendswith"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    RANGE = "range"
    ISNULL = "isnull"


TEXT_LOOKUPS: Final = frozenset(
    {
        Lookup.IEXACT,
        Lookup.CONTAINS,
        Lookup.ICONTAINS,
        Lookup.STARTSWITH,
        Lookup.ISTARTSWITH,
        Lookup.ENDSWITH,
        Lookup.IENDSWITH,
    }
)
"""Lookups that take a piece of text and need a text field."""
CASE_FOLDED: Final = frozenset(
    {Lookup.IEXACT, Lookup.ICONTAINS, Lookup.ISTARTSWITH, Lookup.IENDSWITH}
)
"""Text lookups that ignore case."""
ORDERING_LOOKUPS: Final = frozenset(
    {Lookup.GT, Lookup.GTE, Lookup.LT, Lookup.LTE, Lookup.RANGE}
)
"""Lookups that need a field whose values sort."""

_TEXT_TESTS: Final[dict[Lookup, Callable[[str, str], bool]]] = {
    Lookup.IEXACT: str.__eq__,
    Lookup.CONTAINS: str.__contains__,
    Lookup.ICONTAINS: str.__contains__,
    Lookup.STARTSWITH: str.startswith,
    Lookup.ISTARTSWITH: str.startswith,
    Lookup.ENDSWITH: str.endswith,
    Lookup.IENDSWITH: str.endswith,
}
_COMPARISONS: Final[dict[Lookup, Callable[[Any, Any], bool]]] = {
    Lookup.GT: lambda left, right: left > right,
    Lookup.GTE: lambda left, right: left >= right,
    Lookup.LT: lambda left, right: left < right,
    Lookup.LTE: lambda left, right: left <= right,
}


class Criteria(ABC):
    """A condition on aggregates; combine them with ``&``, ``|``, and ``~``.

    Use ``&`` and ``|``, never ``and`` and ``or``: criteria refuse to be
    true or false in Python, so a mistaken ``and`` fails loudly.
    """

    __slots__ = ()

    def __and__(self, other: "Criteria | Specification[Any]") -> "Criteria":
        """Both hold."""
        return And.of(self, as_criteria(other))

    def __or__(self, other: "Criteria | Specification[Any]") -> "Criteria":
        """Either holds."""
        return Or.of(self, as_criteria(other))

    def __invert__(self) -> "Criteria":
        """This does not hold."""
        return Not(self)

    def __bool__(self) -> NoReturn:
        """Refuse: ``and``, ``or``, and ``if`` would drop conditions silently."""
        raise TypeError("Combine criteria with &, | and ~, not with and, or, not.")

    @abstractmethod
    def bind(self, model: type) -> "Criteria":
        """These criteria checked against ``model``'s fields, values coerced.

        Raises:
            UnknownField: A field is not one of the model's.
            InvalidQuery: A lookup or value does not fit its field.
        """

    @abstractmethod
    def evaluate(self, candidate: object) -> bool:
        """Whether ``candidate`` meets these criteria, once bound to its class."""

    @abstractmethod
    def conditions(self) -> Iterator["Condition"]:
        """Every condition, in order."""

    def matches(self, candidate: object) -> bool:
        """Whether ``candidate`` meets these criteria."""
        return self.bind(type(candidate)).evaluate(candidate)


@dataclass(frozen=True, slots=True)
class Condition(Criteria):
    """One field compared with one value: ``F("amount") >= 10``."""

    field: str
    """The field's name; a dotted name reaches into child entities."""
    lookup: Lookup
    value: object = None
    path: tuple[FieldInfo, ...] = dataclasses.field(
        default=(), compare=False, repr=False
    )
    """The fields the name resolved to, once bound."""

    def bind(self, model: type) -> "Condition":
        """Resolve the field and check, and coerce, the value."""
        path = resolve(model, self.field)
        value = _bound_value(path[-1], self.lookup, self.value)
        return Condition(self.field, self.lookup, value, path)

    def evaluate(self, candidate: object) -> bool:
        """Follow the path from ``candidate``, then compare."""
        if not self.path:
            raise InvalidQuery("Bind the criteria to a class before evaluating them.")
        return _holds(candidate, self.path, self.lookup, self.value)

    def conditions(self) -> Iterator["Condition"]:
        """This condition."""
        yield self


@dataclass(frozen=True, slots=True)
class And(Criteria):
    """Every part holds."""

    parts: tuple[Criteria, ...]

    @classmethod
    def of(cls, *parts: Criteria) -> Criteria:
        """Join ``parts``, flattening nested conjunctions."""
        flat = tuple(p for part in parts for p in _parts(part, cls))
        return flat[0] if len(flat) == 1 else cls(flat)

    def bind(self, model: type) -> "And":
        """Bind every part."""
        return And(tuple(part.bind(model) for part in self.parts))

    def evaluate(self, candidate: object) -> bool:
        """All parts hold."""
        return all(part.evaluate(candidate) for part in self.parts)

    def conditions(self) -> Iterator[Condition]:
        """The parts' conditions."""
        for part in self.parts:
            yield from part.conditions()


@dataclass(frozen=True, slots=True)
class Or(Criteria):
    """At least one part holds."""

    parts: tuple[Criteria, ...]

    @classmethod
    def of(cls, *parts: Criteria) -> Criteria:
        """Join ``parts``, flattening nested disjunctions."""
        flat = tuple(p for part in parts for p in _parts(part, cls))
        return flat[0] if len(flat) == 1 else cls(flat)

    def bind(self, model: type) -> "Or":
        """Bind every part."""
        return Or(tuple(part.bind(model) for part in self.parts))

    def evaluate(self, candidate: object) -> bool:
        """Some part holds."""
        return any(part.evaluate(candidate) for part in self.parts)

    def conditions(self) -> Iterator[Condition]:
        """The parts' conditions."""
        for part in self.parts:
            yield from part.conditions()


@dataclass(frozen=True, slots=True)
class Not(Criteria):
    """The part does not hold."""

    part: Criteria

    def __invert__(self) -> Criteria:
        """Two negations cancel."""
        return self.part

    def bind(self, model: type) -> "Not":
        """Bind the part."""
        return Not(self.part.bind(model))

    def evaluate(self, candidate: object) -> bool:
        """The part does not hold."""
        return not self.part.evaluate(candidate)

    def conditions(self) -> Iterator[Condition]:
        """The part's conditions."""
        return self.part.conditions()


def _parts(criteria: Criteria, kind: type[And | Or]) -> tuple[Criteria, ...]:
    """The parts of ``criteria`` if it is a ``kind`` itself, else just it."""
    return criteria.parts if isinstance(criteria, kind) else (criteria,)


class F:
    """A field of an aggregate, by name, to build conditions and orderings.

    ``F("status") == "open"``, ``F("amount") >= 10``, ``F("name").icontains("x")``,
    ``F("created_at").desc()``. A dotted name reaches into child entities.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Name the field; each part of a dotted name must be an identifier."""
        if not name or not all(part.isidentifier() for part in name.split(".")):
            raise InvalidQuery(f"{name!r} is not a field name.")
        self.name = name

    def __repr__(self) -> str:
        """``F('name')``."""
        return f"F({self.name!r})"

    def __eq__(self, value: object) -> Condition:  # type: ignore[override]
        """Equal to ``value``; ``== None`` is ``isnull()``."""
        return Condition(self.name, Lookup.EXACT, value)

    def __ne__(self, value: object) -> Criteria:  # type: ignore[override]
        """Not equal to ``value``, missing values included."""
        return Not(Condition(self.name, Lookup.EXACT, value))

    def __lt__(self, value: object) -> Condition:
        """Less than ``value``."""
        return Condition(self.name, Lookup.LT, value)

    def __le__(self, value: object) -> Condition:
        """At most ``value``."""
        return Condition(self.name, Lookup.LTE, value)

    def __gt__(self, value: object) -> Condition:
        """Greater than ``value``."""
        return Condition(self.name, Lookup.GT, value)

    def __ge__(self, value: object) -> Condition:
        """At least ``value``."""
        return Condition(self.name, Lookup.GTE, value)

    __hash__ = None  # type: ignore[assignment]

    def in_(self, values: Iterable[object]) -> Condition:
        """Equal to one of ``values``."""
        if isinstance(values, str | bytes):
            raise InvalidQuery("in_ takes a collection of values, not one string.")
        return Condition(self.name, Lookup.IN, tuple(values))

    def range(self, low: object, high: object) -> Condition:
        """Between ``low`` and ``high``, both included (SQL's ``BETWEEN``)."""
        return Condition(self.name, Lookup.RANGE, (low, high))

    between = range

    def isnull(self, value: bool = True) -> Condition:
        """Missing (or, with ``False``, present)."""
        return Condition(self.name, Lookup.ISNULL, value)

    def iexact(self, text: str) -> Condition:
        """Equal to ``text``, ignoring case."""
        return Condition(self.name, Lookup.IEXACT, text)

    def contains(self, text: str) -> Condition:
        """Contains ``text``."""
        return Condition(self.name, Lookup.CONTAINS, text)

    def icontains(self, text: str) -> Condition:
        """Contains ``text``, ignoring case."""
        return Condition(self.name, Lookup.ICONTAINS, text)

    def startswith(self, text: str) -> Condition:
        """Starts with ``text``."""
        return Condition(self.name, Lookup.STARTSWITH, text)

    def istartswith(self, text: str) -> Condition:
        """Starts with ``text``, ignoring case."""
        return Condition(self.name, Lookup.ISTARTSWITH, text)

    def endswith(self, text: str) -> Condition:
        """Ends with ``text``."""
        return Condition(self.name, Lookup.ENDSWITH, text)

    def iendswith(self, text: str) -> Condition:
        """Ends with ``text``, ignoring case."""
        return Condition(self.name, Lookup.IENDSWITH, text)

    def lookup(self, lookup: Lookup | str, value: object) -> Condition:
        """The condition ``lookup`` names, as in ``amount__gte``."""
        return Condition(self.name, Lookup(lookup), value)

    def asc(self) -> "Ordering":
        """Order by this field, smallest first (missing values last)."""
        return Ordering(self.name)

    def desc(self) -> "Ordering":
        """Order by this field, largest first (missing values first)."""
        return Ordering(self.name, descending=True)


@dataclass(frozen=True, slots=True)
class Ordering:
    """Sort by one field of the aggregate itself."""

    field: str
    descending: bool = False

    @classmethod
    def parse(cls, text: str) -> Self:
        """``"name"`` or ``"-name"`` (descending), as Django writes orderings."""
        descending = text.startswith("-")
        name = text.removeprefix("-")
        if not name.isidentifier():
            raise InvalidQuery(f"{text!r} is not an ordering.")
        return cls(name, descending)

    def __str__(self) -> str:
        """``name`` or ``-name``."""
        return f"-{self.field}" if self.descending else self.field

    def bind(self, model: type) -> Self:
        """Check that ``model`` has the field, and that its values sort.

        Raises:
            UnknownField: The model has no such field.
            InvalidQuery: The field is a relation, or its values do not sort.
        """
        (info,) = resolve(model, self.field)
        if not info.orderable:
            raise InvalidQuery(f"{model.__name__}.{self.field} cannot be ordered by.")
        return self


type OrderingLike = Ordering | F | str
"""An ordering, a field (ascending), or Django's ``"-name"``."""
type CriteriaLike = Criteria | Specification[Any]


@dataclass(frozen=True, slots=True)
class RowLock:
    """Lock the rows a query returns until the transaction ends (``FOR UPDATE``).

    ``nowait`` raises ``LockNotAvailable`` instead of waiting for another
    transaction's lock; ``skip_locked`` leaves locked rows out instead.
    """

    nowait: bool = False
    skip_locked: bool = False

    def __post_init__(self) -> None:
        """Refuse both at once, as PostgreSQL does."""
        if self.nowait and self.skip_locked:
            raise ValueError("A row lock takes nowait or skip_locked, not both.")


@dataclass(frozen=True, slots=True, init=False)
class Selection:
    """Which aggregates to load, in which order, and how many.

    ``after`` continues past a position: the values of the ordering's fields,
    and the id last, of the last aggregate of the previous page. ``offset``
    skips aggregates instead; a query takes one or the other.
    """

    criteria: Criteria | None
    ordering: tuple[Ordering, ...]
    limit: int | None
    offset: int
    after: tuple[object, ...] | None
    lock: RowLock | None
    include_deleted: bool
    """Include soft-deleted aggregates (see ``SoftDeletable``)."""

    def __init__(
        self,
        criteria: CriteriaLike | None = None,
        *,
        ordering: Iterable[OrderingLike] = (),
        limit: int | None = None,
        offset: int = 0,
        after: Sequence[object] | None = None,
        lock: RowLock | None = None,
        include_deleted: bool = False,
    ) -> None:
        """Describe the query; the ordering is normalized, the bounds checked."""
        if limit is not None and limit < 0:
            raise ValueError(f"A query's limit cannot be negative, not {limit}.")
        if offset < 0:
            raise ValueError(f"A query's offset cannot be negative, not {offset}.")
        if offset and after is not None:
            raise ValueError("A query continues after a position or skips, not both.")
        assign = object.__setattr__
        assign(self, "criteria", None if criteria is None else as_criteria(criteria))
        assign(self, "ordering", tuple(as_ordering(item) for item in ordering))
        assign(self, "limit", limit)
        assign(self, "offset", offset)
        assign(self, "after", None if after is None else tuple(after))
        assign(self, "lock", lock)
        assign(self, "include_deleted", include_deleted)

    def where(self, *criteria: CriteriaLike) -> "Selection":
        """This query, with ``criteria`` also required."""
        if not criteria:
            return self
        parts = [] if self.criteria is None else [self.criteria]
        return self.replace(criteria=And.of(*parts, *map(as_criteria, criteria)))

    def replace(self, **changes: Any) -> "Selection":  # noqa: ANN401 - any field.
        """A copy with ``changes`` (the constructor's keywords)."""
        fields = {
            "criteria": self.criteria,
            "ordering": self.ordering,
            "limit": self.limit,
            "offset": self.offset,
            "after": self.after,
            "lock": self.lock,
            "include_deleted": self.include_deleted,
        }
        return Selection(**{**fields, **changes})


Query = Selection
"""``Selection``'s name before 0.9.2; 0.10 deprecates it, see ``docs/versioning.md``."""


class Specification[T](ABC):
    """A named business rule about aggregates, for Python and for queries.

    Subclasses state the rule as criteria; the domain asks
    ``is_satisfied_by(invoice)``, a repository ``filter(spec)``::

        class Overdue(Specification[Invoice]):
            def __init__(self, today: date) -> None:
                self.today = today

            def to_criteria(self) -> Criteria:
                return (F("status") == Status.OPEN) & (F("due") < self.today)

    Specifications compose with ``&``, ``|``, and ``~``. A subclass that
    overrides ``is_satisfied_by`` must keep it in step with its criteria.
    """

    @abstractmethod
    def to_criteria(self) -> Criteria:
        """The rule as criteria a repository can evaluate."""

    def is_satisfied_by(self, candidate: T) -> bool:
        """Whether ``candidate`` meets the rule."""
        return self.to_criteria().matches(candidate)

    def __and__(self, other: "Specification[T] | Criteria") -> "Specification[T]":
        """Both rules hold."""
        return _AllOf((self, as_specification(other)))

    def __or__(self, other: "Specification[T] | Criteria") -> "Specification[T]":
        """Either rule holds."""
        return _AnyOf((self, as_specification(other)))

    def __invert__(self) -> "Specification[T]":
        """The rule does not hold."""
        return _NoneOf(self)

    def __repr__(self) -> str:
        """The rule's name: its class."""
        return type(self).__name__


class _Matching[T](Specification[T]):
    """Criteria used as a specification."""

    def __init__(self, criteria: Criteria) -> None:
        """Wrap ``criteria``."""
        self.criteria = criteria

    def to_criteria(self) -> Criteria:
        """The wrapped criteria."""
        return self.criteria

    def __repr__(self) -> str:
        """The criteria."""
        return repr(self.criteria)


class _AllOf[T](Specification[T]):
    """Every one of several rules holds."""

    def __init__(self, parts: tuple[Specification[T], ...]) -> None:
        """Combine ``parts``."""
        self.parts = parts

    def to_criteria(self) -> Criteria:
        """The parts' criteria, joined."""
        return And.of(*(part.to_criteria() for part in self.parts))

    def is_satisfied_by(self, candidate: T) -> bool:
        """Every part is satisfied."""
        return all(part.is_satisfied_by(candidate) for part in self.parts)

    def __repr__(self) -> str:
        """``A & B``."""
        return "(" + " & ".join(map(repr, self.parts)) + ")"


class _AnyOf[T](Specification[T]):
    """At least one of several rules holds."""

    def __init__(self, parts: tuple[Specification[T], ...]) -> None:
        """Combine ``parts``."""
        self.parts = parts

    def to_criteria(self) -> Criteria:
        """The parts' criteria, as alternatives."""
        return Or.of(*(part.to_criteria() for part in self.parts))

    def is_satisfied_by(self, candidate: T) -> bool:
        """Some part is satisfied."""
        return any(part.is_satisfied_by(candidate) for part in self.parts)

    def __repr__(self) -> str:
        """``A | B``."""
        return "(" + " | ".join(map(repr, self.parts)) + ")"


class _NoneOf[T](Specification[T]):
    """A rule does not hold."""

    def __init__(self, part: Specification[T]) -> None:
        """Negate ``part``."""
        self.part = part

    def to_criteria(self) -> Criteria:
        """The part's criteria, negated."""
        return Not(self.part.to_criteria())

    def is_satisfied_by(self, candidate: T) -> bool:
        """The part is not satisfied."""
        return not self.part.is_satisfied_by(candidate)

    def __repr__(self) -> str:
        """``~A``."""
        return f"~{self.part!r}"


def as_criteria(value: CriteriaLike) -> Criteria:
    """Criteria, or a specification's criteria."""
    if isinstance(value, Specification):
        return value.to_criteria()
    if isinstance(value, Criteria):
        return value
    raise TypeError(
        f"Expected criteria or a specification, not {type(value).__name__}."
    )


def as_specification[T](value: Specification[T] | Criteria) -> Specification[T]:
    """A specification, or criteria wrapped as one."""
    return value if isinstance(value, Specification) else _Matching(as_criteria(value))


def as_ordering(value: OrderingLike) -> Ordering:
    """An ordering from an ``Ordering``, an ``F`` (ascending), or ``"-name"``."""
    if isinstance(value, Ordering):
        return value
    if isinstance(value, F):
        return value.asc()
    if isinstance(value, str):
        return Ordering.parse(value)
    raise TypeError(f"Expected an ordering, not {type(value).__name__}.")


def as_query(value: "Query | CriteriaLike | None") -> Query:
    """A query from a query, criteria, a specification, or nothing (everything)."""
    return value if isinstance(value, Query) else Query(value)


def resolve(model: type, name: str) -> tuple[FieldInfo, ...]:
    """The fields a (dotted) name passes through, from ``model``.

    Raises:
        UnknownField: A part names no field.
        InvalidQuery: A part before the last is not a child entity, or the
            last one is (a relation is not a value to compare).
    """
    path: list[FieldInfo] = []
    owner = model
    for part in name.split("."):
        if path:
            entity = path[-1].entity
            if entity is None:
                raise InvalidQuery(
                    f"{name!r}: {path[-1].name} holds no child entities to look into."
                )
            owner = entity
        try:
            path.append(fields_of(owner)[part])
        except KeyError:
            raise UnknownField(f"{owner.__name__} has no field {part!r}.") from None
    if path[-1].entity is not None:
        raise InvalidQuery(f"{name!r} holds child entities; name one of their fields.")
    return tuple(path)


def _bound_value(info: FieldInfo, lookup: Lookup, value: object) -> object:
    """``value`` checked and coerced for ``lookup`` on the field ``info``."""
    if lookup is Lookup.ISNULL:
        if not isinstance(value, bool):
            raise InvalidQuery(
                f"isnull takes True or False, not {type(value).__name__}."
            )
        return value
    if lookup in TEXT_LOOKUPS:
        if not info.text:
            raise InvalidQuery(f"{info.name} is not text; {lookup} does not apply.")
        if not isinstance(value, str):
            raise InvalidQuery(f"{lookup} takes text, not {type(value).__name__}.")
        return value
    if lookup is Lookup.EXACT:
        return info.coerce(value)
    values = value if lookup in {Lookup.IN, Lookup.RANGE} else (value,)
    if not isinstance(values, tuple) or None in values:
        raise InvalidQuery(f"{lookup} on {info.name} takes values, not None.")
    if lookup in ORDERING_LOOKUPS and not info.orderable:
        raise InvalidQuery(f"{info.name} cannot be compared by {lookup}.")
    coerced = tuple(info.coerce(each) for each in values)
    if lookup is Lookup.RANGE and len(coerced) != 2:
        raise InvalidQuery("range takes a low and a high value.")
    return coerced if lookup in {Lookup.IN, Lookup.RANGE} else coerced[0]


def _holds(
    candidate: object, path: tuple[FieldInfo, ...], lookup: Lookup, value: object
) -> bool:
    """Whether the field at ``path`` from ``candidate`` meets the lookup."""
    head, rest = path[0], path[1:]
    current = getattr(candidate, head.name)
    if not rest:
        return _compare(current, lookup, value)
    if head.many:
        return any(_holds(child, rest, lookup, value) for child in current)
    return current is not None and _holds(current, rest, lookup, value)


def _compare(current: object, lookup: Lookup, value: object) -> bool:
    """Compare a field's value with a bound value, missing values false."""
    if lookup is Lookup.ISNULL:
        return (current is None) is value
    if current is None or value is None:
        return current is None and value is None and lookup is Lookup.EXACT
    left = primitive(current)
    if lookup is Lookup.EXACT:
        return bool(left == primitive(value))
    if lookup is Lookup.IN:
        return any(left == primitive(each) for each in value)  # type: ignore[attr-defined]
    if lookup is Lookup.RANGE:
        low, high = cast("tuple[Any, Any]", value)
        return bool(primitive(low) <= left <= primitive(high))  # type: ignore[operator]
    if lookup in _COMPARISONS:
        return _COMPARISONS[lookup](left, primitive(value))
    text, piece = str(left), str(value)
    if lookup in CASE_FOLDED:
        text, piece = text.lower(), piece.lower()
    return _TEXT_TESTS[lookup](text, piece)


def total_ordering(ordering: Iterable[Ordering]) -> tuple[Ordering, ...]:
    """``ordering`` up to the id, with the id added if it is missing."""
    result: list[Ordering] = []
    for item in ordering:
        result.append(item)
        if item.field == ID:
            return tuple(result)
    return (*result, Ordering(ID))


def order[T](candidates: Iterable[T], ordering: Sequence[Ordering]) -> list[T]:
    """``candidates`` sorted as a repository sorts them: missing values last."""
    result = list(candidates)
    for item in reversed(ordering):
        result.sort(key=_sort_key(item.field), reverse=item.descending)
    return result


def _sort_key(name: str) -> Callable[[object], tuple[bool, Any]]:
    """Sort by the field ``name``: values first, then missing ones."""

    def key(candidate: object) -> tuple[bool, Any]:
        value = primitive(getattr(candidate, name))
        return value is None, value

    return key


def position_of(candidate: object, ordering: Sequence[Ordering]) -> tuple[object, ...]:
    """Where ``candidate`` sits in ``ordering``: its values of the fields."""
    return tuple(getattr(candidate, item.field) for item in ordering)


def following(
    model: type, ordering: Sequence[Ordering], position: Sequence[object]
) -> Criteria:
    """Criteria for what sorts after ``position`` in ``ordering`` (keyset paging).

    ``ordering`` is total (see ``total_ordering``), and ``position`` has a
    value for each of its fields. Missing values sort last, as ``order``
    sorts them.

    Raises:
        InvalidQuery: The position does not fit the ordering.
    """
    if len(position) != len(ordering):
        raise InvalidQuery(
            f"A position needs {len(ordering)} values for "
            f"{', '.join(map(str, ordering))}, not {len(position)}."
        )
    fields = fields_of(model)
    alternatives: list[Criteria] = []
    same: list[Criteria] = []
    for item, value in zip(ordering, position, strict=True):
        item.bind(model)
        name = F(item.field)
        nullable = fields[item.field].nullable
        beyond = _beyond(name, value, descending=item.descending, nullable=nullable)
        if beyond is not None:
            alternatives.append(And.of(*same, beyond))
        same.append(name.isnull() if value is None else name == value)
    if not alternatives:
        raise InvalidQuery("Nothing sorts after a position whose values are missing.")
    return Or.of(*alternatives)


def _beyond(
    name: F, value: object, *, descending: bool, nullable: bool
) -> Criteria | None:
    """Criteria for values that sort after ``value``; None if nothing does."""
    if descending:
        return name.isnull(value=False) if value is None else name < value
    if value is None:
        return None
    return (name > value) | name.isnull() if nullable else name > value
