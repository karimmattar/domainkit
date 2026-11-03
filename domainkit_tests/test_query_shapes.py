"""Query shapes: criteria whose values are slots, compiled once (PERF-33).

A repository's ``filter`` keeps the statement of each shape of query, with
bound parameters, so SQLAlchemy neither builds it nor walks it for a cache
key again. What a bound parameter cannot carry just as the value does
keeps its own statement; ``test_db_query.py`` checks that every query
answers on SQL as it does in memory.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, NewType

from sqlalchemy import MetaData
from sqlalchemy.sql import operators

from domainkit.db import map_imperatively, table_for
from domainkit.db.query import Slot, mapper_of, shape_of, with_parameters
from domainkit.ddd import AggregateRoot, Encrypted
from domainkit.ddd.query import And, Condition, F, Lookup, Not, Or
from domainkit_tests.gadgets import Gadget
from domainkit_tests.test_money_db import Wallet

MADE = datetime(2026, 1, 1, tzinfo=UTC)
VaultId = NewType("VaultId", int)


class Vault(AggregateRoot[VaultId]):
    """An aggregate with an encrypted field."""

    secret: Annotated[str, Encrypted()]


map_imperatively(
    Vault, table_for(Vault, "framework_test_shape_vaults", metadata=MetaData())
)
GADGET = mapper_of(Gadget)


def test_values_become_slots_in_order() -> None:
    """Exact, in, and comparisons take slots; isnull keeps its value."""
    criteria = (
        (F("stock") == 3)
        & F("name").in_(("a", "b"))
        & ~(F("made_at") > MADE)
        & ((F("price") >= Decimal("1.5")) | F("stock").isnull(True))
    ).bind(Gadget)

    shaped = shape_of(criteria, GADGET)

    assert shaped is not None
    shape, values = shaped
    assert values == [3, ["a", "b"], MADE, Decimal("1.5")]
    slots = [c.value for c in shape.conditions()]
    assert slots == [
        Slot(0, int),
        Slot(1, frozenset({str})),
        Slot(2, datetime),
        Slot(3, Decimal),
        True,
    ]
    assert isinstance(shape, And)
    assert any(isinstance(part, Not) for part in shape.parts)
    assert any(isinstance(part, Or) for part in shape.parts)


def test_one_shape_for_other_values_another_for_other_types() -> None:
    """The shape is the key: values of the same types share it."""
    one = shape_of((F("stock") == 3).bind(Gadget), GADGET)
    two = shape_of((F("stock") == 7).bind(Gadget), GADGET)
    other = shape_of((F("price") == Decimal(1)).bind(Gadget), GADGET)
    as_int = shape_of(Condition("price", Lookup.EXACT, 1), GADGET)

    assert one is not None
    assert two is not None
    assert one[0] == two[0]
    assert hash(one[0]) == hash(two[0])
    assert one[1] != two[1]
    assert other is not None
    assert as_int is not None
    assert other[0] != as_int[0]


def test_what_a_parameter_cannot_carry_has_no_shape() -> None:
    """None, booleans, text lookups, dotted, unknown, money, encrypted fields."""
    refused = [
        (F("stock") == None).bind(Gadget),  # noqa: E711 - the query's own None.
        (F("active") == True).bind(Gadget),  # noqa: E712 - a boolean value.
        F("name").icontains("a").bind(Gadget),
        (F("parts.sku") == "X").bind(Gadget),
        Condition("name", Lookup.IN, ("a", None)),
        Condition("name", Lookup.IN, ()),
        Condition("name", Lookup.IN, ["a"]),
        Condition("nope", Lookup.EXACT, 1),
        (F("stock") == 3) & F("name").icontains("a"),
        ~F("name").icontains("a"),
    ]
    for criteria in refused:
        assert shape_of(criteria, GADGET) is None, criteria
    assert shape_of(Condition("balance", Lookup.EXACT, 1), mapper_of(Wallet)) is None
    assert shape_of(Condition("secret", Lookup.EXACT, "x"), mapper_of(Vault)) is None


def test_parameters_are_typed_as_the_literals_they_stand_for() -> None:
    """The column's own comparison type of each value, and ``in`` expanding."""
    criteria = ((F("price") > Decimal("1.995")) & F("name").in_(("a",))).bind(Gadget)
    shaped = shape_of(criteria, GADGET)
    assert shaped is not None
    shape, values = shaped

    bound = with_parameters(shape, GADGET, values)

    price, name = bound.conditions()
    column = GADGET.column_attrs["price"].columns[0]
    expected = column.type.coerce_compared_value(operators.gt, Decimal("1.995"))
    assert type(price.value.type) is type(expected)  # type: ignore[attr-defined]
    assert price.value.key == "p0"  # type: ignore[attr-defined]
    assert name.value.expanding  # type: ignore[attr-defined]
    assert with_parameters(F("stock").isnull(True).bind(Gadget), GADGET, []) == (
        F("stock").isnull(True).bind(Gadget)
    )
