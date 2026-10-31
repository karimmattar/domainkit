"""Tables derived from domain classes, so a field is declared once.

``table_for`` reads an entity's dataclass fields and type hints
(``domainkit.ddd.fields``) and builds the ``Table`` to map it onto::

    class Product(AggregateRoot[ProductId]):
        name: ProductName                         # a single-field value object
        price: Annotated[Decimal, Digits(12, 2)]
        status: Status = Status.DRAFT             # an Enum
        published_at: datetime | None = None

    products = table_for(
        Product, "catalog_products", Index("ix_catalog_products_name", "name"),
        domain="catalog",
    )

=============================  ==========================================
Hint                           Column
=============================  ==========================================
``int``                        ``BigInteger`` (``Integer`` for ``version``)
``str``                        ``String(n)`` with ``MaxLength(n)``, else ``Text``
``bool``                       ``Boolean``
``float``                      ``Double``
``Decimal``                    ``ExactDecimal(p, s)``; needs ``Digits(p, s)``
``datetime``                   ``UTCDateTime``
``date``, ``time``             ``Date``, ``Time``
``UUID``                       ``Uuid``
``Currency``                   ``CurrencyCode``: the code
``Money``                      ``MoneyColumns``: ``<name>`` and ``<name>_currency``
an ``Enum``                    ``EnumValue``: its values, as text or integers
a single-field value object    ``ValueObjectType`` around its field's column
a registered class             ``register_column_type(cls, column_type)``
``X | None``                   the same, nullable
``Reference[XId]``             the id's column, indexed
``Encrypted()`` text or bytes  ``EncryptedColumns``: sealed text, and
                               ``<name>_index`` with ``blind_index``
=============================  ==========================================

``id`` is the primary key, and a ``version`` column turns on optimistic
locking when ``map_imperatively`` maps the class. Child entities are not
columns: map them with ``children`` or ``child`` (``domainkit.db.orm``),
their tables holding the foreign key. Columns passed in ``*extra`` replace
the derived column of the same name; other items (indexes, constraints)
are added. The result is an ordinary ``Table`` on ``metadata``, so
``makemigrations`` sees it as it sees hand-written ones.
"""

from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Double,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Time,
    Uuid,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.schema import SchemaItem
from sqlalchemy.types import TypeDecorator, TypeEngine

from domainkit.db.encrypted import EncryptedColumns
from domainkit.db.money import CHECK, PRECISION, SCALE, MoneyColumns, money_check
from domainkit.db.orm import VERSION
from domainkit.db.orm import metadata as default_metadata
from domainkit.db.types import CurrencyCode, ExactDecimal, UTCDateTime
from domainkit.ddd.fields import (
    Digits,
    Encrypted,
    FieldInfo,
    MaxLength,
    fields_of,
    wrapped_field,
)
from domainkit.ddd.personal import Personal
from domainkit.money import Currency, Money

_column_types: dict[type, TypeEngine[Any] | Callable[[], TypeEngine[Any]]] = {}


def register_column_type(
    cls: type, column_type: TypeEngine[Any] | Callable[[], TypeEngine[Any]]
) -> None:
    """Store fields of ``cls`` (and its subclasses) in ``column_type``.

    For value objects of several fields, or any class the table above does
    not cover. ``column_type`` is a type, or a function that makes one.
    """
    _column_types[cls] = column_type


class EnumValue[EnumT: Enum](TypeDecorator[EnumT]):
    """An ``Enum`` stored by its members' values, so order follows them.

    Text values become ``String`` as long as the longest; integer values
    ``Integer``. Renaming a member keeps its rows; changing its value does
    not.
    """

    impl: TypeEngine[Any] | type[TypeEngine[Any]] = String
    cache_ok = True

    def __init__(self, enum: type[EnumT]) -> None:
        """Store members of ``enum``, whose values are all text or all integers."""
        values = [member.value for member in enum]
        impl: TypeEngine[Any]
        if values and all(isinstance(value, str) for value in values):
            impl = String(max(len(value) for value in values))
        elif values and all(type(value) is int for value in values):
            impl = Integer()
        else:
            raise TypeError(
                f"EnumValue stores enums whose values are all text or all "
                f"integers, which {enum.__name__}'s are not."
            )
        super().__init__()
        self.impl = impl
        self.enum = enum

    def process_bind_param(self, value: EnumT | None, dialect: Dialect) -> object:
        """The member's value."""
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: object, dialect: Dialect) -> EnumT | None:
        """The member with the stored value."""
        return None if value is None else self.enum(value)

    def __repr__(self) -> str:
        """``EnumValue(Status)``."""
        return f"EnumValue({self.enum.__name__})"


class ValueObjectType[ValueT](TypeDecorator[ValueT]):
    """A single-field value object, stored as its field (``ProductName.value``)."""

    impl: TypeEngine[Any] | type[TypeEngine[Any]] = String
    cache_ok = True

    def __init__(self, cls: type[ValueT], inner: TypeEngine[Any]) -> None:
        """Store ``cls`` objects in a column of type ``inner``.

        Raises:
            TypeError: ``cls`` is not a value object with exactly one field.
        """
        field = wrapped_field(cls)
        if field is None:
            raise TypeError(
                f"{cls.__name__} is not a value object with one field; "
                "register a column type for it."
            )
        super().__init__()
        self.impl = inner
        self.cls = cls
        self.inner = inner
        self.field = field.name

    def process_bind_param(self, value: ValueT | None, dialect: Dialect) -> object:
        """The wrapped value.

        Raises:
            TypeError: ``value`` is not one of the value objects.
        """
        if value is None:
            return None
        if not isinstance(value, self.cls):
            raise TypeError(
                f"A {self.cls.__name__} column takes {self.cls.__name__}, not "
                f"{type(value).__name__}."
            )
        return getattr(value, self.field)

    def process_result_value(self, value: object, dialect: Dialect) -> ValueT | None:
        """The stored value, wrapped again."""
        return None if value is None else self.cls(value)  # type: ignore[call-arg]

    def loaded(self, value: object) -> ValueT:
        """The value object a fixture's field makes (``loaddata``).

        The class, as the database's value makes it; a column type may
        refuse more, as the password hash's does. ``ValueError`` or
        ``TypeError`` refuses the value without saying why;
        ``domainkit.db.fixtures.FixtureError`` says why, without the value.
        """
        return self.cls(value)  # type: ignore[call-arg]

    def __repr__(self) -> str:
        """``ValueObjectType(ProductName, String(length=200))``."""
        return f"ValueObjectType({self.cls.__name__}, {self.impl_instance!r})"


def table_for(
    cls: type,
    name: str,
    *extra: SchemaItem,
    domain: str | None = None,
    metadata: MetaData = default_metadata,
) -> Table:
    """The table that stores ``cls``: one column per field (see the module).

    ``domain`` tags the table with its domain label, as hand-written tables
    do with ``info={"domain": ...}``, so migrations follow the routing.

    Raises:
        TypeError: A field's type has no column type; pass a ``Column`` for
            it in ``extra``, or register one.
    """
    given = {item.name: item for item in extra if isinstance(item, Column)}
    columns: list[SchemaItem] = []
    fields = fields_of(cls)
    for field in fields.values():
        if field.entity is not None:
            continue
        if field.kind is Money:
            columns.extend(_money_columns(field, given, extra))
            continue
        if field.encrypted:
            columns.extend(_encrypted_columns(field, fields, given))
            continue
        if field.name in given:
            columns.append(given.pop(field.name))
            continue
        columns.append(
            Column(
                field.name,
                column_type(field),
                primary_key=field.name == "id",
                nullable=field.nullable,
                index=field.reference,
            )
        )
    others = [item for item in extra if not isinstance(item, Column)]
    return Table(
        name,
        metadata,
        *columns,
        *given.values(),
        *others,
        info={} if domain is None else {"domain": domain},
    )


def _money_columns(
    field: FieldInfo, given: dict[str, Column[Any]], extra: tuple[SchemaItem, ...]
) -> list[SchemaItem]:
    """The amount and currency columns of a money field, and its check.

    ``Digits(precision, scale)`` sizes the amount (default: 18 digits, 4
    places). Columns given in ``extra`` (``*MoneyColumns(...)``) replace
    the derived ones; a pair that may be missing gets its check, unless
    ``extra`` brings one.
    """
    digits = field.marker(Digits) or Digits(PRECISION, SCALE)
    derived = MoneyColumns(
        field.name, digits.precision, digits.scale, nullable=field.nullable
    )
    if field.name not in given:
        return list(derived)
    amount = given.pop(field.name)
    currency = given.pop(derived.currency.name, derived.currency)
    checked = any(
        isinstance(item, CheckConstraint) and item.info.get(CHECK) == field.name
        for item in extra
    )
    if not field.nullable or checked:
        return [amount, currency]
    return [amount, currency, money_check(field.name, amount, currency)]


def _encrypted_columns(
    field: FieldInfo, fields: Mapping[str, FieldInfo], given: dict[str, Column[Any]]
) -> list[SchemaItem]:
    """The columns of an encrypted field: its sealed value, and its blind index.

    Columns given in ``extra`` replace the derived ones of the same names.

    Raises:
        TypeError: The field is not text or bytes, its subject is not
            another field, or it has a subject but cannot be None.
    """
    marker = field.marker(Encrypted) or Encrypted()
    personal = field.marker(Personal)
    subject = marker.subject or (personal.of if personal else None)
    if subject is not None and (subject == field.name or subject not in fields):
        raise TypeError(
            f"{_where(field)}: Encrypted(subject={subject!r}) must name another "
            "field of the class."
        )
    if subject is not None and not field.nullable:
        raise TypeError(
            f"{_where(field)} is sealed under the key of the person {subject!r} "
            "names, which erasing them destroys; it then reads as None, so hint "
            "it as allowing None."
        )
    if field.kind is None:
        raise TypeError(f"{_where(field)}: the type hint names no class.")
    try:
        derived = EncryptedColumns(
            field.name,
            field.kind,
            subject=subject,
            blind_index=marker.blind_index,
            nullable=field.nullable,
        )
    except TypeError as exc:
        raise TypeError(f"{_where(field)}: {exc}") from None
    value = given.pop(field.name, derived.value)
    if derived.index is None:
        return [value]
    if derived.index.name in given:
        return [value, given.pop(derived.index.name)]
    return [value, *list(derived)[1:]]


def column_type(field: FieldInfo) -> TypeEngine[Any]:
    """The column type of ``field`` (see the module's table).

    Raises:
        TypeError: The field's type has none.
    """
    kind = field.kind
    if kind is None:
        raise TypeError(f"{_where(field)}: the type hint names no class.")
    if registered := _registered(kind):
        return registered
    if kind is bool:
        return Boolean()
    if kind is int:
        if field.name == VERSION:
            return Integer()
        return BigInteger().with_variant(Integer(), "sqlite")
    if kind is str:
        limit = field.marker(MaxLength)
        return Text() if limit is None else String(limit.length)
    if kind is Decimal:
        digits = field.marker(Digits)
        if digits is None:
            raise TypeError(
                f"{_where(field)}: say how many digits it holds, as "
                "Annotated[Decimal, Digits(precision, scale)]."
            )
        return ExactDecimal(digits.precision, digits.scale)
    if issubclass(kind, Enum):
        return EnumValue(kind)
    if (inner := wrapped_field(kind)) is not None:
        nested = FieldInfo(inner.name, kind, inner.kind, metadata=inner.metadata)
        return ValueObjectType(kind, column_type(nested))
    if kind in _SIMPLE:
        return _SIMPLE[kind]()
    raise TypeError(
        f"{_where(field)}: no column type for {kind.__name__}; pass a Column "
        "for it, or call register_column_type."
    )


_SIMPLE: Final[dict[type, Callable[[], TypeEngine[Any]]]] = {
    float: Double,
    datetime: UTCDateTime,
    date: Date,
    time: Time,
    UUID: Uuid,
    Currency: CurrencyCode,
}


def _registered(kind: type) -> TypeEngine[Any] | None:
    """The column type registered for ``kind`` or one of its bases."""
    for base in kind.__mro__:
        if base in _column_types:
            registered = _column_types[base]
            return registered if isinstance(registered, TypeEngine) else registered()
    return None


def _where(field: FieldInfo) -> str:
    """``Product.price``, for error messages."""
    return f"{field.owner.__name__}.{field.name}"
