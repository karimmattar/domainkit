"""Schemas derived from domain classes, partial changes, entity tags, and versions."""

import copy
import pickle
from dataclasses import field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, NewType, get_args
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field, ValidationError

from domainkit.ddd import (
    UNSET,
    AggregateRoot,
    Digits,
    Entity,
    InMemoryRepository,
    MaxLength,
    PreconditionFailed,
    Reference,
    Unset,
    ValueObject,
)
from domainkit.ddd.fields import fields_of, unaliased
from domainkit.ddd.preconditions import check_expected, expect_version
from domainkit.ddd.query import F
from domainkit.resources import (
    DomainSchema,
    FieldPolicy,
    PatchModel,
    entity_tag,
    expected_versions,
    schema_for,
)
from domainkit.utils.headers import EntityTag, none_match, parse_tags

CustomerId = NewType("CustomerId", UUID)
OrderId = NewType("OrderId", UUID)


class Status(StrEnum):
    """An order's state."""

    OPEN = "open"
    PAID = "paid"


class Code(ValueObject):
    """A single-field value object, limited in length."""

    value: Annotated[str, MaxLength(8)]


class Money(ValueObject):
    """A value object of two fields."""

    amount: Annotated[Decimal, Digits(10, 2)]
    currency: str


class Line(Entity[int]):
    """A child entity."""

    sku: str
    quantity: int


class Note(Entity[int]):
    """A child entity held in a set."""

    text: str


class Order(AggregateRoot[OrderId]):
    """An aggregate with a field of each kind ``schema_for`` maps."""

    code: Code
    total: Money
    customer: Reference[CustomerId]
    status: Status = Status.OPEN
    placed_on: date
    paid_at: datetime | None = None
    count: int = 0
    tags: set[str] = field(default_factory=set)
    labels: frozenset[str] = frozenset()
    sizes: list[int] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)
    anything: Any = None
    either: int | str = 0
    lines: list[Line] = field(default_factory=list)
    notes: set[Note] = field(default_factory=set)
    first: Line | None = None
    alias: Code | None = None
    discount: Annotated[Decimal, Digits(4, 2)] = Decimal(0)
    mixed: set[int | str] = field(default_factory=set)
    maybe: set[str] | None = None


def make_order(**fields: Any) -> Order:  # noqa: ANN401 - Order's fields.
    """An order with every field set, unless given."""
    values: dict[str, Any] = {
        "id": OrderId(UUID(int=1)),
        "code": Code("A-1"),
        "total": Money(Decimal("12.50"), "EUR"),
        "customer": CustomerId(UUID(int=2)),
        "placed_on": date(2026, 1, 2),
        "tags": {"b", "a"},
        "labels": frozenset({"z", "y"}),
        "lines": [Line(id=2, sku="s2", quantity=1), Line(id=1, sku="s1", quantity=3)],
        "notes": {Note(id=2, text="two"), Note(id=1, text="one")},
        "first": Line(id=9, sku="s9", quantity=9),
        "mixed": {"b", 2, "a", 1},
        "maybe": {"y", "x"},
    }
    return Order(**{**values, **fields})


def test_read_schemas_list_every_field_required_and_unwrapped() -> None:
    """Ids, references, value objects, enums, and children, as clients see them."""
    schema = schema_for(Order)
    document = schema.model_json_schema()
    properties = document["properties"]

    assert schema.__name__ == "OrderOut"
    assert issubclass(schema, DomainSchema)
    assert schema.domain_class is Order
    assert document["required"] == list(fields_of(Order))
    assert properties["id"] == {"format": "uuid", "title": "Id", "type": "string"}
    assert properties["code"] == {"maxLength": 8, "title": "Code", "type": "string"}
    assert properties["customer"]["description"] == "The id of another aggregate."
    assert properties["total"] == {"$ref": "#/$defs/MoneyOut"}
    assert properties["tags"] == {
        "items": {"type": "string"},
        "title": "Tags",
        "type": "array",
    }
    assert properties["lines"]["items"] == {"$ref": "#/$defs/LineOut"}
    assert properties["alias"]["anyOf"][1] == {"type": "null"}
    assert properties["discount"]["anyOf"][0]["type"] == "number"
    assert set(document["$defs"]) == {"LineOut", "MoneyOut", "NoteOut", "Status"}


def test_from_domain_shows_values_plainly_and_sets_in_order() -> None:
    """Value objects unwrapped, sets sorted, children nested, None kept."""
    schema = schema_for(Order)

    shown = schema.from_domain(make_order())  # type: ignore[attr-defined]
    document = shown.model_dump(mode="json")

    assert document["code"] == "A-1"
    assert document["total"] == {"amount": "12.50", "currency": "EUR"}
    assert document["customer"] == str(UUID(int=2))
    assert document["status"] == "open"
    assert (document["tags"], document["labels"]) == (["a", "b"], ["y", "z"])
    assert [line["sku"] for line in document["lines"]] == ["s2", "s1"]
    assert [note["text"] for note in document["notes"]] == ["one", "two"]
    assert document["first"] == {"id": 9, "sku": "s9", "quantity": 9}
    assert (document["paid_at"], document["alias"]) == (None, None)
    assert schema.from_domain(make_order(alias=Code("B"))).alias == "B"  # type: ignore[attr-defined]


def test_write_schemas_take_defaults_and_refuse_what_they_do_not_know() -> None:
    """No id, version, or deleted_at; defaults as the domain says; extras refused."""
    schema = schema_for(Order, write=True)
    document = schema.model_json_schema()

    assert schema.__name__ == "OrderIn"
    assert "id" not in document["properties"]
    assert "version" not in document["properties"]
    assert document["required"] == ["code", "total", "customer", "placed_on"]
    assert document["properties"]["status"]["default"] == "open"
    assert document["additionalProperties"] is False
    body = schema.model_validate(
        {
            "code": "X",
            "total": {"amount": "1.5", "currency": "EUR"},
            "customer": str(UUID(int=3)),
            "placed_on": "2026-01-02",
            "tags": ["a", "a"],
        }
    )
    assert body.status is Status.OPEN  # type: ignore[attr-defined]
    assert body.tags == {"a"}  # type: ignore[attr-defined]
    assert body.extra == {}  # type: ignore[attr-defined]
    with pytest.raises(ValidationError) as refused:
        schema.model_validate({"code": "X" * 9, "unknown": 1})
    kinds = {(error["loc"][0], error["type"]) for error in refused.value.errors()}
    assert ("code", "string_too_long") in kinds
    assert ("unknown", "extra_forbidden") in kinds


def test_write_schemas_bound_integers_and_need_aware_times() -> None:
    """64-bit integers only; a datetime without a time zone is refused."""
    schema = schema_for(Order, write=True, include=["count", "paid_at"])

    assert schema.model_validate({"count": -(2**63), "paid_at": None}).count == -(2**63)  # type: ignore[attr-defined]
    for body in ({"count": 2**63}, {"paid_at": "2026-01-01T00:00:00"}):
        with pytest.raises(ValidationError):
            schema.model_validate(body)
    aware = schema.model_validate({"paid_at": "2026-01-01T00:00:00Z"})
    assert aware.paid_at == datetime(2026, 1, 1, tzinfo=UTC)  # type: ignore[attr-defined]


def test_value_object_defaults_are_given_as_requests_give_them() -> None:
    """A default value object, or one a factory makes, is what it wraps."""

    class Tagged(AggregateRoot[UUID]):
        code: Code = Code("DEF")
        later: Code = field(default_factory=lambda: Code("NEW"))
        when: Status = field(default_factory=lambda: Status.PAID)

    body = schema_for(Tagged, write=True).model_validate({})

    assert (body.code, body.later, body.when) == ("DEF", "NEW", Status.PAID)  # type: ignore[attr-defined]


def test_include_exclude_and_names() -> None:
    """``include`` keeps fields in its order; unknown names are refused."""
    schema = schema_for(Order, include=["status", "code", "id"], exclude=["id"])

    assert list(schema.model_fields) == ["status", "code"]
    assert schema_for(Order, name="Brief", include=["id"]).__name__ == "Brief"
    described = schema_for(Order, include=["code"], descriptions={"code": "Its code."})
    assert described.model_fields["code"].description == "Its code."
    for options in ({"include": ["nope"]}, {"exclude": ["gone"]}):
        with pytest.raises(TypeError, match="Order has no field"):
            schema_for(Order, **options)  # type: ignore[arg-type]


def test_nested_schemas_are_made_once() -> None:
    """Two schemas of one aggregate share their children's schemas."""
    first = schema_for(Order).model_fields["lines"].annotation
    second = schema_for(Order, include=["lines"]).model_fields["lines"].annotation

    assert get_args(first)[0] is get_args(second)[0]


def test_types_without_a_schema_fail_when_derived() -> None:
    """A field pydantic cannot describe is refused at once."""

    class Opaque:
        """No schema for this."""

    class Holder(AggregateRoot[UUID]):
        thing: Opaque

    with pytest.raises(Exception, match="Opaque"):
        schema_for(Holder)
    assert list(schema_for(Holder, exclude=["thing"]).model_fields) == ["id", "version"]


def test_patch_models_default_every_field_to_unset() -> None:
    """Only what the request sent is a change; null is a value, absence is not."""

    class OrderPatch(PatchModel):
        note: str | None
        count: int = Field(ge=0)

    empty = OrderPatch.model_validate({})
    cleared = OrderPatch.model_validate({"note": None})

    assert (getattr(empty, "note"), getattr(empty, "count")) == (UNSET, UNSET)  # noqa: B009
    assert empty.changes() == {}
    assert cleared.changes() == {"note": None}
    both = OrderPatch.model_validate({"count": 2, "note": "x"})
    assert both.changes() == {"note": "x", "count": 2}
    assert "default" not in str(OrderPatch.model_json_schema())
    assert OrderPatch.model_json_schema().get("required") is None
    for body in ({"count": None}, {"count": -1}, {"other": 1}):
        with pytest.raises(ValidationError):
            OrderPatch.model_validate(body)

    class Wider(OrderPatch):
        extra: str

    assert Wider.model_validate({}).changes() == {}
    assert getattr(Wider.model_validate({}), "extra") is UNSET  # noqa: B009 - typed as str.
    assert Wider.model_validate({"extra": "x"}).changes() == {"extra": "x"}


def test_patch_models_refuse_defaults_of_their_own() -> None:
    """A default would be indistinguishable from a change."""
    with pytest.raises(
        TypeError, match=r"Bad\.note: a PatchModel field has no default"
    ):

        class Bad(PatchModel):
            note: str = "x"


def test_partial_schemas_are_patch_models() -> None:
    """``partial=True`` gives a ``PatchModel`` of the write fields."""
    schema = schema_for(Order, partial=True, include=["code", "alias"])

    assert issubclass(schema, PatchModel)
    assert schema.__name__ == "OrderPatch"
    assert schema.model_validate({"alias": None}).changes() == {"alias": None}
    with pytest.raises(ValidationError):
        schema.model_validate({"code": None})


def test_unset_is_one_false_object() -> None:
    """Copies and pickles of ``UNSET`` are ``UNSET``."""
    assert Unset() is UNSET
    assert not UNSET
    assert repr(UNSET) == "UNSET"
    assert copy.copy(UNSET) is UNSET
    assert copy.deepcopy([UNSET])[0] is UNSET
    assert pickle.loads(pickle.dumps(UNSET)) is UNSET  # noqa: S301 - our own bytes.


def test_references_are_ids_marked_as_references() -> None:
    """``Reference[XId]`` is the id's type, marked; aliases expand."""
    fields = fields_of(Order)

    assert (fields["customer"].kind, fields["customer"].reference) == (UUID, True)
    assert fields["code"].reference is False
    assert unaliased(int) is int

    type Plain = Annotated[str, MaxLength(3)]
    assert unaliased(Plain) == Annotated[str, MaxLength(3)]


def test_entity_tags_carry_the_version_and_a_digest() -> None:
    """Weak tags: the version, then the representation's digest."""
    tag = entity_tag(3, b'{"a":1}')

    assert tag.startswith('W/"3-')
    assert len(tag) == len('W/"3-"') + 16
    assert tag == entity_tag(3, b'{"a":1}')
    assert tag != entity_tag(3, b'{"a":2}')
    assert str(EntityTag("x")) == '"x"'
    assert str(EntityTag("x", weak=True)) == 'W/"x"'


@pytest.mark.parametrize(
    ("header", "tags"),
    [
        ("*", []),
        (" * ", []),
        ('"a"', [EntityTag("a")]),
        ('W/"a", "b"', [EntityTag("a", weak=True), EntityTag("b")]),
        ('""', [EntityTag("")]),
        ("a", None),
        ('"a" "b"', None),
        ('"a",', None),
        ('"a\\"b"', None),
        ('w/"a"', None),
        (", ".join(['"t"'] * 21), None),
    ],
)
def test_tag_lists_are_parsed_strictly(
    header: str, tags: list[EntityTag] | None
) -> None:
    """``*``, or quoted tags with an optional ``W/``, at most twenty."""
    assert parse_tags(header) == tags
    assert parse_tags(", ".join(['"t"'] * 20)) == [EntityTag("t")] * 20


def test_if_none_match_compares_weakly() -> None:
    """``W/"x"`` and ``"x"`` match each other; malformed values match nothing."""
    etag = 'W/"3-abc"'

    assert none_match('"3-abc"', etag)
    assert none_match('W/"x", W/"3-abc"', etag)
    assert none_match("*", etag)
    assert not none_match(None, etag)
    assert not none_match("", etag)
    assert not none_match('"3-ab"', etag)
    assert not none_match("3-abc", etag)


@pytest.mark.parametrize(
    ("header", "versions"),
    [
        ("*", None),
        ('W/"3-0123456789abcdef"', {3}),
        ('"3"', {3}),
        ('"3", "4-ff"', {3, 4}),
        ('"x3"', set()),
        ('"3-XYZ"', set()),
        ('"٣"', set()),
        ('"1234567890123456789"', set()),
        ("garbage", set()),
    ],
)
def test_if_match_names_versions(header: str, versions: set[int] | None) -> None:
    """Tags carry versions; one this API did not make carries none."""
    expected = expected_versions(header)

    assert expected == (None if versions is None else frozenset(versions))


@pytest.mark.anyio
async def test_expectations_are_checked_once_when_first_loaded() -> None:
    """The first load checks; later loads, after the use case's own changes, do not."""
    order = make_order(version=3)
    other = make_order(id=OrderId(uuid4()), version=1)
    repository = InMemoryRepository[Order, OrderId]([order, other])

    with expect_version(Order, order.id, {3}) as expected:
        assert not expected.checked
        await repository.get(other.id)
        assert not expected.checked
        assert await repository.filter(F("version") == 3) == [order]
        assert expected.checked
        order.version = 4
        assert await repository.get(order.id) is order
    with (
        expect_version(Order, order.id, {3}),
        pytest.raises(PreconditionFailed) as stale,
    ):
        await repository.get(order.id)
    with expect_version(Order, order.id, None) as anything:
        await repository.get(order.id)
    with expect_version(Line, order.id, {9}) as unrelated:
        await repository.get(order.id)

    assert stale.value.code == "precondition_failed"
    assert anything.checked
    assert not unrelated.checked
    check_expected(order)  # Outside any expectation: nothing to check.


@pytest.mark.anyio
async def test_stale_aggregates_are_refused_by_filters_too() -> None:
    """A query that loads the aggregate checks it as ``get`` does."""
    order = make_order(version=2)
    repository = InMemoryRepository[Order, OrderId]([order])

    with expect_version(Order, order.id, {1}), pytest.raises(PreconditionFailed):
        await repository.first(F("id") == order.id)


def test_field_policies_name_only_known_fields() -> None:
    """A policy is checked against the schema it guards."""
    policy = FieldPolicy(read_only=["code"], visible={"count": "x.y"})

    policy.check(["code", "count"], "OrderOut")
    with pytest.raises(TypeError, match=r"OrderOut lacks: \['count'\]"):
        policy.check(["code"], "OrderOut")
    with pytest.raises(ValueError, match="read-only and editable: code, count"):
        FieldPolicy(
            read_only=["code", "count"], editable={"code": "x.y", "count": "x.z"}
        )


def test_field_policies_tell_openapi_what_they_do() -> None:
    """Fields some callers do not see are optional, and say who sees them."""

    class Out(BaseModel):
        name: str = Field(max_length=3)
        cost: int = Field(ge=0, description="What it cost.")
        margin: int

    policy = FieldPolicy(visible={"cost": "x.view_cost", "margin": "x.view_margin"})
    documented = policy.document(Out)
    schema = documented.model_json_schema()

    assert documented.__name__ == "Out"
    assert schema["required"] == ["name"]
    assert schema["properties"]["cost"] == {
        "description": "What it cost. Shown only to users granted x.view_cost.",
        "minimum": 0,
        "title": "Cost",
        "type": "integer",
    }
    assert schema["properties"]["margin"]["description"] == (
        "Shown only to users granted x.view_margin."
    )
    assert FieldPolicy().document(Out) is Out
    assert FieldPolicy().describe_changes(Out) is Out


def test_schemas_keep_the_types_inside_containers() -> None:
    """Lists, dicts, and sets keep their items' types; sets show sorted."""
    properties = schema_for(Order).model_json_schema()["properties"]

    assert properties["sizes"]["items"] == {"type": "integer"}
    assert properties["extra"]["additionalProperties"] == {"type": "string"}
    assert properties["mixed"]["items"] == {
        "anyOf": [{"type": "integer"}, {"type": "string"}]
    }
    assert properties["maybe"]["anyOf"][0]["items"] == {"type": "string"}
    shown = schema_for(Order).from_domain(make_order())  # type: ignore[attr-defined]
    assert (shown.mixed, shown.maybe) == ([1, 2, "a", "b"], ["x", "y"])
    assert schema_for(Order).from_domain(make_order(maybe=None)).maybe is None  # type: ignore[attr-defined]
    written = schema_for(Order, write=True, include=["mixed"]).model_validate(
        {"mixed": [1, "1", 1]}
    )
    assert written.mixed == {1, "1"}  # type: ignore[attr-defined]


def test_children_in_sets_show_in_the_order_of_their_ids() -> None:
    """However the set iterates, the representation is the same."""
    notes = {Note(id=number, text=str(number)) for number in (5, 3, 6, 1, 4, 2)}

    shown = schema_for(Order).from_domain(make_order(notes=notes))  # type: ignore[attr-defined]

    assert [note.id for note in shown.notes] == [1, 2, 3, 4, 5, 6]


def test_write_schemas_bound_amounts_and_integers_both_ways() -> None:
    """An amount has its digits, an integer fits 64 bits, low and high."""
    schema = schema_for(Order, write=True, include=["discount", "count"])

    assert schema.model_validate({"discount": "12.34"}).discount == Decimal("12.34")  # type: ignore[attr-defined]
    for body in (
        {"discount": "123.4"},
        {"discount": "1.234"},
        {"count": -(2**63) - 1},
        {"count": 2**63},
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(body)


def test_unknown_fields_are_named_together() -> None:
    """Every unknown name is listed in the refusal."""
    with pytest.raises(TypeError, match=r"^Order has no field gone, nope\.$"):
        schema_for(Order, include=["nope"], exclude=["gone"])


class Loose(AggregateRoot[UUID]):
    """Sets hinted loosely: without their items' type, or None first."""

    anything: set  # type: ignore[type-arg]
    later: None | set[str] = None  # noqa: RUF036 - None first is the point.


def test_loosely_hinted_sets_show_as_lists() -> None:
    """A set without an item type holds anything; None may come first."""
    properties = schema_for(Loose).model_json_schema()["properties"]

    assert properties["anything"] == {"items": {}, "title": "Anything", "type": "array"}
    assert properties["later"]["anyOf"][0]["items"] == {"type": "string"}


def test_write_schemas_nest_write_schemas_of_children() -> None:
    """A child in a write schema takes the child's write schema, by its name."""
    schema = schema_for(Order, write=True, include=["lines"]).model_json_schema()

    assert schema["properties"]["lines"]["items"] == {"$ref": "#/$defs/LineIn"}
