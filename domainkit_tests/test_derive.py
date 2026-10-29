"""Tables derived from domain classes, and child entities mapped onto them."""

import io
import re
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any, NewType
from uuid import UUID

import pytest
from alembic.autogenerate.api import AutogenContext
from alembic.migration import MigrationContext
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    Double,
    Index,
    Integer,
    Interval,
    MetaData,
    String,
    Text,
    Time,
    Uuid,
    create_engine,
)
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.types import TypeEngine, UserDefinedType

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import (
    EnumValue,
    ExactDecimal,
    UTCDateTime,
    ValueObjectType,
    child,
    children,
    register_column_type,
    table_for,
)
from domainkit.db import migrations as migrations_module
from domainkit.db.derive import _column_types, column_type
from domainkit.db.migrations import render_item
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import AggregateRoot, Digits, Entity, MaxLength, ValueObject
from domainkit.ddd.fields import FieldInfo, fields_of
from domainkit.management import call_command
from domainkit_tests.conftest import ALPHA
from domainkit_tests.gadgets import Color, Gadget, Label, Part, Size, gadgets

SQLITE = create_engine("sqlite://").dialect
POSTGRES = create_async_engine("postgresql+asyncpg://app@db/app").dialect
ShelfId = NewType("ShelfId", int)


class Money(ValueObject):
    """Two fields: no column type unless one is registered."""

    amount: Decimal
    currency: str


class Code(ValueObject):
    """One field of an exact amount."""

    value: Annotated[Decimal, Digits(6, 3)]


class Mixed(Enum):
    """Values of two kinds: not storable by value."""

    ONE = 1
    TWO = "two"


class Empty(Enum):
    """No members."""


class Shelf(AggregateRoot[ShelfId]):
    """An aggregate with every kind of field ``table_for`` derives."""

    count: int
    title: Annotated[str, MaxLength(40)]
    notes: str
    open: bool
    ratio: float
    price: Annotated[Decimal, Digits(8, 2)]
    since: datetime
    on: date
    at: time
    ref: UUID
    color: Color
    size: Size
    label: Label
    code: Code | None


def types_of(table: Any) -> dict[str, TypeEngine[Any]]:  # noqa: ANN401 - a Table.
    """Each column's type, by name."""
    return {column.name: column.type for column in table.columns}


def test_columns_are_derived_from_the_hints() -> None:
    """Each hint gets the column type documented in ``derive``."""
    table = table_for(Shelf, "framework_test_shelves", metadata=MetaData())
    types = types_of(table)

    assert list(types) == [
        "id", "version", "count", "title", "notes", "open", "ratio", "price",
        "since", "on", "at", "ref", "color", "size", "label", "code",
    ]  # fmt: skip
    assert table.c.id.primary_key
    assert [column.name for column in table.primary_key] == ["id"]
    assert isinstance(types["version"], Integer)
    assert not isinstance(types["version"], BigInteger)
    assert isinstance(types["count"].dialect_impl(SQLITE), Integer)
    assert isinstance(types["count"], BigInteger)
    assert isinstance(types["title"], String)
    assert types["title"].length == 40
    assert isinstance(types["notes"], Text)
    assert isinstance(types["open"], Boolean)
    assert isinstance(types["ratio"], Double)
    assert isinstance(types["price"], ExactDecimal)
    assert (types["price"].precision, types["price"].scale) == (8, 2)
    assert isinstance(types["since"], UTCDateTime)
    assert isinstance(types["on"], Date)
    assert isinstance(types["at"], Time)
    assert isinstance(types["ref"], Uuid)
    assert repr(types["color"]) == "EnumValue(Color)"
    assert repr(types["label"]) == "ValueObjectType(Label, String(length=20))"
    assert (
        repr(types["code"])
        == "ValueObjectType(Code, ExactDecimal(precision=6, scale=3))"
    )
    nullable = {column.name for column in table.columns if column.nullable}
    assert nullable == {"code"}
    assert table.info == {}


def test_given_columns_and_items_replace_or_join_the_derived() -> None:
    """A Column replaces its field's; other items and columns are added."""
    table = table_for(
        Shelf,
        "framework_test_shelves",
        Column("title", String(10), nullable=True),
        Column("extra", Integer),
        Index("ix_framework_test_shelves_title", "title"),
        domain="shelving",
        metadata=MetaData(),
    )

    assert table.c.title.type.length == 10  # type: ignore[attr-defined]
    assert table.c.title.nullable is True
    assert "extra" in table.c
    assert [index.name for index in table.indexes] == [
        "ix_framework_test_shelves_title"
    ]
    assert table.info == {"domain": "shelving"}


def test_child_entities_are_relations_not_columns() -> None:
    """The gadget's parts and maker live in tables of their own."""
    assert "parts" not in gadgets.c
    assert "maker" not in gadgets.c
    assert set(gadgets.c.keys()) >= {"deleted_at", "label", "price"}


def test_fields_without_a_column_type_are_refused() -> None:
    """Amounts need Digits; unknown classes need a registered type."""

    class Untyped(AggregateRoot[int]):
        amount: Decimal

    class Valued(AggregateRoot[int]):
        money: Money

    class Loose(AggregateRoot[int]):
        anything: Any

    class Odd(AggregateRoot[int]):
        mixed: Mixed

    with pytest.raises(TypeError, match=r"Untyped\.amount: say how many digits"):
        table_for(Untyped, "untyped", metadata=MetaData())
    with pytest.raises(TypeError, match=r"Valued\.money: no column type for Money"):
        table_for(Valued, "valued", metadata=MetaData())
    with pytest.raises(
        TypeError, match=r"Loose\.anything: the type hint names no class"
    ):
        table_for(Loose, "loose", metadata=MetaData())
    with pytest.raises(TypeError, match="Mixed's are not"):
        table_for(Odd, "odd", metadata=MetaData())
    with pytest.raises(TypeError, match="Empty's are not"):
        EnumValue(Empty)
    with pytest.raises(TypeError, match="Money is not a value object with one field"):
        ValueObjectType(Money, String())
    with pytest.raises(TypeError, match="int is not a value object"):
        ValueObjectType(int, String())


def test_registered_column_types_cover_other_classes() -> None:
    """A type, or a function making one, for a class and its subclasses."""

    class Euros(Money):
        """A subclass finds its base's type."""

    try:
        register_column_type(Money, String(30))
        assert isinstance(column_type(FieldInfo("m", Shelf, Euros)), String)
        register_column_type(Money, lambda: Integer())
        assert isinstance(column_type(FieldInfo("m", Shelf, Money)), Integer)
    finally:
        _column_types.pop(Money)
    with pytest.raises(TypeError, match="no column type for Money"):
        column_type(FieldInfo("m", Shelf, Money))


def test_enums_are_stored_by_value() -> None:
    """Text values as text, integer values as integers; members come back."""
    colors, sizes = EnumValue(Color), EnumValue(Size)

    assert isinstance(colors.impl_instance, String)
    assert colors.impl_instance.length == 5
    assert isinstance(sizes.impl_instance, Integer)
    assert colors.process_bind_param(Color.GREEN, SQLITE) == "green"
    assert colors.process_bind_param("blue", SQLITE) == "blue"  # type: ignore[arg-type]
    assert colors.process_bind_param(None, SQLITE) is None
    assert sizes.process_bind_param(Size.LARGE, SQLITE) == 10
    assert colors.process_result_value("red", SQLITE) is Color.RED
    assert sizes.process_result_value(2, SQLITE) is Size.SMALL
    assert sizes.process_result_value(None, SQLITE) is None
    with pytest.raises(ValueError, match="is not a valid Color"):
        colors.process_bind_param("purple", SQLITE)  # type: ignore[arg-type]

    class Flags(IntEnum):
        ON = 1

    assert isinstance(EnumValue(Flags).impl_instance, Integer)

    class Letters(StrEnum):
        A = "a"

    assert EnumValue(Letters).impl_instance.length == 1  # type: ignore[attr-defined]


def test_value_objects_are_stored_as_what_they_wrap() -> None:
    """Only the value object is taken, and it comes back wrapped."""
    labels = ValueObjectType(Label, String(20))

    assert labels.process_bind_param(Label("x"), SQLITE) == "x"
    assert labels.process_bind_param(None, SQLITE) is None
    assert labels.process_result_value("y", SQLITE) == Label("y")
    assert labels.process_result_value(None, SQLITE) is None
    with pytest.raises(TypeError, match="A Label column takes Label, not str"):
        labels.process_bind_param("x", SQLITE)  # type: ignore[arg-type]


def test_revisions_name_the_storage_types() -> None:
    """Alembic renders derived types as their plain SQL types."""
    context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))

    assert render_item("type", EnumValue(Color), context) == "sa.String(length=5)"
    assert render_item("type", EnumValue(Size), context) == "sa.Integer()"
    assert render_item("type", ValueObjectType(Label, String(20)), context) == (
        "sa.String(length=20)"
    )


def test_revisions_follow_a_value_object_down_to_what_it_stores() -> None:
    """One around another, or around an amount or a time, renders as its storage.

    Until 0.13 Alembic rendered these as ``domainkit.db.derive.ValueObjectType(
    Code, ValueObjectType(...))``, naming classes the revision never imports,
    so it failed to run. SQLAlchemy's own decorators keep their names.
    """
    context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))
    nested = ValueObjectType(Label, ValueObjectType(Label, String(20)))

    assert render_item("type", nested, context) == "sa.String(length=20)"
    assert render_item("type", ValueObjectType(Label, ExactDecimal(6, 3)), context) == (
        "sa.Numeric(precision=6, scale=3)"
    )
    assert render_item("type", ValueObjectType(Label, UTCDateTime()), context) == (
        "sa.DateTime(timezone=True)"
    )
    assert render_item("type", ValueObjectType(Label, Interval()), context) == (
        "sa.Interval()"
    )


BinId = NewType("BinId", int)


class Bin(AggregateRoot[BinId]):
    """An aggregate for a revision."""

    name: Annotated[str, MaxLength(20)]
    price: Annotated[Decimal, Digits(8, 2)]
    color: Color
    label: Label | None = None


@pytest.mark.usefixtures("database_access")
def test_makemigrations_writes_derived_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived table is a revision like any other, and it applies."""
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    table_for(Bin, "framework_test_bins", metadata=schema)
    monkeypatch.setattr(migrations_module, "metadata", schema)
    registry.clear()
    database = tmp_path / "bins.sqlite3"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{database}"}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "bins", stdout=io.StringIO())
        (revision,) = (tmp_path / "migrations").glob("*.py")
        call_command("migrate", stdout=io.StringIO())
    registry.clear()
    text = revision.read_text()
    assert "framework_test_bins" in text
    assert "sa.String(length=5)" in text  # The color, by value.
    assert "sa.Numeric(precision=8, scale=2)" in text
    assert "sa.String(length=20)" in text  # The name, and the label.
    assert "domainkit_tests" not in text  # Revisions never import domain code.


class Stamp(ValueObject):
    """One field of a time."""

    value: datetime


HamperId = NewType("HamperId", int)


class Hamper(AggregateRoot[HamperId]):
    """An aggregate of value objects around an amount and a time."""

    code: Code
    packed: Stamp | None = None


def test_revisions_render_a_value_object_around_an_amount_or_a_time() -> None:
    """A one-field value object of a ``Decimal`` or a ``datetime`` is its storage.

    Until 0.12.1 Alembic rendered these as ``domainkit.db.derive.ValueObjectType(
    Code, ExactDecimal(...))``, naming classes the revision never imports.
    SQLAlchemy's own decorators keep their names.
    """
    context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))

    assert render_item("type", ValueObjectType(Code, ExactDecimal(6, 3)), context) == (
        "sa.Numeric(precision=6, scale=3)"
    )
    assert render_item("type", ValueObjectType(Stamp, UTCDateTime()), context) == (
        "sa.DateTime(timezone=True)"
    )
    assert render_item("type", ValueObjectType(Label, Interval()), context) == (
        "sa.Interval()"
    )


class Point(UserDefinedType[str]):
    """A project's own type, not a decorator: Alembic renders it."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        """``POINT``."""
        return "POINT"


def test_revisions_leave_other_types_to_alembic() -> None:
    """SQLAlchemy's own decorators and a project's own types render as Alembic does."""
    context = AutogenContext(MigrationContext.configure(dialect_name="sqlite"))

    assert render_item("type", Interval(), context) is False
    assert render_item("type", Point(), context) is False
    assert render_item("type", ValueObjectType(Label, Point()), context) is False
    assert render_item("server_default", EnumValue(Color), context) is False


def test_makemigrations_writes_a_revision_of_amounts_and_times_that_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Value objects around an amount or a time make a revision that applies."""
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    table_for(Hamper, "framework_test_hampers", metadata=schema)
    monkeypatch.setattr(migrations_module, "metadata", schema)
    registry.clear()
    database = tmp_path / "hampers.sqlite3"
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{database}"}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        call_command("makemigrations", "-m", "hampers", stdout=io.StringIO())
        (revision,) = (tmp_path / "migrations").glob("*.py")
        call_command("migrate", stdout=io.StringIO())
    registry.clear()
    text = revision.read_text()
    assert "sa.Numeric(precision=6, scale=3)" in text  # The code.
    assert "sa.DateTime(timezone=True)" in text  # When it was packed.
    assert "domainkit" not in text.split("def upgrade", 1)[1]  # Nor name its types.


def test_children_load_with_and_belong_to_their_aggregate() -> None:
    """Loaded by selectin, cascading delete-orphan, unless overridden."""
    many = children(Part, order_by="sku")
    one = child(Part, lazy="joined")

    assert many.lazy == "selectin"
    assert many.cascade.delete_orphan
    assert many.uselist is None or many.uselist is True
    assert one.lazy == "joined"
    assert one.uselist is False
    assert one.cascade.delete_orphan
    assert fields_of(Gadget)["parts"].entity is Part
    assert isinstance(Entity, type)


def test_integers_are_big_except_on_sqlite() -> None:
    """BIGINT, but INTEGER on SQLite, whose auto-incremented keys must be INTEGER."""
    types = types_of(table_for(Shelf, "framework_test_shelves", metadata=MetaData()))

    assert types["count"].compile(dialect=SQLITE) == "INTEGER"
    assert types["count"].compile(dialect=POSTGRES) == "BIGINT"
    assert types["version"].compile(dialect=POSTGRES) == "INTEGER"


def test_given_columns_take_the_place_of_the_derived_ones() -> None:
    """A Column given for a field keeps its position; the fields after it follow."""
    table = table_for(
        Shelf,
        "framework_test_shelves",
        Column("title", String(10)),
        Column("extra", Integer),
        metadata=MetaData(),
    )

    assert list(types_of(table)) == [
        "id", "version", "count", "title", "notes", "open", "ratio", "price",
        "since", "on", "at", "ref", "color", "size", "label", "code", "extra",
    ]  # fmt: skip


class Crate(AggregateRoot[int]):
    """Child entities before an ordinary field."""

    parts: list[Part]
    title: Annotated[str, MaxLength(40)]


def test_fields_after_child_entities_are_columns_too() -> None:
    """A child entity is passed by, not the end of the fields."""
    table = table_for(Crate, "framework_test_crates", metadata=MetaData())

    assert list(types_of(table)) == ["id", "version", "title"]


class Amount(ValueObject):
    """One field of an amount whose digits are not stated."""

    value: Decimal


def test_refusals_name_the_field_and_say_what_to_do() -> None:
    """Each message ends with the fix; a value object's field is named as its own."""

    class Untyped(AggregateRoot[int]):
        amount: Decimal

    class Valued(AggregateRoot[int]):
        money: Money

    class Till(AggregateRoot[int]):
        amount: Amount

    digits = (
        "say how many digits it holds, as Annotated[Decimal, Digits(precision, scale)]."
    )
    with pytest.raises(TypeError, match=f"^{re.escape(f'Untyped.amount: {digits}')}$"):
        table_for(Untyped, "untyped", metadata=MetaData())
    with pytest.raises(TypeError, match=f"^{re.escape(f'Amount.value: {digits}')}$"):
        table_for(Till, "till", metadata=MetaData())
    unknown = (
        "Valued.money: no column type for Money; pass a Column for it, or call "
        "register_column_type."
    )
    with pytest.raises(TypeError, match=f"^{re.escape(unknown)}$"):
        table_for(Valued, "valued", metadata=MetaData())
    several = (
        "Money is not a value object with one field; register a column type for it."
    )
    with pytest.raises(TypeError, match=f"^{re.escape(several)}$"):
        ValueObjectType(Money, String())


def test_value_object_types_keep_their_arguments_for_statement_caching() -> None:
    """SQLAlchemy keys cached statements on attributes named after the arguments.

    With ``cache_ok``, a type missing ``inner`` would share compiled SQL with
    the same value object in another column type.
    """
    inner = String(20)
    labels = ValueObjectType(Label, inner)

    assert labels.cls is Label
    assert labels.inner is inner
