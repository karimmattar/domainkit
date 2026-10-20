"""Opaque cursors, positions, and pages over HTTP."""

import base64
import inspect
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from domainkit.ddd import (
    AggregateRoot,
    Digits,
    InMemoryRepository,
    InvalidQuery,
    MaxLength,
    Page,
    UnknownField,
    ValueObject,
)
from domainkit.ddd.query import F, Ordering, Query, total_ordering
from domainkit.http import InvalidCursor, decode_cursor, encode_cursor
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import FilterSet
from domainkit.http.pagination import (
    CURSOR_PATTERN,
    CURSOR_VERSION,
    MAX_CURSOR_LENGTH,
    PageOut,
    PageRequest,
    Pagination,
    cursor_length,
    decode_position,
    encode_position,
    longest_ordering,
)
from domainkit_tests.gadgets import Color, InMemoryGadgets, Label, Size, make_gadget


def test_pages_built_by_hand_know_whether_more_follow() -> None:
    """``limit + 1`` fetched items mean more; ``limit`` or fewer do not."""
    assert Page.from_items([1, 2, 3], 2) == Page([1, 2], has_more=True)
    assert Page.from_items([1, 2], 2) == Page([1, 2], has_more=False)


def test_round_trip() -> None:
    """Cursors are URL-safe and decode to the same values."""
    cursor = encode_cursor(["Ünïcode name", "0f1c"])

    assert "=" not in cursor
    assert decode_cursor(cursor, 2) == ["Ünïcode name", "0f1c"]


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 !",
        base64.urlsafe_b64encode(b"{not json").decode(),
        encode_cursor(["one"]),
        base64.urlsafe_b64encode(b'{"a": 1}').decode(),
        base64.urlsafe_b64encode(b'[1, "x"]').decode(),
        "A" * 3000,
    ],
)
def test_forged_cursors_are_rejected(cursor: str) -> None:
    """Anything but a well-formed cursor of the right size is a 422."""
    with pytest.raises(InvalidCursor) as caught:
        decode_cursor(cursor, 2)

    assert (caught.value.status_code, caught.value.code) == (422, "invalid_cursor")
    assert caught.value.detail == "The cursor is invalid; start again without one."


class Single(StrEnum):
    """An enum of one member."""

    ONLY = "only"


class Record(AggregateRoot[UUID]):
    """An aggregate with a field of every kind a cursor holds."""

    text: str
    count: int
    flag: bool
    ratio: float
    amount: Decimal
    when: datetime
    day: date
    moment: time
    ref: UUID
    color: Color
    size: Size
    label: Label
    missing: int | None
    single: Single
    tags: list[str]
    anything: Any = None


ORDER = tuple(
    Ordering(name)
    for name in (
        "text", "count", "flag", "ratio", "amount", "when", "day", "moment", "ref",
        "color", "size", "label", "missing", "single", "id",
    )
)  # fmt: skip
POSITION = (
    "Ünïcode",
    -(2**62),
    True,
    0.25,
    Decimal("-12.340"),
    datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC),
    date(2026, 1, 2),
    time(3, 4, 5),
    UUID(int=7),
    Color.GREEN,
    Size.LARGE,
    Label("x"),
    None,
    Single.ONLY,
    UUID(int=8),
)


def payload(*values: object) -> str:
    """A cursor holding ``values`` as they are, however wrong."""
    raw = json.dumps(values).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_positions_round_trip_with_their_types() -> None:
    """Each value comes back as the field holds it."""
    cursor = encode_position(POSITION, ORDER)

    assert re.fullmatch(CURSOR_PATTERN, cursor)
    assert decode_position(cursor, ORDER, Record) == POSITION
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert decoded[:2] == [CURSOR_VERSION, ",".join(map(str, ORDER))]
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    assert b", " not in raw  # Compact JSON keeps cursors short.
    assert decoded[2:5] == ["Ünïcode", -(2**62), True]


def test_positions_of_other_versions_and_orderings_are_refused() -> None:
    """The version and the ordering must be this API's, value for value."""
    signature = ",".join(map(str, ORDER))
    values = json.loads(
        base64.urlsafe_b64decode(
            (cursor := encode_position(POSITION, ORDER)) + "=" * (-len(cursor) % 4)
        )
    )[2:]
    for forged in (
        payload(2, signature, *values),
        payload(True, signature, *values),
        payload("1", signature, *values),
        payload(1, "text,id", *values),
        payload(1, signature, *values[:-1]),
        payload(1, signature, *values, "extra"),
        payload({"v": 1}),
        encode_cursor(["text"]),
        "not base64 !",
        "",
        "A" * (MAX_CURSOR_LENGTH + 1),
        base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="),
    ):
        with pytest.raises(InvalidCursor):
            decode_position(forged, ORDER, Record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", 1),
        ("count", True),
        ("count", "1"),
        ("count", 2**63),
        ("count", 1.0),
        ("flag", 1),
        ("ratio", "0.5"),
        ("ratio", False),
        ("amount", 1),
        ("amount", "1e5"),
        ("amount", "NaN"),
        ("amount", "1.2.3"),
        ("when", "2026-01-02T03:04:05"),
        ("when", "yesterday"),
        ("day", "2026-01-02T00:00:00"),
        ("moment", 5),
        ("ref", "not-a-uuid"),
        ("ref", 7),
        ("color", "purple"),
        ("color", 1),
        ("size", "10"),
        ("label", "x" * 21),
        ("label", 5),
        ("text", None),
        ("anything", "x"),
    ],
)
def test_values_that_do_not_fit_their_field_are_refused(
    field: str, value: object
) -> None:
    """Every value is checked against its field's type; refusals are 422s."""
    ordering = (Ordering(field), Ordering("id"))
    cursor = payload(CURSOR_VERSION, f"{field},id", value, str(UUID(int=1)))

    with pytest.raises(InvalidCursor) as caught:
        decode_position(cursor, ordering, Record)
    assert caught.value.status_code == 422


def test_cursors_hold_any_base64url_text_and_nothing_else() -> None:
    """Cursors up to the longest round-trip; a character outside base64url fails."""
    ordering = (Ordering("count"), Ordering("id"))
    text = "x" * (MAX_CURSOR_LENGTH * 3 // 4 - len('[""]'))
    longest = encode_cursor([text])
    assert len(longest) == MAX_CURSOR_LENGTH
    assert decode_cursor(longest, 1) == [text]
    with pytest.raises(
        ValueError, match=r"^Sort key too long for a cursor \(4100 chars\)\.$"
    ):
        encode_cursor([f"{text}xyz"])
    cursor = encode_position((1, UUID(int=1)), ordering)
    with pytest.raises(InvalidCursor):
        decode_position(cursor[:4] + "...." + cursor[4:], ordering, Record)


def test_missing_values_fit_only_nullable_fields() -> None:
    """None is a position value where the field allows it."""
    ordering = (Ordering("missing"), Ordering("id"))
    cursor = encode_position((None, UUID(int=1)), ordering)

    assert decode_position(cursor, ordering, Record) == (None, UUID(int=1))


def test_some_values_have_no_cursor_form() -> None:
    """Lists and non-finite floats are refused when encoding."""
    with pytest.raises(TypeError, match="list values cannot be put in a cursor"):
        encode_position((["a"],), (Ordering("tags"),))
    with pytest.raises(TypeError, match="float values cannot be put"):
        encode_position((float("nan"),), (Ordering("ratio"),))


def test_positions_are_never_refused_for_their_length(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A page always answers; a cursor too long to come back is logged.

    Only a value longer than its field declares, or an ordering no
    ``FilterSet`` checked, makes one: it is refused when it comes back.
    """
    ordering = (Ordering("text"), Ordering("id"))
    fits = encode_position(("x" * 3000, UUID(int=1)), ordering)
    with caplog.at_level(logging.WARNING, logger="domainkit.http.pagination"):
        too_long = encode_position(("x" * 3100, UUID(int=1)), ordering)

    assert len(fits) <= MAX_CURSOR_LENGTH < len(too_long)
    assert caplog.messages == [
        (
            f"A cursor for the ordering text,id took {len(too_long)} characters, "
            "more than MAX_CURSOR_LENGTH: a value is longer than its field declares."
        )
    ]
    assert decode_position(fits, ordering, Record)[0] == "x" * 3000
    with pytest.raises(InvalidCursor):
        decode_position(too_long, ordering, Record)


def test_amounts_are_written_without_exponents() -> None:
    """``0E-8``, as zero comes back at eight places, was refused coming back."""
    ordering = (Ordering("amount"), Ordering("id"))
    for amount in (
        Decimal("0E-8"),
        Decimal("1E-7"),
        Decimal("1E+2"),
        Decimal("-0.50"),
        Decimal("9E+39"),
        Decimal("-1E-40"),
    ):
        cursor = encode_position((amount, UUID(int=1)), ordering)
        assert decode_position(cursor, ordering, Record) == (amount, UUID(int=1))
        assert "E" not in json.loads(base64.urlsafe_b64decode(cursor + "==="))[2]
    for odd in map(Decimal, ("1E+40", "1E-41", "1E+999999999", "NaN", "-Infinity")):
        cursor = encode_position((odd, UUID(int=1)), ordering)  # Not a gigabyte.
        assert json.loads(base64.urlsafe_b64decode(cursor + "==="))[2] == str(odd)
        with pytest.raises(InvalidCursor):
            decode_position(cursor, ordering, Record)


def test_cursors_name_fields_of_the_aggregate() -> None:
    """A server-side ordering on an unknown field is a programming error."""
    with pytest.raises(UnknownField):
        decode_position(payload(1, "nope", "x"), (Ordering("nope"),), Record)
    with pytest.raises(InvalidQuery, match="cannot be ordered by"):
        decode_position(payload(1, "tags", ["a"]), (Ordering("tags"),), Record)


@given(
    st.lists(
        st.one_of(
            st.none(), st.booleans(), st.integers(), st.text(max_size=5), st.floats()
        ),
        max_size=4,
    )
)
def test_forged_positions_never_crash(values: list[object]) -> None:
    """Whatever a cursor holds, decoding gives a position or a 422."""
    ordering = (Ordering("count"), Ordering("missing"), Ordering("id"))
    for forged in (payload(*values), payload(1, "count,missing,id", *values)):
        try:
            position = decode_position(forged, ordering, Record)
        except InvalidCursor:
            continue
        assert len(position) == 3


def test_pagination_declares_its_parameters() -> None:
    """Cursor or offset mode, with bounds FastAPI checks."""
    refusal = r"^Pagination needs 1 <= default_limit <= max_limit\.$"
    with pytest.raises(ValueError, match=refusal):
        Pagination(default_limit=0)
    with pytest.raises(ValueError, match=refusal):
        Pagination(default_limit=10, max_limit=5)
    assert Pagination(default_limit=1, max_limit=1).page(1) == PageRequest(1)
    cursors = Pagination(default_limit=2, max_limit=3, count=True)
    offsets = Pagination(offset=True, max_offset=6)

    assert list(inspect.signature(cursors).parameters) == ["limit", "cursor"]
    assert list(inspect.signature(offsets).parameters) == ["limit", "offset"]
    assert cursors.page() == PageRequest(limit=50, cursor=None, offset=None, count=True)
    assert cursors.page(2, "abc", 5) == PageRequest(2, "abc", None, count=True)
    assert offsets.page(2, "abc", 5) == PageRequest(2, None, 5, count=False)
    assert offsets.page() == PageRequest(50, None, 0, count=False)
    assert repr(offsets) == "Pagination(offset=True, count=False)"


def application(pages: Pagination) -> TestClient:
    """An application that lists stocked gadgets through ``pages``."""
    repository = InMemoryGadgets(
        make_gadget(name, stock=index, price=f"{index}.00")
        for index, name in enumerate("abcde")
    )
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/gadgets")
    async def gadgets(
        page: Annotated[PageRequest, Depends(pages)], ordering: str = "name"
    ) -> PageOut[str]:
        query = Query(F("stock") > 0, ordering=[ordering])
        found = await page.fetch(repository, query)
        return page.respond(found, [each.name for each in found.items], query)

    return TestClient(app)


def test_cursor_pages_over_http() -> None:
    """next_cursor leads through every page; tampering is a 422."""
    client = application(Pagination(default_limit=2, max_limit=3, count=True))

    first = client.get("/gadgets").json()
    assert (first["items"], first["total"], first["next_offset"]) == (
        ["b", "c"],
        4,
        None,
    )
    second = client.get("/gadgets", params={"cursor": first["next_cursor"]}).json()
    assert second["items"] == ["d", "e"]
    assert second["next_cursor"] is None
    descending = client.get(
        "/gadgets", params={"cursor": first["next_cursor"], "ordering": "-name"}
    )
    assert descending.status_code == 422
    assert descending.json()["code"] == "invalid_cursor"
    for params in ({"limit": 4}, {"limit": 0}, {"cursor": "?"}):
        refused = client.get("/gadgets", params=params)
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "validation_error",
        )
    assert len(client.get("/gadgets", params={"limit": 3}).json()["items"]) == 3
    parameters = client.get("/openapi.json").json()["paths"]["/gadgets"]["get"][
        "parameters"
    ]
    named = {each["name"]: each for each in parameters}
    limit, cursor = named["limit"], named["cursor"]
    assert (limit["schema"]["maximum"], limit["schema"]["default"]) == (3, 2)
    assert limit["schema"]["minimum"] == 1
    assert limit["description"] == "Items per page."
    assert cursor["description"] == "The previous page's next_cursor."
    assert cursor["schema"]["anyOf"][0]["maxLength"] == MAX_CURSOR_LENGTH
    too_long = client.get("/gadgets", params={"cursor": "A" * (MAX_CURSOR_LENGTH + 1)})
    assert too_long.json()["code"] == "validation_error"


def test_offset_pages_over_http() -> None:
    """next_offset leads on; offsets past the bound are refused."""
    client = application(Pagination(offset=True, default_limit=3, max_offset=3))

    first = client.get("/gadgets").json()
    assert (first["items"], first["next_offset"], first["next_cursor"]) == (
        ["b", "c", "d"],
        3,
        None,
    )
    assert first["total"] is None
    last = client.get("/gadgets", params={"offset": 3}).json()
    assert (last["items"], last["next_offset"]) == (["e"], None)
    counted = application(Pagination(offset=True, count=True))
    assert counted.get("/gadgets").json()["total"] == 4
    for offset in (4, -1):
        refused = client.get("/gadgets", params={"offset": offset})
        assert refused.json()["code"] == "validation_error"
    spec = client.get("/openapi.json").json()["paths"]["/gadgets"]["get"]
    offset_parameter = next(p for p in spec["parameters"] if p["name"] == "offset")
    assert offset_parameter["description"] == "Items to skip."
    assert offset_parameter["schema"]["minimum"] == 0


class Title(ValueObject):
    """A single-field value object around bounded text."""

    value: Annotated[str, MaxLength(4)]


class Bounded(AggregateRoot[UUID]):
    """An aggregate whose every field has a bound in a cursor."""

    name: Annotated[str, MaxLength(5)]
    title: Title
    count: int
    flag: bool
    ratio: float
    price: Annotated[Decimal, Digits(10, 2)]
    share: Annotated[Decimal, Digits(3, 3)]
    units: Annotated[Decimal, Digits(4, 0)]
    when: datetime
    day: date
    moment: time
    color: Color
    size: Size
    maybe: Size | None
    single: Single


ODD: Final = timezone(timedelta(hours=5, minutes=30, seconds=15, microseconds=1))
"""An offset with every part isoformat writes."""
LONGEST: Final[dict[str, object]] = {
    "name": "\x1f" * 5,  # JSON writes each as \u001f: six bytes.
    "title": Title("\x00" * 4),
    "count": 1 - 2**63,
    "flag": False,
    "ratio": -2.2250738585072014e-308,
    "price": Decimal("-12345678.91"),
    "share": Decimal("-0.123"),
    "units": Decimal("-1234"),
    "when": datetime(1, 1, 1, 0, 0, 0, 1, tzinfo=ODD),
    "day": date(1, 1, 1),
    "moment": time(0, 0, 0, 1, tzinfo=ODD),
    "color": Color.GREEN,
    "size": Size.LARGE,
    "maybe": None,
    "single": Single.ONLY,
    "id": UUID(int=0),
}
"""The value of each field of ``Bounded`` that takes most room in a cursor."""


@pytest.mark.parametrize("name", list(LONGEST))
def test_cursor_lengths_are_those_of_the_longest_values(name: str) -> None:
    """The bound is exact: the longest value of each type reaches it."""
    ordering = (Ordering(name, descending=True),)
    complete = total_ordering(ordering)
    position = tuple(LONGEST[item.field] for item in complete)

    cursor = encode_position(position, complete)

    assert len(cursor) == cursor_length(ordering, Bounded)
    assert decode_position(cursor, complete, Bounded) == position


def test_cursor_lengths_need_bounded_fields() -> None:
    """Text needs ``MaxLength``, amounts ``Digits``; some types have no cursor."""
    refusals = {
        "text": (
            r"^Record\.text holds text of any length, which no cursor can bound: "
            r"declare its MaxLength\(n\), as Annotated\[str, MaxLength\(n\)\], or "
            r"do not order by it\.$"
        ),
        "amount": (
            r"^Record\.amount holds amounts of any size, which no cursor can bound: "
            r"declare its Digits\(precision, scale\), as Annotated\[Decimal, "
            r"Digits\(precision, scale\)\], or do not order by it\.$"
        ),
        "anything": (
            r"^Record\.anything holds values of no known type, which a cursor "
            r"cannot hold: do not order by it\.$"
        ),
        "label": None,
    }
    for name, refusal in refusals.items():
        if refusal is None:
            assert cursor_length((Ordering(name),), Record) > 0
            continue
        with pytest.raises(TypeError, match=refusal):
            cursor_length((Ordering(name),), Record)
    with pytest.raises(TypeError, match=r"^Unbounded\.slug holds Slug, which"):
        cursor_length((Ordering("slug"),), Unbounded)
    with pytest.raises(InvalidQuery, match="cannot be ordered by"):
        cursor_length((Ordering("tags"),), Record)


class Slug(str):
    """Text of a class of its own, which cursors do not read back."""

    __slots__ = ()


class Unbounded(AggregateRoot[int]):
    """An aggregate ordered by something a cursor cannot hold."""

    slug: Slug


def test_the_longest_ordering_takes_the_roomiest_fields() -> None:
    """Up to ``count`` fields, longest names and values first, each descending."""
    assert longest_ordering(("count", "name", "flag", "day"), Bounded, 2) == (
        Ordering("name", descending=True),
        Ordering("count", descending=True),
    )
    assert longest_ordering(("flag", "flag", "id"), Bounded, 3) == (
        Ordering("flag", descending=True),
        Ordering("id", descending=True),
    )
    assert longest_ordering(("flag", "count", "id"), Bounded, 2) == (
        Ordering("count", descending=True),
        Ordering("flag", descending=True),
    )
    assert longest_ordering((), Bounded, 3) == ()
    assert longest_ordering(("id",), Bounded, 0) == ()
    by_id = total_ordering(longest_ordering(("id",), Bounded, 1))
    assert len(encode_position((UUID(int=0),), by_id)) == cursor_length(by_id, Bounded)


class Book(AggregateRoot[UUID]):
    """Three text fields as long as ``MAX_CURSOR_LENGTH`` allows them together."""

    title: Annotated[str, MaxLength(150)]
    subtitle: Annotated[str, MaxLength(150)]
    author: Annotated[str, MaxLength(150)]


class Books(InMemoryRepository[Book, UUID]):
    """Books in memory."""


def test_three_text_fields_of_the_widest_characters_always_page() -> None:
    """Control characters, astral and Arabic letters: every cursor comes back."""
    widest = ("\x1f", "\U0001d538", "\u0628", "\x00")
    books = [
        Book(
            id=UUID(int=number),
            title=character * 150,
            subtitle=character * 149 + str(number),
            author=widest[number % 4] * 150,
        )
        for number, character in enumerate(widest * 2)
    ]

    class BookFilters(FilterSet):
        model = Book
        ordering_fields = ("title", "subtitle", "author")

    app = FastAPI()
    install_exception_handlers(app)
    pages = Pagination(default_limit=1, max_limit=1)

    @app.get("/books")
    async def listing(
        filters: Annotated[BookFilters, Depends()],
        page: Annotated[PageRequest, Depends(pages)],
    ) -> PageOut[int]:
        query = filters.query()
        found = await page.fetch(Books(books), query)
        return page.respond(found, [each.id.int for each in found.items], query)

    client = TestClient(app)
    ordering = ["-author", "-subtitle", "-title"]
    seen: list[int] = []
    cursors: list[str] = []
    params: dict[str, Any] = {"ordering": ordering}
    while True:
        answer = client.get("/books", params=params)
        assert answer.status_code == 200, answer.text
        seen += answer.json()["items"]
        if answer.json()["next_cursor"] is None:
            break
        cursors.append(answer.json()["next_cursor"])
        params = {"ordering": ordering, "cursor": cursors[-1]}

    assert sorted(seen) == list(range(8))
    bound = cursor_length(longest_ordering(BookFilters.ordering_fields, Book, 3), Book)
    assert max(map(len, cursors)) <= bound <= MAX_CURSOR_LENGTH
