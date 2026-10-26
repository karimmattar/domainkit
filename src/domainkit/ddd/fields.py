"""What a domain class's fields hold, read from its type hints.

Queries, tables, and filters all need to know a field's type: whether it
may be None, whether it sorts or is searched as text, and which child
entities it leads to. ``fields_of(cls)`` reads that once per class from the
dataclass fields and their hints, so every adapter agrees.

Limits that storage needs, and that the domain should state anyway, are
``Annotated`` metadata::

    class Product(AggregateRoot[ProductId]):
        name: Annotated[str, MaxLength(200)]
        price: Annotated[Decimal, Digits(12, 2)]

A value object with a single field (``ProductName(value: str)``) counts as
that field's value: it is compared, ordered, and searched by it, as the
column holding it is.

Another aggregate is referenced by its id only, never held as an object
(each aggregate is its own consistency boundary)::

    class Invoice(AggregateRoot[InvoiceId]):
        customer_id: Reference[CustomerId]

``Reference[CustomerId]`` is ``CustomerId`` to type checkers; ``fields_of``
marks the field as a reference, so a derived table indexes its column and a
derived schema says what it is.
"""

import dataclasses
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from functools import cache
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Final,
    TypeAliasType,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)
from uuid import UUID

from domainkit.ddd.model import Entity, ValueObject
from domainkit.exceptions import DomainkitError

COLLECTIONS: Final = frozenset({list, set, frozenset, tuple})
"""Containers that hold child entities: the "many" side of a relation."""
ORDERED: Final = (int, float, Decimal, str, datetime, date, time, UUID, Enum)
"""Types whose values sort (``bool`` with ``int``; enums by their values)."""


class InvalidQuery(ValueError, DomainkitError):
    """A query names a field, lookup, or value its aggregate cannot take."""


class UnknownField(InvalidQuery):
    """A query names a field the class does not have."""


@dataclass(frozen=True, slots=True)
class MaxLength:
    """``Annotated`` metadata: text of at most ``length`` characters."""

    length: int

    def __post_init__(self) -> None:
        """Refuse lengths below one."""
        if self.length < 1:
            raise ValueError(
                f"MaxLength needs a length of at least 1, not {self.length}."
            )


@dataclass(frozen=True, slots=True)
class Digits:
    """``Annotated`` metadata: ``precision`` digits, ``scale`` of them decimal."""

    precision: int
    scale: int

    def __post_init__(self) -> None:
        """Refuse a scale outside the precision."""
        if not 0 <= self.scale <= self.precision or self.precision < 1:
            raise ValueError(
                f"Digits needs 1 <= precision and 0 <= scale <= precision, not "
                f"precision={self.precision}, scale={self.scale}."
            )


class Sensitive:
    """A type whose values no schema shows or takes, such as a password hash.

    Subclass it beside ``ValueObject``: ``schema_for`` refuses a field that
    holds one, so a secret never leaves or enters through a derived schema
    unless the field is excluded::

        class PinHash(ValueObject, Sensitive):
            value: str
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class AggregateReference:
    """``Annotated`` metadata: the field holds another aggregate's id."""


REFERENCE: Final = AggregateReference()

type Reference[IdT] = Annotated[IdT, REFERENCE]
"""The id of another aggregate, which is referenced by id only (``Reference[XId]``)."""


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """One field of a domain class, as queries and tables see it."""

    name: str
    owner: type
    """The class the field belongs to."""
    kind: type | None
    """The class of its values (of its entities, if ``many``); None if unknown."""
    nullable: bool = False
    """Whether the hint allows None."""
    metadata: tuple[object, ...] = ()
    """The ``Annotated`` metadata, such as ``MaxLength``."""
    many: bool = False
    """A collection of child entities (``list[OrderLine]``)."""

    @property
    def entity(self) -> type[Entity[object]] | None:
        """The child entity class this field leads to, if it is a relation."""
        if self.kind is not None and issubclass(self.kind, Entity):
            return self.kind
        return None

    @property
    def reference(self) -> bool:
        """Whether the field holds another aggregate's id (``Reference[XId]``)."""
        return self.marker(AggregateReference) is not None

    @property
    def text(self) -> bool:
        """Whether text lookups apply: the values are, or wrap, ``str``."""
        kind = base_type(self.kind)
        return kind is not None and issubclass(kind, str) and not issubclass(kind, Enum)

    @property
    def orderable(self) -> bool:
        """Whether values sort; a field of unknown type is assumed to."""
        if self.kind is None:
            return not self.many
        kind = base_type(self.kind)
        return kind is not None and issubclass(kind, ORDERED)

    def marker[MarkerT](self, kind: type[MarkerT]) -> MarkerT | None:
        """The ``Annotated`` metadata of type ``kind``, if the field has one."""
        for item in self.metadata:
            if isinstance(item, kind):
                return item
        return None

    def coerce(self, value: object) -> object:
        """``value`` as this field holds it: checked, and wrapped if need be.

        An enum field takes a member or a member's value. A single-field
        value object field takes the object or what it wraps. Other fields
        take values of their type; an amount (``Decimal``) also takes an
        ``int``, and ``bool`` is never taken for a number. Floats and
        amounts must be finite, and datetimes aware.

        Raises:
            InvalidQuery: The value does not fit the field.
        """
        kind = self.kind
        if value is None or kind is None:
            return value
        if isinstance(value, bool) and base_type(kind) is not bool:
            raise self._refuse(kind, value)
        if isinstance(value, datetime) and kind is date:
            raise self._refuse(kind, value)
        if isinstance(value, kind):
            return self._finite(value)
        if issubclass(kind, Enum):
            try:
                return kind(value)
            except ValueError:
                raise self._refuse(kind, value) from None
        if (inner := wrapped_field(kind)) is not None:
            return cast("Callable[[object], object]", kind)(inner.coerce(value))
        if kind is Decimal and isinstance(value, int):
            return value
        raise self._refuse(kind, value)

    def _finite(self, value: object) -> object:
        """Refuse what compares unlike anywhere else: NaN, infinity, naive times."""
        if isinstance(value, float | Decimal) and not _is_finite(value):
            raise InvalidQuery(f"{self.name} cannot be compared with {value}.")
        if isinstance(value, datetime) and value.utcoffset() is None:
            raise InvalidQuery(f"{self.name} needs a timezone-aware datetime.")
        return value

    def _refuse(self, kind: type, value: object) -> InvalidQuery:
        """The error for a value of the wrong type; the value itself stays out."""
        return InvalidQuery(
            f"{self.owner.__name__}.{self.name} holds {kind.__name__}, "
            f"not {type(value).__name__}."
        )


def _is_finite(value: float | Decimal) -> bool:
    """Whether a float or an amount is a number, and not infinite."""
    return value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)


@cache
def fields_of(cls: type) -> Mapping[str, FieldInfo]:
    """The fields of the dataclass ``cls`` (an entity or aggregate), by name.

    Raises:
        TypeError: ``cls`` is not a dataclass.
    """
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise TypeError(f"{cls!r} is not a dataclass.")
    hints = get_type_hints(cls, include_extras=True)
    bound = type_arguments(cls)
    return {
        field.name: describe(cls, field.name, _bind(hints[field.name], bound))
        for field in dataclasses.fields(cls)
    }


def _bind(hint: object, bound: Mapping[object, object]) -> object:
    """``hint``, or what it stands for if it is a bound type parameter."""
    return bound.get(hint, hint) if isinstance(hint, TypeVar) else hint


def type_arguments(cls: type) -> dict[object, object]:
    """What each type parameter of ``cls``'s generic bases stands for.

    ``class Order(AggregateRoot[OrderId])`` binds the ``IdT`` that
    ``Entity.id`` is declared with to ``OrderId``.
    """
    bound: dict[object, object] = {}
    for klass in cls.__mro__:
        for alias in klass.__dict__.get("__orig_bases__", ()):
            origin = get_origin(alias)
            parameters = getattr(origin, "__parameters__", ())
            for parameter, argument in zip(parameters, get_args(alias), strict=False):
                bound.setdefault(parameter, bound.get(argument, argument))
    return bound


def describe(owner: type, name: str, hint: object) -> FieldInfo:
    """Describe the field ``name`` of ``owner`` from its type hint."""
    metadata, hint = _annotations(hint)
    members = list(get_args(hint)) if get_origin(hint) in {Union, UnionType} else [hint]
    nullable = NoneType in members
    kinds: list[type | None] = []
    many = False
    for member in members:
        if member is NoneType:
            continue
        extra, member_type = _annotations(member)
        metadata += extra
        origin = get_origin(member_type)
        entities = [
            argument
            for argument in map(_supertype, get_args(member_type))
            if isinstance(argument, type) and issubclass(argument, Entity)
        ]
        if origin in COLLECTIONS and entities:
            many = True
            kinds.append(entities[0])
        elif origin is not None:
            kinds.append(origin if isinstance(origin, type) else None)
        elif member_type is Any or not isinstance(member_type, type):
            kinds.append(None)  # Any is a class since Python 3.11.
        else:
            kinds.append(member_type)
    kind = kinds[0] if len(kinds) == 1 else None
    return FieldInfo(name, owner, kind, nullable, tuple(metadata), many)


def _annotations(hint: object) -> tuple[tuple[object, ...], object]:
    """The ``Annotated`` metadata of ``hint``, and the type beneath, unaliased."""
    hint = unaliased(hint)
    if get_origin(hint) is Annotated:
        return hint.__metadata__, _supertype(hint.__origin__)  # type: ignore[attr-defined]
    return (), _supertype(hint)


def unaliased(hint: object) -> object:
    """What a ``type`` alias (``Reference[CustomerId]``) stands for; others as given."""
    while True:
        alias = hint if isinstance(hint, TypeAliasType) else get_origin(hint)
        if not isinstance(alias, TypeAliasType):
            return hint
        value: Any = alias.__value__
        hint = value[get_args(hint)] if alias.__type_params__ else value


def _supertype(hint: object) -> object:
    """The type behind ``NewType`` aliases."""
    while hasattr(hint, "__supertype__"):
        hint = hint.__supertype__
    return hint


def wrapped_field(cls: type) -> FieldInfo | None:
    """The one field of a single-field value object; None for any other class."""
    if not issubclass(cls, ValueObject):
        return None
    fields = fields_of(cls)
    return next(iter(fields.values())) if len(fields) == 1 else None


def base_field(field: FieldInfo) -> FieldInfo:
    """The field whose ``Annotated`` metadata bounds what ``field`` holds.

    For a single-field value object, the field it wraps, followed down as
    ``base_type`` follows types (and as ``table_for`` derives the column):
    its ``MaxLength`` or ``Digits`` apply. For any other field, ``field``.
    """
    while field.kind is not None and (inner := wrapped_field(field.kind)) is not None:
        field = inner
    return field


def base_type(kind: type | None) -> type | None:
    """The type values are compared as: what single-field value objects wrap."""
    while kind is not None and (inner := wrapped_field(kind)) is not None:
        kind = inner.kind
    return kind


def primitive(value: object) -> object:
    """``value`` as a column holds it: enums by value, value objects unwrapped."""
    while True:
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, ValueObject) and (inner := wrapped_field(type(value))):
            value = getattr(value, inner.name)
        else:
            return value
