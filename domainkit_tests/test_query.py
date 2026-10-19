"""Queries in the domain: fields, criteria, ordering, specifications, positions."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal, NewType
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from domainkit.ddd import ValueObject
from domainkit.ddd.fields import (
    Digits,
    FieldInfo,
    InvalidQuery,
    MaxLength,
    UnknownField,
    base_type,
    describe,
    fields_of,
    primitive,
    type_arguments,
    wrapped_field,
)
from domainkit.ddd.query import (
    And,
    Condition,
    Criteria,
    F,
    Lookup,
    Not,
    Or,
    Ordering,
    Query,
    RowLock,
    Specification,
    as_criteria,
    as_ordering,
    as_query,
    following,
    order,
    position_of,
    total_ordering,
)
from domainkit_tests.gadgets import (
    MADE,
    Color,
    Gadget,
    GadgetId,
    Label,
    LabelTooLong,
    Maker,
    Part,
    Size,
    make_gadget,
    make_maker,
    make_part,
)

LATER = datetime(2026, 2, 1, tzinfo=UTC)


class Pair(ValueObject):
    """A value object of two fields: it sorts and searches as nothing."""

    left: int
    right: int


class Wrapper(ValueObject):
    """A value object that wraps another."""

    label: Label


ThingId = NewType("ThingId", int)


@dataclass
class Thing:
    """A plain dataclass with hints of every awkward kind."""

    id: ThingId
    either: int | str
    tags: list[str]
    mode: Literal["a", "b"]
    mapping: dict[str, int]
    anything: Any
    parts: tuple[Part, ...]
    pair: Pair
    wrapped: Wrapper
    count: Annotated[int | None, MaxLength(3)]
    later: None | int  # noqa: RUF036 - None first, on purpose.
    noted: Annotated[str, ["unhashable", "metadata"]]
    on: date
    at: time


def matching(criteria: Criteria, *candidates: Gadget) -> list[str]:
    """The names of the candidates ``criteria`` matches."""
    return [each.name for each in candidates if criteria.matches(each)]


def test_fields_are_read_from_the_hints() -> None:
    """Types, nullability, metadata, and relations come from the hints."""
    fields = fields_of(Gadget)

    assert list(fields)[:4] == ["id", "version", "deleted_at", "name"]
    assert fields["id"].kind is UUID
    assert fields["id"].nullable is False
    assert fields["deleted_at"].kind is datetime
    assert fields["deleted_at"].nullable is True
    assert fields["name"].marker(MaxLength) == MaxLength(30)
    assert fields["name"].marker(Digits) is None
    assert fields["price"].marker(Digits) == Digits(10, 2)
    assert fields["stock"].kind is int
    assert fields["stock"].nullable is True
    assert fields["label"].kind is Label
    assert fields["parts"].many is True
    assert fields["parts"].entity is Part
    assert fields["maker"].entity is Maker
    assert fields["maker"].many is False
    assert fields["maker"].nullable is True
    assert fields["name"].entity is None
    assert fields["name"].owner is Gadget


def test_awkward_hints_are_described_as_well_as_they_can_be() -> None:
    """Unions, literals, and containers of values are types, or unknown."""
    fields = fields_of(Thing)

    assert fields["id"].kind is int
    assert fields["either"].kind is None
    assert fields["tags"].kind is list
    assert fields["tags"].many is False
    assert fields["mode"].kind is None
    assert fields["mapping"].kind is dict
    assert fields["anything"].kind is None
    assert fields["parts"].entity is Part
    assert fields["parts"].many is True
    assert fields["count"].kind is int
    assert fields["count"].nullable is True
    assert fields["count"].metadata == (MaxLength(3),)
    assert (fields["later"].kind, fields["later"].nullable) == (int, True)
    assert fields["noted"].metadata == (["unhashable", "metadata"],)
    union = describe(Thing, "x", Annotated[int, MaxLength(2)] | None)
    assert (union.kind, union.nullable, union.metadata) == (int, True, (MaxLength(2),))


def test_what_sorts_and_what_is_text() -> None:
    """Text is str or what wraps it; enums sort but are not text."""
    fields = {**fields_of(Thing), **fields_of(Gadget)}

    assert [
        name for name in ("name", "label", "color", "price") if fields[name].text
    ] == [
        "name",
        "label",
    ]
    assert fields["wrapped"].text is True
    assert fields["wrapped"].orderable is True
    assert fields["anything"].orderable is True
    assert fields["anything"].text is False
    assert fields["parts"].orderable is False
    assert fields["tags"].orderable is False
    assert fields["pair"].orderable is False
    assert fields["color"].orderable is True
    assert fields["on"].orderable is True
    assert fields["at"].orderable is True
    assert FieldInfo("x", Thing, None, many=True).orderable is False


def test_only_dataclasses_have_fields() -> None:
    """Anything else is refused."""
    with pytest.raises(TypeError, match="not a dataclass"):
        fields_of(int)
    with pytest.raises(TypeError, match="not a dataclass"):
        fields_of(Label("a"))


def test_type_parameters_bind_through_generic_bases() -> None:
    """``Entity.id`` is the id type the aggregate names, two bases up."""
    bound = type_arguments(Gadget)

    assert GadgetId in bound.values()
    assert type_arguments(Thing) == {}


def test_limits_are_checked() -> None:
    """A length is positive; a scale fits its precision."""
    for wrong in (0, -1):
        with pytest.raises(ValueError, match="at least 1"):
            MaxLength(wrong)
    for precision, scale in ((2, 3), (0, 0), (3, -1)):
        with pytest.raises(ValueError, match="Digits needs"):
            Digits(precision, scale)
    assert Digits(1, 0).scale == 0
    assert Digits(3, 3).precision == 3
    assert MaxLength(1).length == 1


def test_values_are_coerced_to_their_field() -> None:
    """Members, wrapped values, and amounts are taken as the field holds them."""
    fields = fields_of(Gadget)

    assert fields["color"].coerce("blue") is Color.BLUE
    assert fields["color"].coerce(Color.RED) is Color.RED
    assert fields["size"].coerce(10) is Size.LARGE
    assert fields["label"].coerce("A") == Label("A")
    assert fields["label"].coerce(Label("B")) == Label("B")
    assert fields["price"].coerce(3) == 3
    assert fields["price"].coerce(Decimal("1.5")) == Decimal("1.5")
    assert fields["price"].coerce(Decimal("1E+400")) == Decimal("1E+400")  # Finite.
    assert fields["stock"].coerce(None) is None
    assert fields["active"].coerce(True) is True
    assert fields_of(Thing)["anything"].coerce(object) is object
    assert fields_of(Thing)["wrapped"].coerce("a") == Wrapper(Label("a"))
    assert fields_of(Thing)["on"].coerce(date(2026, 1, 1)) == date(2026, 1, 1)
    assert fields["made_at"].coerce(MADE) == MADE


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("stock", True, r"Gadget.stock holds int, not bool"),
        ("stock", "3", r"Gadget.stock holds int, not str"),
        ("price", 1.5, r"holds Decimal, not float"),
        ("price", Decimal("NaN"), "cannot be compared with NaN"),
        ("price", Decimal("Infinity"), "cannot be compared with Infinity"),
        ("color", "purple", r"Gadget.color holds Color, not str"),
        ("made_at", datetime(2026, 1, 1), "timezone-aware"),  # noqa: DTZ001 - refused.
        ("made_at", "2026-01-01", "holds datetime, not str"),
        ("label", 5, "Label.value holds str, not int"),
    ],
)
def test_values_of_the_wrong_kind_are_refused(
    name: str, value: object, message: str
) -> None:
    """The message names the field and the types, never the value."""
    with pytest.raises(InvalidQuery, match=message):
        fields_of(Gadget)[name].coerce(value)


def test_dates_and_numbers_keep_apart() -> None:
    """A datetime is no date, and infinite floats compare with nothing."""
    with pytest.raises(InvalidQuery, match="holds date, not datetime"):
        fields_of(Thing)["on"].coerce(MADE)
    floats = FieldInfo("ratio", Thing, float)
    assert floats.coerce(0.5) == 0.5
    with pytest.raises(InvalidQuery, match="cannot be compared with inf"):
        floats.coerce(float("inf"))
    flags = FieldInfo("flag", Thing, bool)
    with pytest.raises(InvalidQuery, match="holds bool, not int"):
        flags.coerce(1)


def test_value_objects_are_compared_by_what_they_wrap() -> None:
    """Enums by value, value objects unwrapped, others as they are."""
    assert primitive(Color.RED) == "red"
    assert primitive(Wrapper(Label("x"))) == "x"
    assert primitive(Pair(1, 2)) == Pair(1, 2)
    assert primitive(None) is None
    assert base_type(Wrapper) is str
    assert base_type(Pair) is Pair
    assert base_type(None) is None
    assert wrapped_field(Pair) is None
    assert wrapped_field(int) is None
    assert wrapped_field(Label) == fields_of(Label)["value"]


def test_field_names_must_be_names() -> None:
    """Each part of a dotted name is an identifier."""
    for wrong in ("", "a..b", "1a", "a b", ".a", "a."):
        with pytest.raises(InvalidQuery, match="is not a field name"):
            F(wrong)
    assert F("parts.sku").name == "parts.sku"
    assert repr(F("name")) == "F('name')"


def test_expressions_build_conditions() -> None:
    """Each operator and method makes the condition Django would."""
    name = F("name")
    cases: list[tuple[Criteria, Lookup, object]] = [
        (name == "a", Lookup.EXACT, "a"),
        (name < "a", Lookup.LT, "a"),
        (name <= "a", Lookup.LTE, "a"),
        (name > "a", Lookup.GT, "a"),
        (name >= "a", Lookup.GTE, "a"),
        (name.in_(["a", "b"]), Lookup.IN, ("a", "b")),
        (name.range("a", "b"), Lookup.RANGE, ("a", "b")),
        (name.between("a", "b"), Lookup.RANGE, ("a", "b")),
        (name.isnull(), Lookup.ISNULL, True),
        (name.isnull(False), Lookup.ISNULL, False),
        (name.iexact("a"), Lookup.IEXACT, "a"),
        (name.contains("a"), Lookup.CONTAINS, "a"),
        (name.icontains("a"), Lookup.ICONTAINS, "a"),
        (name.startswith("a"), Lookup.STARTSWITH, "a"),
        (name.istartswith("a"), Lookup.ISTARTSWITH, "a"),
        (name.endswith("a"), Lookup.ENDSWITH, "a"),
        (name.iendswith("a"), Lookup.IENDSWITH, "a"),
        (name.lookup("gte", "a"), Lookup.GTE, "a"),
    ]
    for condition, lookup, value in cases:
        assert condition == Condition("name", lookup, value)
    assert (name != "a") == Not(Condition("name", Lookup.EXACT, "a"))
    assert (1 < F("stock")) == Condition("stock", Lookup.GT, 1)  # noqa: SIM300 - reflected.
    with pytest.raises(
        InvalidQuery, match=r"^in_ takes a collection of values, not one string\.$"
    ):
        name.in_("ab")
    with pytest.raises(InvalidQuery, match="not one string"):
        name.in_(b"ab")
    with pytest.raises(TypeError, match="unhashable"):
        hash(name)


def test_criteria_combine_and_flatten() -> None:
    """``&`` and ``|`` flatten their own kind; ``~~`` cancels."""
    a, b, c = F("name") == "a", F("name") == "b", F("name") == "c"

    assert (a & b) & c == And((a, b, c))
    assert a & (b & c) == And((a, b, c))
    assert (a | b) | c == Or((a, b, c))
    assert (a & b) | c == Or((And((a, b)), c))
    assert ~a == Not(a)
    assert ~~a == a
    assert And.of(a) is a
    assert Or.of(a) is a
    assert list((~(a & b) | c).conditions()) == [a, b, c]
    with pytest.raises(
        TypeError, match=r"^Combine criteria with &, \| and ~, not with and, or, not\.$"
    ):
        bool(a)
    with pytest.raises(TypeError, match="Combine criteria with &"):
        a and b  # noqa: B018 - the mistake being refused.


def test_criteria_check_fields_and_values_when_bound() -> None:
    """Unknown fields, wrong lookups, and wrong values fail loudly."""
    cases: list[tuple[Criteria, type[Exception], str]] = [
        (F("nope") == 1, UnknownField, "Gadget has no field 'nope'"),
        (F("parts.nope") == 1, UnknownField, "Part has no field 'nope'"),
        (F("name.upper") == 1, InvalidQuery, "name holds no child entities"),
        (F("parts") == 1, InvalidQuery, "holds child entities; name one of"),
        (F("maker") == 1, InvalidQuery, "holds child entities"),
        (
            F("stock").lookup("isnull", 1),
            InvalidQuery,
            r"^isnull takes True or False, not int\.$",
        ),
        (F("price").contains("1"), InvalidQuery, "price is not text"),
        (F("color").icontains("r"), InvalidQuery, "color is not text"),
        (F("name").contains(1), InvalidQuery, "contains takes text, not int"),  # type: ignore[arg-type]
        (F("stock") > None, InvalidQuery, "gt on stock takes values, not None"),
        (F("stock").in_([1, None]), InvalidQuery, "in on stock takes values"),
        (F("stock").range(1, None), InvalidQuery, "range on stock takes values"),
        (
            F("stock").lookup("range", (1, 2, 3)),
            InvalidQuery,
            r"^range takes a low and a high value\.$",
        ),
        (F("stock").lookup("in", [1]), InvalidQuery, "in on stock takes values"),
        (F("stock") > "1", InvalidQuery, "holds int, not str"),
    ]
    for criteria, error, message in cases:
        with pytest.raises(error, match=message):
            criteria.bind(Gadget)
    thing = fields_of(Thing)
    with pytest.raises(InvalidQuery, match="tags cannot be compared by gt"):
        Condition("tags", Lookup.GT, ["a"]).bind(Thing)
    with pytest.raises(InvalidQuery, match="pair cannot be compared by lte"):
        (F("pair") <= Pair(1, 2)).bind(Thing)
    assert thing["pair"].coerce(Pair(1, 2)) == Pair(1, 2)
    with pytest.raises(
        InvalidQuery, match=r"^Bind the criteria to a class before evaluating them\.$"
    ):
        (F("name") == "a").evaluate(make_gadget())


def test_binding_coerces_values() -> None:
    """Bound criteria hold the values as the fields hold them."""
    bound = (
        (F("color") == "red") & F("size").in_([10]) & ~F("label").range("A", "B")
    ).bind(Gadget)

    assert isinstance(bound, And)
    first, second, third = bound.parts
    assert isinstance(first, Condition)
    assert first.value is Color.RED
    assert first.path == (fields_of(Gadget)["color"],)
    assert isinstance(second, Condition)
    assert second.value == (Size.LARGE,)
    assert isinstance(third, Not)
    assert isinstance(third.part, Condition)
    assert third.part.value == (Label("A"), Label("B"))


def test_conditions_on_missing_values_are_false_and_negate_exactly() -> None:
    """Two-valued logic: ``~`` includes what a condition leaves out."""
    none = make_gadget("none", stock=None)
    some = make_gadget("some", stock=5)

    assert matching(F("stock") > 1, none, some) == ["some"]
    assert matching(~(F("stock") > 1), none, some) == ["none"]
    assert matching(F("stock") == None, none, some) == ["none"]  # noqa: E711
    assert matching(F("stock") != None, none, some) == ["some"]  # noqa: E711
    assert matching(F("stock") != 5, none, some) == ["none"]
    assert matching(F("stock") == 5, none, some) == ["some"]
    assert matching(F("stock").isnull(), none, some) == ["none"]
    assert matching(F("stock").isnull(False), none, some) == ["some"]
    assert matching(F("stock").in_([5, 6]), none, some) == ["some"]
    assert matching(~F("stock").in_([5]), none, some) == ["none"]
    assert matching(F("stock").in_([]), none, some) == []
    assert matching(~F("stock").in_([]), none, some) == ["none", "some"]


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        (F("price") < Decimal("2"), ["cheap"]),
        (F("price") <= Decimal("2.00"), ["cheap", "mid"]),
        (F("price") > Decimal("2"), ["dear"]),
        (F("price") >= 2, ["mid", "dear"]),
        (F("price") == Decimal("2.000"), ["mid"]),
        (F("price").range(Decimal("1.5"), 2), ["cheap", "mid"]),
        (F("price").range(3, 1), []),
        (F("color") == "blue", ["mid"]),
        (F("color").in_(["red", Color.GREEN]), ["cheap", "dear"]),
        (F("size") > Size.SMALL, ["dear"]),
        (F("label") == "MID", ["mid"]),
        (F("label") > Label("CHEAP"), ["mid", "dear"]),
        (F("made_at") > MADE, ["dear"]),
        (F("active") == False, ["mid"]),  # noqa: E712
        (F("name").iexact("MiD"), ["mid"]),
        (F("name").contains("ea"), ["cheap", "dear"]),
        (F("name").contains("EA"), []),
        (F("name").icontains("EA"), ["cheap", "dear"]),
        (F("name").startswith("d"), ["dear"]),
        (F("name").istartswith("D"), ["dear"]),
        (F("name").startswith("D"), []),
        (F("name").endswith("p"), ["cheap"]),
        (F("name").iendswith("P"), ["cheap"]),
        (F("name").endswith("P"), []),
        (F("name").contains(""), ["cheap", "mid", "dear"]),
        (F("label").icontains("ea"), ["cheap", "dear"]),
        (F("parts.sku") == "A", ["cheap", "mid"]),
        (F("parts.weight") > 4, ["mid"]),
        (~(F("parts.sku") == "A"), ["dear"]),
        (F("parts.sku").isnull(), []),
        (F("maker.name") == "acme", ["cheap"]),
        (F("maker.name").isnull(False), ["cheap"]),
        (~(F("maker.name") == "acme"), ["mid", "dear"]),
    ],
)
def test_lookups_match_as_sql_does(criteria: Criteria, expected: list[str]) -> None:
    """Every lookup, on every kind of field, in Python."""
    cheap = make_gadget(
        "cheap",
        price="1.50",
        parts=[make_part("A", 1)],
        maker=make_maker("acme"),
    )
    mid = make_gadget(
        "mid",
        price="2.00",
        color=Color.BLUE,
        active=False,
        parts=[make_part("B", 1), make_part("A", 5)],
    )
    dear = make_gadget(
        "dear", price="9.99", color=Color.GREEN, size=Size.LARGE, made_at=LATER
    )

    assert matching(criteria, cheap, mid, dear) == expected


def test_orderings_are_parsed_and_checked() -> None:
    """``-name`` is descending; fields must exist and sort."""
    assert Ordering.parse("-name") == Ordering("name", descending=True)
    assert Ordering.parse("name") == Ordering("name")
    assert str(Ordering("name", descending=True)) == "-name"
    assert str(Ordering("name")) == "name"
    assert F("name").asc() == Ordering("name")
    assert F("name").desc() == Ordering("name", descending=True)
    for wrong in ("--name", "a.b", "", "-"):
        with pytest.raises(InvalidQuery, match="is not an ordering"):
            Ordering.parse(wrong)
    with pytest.raises(UnknownField, match="no field 'nope'"):
        Ordering("nope").bind(Gadget)
    with pytest.raises(InvalidQuery, match="'parts' holds child entities"):
        Ordering("parts").bind(Gadget)
    with pytest.raises(InvalidQuery, match=r"Thing\.pair cannot be ordered by"):
        Ordering("pair").bind(Thing)
    assert Ordering("price").bind(Gadget) == Ordering("price")


def test_helpers_accept_what_they_document() -> None:
    """Orderings from three forms; criteria from criteria or specifications."""
    assert as_ordering(Ordering("a")) == Ordering("a")
    assert as_ordering(F("a")) == Ordering("a")
    assert as_ordering("-a") == Ordering("a", descending=True)
    with pytest.raises(TypeError, match="Expected an ordering, not int"):
        as_ordering(1)  # type: ignore[arg-type]
    criteria = F("name") == "a"
    assert as_criteria(criteria) is criteria
    assert as_criteria(Cheap()) == Cheap().to_criteria()
    with pytest.raises(
        TypeError, match="Expected criteria or a specification, not str"
    ):
        as_criteria("name")  # type: ignore[arg-type]
    query = Query(criteria)
    assert as_query(query) is query
    assert as_query(criteria) == Query(criteria)
    assert as_query(None) == Query()


def test_queries_check_their_bounds() -> None:
    """Limits and offsets are not negative; a query skips or continues, not both."""
    with pytest.raises(ValueError, match="limit cannot be negative"):
        Query(limit=-1)
    with pytest.raises(ValueError, match="offset cannot be negative"):
        Query(offset=-1)
    with pytest.raises(
        ValueError, match=r"^A query continues after a position or skips, not both\.$"
    ):
        Query(offset=1, after=("a",))
    query = Query(Cheap(), ordering=["-name", F("price"), Ordering("id")], limit=0)
    assert query.ordering == (
        Ordering("name", descending=True),
        Ordering("price"),
        Ordering("id"),
    )
    assert query.criteria == Cheap().to_criteria()
    assert (query.limit, query.offset, query.after, query.lock) == (0, 0, None, None)
    assert query.include_deleted is False
    assert Query(after=["a", 1]).after == ("a", 1)
    assert Query(offset=0, after=()).after == ()


def test_queries_are_refined_by_copies() -> None:
    """``where`` adds criteria; ``replace`` changes any field."""
    base = Query(F("name") == "a", ordering=["name"])
    narrower = base.where(F("stock") > 1, Cheap())

    assert narrower.criteria == And(
        (F("name") == "a", F("stock") > 1, *Cheap().to_criteria().parts)  # type: ignore[attr-defined]
    )
    assert narrower.ordering == base.ordering
    assert Query().where(F("a") == 1).criteria == (F("a") == 1)
    assert Query().where() == Query()
    assert base.where() is base
    changed = base.replace(limit=5, include_deleted=True, lock=RowLock(nowait=True))
    assert (changed.limit, changed.include_deleted) == (5, True)
    assert changed.lock == RowLock(nowait=True)
    assert changed.criteria == base.criteria
    assert base.limit is None


def test_row_locks_wait_skip_or_refuse() -> None:
    """Not both of nowait and skip_locked."""
    assert RowLock() == RowLock(nowait=False, skip_locked=False)
    with pytest.raises(
        ValueError, match=r"^A row lock takes nowait or skip_locked, not both\.$"
    ):
        RowLock(nowait=True, skip_locked=True)


class Cheap(Specification[Gadget]):
    """Gadgets under 2.00."""

    def to_criteria(self) -> Criteria:
        """Price below two."""
        return (F("price") < 2) & F("active").isnull(False)


class Labelled(Specification[Gadget]):
    """A rule decided in Python, kept in step with its criteria."""

    def __init__(self) -> None:
        """Count the Python checks."""
        self.checked = 0

    def to_criteria(self) -> Criteria:
        """A label starting with C."""
        return F("label").startswith("C")

    def is_satisfied_by(self, candidate: Gadget) -> bool:
        """Checked in Python."""
        self.checked += 1
        return candidate.label.value.startswith("C")


def test_specifications_name_rules_for_python_and_queries() -> None:
    """A specification tests one aggregate, and composes like criteria."""
    cheap = make_gadget("cheap", price="1.00")
    dear = make_gadget("dear", price="5.00")
    labelled = Labelled()

    assert Cheap().is_satisfied_by(cheap)
    assert not Cheap().is_satisfied_by(dear)
    assert (~Cheap()).is_satisfied_by(dear)
    assert not (~Cheap()).is_satisfied_by(cheap)
    assert (Cheap() & labelled).is_satisfied_by(cheap)
    assert not (Cheap() & labelled).is_satisfied_by(dear)
    assert (Cheap() | (F("name") == "dear")).is_satisfied_by(dear)
    assert not (Cheap() | labelled).is_satisfied_by(dear)
    assert labelled.checked == 2  # Each composite stops at its answer.
    assert (Cheap() & labelled).to_criteria() == And(
        (*Cheap().to_criteria().parts, F("label").startswith("C"))  # type: ignore[attr-defined]
    )
    assert (Cheap() | labelled).to_criteria() == Or(
        (Cheap().to_criteria(), F("label").startswith("C"))
    )
    assert (~labelled).to_criteria() == Not(F("label").startswith("C"))
    assert repr(Cheap()) == "Cheap"
    assert repr(Cheap() & labelled) == "(Cheap & Labelled)"
    assert repr(Cheap() | labelled) == "(Cheap | Labelled)"
    assert repr(~Cheap()) == "~Cheap"
    assert repr(Cheap() & (F("name") == "a")).startswith(
        "(Cheap & Condition(field='name'"
    )
    assert ((F("name") == "a") & Cheap()) == And(
        (F("name") == "a", *Cheap().to_criteria().parts)  # type: ignore[attr-defined]
    )
    assert ((F("name") == "a") | Cheap()) == Or(
        (F("name") == "a", Cheap().to_criteria())
    )


def test_orderings_end_with_the_id() -> None:
    """The id breaks ties; orderings stop at it."""
    assert total_ordering([]) == (Ordering("id"),)
    assert total_ordering([Ordering("name")]) == (Ordering("name"), Ordering("id"))
    assert total_ordering([Ordering("id", True), Ordering("name")]) == (
        Ordering("id", True),
    )
    assert total_ordering([Ordering("a"), Ordering("id"), Ordering("b")]) == (
        Ordering("a"),
        Ordering("id"),
    )


def test_missing_values_sort_last_ascending_and_first_descending() -> None:
    """``order`` sorts as ``NULLS LAST`` / ``NULLS FIRST``, stably by key."""
    a = make_gadget("a", stock=2, color=Color.RED)
    b = make_gadget("b", stock=None, color=Color.BLUE)
    c = make_gadget("c", stock=1, color=Color.RED)
    d = make_gadget("d", stock=None, color=Color.GREEN)

    def names(*ordering: str) -> str:
        return "".join(
            each.name for each in order([a, b, c, d], [*map(Ordering.parse, ordering)])
        )

    assert names("stock", "name") == "cabd"
    assert names("-stock", "name") == "bdac"
    assert names("color", "name") == "bdac"
    assert names("-color", "-name") == "cadb"
    assert names("label") == "abcd"
    assert names() == "abcd"
    assert position_of(a, [Ordering("stock"), Ordering("name")]) == (2, "a")


def test_positions_must_fit_their_ordering() -> None:
    """One value per field; a position past every missing value is refused."""
    ordering = (Ordering("stock"), Ordering("id"))
    with pytest.raises(InvalidQuery, match=r"needs 2 values for stock, id, not 1"):
        following(Gadget, ordering, ("a",))
    with pytest.raises(
        InvalidQuery,
        match=r"^Nothing sorts after a position whose values are missing\.$",
    ):
        following(Gadget, (Ordering("stock"),), (None,))
    with pytest.raises(UnknownField):
        following(Gadget, (Ordering("nope"),), (1,))


def test_what_follows_a_position_is_what_sorts_after_it() -> None:
    """Keyset criteria for both directions, with and without missing values."""
    one = make_gadget("one", stock=None, size=Size.LARGE)
    two = make_gadget("two", stock=1, size=Size.SMALL)

    stock_asc = following(Gadget, (Ordering("stock"), Ordering("id")), (None, one.id))
    assert stock_asc == And((F("stock").isnull(), F("id") > one.id))
    stock_desc = following(
        Gadget, (Ordering("stock", True), Ordering("id")), (None, one.id)
    )
    assert stock_desc == Or(
        (F("stock").isnull(False), And((F("stock").isnull(), F("id") > one.id)))
    )
    sized = following(Gadget, (Ordering("size"), Ordering("id")), (Size.SMALL, two.id))
    assert sized == Or(
        (F("size") > Size.SMALL, And((F("size") == Size.SMALL, F("id") > two.id)))
    )
    nullable = following(Gadget, (Ordering("stock"), Ordering("id")), (1, two.id))
    assert nullable == Or(
        (F("stock") > 1, F("stock").isnull(), And((F("stock") == 1, F("id") > two.id)))
    )
    down = following(Gadget, (Ordering("stock", True), Ordering("id")), (1, two.id))
    assert down == Or((F("stock") < 1, And((F("stock") == 1, F("id") > two.id))))


ORDERABLE = ("name", "label", "price", "stock", "active", "color", "size", "made_at")
gadget_values = st.builds(
    make_gadget,
    st.sampled_from(["a", "B", "b", "ab"]),
    price=st.sampled_from(["0.00", "1.50", "-2.25", "10.00"]),
    made_at=st.sampled_from([MADE, LATER]),
    stock=st.one_of(st.none(), st.integers(-1, 2)),
    active=st.booleans(),
    color=st.sampled_from(Color),
    size=st.sampled_from(Size),
)
orderings = st.lists(
    st.builds(Ordering, st.sampled_from(ORDERABLE), st.booleans()),
    max_size=3,
    unique_by=lambda each: each.field,
)


@given(st.lists(gadget_values, max_size=8), orderings)
def test_keyset_criteria_select_exactly_what_follows(
    candidates: list[Gadget], ordering: list[Ordering]
) -> None:
    """For each item, what follows its position is what sorts after it."""
    total = total_ordering(ordering)
    ordered = order(candidates, total)
    for index, item in enumerate(ordered):
        criteria = following(Gadget, total, position_of(item, total)).bind(Gadget)
        assert [each for each in ordered if criteria.evaluate(each)] == ordered[
            index + 1 :
        ]


def test_ids_are_compared_as_uuids() -> None:
    """A gadget's id condition takes the UUID."""
    gadget = make_gadget()

    assert (F("id") == gadget.id).matches(gadget)
    assert not (F("id") == uuid4()).matches(gadget)
    with pytest.raises(LabelTooLong):
        (F("label") == "x" * 21).matches(gadget)
