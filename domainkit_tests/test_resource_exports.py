"""Lists as CSV and NDJSON: negotiation, streaming, bounds, and safe cells."""

import csv
import io
import json
from uuid import UUID

import pytest

from domainkit.ddd.query import F
from domainkit.resources import FieldPolicy, routing
from domainkit.resources.renderers import (
    CSV,
    JSON,
    NDJSON,
    NotAcceptable,
    cell,
    guarded,
    ndjson_lines,
    negotiate,
)
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import InMemoryGadgets, Part, PartId, make_gadget
from domainkit_tests.test_resources import GadgetFilters, as_, client_of, gadgets


@pytest.fixture
def store() -> InMemoryGadgets:
    """Gadgets whose names a spreadsheet would run, and a plain one."""
    return InMemoryGadgets(
        [
            make_gadget("=cmd|calc", price="2.00"),
            make_gadget("-5", price="3.00"),
            make_gadget(
                "plain", parts=[Part(id=PartId(UUID(int=1)), sku="s1", weight=2)]
            ),
        ]
    )


def rows(text: str) -> list[list[str]]:
    """CSV text as rows."""
    return list(csv.reader(io.StringIO(text)))


def test_lists_answer_csv_by_accept(store: InMemoryGadgets, use_auth: UseAuth) -> None:
    """Every match, in the listing's order, cells safe from formulas."""
    use_auth()
    client = client_of(
        gadgets(
            store,
            exports=("csv", "ndjson"),
            filters=GadgetFilters,
            list_fields=["name", "price", "parts"],
        )
    )

    answer = client.get("/gadgets", headers=as_("viewer", Accept=CSV))

    assert answer.status_code == 200
    assert answer.headers["content-type"] == "text/csv; charset=utf-8"
    assert answer.headers["content-disposition"] == 'attachment; filename="gadgets.csv"'
    assert answer.headers["vary"] == "Accept"
    header, *body = rows(answer.text)
    assert header == ["name", "price", "parts"]
    assert body == [
        ["-5", "3.00", "[]"],
        ["'=cmd|calc", "2.00", "[]"],
        [
            "plain",
            "1.00",
            json.dumps([{"id": str(UUID(int=1)), "sku": "s1", "weight": 2}]),
        ],
    ]


def test_lists_answer_ndjson_and_json_by_preference(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """NDJSON lines; JSON pages when JSON is preferred, or nothing is said."""
    use_auth()
    client = client_of(gadgets(store, exports=("ndjson",)))

    lines = client.get("/gadgets", headers=as_("viewer", Accept=NDJSON))
    preferred = client.get(
        "/gadgets",
        headers=as_("viewer", Accept=f"{NDJSON};q=0.5, {JSON}"),
    )
    unsaid = client.get("/gadgets", headers=as_("viewer"))
    refused = client.get("/gadgets", headers=as_("viewer", Accept="text/html"))

    assert lines.headers["content-type"] == NDJSON
    assert sorted(json.loads(line)["name"] for line in lines.text.splitlines()) == [
        "-5",
        "=cmd|calc",
        "plain",
    ]
    assert preferred.json()["items"]
    assert unsaid.headers["content-type"] == JSON
    assert unsaid.headers["vary"] == "Accept"
    assert (refused.status_code, refused.json()["code"]) == (406, "not_acceptable")
    assert refused.json()["detail"] == (
        "This resource cannot answer in a media type the request accepts."
    )
    assert refused.headers["accept"] == f"{JSON}, {NDJSON}"


def test_exports_are_bounded_streamed_in_chunks_and_hide_fields(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Too many rows is 422; chunks follow each other; hidden fields stay out."""
    use_auth()
    monkeypatch.setattr(routing, "EXPORT_CHUNK", 2)
    policy = FieldPolicy(visible={"price": "shop.view_cost"})
    client = client_of(gadgets(store, exports=("csv",), policy=policy))
    small = client_of(gadgets(store, exports=("csv",), max_export=2))

    viewer = rows(client.get("/gadgets", headers=as_("viewer", Accept=CSV)).text)
    costs = rows(client.get("/gadgets", headers=as_("costs", Accept=CSV)).text)
    too_many = small.get("/gadgets", headers=as_("viewer", Accept=CSV))

    assert "price" not in viewer[0]
    assert "price" in costs[0]
    assert len(viewer) == len(costs) == 4
    assert (too_many.status_code, too_many.json()["code"]) == (422, "export_too_large")
    assert too_many.json()["detail"] == (
        "This export would be too large; narrow it with filters."
    )


def test_exports_stop_at_their_bound_even_if_more_arrive(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows stored after the count are not sent past ``max_export``."""
    use_auth()

    async def fewer(*args: object, **options: object) -> int:
        return 2

    monkeypatch.setattr(store, "count", fewer)
    client = client_of(gadgets(store, exports=("ndjson",), max_export=2))

    lines = client.get("/gadgets", headers=as_("viewer", Accept=NDJSON)).text

    assert len(lines.splitlines()) == 2


def test_exports_are_declared_and_documented(store: InMemoryGadgets) -> None:
    """Unknown formats are refused; OpenAPI lists the media types and 406."""
    with pytest.raises(TypeError, match="not pdf"):
        gadgets(store, exports=("pdf",))
    document = client_of(gadgets(store, exports=("csv", "ndjson"))).get("/openapi.json")

    responses = document.json()["paths"]["/gadgets"]["get"]["responses"]
    assert sorted(responses["200"]["content"]) == [JSON, NDJSON, CSV]
    assert "406" in responses


@pytest.mark.parametrize(
    ("accept", "chosen"),
    [
        (None, JSON),
        ("", JSON),
        ("*/*", JSON),
        ("text/*", CSV),
        ("text/csv;q=0.4, application/*;q=0.5", JSON),
        ("text/csv, application/json;q=0.9", CSV),
        ("TEXT/CSV", CSV),
        ("text/csv;q=oops, application/json;q=0.1", JSON),
        ("text/csv;q=7", CSV),
        ("text/csv;level=1", CSV),
        ("*/*;q=0.1, text/csv;q=0", JSON),
        ("text/csv;q=0.2, */*", JSON),
        ("application/json;q=1, text/csv", JSON),
        ("text/csv;q=7, application/json", JSON),
        ("text/csv;q=0, text/csv, application/json;q=0.5", JSON),
    ],
)
def test_negotiation_takes_the_most_specific_preference(
    accept: str | None, chosen: str
) -> None:
    """Quality by the most specific range; ties go to the order offered."""
    assert negotiate(accept, (JSON, CSV)) == chosen


def test_negotiation_weighs_ranges_by_how_specific_they_are() -> None:
    """A subtype beats its type's range, which beats ``*/*``, wherever it is."""
    assert negotiate("text/*, text/csv;q=0.2", (CSV, "text/plain")) == "text/plain"
    assert negotiate("*/*, text/*;q=0.3", (CSV, JSON)) == JSON


def test_negotiation_refuses_what_nothing_offered_matches() -> None:
    """Refused types, and ranges past the bound, match nothing."""
    with pytest.raises(NotAcceptable):
        negotiate("text/csv;q=0", (CSV,))
    with pytest.raises(NotAcceptable):
        negotiate(", ".join(["text/html"] * 32 + [CSV]), (CSV,))


def test_cells_are_safe_from_formulas() -> None:
    """Formulas get an apostrophe; numbers, and everything else, stay."""
    assert [cell(value) for value in (None, True, False, 3, 1.5, "x")] == [
        "",
        "true",
        "false",
        "3",
        "1.5",
        "x",
    ]
    assert cell({"a": "=1"}) == '{"a": "=1"}'
    assert cell({"é": "ü"}) == '{"é": "ü"}'
    assert ndjson_lines([{"é": 1, "b": [1, 2]}]) == '{"é":1,"b":[1,2]}\n'
    assert cell(["@x"]) == '["@x"]'
    assert [guarded(text) for text in ("=1+1", "+1", "@SUM(A1)", "\tx", "\rx")] == [
        "'=1+1",
        "'+1",
        "'@SUM(A1)",
        "'\tx",
        "'\rx",
    ]
    assert [guarded(text) for text in ("-1", "-1.50", "-2e3", "-2+3", "-")] == [
        "-1",
        "-1.50",
        "-2e3",
        "'-2+3",
        "'-",
    ]


@pytest.mark.parametrize(
    "text",
    [
        " =1+1",
        "  @SUM(1)",
        "\n=1+1",
        "\u00a0=1+1",
        "\u3000=1+1",
        "\ufeff=1+1",
        "\uff1d1+1",
        "\uff0b1",
        "\uff0d1",
        "\uff20SUM(1)",
        "\ufe621",
        "\ufe631",
        "\ufe661",
        "\ufe6bSUM(1)",
        "\u22121",
        " -1+1",
        "-\u0661",
    ],
)
def test_formulas_behind_spaces_or_in_other_widths_are_guarded(text: str) -> None:
    """Spreadsheets skip leading spaces, and read wide and small signs as signs."""
    assert guarded(text) == f"'{text}"


def test_numbers_behind_spaces_stay_numbers() -> None:
    """A number with spaces before it is still a number."""
    assert [guarded(text) for text in (" -1", "\u00a0-2.5")] == [" -1", "\u00a0-2.5"]
    assert [guarded(text) for text in ("-1 2", " -1 2")] == ["'-1 2", "' -1 2"]


def test_ndjson_lines_end_only_at_their_newline() -> None:
    """Separators that split lines elsewhere are escaped inside a line."""
    text = ndjson_lines([{"name": 'x\u2028{"id":"forged"}\u2029y\u0085z'}])

    assert text.splitlines() == [text.removesuffix("\n")]
    assert json.loads(text) == {"name": 'x\u2028{"id":"forged"}\u2029y\u0085z'}


def test_exports_count_what_the_filters_keep_and_leave_links_out(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """The bound counts matches; CSV has no links; NDJSON has the list's fields."""
    use_auth()
    client = client_of(
        gadgets(
            store,
            exports=("csv", "ndjson"),
            filters=GadgetFilters,
            max_export=1,
            links=True,
            list_fields=["name"],
        )
    )
    one = {"search": "plain"}

    table = client.get("/gadgets", params=one, headers=as_("viewer", Accept=CSV))
    lines = client.get("/gadgets", params=one, headers=as_("viewer", Accept=NDJSON))

    assert rows(table.text) == [["name"], ["plain"]]
    (document,) = [json.loads(line) for line in lines.text.splitlines()]
    assert sorted(document) == ["links", "name"]


def test_exports_stop_at_their_bound_across_chunks(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each chunk asks only for what the bound leaves."""
    use_auth()
    store.items.update(
        {gadget.id: gadget for gadget in (make_gadget("d"), make_gadget("e"))}
    )

    async def fewer(*args: object, **options: object) -> int:
        return 3

    monkeypatch.setattr(store, "count", fewer)
    monkeypatch.setattr(routing, "EXPORT_CHUNK", 2)
    client = client_of(gadgets(store, exports=("ndjson",), max_export=3))

    lines = client.get("/gadgets", headers=as_("viewer", Accept=NDJSON)).text

    assert len(lines.splitlines()) == 3


def test_exports_keep_to_the_callers_scope_and_filters(
    store: InMemoryGadgets, use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every chunk is of the list's query: out of scope and filtered out stay out."""
    use_auth()
    monkeypatch.setattr(routing, "EXPORT_CHUNK", 1)

    async def not_plain(request: object) -> object:
        return F("name") != "plain"

    client = client_of(
        gadgets(
            store,
            exports=("csv",),
            filters=GadgetFilters,
            scope=not_plain,
            list_fields=["name"],
        )
    )

    every = client.get("/gadgets", headers=as_("viewer", Accept=CSV))
    some = client.get(
        "/gadgets", params={"search": "5"}, headers=as_("viewer", Accept=CSV)
    )

    assert sorted(rows(every.text)[1:]) == [["'=cmd|calc"], ["-5"]]
    assert rows(some.text) == [["name"], ["-5"]]
