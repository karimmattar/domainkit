"""FilterSets: typed, bounded filters, ordering, and search from the query string."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from domainkit.ddd import AggregateRoot, Digits, MaxLength, ValueObject
from domainkit.ddd.query import F, Ordering, Query
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import Filter, FilterSet, InvalidFilter
from domainkit.http.pagination import (
    MAX_CURSOR_LENGTH,
    PageOut,
    PageRequest,
    Pagination,
    cursor_length,
    longest_ordering,
)
from domainkit_tests.gadgets import (
    MADE,
    Color,
    Gadget,
    InMemoryGadgets,
    Size,
    make_gadget,
    make_maker,
    make_part,
)

LATER = datetime(2026, 2, 1, tzinfo=UTC)


def gadget_filters() -> type[FilterSet]:
    """Declare the gadget listing's filters.

    Tests declare their filter sets as they run, so mutation testing maps
    the declaration code to the tests that check its result.
    """

    class GadgetFilters(FilterSet):
        """Every kind of filter a gadget listing offers."""

        model = Gadget
        name = Filter("exact", "icontains", "istartswith", max_length=10)
        price = Filter("gte", "lte")
        stock = Filter("exact", "isnull", "gt")
        color = Filter("exact", "in")
        size = Filter("lt")
        label = Filter()
        made_at = Filter("gt")
        active = Filter()
        sku = Filter("exact", field="parts.sku")
        maker = Filter("iexact", field="maker.name")
        ordering_fields = ("name", "price", "made_at")
        default_ordering = ("-made_at",)
        search_fields = ("name", "label")
        max_conditions = 4
        max_values = 3
        max_search_words = 2

    return GadgetFilters


ITEMS = [
    make_gadget("alpha", price="1.00", stock=1, parts=[make_part("S1")]),
    make_gadget(
        "beta",
        price="5.00",
        color=Color.BLUE,
        made_at=LATER,
        maker=make_maker("Acme"),
        active=False,
    ),
    make_gadget(
        "gamma",
        price="9.50",
        stock=3,
        size=Size.LARGE,
        made_at=MADE + timedelta(hours=1),
    ),
]


@pytest.fixture
def declared() -> type[FilterSet]:
    """The gadget listing's filter set, freshly declared."""
    return gadget_filters()


@pytest.fixture
def client(declared: type[FilterSet]) -> TestClient:
    """An application listing ``ITEMS`` through the gadget filters."""
    repository = InMemoryGadgets(ITEMS)
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/gadgets")
    async def listing(filters: Annotated[FilterSet, Depends(declared)]) -> list[str]:
        return [each.name for each in await repository.filter(filters.query())]

    return TestClient(app)


def names(client: TestClient, **params: Any) -> list[str]:  # noqa: ANN401 - any value.
    """The names the listing answers with."""
    response = client.get("/gadgets", params=params)
    assert response.status_code == 200, response.json()
    return list(response.json())


def refusal(client: TestClient, **params: Any) -> tuple[str, str]:  # noqa: ANN401
    """The code and detail of a 422."""
    response = client.get("/gadgets", params=params)
    assert response.status_code == 422, response.json()
    body = response.json()
    return body["code"], body["detail"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ["beta", "gamma", "alpha"]),
        ({"name": "beta"}, ["beta"]),
        ({"name__icontains": "AM"}, ["gamma"]),
        ({"name__istartswith": "A"}, ["alpha"]),
        ({"price__gte": "5", "price__lte": "9.50"}, ["beta", "gamma"]),
        ({"stock": 3}, ["gamma"]),
        ({"stock__isnull": "true"}, ["beta"]),
        ({"stock__isnull": "false", "stock__gt": 1}, ["gamma"]),
        ({"color": "blue"}, ["beta"]),
        ({"color__in": ["red", "green"]}, ["gamma", "alpha"]),
        ({"size__lt": 10}, ["beta", "alpha"]),
        ({"label": "GAMMA"}, ["gamma"]),
        ({"made_at__gt": MADE.isoformat()}, ["beta", "gamma"]),
        ({"active": "false"}, ["beta"]),
        ({"sku": "S1"}, ["alpha"]),
        ({"maker__iexact": "ACME"}, ["beta"]),
        ({"ordering": "name"}, ["alpha", "beta", "gamma"]),
        ({"ordering": ["-price", "name"]}, ["gamma", "beta", "alpha"]),
        ({"search": "ALP"}, ["alpha"]),
        ({"search": "a  MM"}, ["gamma"]),
        ({"search": "   "}, ["beta", "gamma", "alpha"]),
        ({"limit": 5, "cursor": "abc"}, ["beta", "gamma", "alpha"]),
    ],
)
def test_filters_narrow_and_order_the_listing(
    client: TestClient, params: dict[str, Any], expected: list[str]
) -> None:
    """Each declared lookup, the ordering, and the search."""
    assert names(client, **params) == expected


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        (
            {"price__regex": "1"},
            "Unknown lookup on price; it offers price__gte, price__lte.",
        ),
        (
            {"name__contains": "a"},
            "Unknown lookup on name; it offers name, name__icontains",
        ),
        ({"price__gte__x": 1}, "Unknown lookup on price; it offers"),
        ({"name": ["a", "b"]}, "Give the name filter once."),
        ({"ordering": ["name", "-name"]}, "Name each field once in the ordering."),
        ({"search": "a b c"}, "Search for at most 2 words."),
        (
            {"name": "a", "stock": 1, "color": "red", "size__lt": 2, "active": "true"},
            "Combine at most 4 filters.",
        ),
        (
            {"name": "a", "stock": 1, "color": "red", "search": "x y"},
            "Combine at most 4",
        ),
        ({"label": "x" * 21}, "A filter value does not fit its field: A label is"),
    ],
)
def test_what_fastapi_cannot_see_is_refused(
    client: TestClient, params: dict[str, Any], detail: str
) -> None:
    """422 ``invalid_filter``, with the reason."""
    code, message = refusal(client, **params)

    assert code == "invalid_filter"
    assert message.startswith(detail)


@pytest.mark.parametrize(
    "params",
    [
        {"color": "purple"},
        {"color__in": ["red", "red", "red", "red"]},
        {"price__gte": "cheap"},
        {"made_at__gt": "2026-01-01T00:00:00"},
        {"stock__gt": 2**63},
        {"stock__gt": -(2**63) - 1},
        {"color__in": ["x" * 201]},
        {"name": "x" * 11},
        {"ordering": "stock"},
        {"ordering": ["name", "price", "made_at", "-name"]},
        {"search": "x" * 101},
        {"active": "maybe"},
        {"price__gte": "1e999999999"},  # Digits(10, 2): 8 digits, then 2.
        {"price__gte": "1e-999999999"},
        {"price__lte": "100000000"},
        {"price__gte": "0.001"},
    ],
)
def test_values_of_the_wrong_type_or_size_are_validation_errors(
    client: TestClient, params: dict[str, Any]
) -> None:
    """FastAPI checks types, enums, and bounds: 422 ``validation_error``."""
    assert refusal(client, **params)[0] == "validation_error"


def test_amounts_up_to_the_fields_digits_are_taken(client: TestClient) -> None:
    """The widest amounts of a ``Digits(10, 2)`` field are filters like others."""
    widest = {"price__gte": "-99999999.99", "price__lte": "99999999.99"}
    assert names(client, **widest) == ["beta", "gamma", "alpha"]
    assert names(client, price__gte="9.5") == ["gamma"]


class Cost(ValueObject):
    """An amount of a value object, whose own field has ``Digits``."""

    value: Annotated[Decimal, Digits(5, 1)]


class Priced(AggregateRoot[int]):
    """Amounts with and without ``Digits``."""

    amount: Decimal
    cost: Cost


def test_amounts_are_bounded_without_digits_too() -> None:
    """40 digits either side of the point; a value object's ``Digits`` apply."""

    class PricedFilters(FilterSet):
        model = Priced
        amount = Filter("gte")
        cost = Filter("lte")

    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/priced")
    async def priced(filters: Annotated[PricedFilters, Depends()]) -> str:
        return str(filters.criteria)

    client = TestClient(app)
    for params in (
        {"amount__gte": "9" * 40 + "." + "9" * 40},
        {"amount__gte": "0." + "0" * 39 + "1"},
        {"cost__lte": "9999.9"},
    ):
        assert client.get("/priced", params=params).status_code == 200, params
    for params in (
        {"amount__gte": "9" * 41},
        {"amount__gte": "1e40"},
        {"amount__gte": "1e-41"},
        {"cost__lte": "99999"},
        {"cost__lte": "0.01"},
    ):
        refused = client.get("/priced", params=params)
        assert refused.json()["code"] == "validation_error", params


def test_lookups_are_unknown_only_for_filtered_names(client: TestClient) -> None:
    """Parameters of other names, the route's own, pass by."""
    assert names(client, other__thing="x", unrelated="y") == ["beta", "gamma", "alpha"]


def test_answers_tell_nothing_of_fields_the_set_does_not_filter(
    client: TestClient,
) -> None:
    """A field of the model the set does not filter is any other name.

    ``?version__gt=1`` was a 422 naming ``version`` while ``?nosuch__gt=1``
    passed: a client could list the model's fields, one guess at a time.
    """
    unfiltered = client.get("/gadgets")
    for probe in ("version__gt", "deleted_at__isnull", "parts__x", "nosuch__gt"):
        answer = client.get("/gadgets", params={probe: "1"})
        assert (answer.status_code, answer.json()) == (200, unfiltered.json()), probe


def test_openapi_documents_each_parameter_with_its_type(
    client: TestClient, declared: type[FilterSet]
) -> None:
    """Types, enums, bounds, and descriptions reach the schema."""
    spec = client.get("/openapi.json").json()
    parameters = {
        each["name"]: each for each in spec["paths"]["/gadgets"]["get"]["parameters"]
    }

    assert "_request" not in parameters
    assert set(parameters) == {
        *declared.filters,
        "ordering",
        "search",
    }
    color = parameters["color"]["schema"]["anyOf"][0]["$ref"]
    assert spec["components"]["schemas"][color.rsplit("/", 1)[1]]["enum"] == [
        "red",
        "green",
        "blue",
    ]
    listed = parameters["color__in"]["schema"]["anyOf"][0]
    assert (listed["type"], listed["maxItems"]) == ("array", 3)
    assert parameters["name"]["schema"]["anyOf"][0] == {
        "type": "string",
        "maxLength": 10,
    }
    assert parameters["stock__isnull"]["schema"]["anyOf"][0] == {"type": "boolean"}
    assert parameters["stock__gt"]["schema"]["anyOf"][0]["type"] == "integer"
    assert parameters["label"]["schema"]["anyOf"][0]["type"] == "string"
    assert parameters["made_at__gt"]["schema"]["anyOf"][0]["format"] == "date-time"
    ordering = parameters["ordering"]["schema"]["anyOf"][0]
    assert ordering["items"]["enum"] == [
        "name", "-name", "price", "-price", "made_at", "-made_at",
    ]  # fmt: skip
    assert ordering["maxItems"] == 3
    assert parameters["search"]["schema"]["anyOf"][0]["maxLength"] == 100
    assert parameters["price__gte"]["description"] == (
        "Only items whose price is at least this."
    )
    assert parameters["sku"]["description"] == "Only items whose parts.sku is this."
    assert parameters["stock__isnull"]["description"] == (
        "Only items whose stock is missing (true) or present (false)."
    )
    assert parameters["color__in"]["description"] == (
        "Only items whose color is one of these."
    )
    assert parameters["ordering"]["description"] == (
        "Order by these fields; - for descending."
    )
    assert parameters["search"]["description"] == (
        "Words to find in name, label, ignoring case."
    )
    bounds = parameters["stock__gt"]["schema"]["anyOf"][0]
    assert (bounds["minimum"], bounds["exclusiveMaximum"]) == (-(2**63), 2**63)


def test_filter_sets_build_queries_in_code(declared: type[FilterSet]) -> None:
    """Without a request, the parameters are keyword arguments."""
    GadgetFilters = declared
    filters = GadgetFilters(price__gte=Decimal(1), color__in=[Color.RED], search="al")

    assert filters.ordering == (Ordering("made_at", descending=True),)
    assert filters.values == {"price__gte": Decimal(1), "color__in": [Color.RED]}
    assert filters.search == ["al"]
    assert filters.criteria == (
        (F("price") >= Decimal(1))
        & F("color").in_([Color.RED])
        & (F("name").icontains("al") | F("label").icontains("al"))
    )
    query = filters.query(limit=2)
    assert query == Query(filters.criteria, ordering=filters.ordering, limit=2)
    assert GadgetFilters().criteria is None
    assert GadgetFilters(ordering=["name"]).ordering == (Ordering("name"),)
    with pytest.raises(InvalidFilter) as caught:
        GadgetFilters(nope=1, other=2)
    assert caught.value.detail == "Unknown filter: nope, other."
    at_most = GadgetFilters(name="alpha", stock=1, price__gte=Decimal(0), search="a")
    assert at_most.values == {
        "name": "alpha",
        "stock": 1,
        "price__gte": Decimal(0),
    }


def test_filter_sets_are_checked_when_declared() -> None:
    """Lookups must fit their fields; orderings and search fields must exist."""
    declarations: list[tuple[dict[str, Any], str]] = [
        ({"price": Filter("icontains")}, "price: icontains needs a text field"),
        ({"nope": Filter()}, "Gadget has no field 'nope'"),
        ({"ordering_fields": ("parts",)}, "holds child entities"),
        ({"default_ordering": ("-nope",)}, "has no field 'nope'"),
        ({"search_fields": ("price",)}, "price is not text to search"),
        (
            {"search": Filter(field="name"), "ordering": Filter(field="name")},
            r"^Broken: ordering, search cannot name a filter\.$",
        ),
    ]
    for body, message in declarations:
        with pytest.raises((TypeError, ValueError), match=message):
            type("Broken", (FilterSet,), {"model": Gadget, **body})
    with pytest.raises(ValueError, match=r"^Offer gte and lte instead of range\.$"):
        Filter("range")
    with pytest.raises(ValueError, match="is not a valid Lookup"):
        Filter("regex")


class Tagged(AggregateRoot[int]):
    """An aggregate with fields that do not sort, or have no known type."""

    tags: list[str]
    anything: Any = None


class Code(ValueObject):
    """A value object whose own errors quote the value."""

    value: str

    def __post_init__(self) -> None:
        """Parse like int(), whose message repeats what it was given."""
        int(self.value)


class Coded(AggregateRoot[int]):
    """An aggregate with such a value object."""

    code: Code


def test_refusals_never_repeat_the_value(declared: type[FilterSet]) -> None:
    """A value object's own ValueError is not shown; a domain error is."""

    class CodedFilters(FilterSet):
        model = Coded
        code = Filter()

    with pytest.raises(InvalidFilter) as caught:
        CodedFilters(code="secret-42")
    assert caught.value.detail == "A filter value does not fit its field."
    assert isinstance(caught.value.__cause__, ValueError)
    with pytest.raises(InvalidFilter, match="A label is at most 20"):
        declared(label="x" * 21)


def test_fields_of_no_known_type_are_filtered_as_text() -> None:
    """Their parameters are strings; an ``__in`` bounds each one."""

    class TaggedFilters(FilterSet):
        model = Tagged
        anything = Filter()
        tag = Filter("in", field="anything", max_length=4)

    app = FastAPI()

    @app.get("/tagged")
    async def tagged(
        filters: Annotated[FilterSet, Depends(TaggedFilters)],
    ) -> list[str]:
        return [str(filters.criteria)]

    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    parameters = {
        each["name"]: each["schema"]["anyOf"][0]
        for each in spec["paths"]["/tagged"]["get"]["parameters"]
    }
    assert parameters["anything"] == {"type": "string", "maxLength": 200}
    assert parameters["tag__in"]["items"] == {"type": "string", "maxLength": 4}
    assert client.get("/tagged", params={"tag__in": ["abcde"]}).status_code == 422
    assert client.get("/tagged", params={"anything": "x"}).status_code == 200
    with pytest.raises(TypeError, match="tags: gt needs a field whose values sort"):
        type("Broken", (FilterSet,), {"model": Tagged, "tags": Filter("gt")})

    class Exact(FilterSet):
        model = Tagged
        tags = Filter()

    assert set(Exact.filters) == {"tags"}


def test_orderings_need_fields_a_cursor_can_bound() -> None:
    """Text without ``MaxLength`` cannot be ordered by, as a cursor holds it.

    A page ending on a long value could not encode its cursor: a 500 for
    every page that ended there.
    """
    for body in (
        {"ordering_fields": ("code",)},
        {"default_ordering": ("-code",)},
    ):
        with pytest.raises(
            TypeError,
            match=(
                r"^Broken: Coded\.code holds text of any length, which no cursor "
                r"can bound: declare its MaxLength\(n\)"
            ),
        ):
            type("Broken", (FilterSet,), {"model": Coded, **body})


class Article(AggregateRoot[UUID]):
    """Three text fields too long to order by all at once."""

    title: Annotated[str, MaxLength(200)]
    subtitle: Annotated[str, MaxLength(200)]
    author: Annotated[str, MaxLength(200)]
    lead: Annotated[str, MaxLength(800)]


def test_orderings_whose_cursors_could_be_too_long_are_refused() -> None:
    """Clients' longest ordering, and the default one, must fit a cursor."""
    fields = ("title", "subtitle", "author")
    with pytest.raises(TypeError) as refused:
        type("Broken", (FilterSet,), {"model": Article, "ordering_fields": fields})
    # [1,"-lead,id", 6 * 800 + 2 bytes, 38 for the id]: 4,856 bytes of JSON.
    with pytest.raises(TypeError, match=r"ordering -lead,id can take 6475 char"):
        type("Broken", (FilterSet,), {"model": Article, "default_ordering": ("-lead",)})

    # The longest names first; 3 * (6 * 200 + 2) bytes of text: 3,681 of JSON.
    assert str(refused.value) == (
        "Broken: a cursor for the ordering -subtitle,-author,-title,id can take "
        "4908 characters, more than MAX_CURSOR_LENGTH (4096): lower its text "
        "fields' MaxLength, or max_ordering."
    )

    class Fits(FilterSet):
        model = Article
        ordering_fields = ("title", "subtitle", "author")
        max_ordering = 2

    longest = longest_ordering(Fits.ordering_fields, Article, Fits.max_ordering)
    assert cursor_length(longest, Article) <= MAX_CURSOR_LENGTH


def test_a_long_sort_value_never_breaks_a_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pages ending on a value longer than its field declares still answer.

    The in-memory repository keeps names past ``MaxLength(30)``, as SQLite
    does; such a page was a 500.
    """

    class Listing(FilterSet):
        model = Gadget
        ordering_fields = ("name",)
        default_ordering = ("name",)

    arabic = make_gadget("b")
    arabic.name = "b" + "\u0628" * 800  # 1,600 bytes of UTF-8.
    control = make_gadget("c")
    control.name = "c" + "\x1f" * 3000  # 18,000 bytes of JSON.
    repository = InMemoryGadgets([make_gadget("a"), arabic, control, make_gadget("d")])
    app = FastAPI()
    install_exception_handlers(app)
    pages = Pagination(default_limit=1, max_limit=1)

    @app.get("/gadgets")
    async def listing(
        filters: Annotated[Listing, Depends()],
        page: Annotated[PageRequest, Depends(pages)],
    ) -> PageOut[str]:
        query = filters.query()
        found = await page.fetch(repository, query)
        return page.respond(found, [each.name[0] for each in found.items], query)

    client = TestClient(app)
    first = client.get("/gadgets").json()
    second = client.get("/gadgets", params={"cursor": first["next_cursor"]}).json()
    with caplog.at_level(logging.WARNING, logger="domainkit.http.pagination"):
        third = client.get("/gadgets", params={"cursor": second["next_cursor"]})

    assert (first["items"], second["items"]) == (["a"], ["b"])
    assert (third.status_code, third.json()["items"]) == (200, ["c"])
    assert len(caplog.messages) == 1
    after = client.get("/gadgets", params={"cursor": third.json()["next_cursor"]})
    assert (after.status_code, after.json()["code"]) == (422, "validation_error")


def test_filter_sets_without_a_model_are_bases() -> None:
    """A subclass that sets no model declares nothing yet."""

    class Base(FilterSet):
        name = Filter()

    class Concrete(Base):
        model = Gadget

    assert Base.filters == {}
    assert set(Concrete.filters) == {"name"}
    assert Filter().lookups == (Filter("exact").lookups[0],)
    assert Filter("gt", "gt").lookups == (Filter("gt").lookups[0],)
