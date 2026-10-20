"""The API surface: operation ids, the ``openapi`` command, versions, deprecations."""

import io
import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from domainkit.conf import override_settings
from domainkit.ddd import NotFound
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http import openapi as openapi_module
from domainkit.http.openapi import (
    MAX_DEPTH,
    breaking_changes,
    duplicate_operation_ids,
)
from domainkit.http.versioning import (
    Deprecation,
    DeprecationHeadersMiddleware,
    versioned_router,
)
from domainkit.management import CommandError, call_command

ROUTES = "domainkit_tests.test_openapi.router"
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
SUNSET = datetime(2027, 1, 1, 12, tzinfo=UTC)
OLD = Deprecation(since=SINCE, sunset=SUNSET, link="https://example.com/v2")
GONE_SOON = Deprecation(since=date(2026, 6, 1))
LINK_REFUSED = (
    "A deprecation's link is an absolute http(s) URL without spaces, quotes, or "
    "angle brackets."
)


class Item(BaseModel):
    """An item."""

    name: str


items = APIRouter()


@items.get("/items")
async def list_items() -> list[Item]:
    """Every item."""
    return [Item(name="lamp")]


@items.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int) -> Response:
    """Delete an item, answering with a ready-made response."""
    if item_id == 0:
        raise NotFound("No such item.")
    if item_id == 1:  # A route may announce its own sunset.
        return Response(status_code=204, headers={"Sunset": "soon"})
    return Response(status_code=204, headers={"Link": '</items>; rel="collection"'})


@items.get("/items/{item_id}", dependencies=[Depends(GONE_SOON)])
async def read_item(item_id: int) -> Item:
    """One item; deprecated more specifically than its version."""
    return Item(name=str(item_id))


v1 = versioned_router("v1", deprecation=OLD, tags=["v1"])
v1.include_router(items)
v2 = versioned_router("v2")
v2.include_router(items)
router = APIRouter()
router.include_router(v1)
router.include_router(v2)


@router.get("/both")
async def both() -> dict[str, bool]:
    """Answer."""
    return {"ok": True}


@router.get("/hidden", include_in_schema=False)
async def hidden() -> None:
    """Undocumented."""


twice = APIRouter()


@twice.get("/a")
async def same() -> None:
    """One route named ``same``."""


twice.add_api_route("/b", same, methods=["GET"])


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of the routes above, operation ids from route names."""
    with (
        override_settings(ROOT_ROUTER=ROUTES, OPENAPI_OPERATION_IDS="route_name"),
        TestClient(get_asgi_application()) as test_client,
    ):
        yield test_client


def operation_ids(schema: dict[str, Any]) -> dict[str, str]:
    """``METHOD path`` to operation id."""
    return {
        f"{method.upper()} {path}": operation["operationId"]
        for path, item in schema["paths"].items()
        for method, operation in item.items()
    }


def test_operation_ids_come_from_route_names(client: TestClient) -> None:
    """Versions prefix their routes' names; several methods get a suffix."""
    ids = operation_ids(client.get("/openapi.json").json())

    assert ids["GET /v1/items"] == "v1_list_items"
    assert ids["DELETE /v2/items/{item_id}"] == "v2_delete_item"
    assert ids["GET /both"] == "both"
    assert len(set(ids.values())) == len(ids)


def test_fastapi_operation_ids_stay_the_default() -> None:
    """Existing projects keep FastAPI's ids, path and method included."""
    with override_settings(ROOT_ROUTER=ROUTES):
        app = get_asgi_application()
        schema = app.openapi()

    assert app.openapi() is schema  # Built once.
    assert operation_ids(schema)["GET /both"] == "both_both_get"
    assert "GET /hidden" not in operation_ids(schema)
    assert operation_ids(schema)["GET /v1/items"] == "v1_list_items"


def test_clashing_operation_ids_are_problems() -> None:
    """``check`` lists clashes; staging and production refuse to start."""
    app = FastAPI()
    app.include_router(twice)
    app.add_api_route("/several", same, methods=["GET", "POST"], name="several")
    app.mount("/static", FastAPI())
    assert duplicate_operation_ids(app) == []
    with override_settings(OPENAPI_OPERATION_IDS="route_name"):
        clashing = FastAPI(generate_unique_id_function=openapi_module.operation_id)
        clashing.include_router(twice)
        assert duplicate_operation_ids(clashing) == [
            (
                "Operation id 'same' names GET /a and GET /b; give the routes "
                "distinct names (name=...)."
            )
        ]

    with override_settings(
        ROOT_ROUTER="domainkit_tests.test_openapi.twice",
        OPENAPI_OPERATION_IDS="route_name",
    ):
        with pytest.raises(CommandError, match="Operation id 'same'"):
            call_command("check", stdout=io.StringIO())
        with (
            override_settings(ENVIRONMENT="staging", API_DOCS=False),
            pytest.raises(ImproperlyConfigured, match="Operation id 'same'"),
        ):
            get_asgi_application()


def test_deprecated_routes_send_their_headers(client: TestClient) -> None:
    """Every response of a deprecated route: success, returned, or an error."""
    listed = client.get("/v1/items")
    deleted = client.delete("/v1/items/3")
    missing = client.delete("/v1/items/0")
    invalid = client.delete("/v1/items/x")
    current = client.get("/v2/items")

    for response in (listed, deleted, missing, invalid):
        assert response.headers["Deprecation"] == "@1767225600"
    assert listed.headers["Sunset"] == "Fri, 01 Jan 2027 12:00:00 GMT"
    assert listed.headers["Link"] == '<https://example.com/v2>; rel="deprecation"'
    assert deleted.headers.get_list("Link") == [
        '</items>; rel="collection"',
        '<https://example.com/v2>; rel="deprecation"',
    ]
    assert (missing.status_code, invalid.status_code) == (404, 422)
    assert "Deprecation" not in current.headers
    assert client.delete("/v1/items/1").headers["Sunset"] == "soon"


def test_the_most_specific_deprecation_wins(client: TestClient) -> None:
    """A route's own deprecation replaces its version's; without dates, none."""
    response = client.get("/v1/items/3")

    assert response.headers["Deprecation"] == "@1780272000"
    assert "Sunset" not in response.headers
    assert "Link" not in response.headers


def test_openapi_marks_deprecated_operations(client: TestClient) -> None:
    """``deprecated``, the dates, the link, and the headers each response sends."""
    paths = client.get("/openapi.json").json()["paths"]

    listing = paths["/v1/items"]["get"]
    assert listing["deprecated"] is True
    assert listing["x-sunset"] == SUNSET.isoformat()
    assert listing["x-deprecated-since"] == SINCE.isoformat()
    assert listing["externalDocs"] == {
        "url": "https://example.com/v2",
        "description": "Deprecation",
    }
    assert set(listing["responses"]["200"]["headers"]) == {
        "Deprecation",
        "Sunset",
        "Link",
    }
    single = paths["/v1/items/{item_id}"]["get"]
    assert set(single["responses"]["200"]["headers"]) == {"Deprecation"}
    assert "deprecated" not in paths["/v2/items"]["get"]
    assert "deprecated" in paths["/v2/items/{item_id}"]["get"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"since": datetime(2026, 1, 1)}, "Deprecation since needs a time zone."),  # noqa: DTZ001
        (
            {"since": SINCE, "sunset": datetime(2027, 1, 1)},  # noqa: DTZ001
            "Deprecation sunset needs a time zone.",
        ),
        (
            {"since": SINCE, "sunset": SINCE},
            "A deprecation's sunset must follow its since.",
        ),
        ({"since": SINCE, "link": "ftp://example.com"}, LINK_REFUSED),
        ({"since": SINCE, "link": "https://"}, LINK_REFUSED),
        ({"since": SINCE, "link": 'https://a.example/x>; rel="evil"'}, LINK_REFUSED),
        ({"since": SINCE, "link": "https://a.example/\r\nSet-Cookie: x"}, LINK_REFUSED),
    ],
)
def test_deprecations_refuse_what_headers_cannot_carry(
    options: dict[str, Any], message: str
) -> None:
    """Naive times, a sunset before the start, and links that break headers."""
    with pytest.raises(ImproperlyConfigured) as refused:
        Deprecation(**options)
    assert str(refused.value) == message


def test_deprecation_times_are_utc() -> None:
    """Other zones are converted; dates mean midnight UTC."""
    later = timezone(timedelta(hours=2))
    deprecation = Deprecation(
        since=datetime(2026, 1, 1, 2, tzinfo=later), sunset=date(2026, 2, 1)
    )

    assert deprecation.since == SINCE
    assert deprecation.since.tzinfo is UTC
    assert deprecation.headers() == [
        ("Deprecation", "@1767225600"),
        ("Sunset", "Sun, 01 Feb 2026 00:00:00 GMT"),
    ]


@pytest.mark.parametrize("version", ["", "v 1", "/v1", "x" * 33])
def test_versions_are_plain_names(version: str) -> None:
    """A version becomes a path segment and a prefix of names."""
    message = (
        f"API version {version!r} must be letters, digits, '.', '_', or '-' "
        "(at most 32), such as 'v1'."
    )
    with pytest.raises(ImproperlyConfigured, match=f"^{re.escape(message)}$"):
        versioned_router(version)


@pytest.mark.anyio
async def test_the_header_middleware_passes_other_traffic() -> None:
    """Lifespan and WebSocket traffic go through untouched."""
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        seen.append(scope["type"])

    middleware = DeprecationHeadersMiddleware(app)
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert seen == ["lifespan"]


# The openapi command ------------------------------------------------------------


def run(*args: str) -> str:
    """Run ``openapi`` on the routes above; return what it printed."""
    output = io.StringIO()
    with override_settings(ROOT_ROUTER=ROUTES, OPENAPI_OPERATION_IDS="route_name"):
        call_command("openapi", *args, stdout=output)
    return output.getvalue()


def test_openapi_prints_or_writes_the_schema(tmp_path: Path) -> None:
    """JSON to stdout, or to a file; the same schema the application serves."""
    printed = json.loads(run())
    path = tmp_path / "openapi.json"

    assert run("--output", str(path)) == f"Wrote {path}.\n"
    assert json.loads(path.read_text(encoding="utf-8")) == printed
    assert printed["paths"]["/v1/items"]["get"]["operationId"] == "v1_list_items"
    assert run("--indent", "0").count("\n") == 1


def test_openapi_diff_passes_without_breaking_changes(tmp_path: Path) -> None:
    """The same schema, or one that only adds, has no breaking change."""
    old = tmp_path / "old.json"
    schema = json.loads(run())
    del schema["paths"]["/both"]
    old.write_text(json.dumps(schema), encoding="utf-8")

    assert run("--diff", str(old)) == f"No breaking changes since {old}.\n"


def test_openapi_diff_fails_on_breaking_changes(tmp_path: Path) -> None:
    """Each change is printed; the command exits with an error."""
    old = tmp_path / "old.json"
    schema = json.loads(run())
    schema["paths"]["/gone"] = {"get": {"responses": {}}}
    schema["paths"]["/v2/items"]["patch"] = {"responses": {}}
    old.write_text(json.dumps(schema), encoding="utf-8")
    output = io.StringIO()

    with (
        override_settings(ROOT_ROUTER=ROUTES),
        pytest.raises(CommandError, match="2 breaking changes since"),
    ):
        call_command("openapi", "--diff", str(old), stdout=output)

    assert output.getvalue() == (
        "Breaking: PATCH /v2/items: removed\nBreaking: /gone: removed\n"
    )
    schema["paths"].pop("/gone")
    old.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(CommandError, match=r"^1 breaking change since"):
        run("--diff", str(old))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "Cannot read"),
        ("{", "is not a JSON document"),
        ("[1]", "is not an OpenAPI schema"),
        ('{"paths": []}', "is not an OpenAPI schema"),
        ("[" * 100_000 + "]" * 100_000, "nests deeper than 256"),
        ("[" * 257 + "]" * 257, "nests deeper than 256"),
        ("[" * 256 + "]" * 256, "is not an OpenAPI schema"),
    ],
)
def test_openapi_diff_refuses_what_is_not_a_schema(
    tmp_path: Path, content: str | None, message: str
) -> None:
    """Unreadable files, other JSON, and absurd nesting are refused."""
    path = tmp_path / "old.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(CommandError, match=message):
        run("--diff", str(path))


def test_brackets_in_strings_do_not_count_as_nesting(tmp_path: Path) -> None:
    """Only arrays and objects nest; text that holds brackets does not."""
    path = tmp_path / "old.json"
    brackets = '[{\\"' * 300
    path.write_text(f'{{"paths": {{}}, "info": {{"title": "{brackets}"}}}}', "utf-8")

    assert run("--diff", str(path)).startswith("No breaking changes since")


def test_openapi_diff_refuses_huge_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the size limit is not read."""
    from domainkit.management.commands import openapi

    path = tmp_path / "old.json"
    path.write_text('{"paths": {}}', encoding="utf-8")
    monkeypatch.setattr(openapi, "MAX_SCHEMA_BYTES", 4)

    with pytest.raises(CommandError, match="is larger than 4 bytes"):
        run("--diff", str(path))
    monkeypatch.setattr(openapi, "MAX_SCHEMA_BYTES", path.stat().st_size)
    assert run("--diff", str(path)).startswith("No breaking changes since")


# Breaking changes ---------------------------------------------------------------


def document(
    operation: dict[str, Any],
    *,
    path: str = "/things/{thing_id}",
    method: str = "post",
    schemas: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A schema with one operation, and components."""
    return {
        "paths": {path: {method: operation, "parameters": []}},
        "components": {"schemas": schemas or {}, "parameters": parameters or {}},
    }


def body(schema: dict[str, Any], *, required: bool = True) -> dict[str, Any]:
    """An operation taking a JSON body."""
    return {
        "requestBody": {
            "required": required,
            "content": {"application/json": {"schema": schema}},
        },
        "responses": {},
    }


def answer(schema: dict[str, Any], status: str = "200") -> dict[str, Any]:
    """An operation answering ``status`` with a JSON body."""
    return {
        "responses": {status: {"content": {"application/json": {"schema": schema}}}}
    }


def obj(required: tuple[str, ...] = (), **properties: Any) -> dict[str, Any]:  # noqa: ANN401
    """An object schema."""
    return {"type": "object", "properties": properties, "required": list(required)}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
NULLABLE = {"anyOf": [STRING, {"type": "null"}]}


def changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """``breaking_changes`` between two operations' documents."""
    return breaking_changes(old, new)


def test_renaming_a_path_parameter_breaks_nothing() -> None:
    """Paths are matched with their parameters unnamed."""
    old = document(answer(STRING))
    new = document(answer(STRING), path="/things/{id}")

    assert changes(old, new) == []
    assert changes({"paths": "nonsense"}, new) == []
    assert changes(document({"responses": {}}, method="parameters"), new) == []


def test_new_required_parameters_break_requests() -> None:
    """A new required parameter, or an optional one now required."""
    old = document(
        {
            "parameters": [{"name": "page", "in": "query", "schema": INTEGER}],
            "responses": {},
        }
    )
    new = document(
        {
            "parameters": [
                {"name": "page", "in": "query", "required": True, "schema": INTEGER},
                {"$ref": "#/components/parameters/Lang"},
                {"name": "debug", "in": "query", "schema": STRING},
                {"$ref": "#/components/parameters/Missing"},
            ],
            "responses": {},
        },
        parameters={
            "Lang": {"name": "lang", "in": "header", "required": True, "schema": STRING}
        },
    )

    assert changes(old, new) == [
        "POST /things/{thing_id}: new required query parameter 'page'",
        "POST /things/{thing_id}: new required header parameter 'lang'",
    ]


def test_narrower_request_values_break_requests() -> None:
    """Types, enums, bounds, and fields the server no longer accepts."""
    old = document(
        body(
            obj(
                amount=NUMBER,
                note=NULLABLE,
                kind={"enum": ["a", "b"]},
                code={"type": "string", "maxLength": 10},
                tags={"type": "array", "items": NUMBER},
                free={},
                size=INTEGER,
                level={"type": "integer", "minimum": 1},
            )
        )
    )
    new = document(
        body(
            {
                **obj(
                    ("note",),
                    amount=INTEGER,
                    note=STRING,
                    kind={"const": "a"},
                    code={"type": "string", "maxLength": 5},
                    tags={"type": "array", "items": INTEGER},
                    free=STRING,
                    size=NUMBER,
                    level={"type": "integer", "minimum": 1, "maximum": 9},
                ),
                "additionalProperties": False,
            }
        )
    )

    where = "POST /things/{thing_id}: request body"
    assert changes(old, new) == [
        f"{where} field 'note' is now required",
        f"{where} no longer accepts fields it does not declare",
        f"{where} field 'amount' no longer accepts number",
        f"{where} field 'note' no longer accepts null",
        f"{where} field 'kind' no longer accepts \"b\"",
        f"{where} field 'code' has a tighter maxLength (5)",
        f"{where} field 'tags'[] no longer accepts number",
        f"{where} field 'free' no longer accepts other types",
        f"{where} field 'level' has a tighter maximum (9)",
    ]


def test_new_enums_and_dropped_fields_break_requests() -> None:
    """A field gaining an enum, and one dropped from a closed object."""
    old = document(body(obj(colour=STRING, size=STRING)))
    new = document(
        body(
            {
                **obj(colour={"enum": ["red"]}),
                "additionalProperties": False,
            }
        )
    )
    closed_before = document(
        body({**obj(colour=STRING, size=STRING), "additionalProperties": False})
    )

    where = "POST /things/{thing_id}: request body"
    assert changes(old, new) == [
        f"{where} no longer accepts fields it does not declare",
        f"{where} no longer accepts field 'size'",
        f"{where} field 'colour' no longer accepts other values",
    ]
    assert changes(closed_before, new) == [
        f"{where} no longer accepts field 'size'",
        f"{where} field 'colour' no longer accepts other values",
    ]


def test_bodies_newly_required_or_of_another_type_break_requests() -> None:
    """A body now required, and a media type no longer accepted."""
    old = document(body(STRING, required=False))
    new = document(
        {
            "requestBody": {
                "required": True,
                "content": {"application/xml": {"schema": STRING}},
            },
            "responses": {},
        }
    )
    emptied = document({"requestBody": {"content": {}}, "responses": {}})

    assert changes(old, new) == [
        "POST /things/{thing_id}: the request body is now required",
        "POST /things/{thing_id}: no longer accepts application/json",
    ]
    assert changes(old, emptied) == []


def test_responses_must_keep_what_clients_read() -> None:
    """Removed or optional fields, wider types, more enum values, gone statuses."""
    old = document(
        answer(
            obj(
                ("id", "name"),
                id=INTEGER,
                name=STRING,
                state={"enum": ["open"]},
                tags={"type": "array", "items": STRING},
                gone=STRING,
                status={"const": "ok"},
            )
        )
    )
    new = document(
        answer(
            obj(
                ("id",),
                id=NUMBER,
                name=NULLABLE,
                state={"enum": ["open", "closed"]},
                tags={"type": "array", "items": {"type": ["string", "integer"]}},
                status=STRING,
            )
        )
    )

    where = "POST /things/{thing_id} 200"
    assert changes(old, new) == [
        f"{where}: removed field 'gone'",
        f"{where} field 'id' may now be number",
        f"{where}: field 'name' is no longer always present",
        f"{where} field 'name' may now be null",
        f"{where} field 'state' may now be \"closed\"",
        f"{where} field 'status' may now be other values",
        f"{where} field 'tags'[] may now be integer",
    ]


def test_success_statuses_must_stay() -> None:
    """A success status no longer answered breaks; error statuses may go."""
    old = document(
        {"responses": {"200": {"description": "ok"}, "404": {"description": "no"}}}
    )
    new = document({"responses": {"201": {"description": "created"}}})

    assert changes(old, new) == ["POST /things/{thing_id}: no longer answers 200"]


def test_references_are_followed_once() -> None:
    """``$ref``s resolve in each document; cycles and bad refs stop safely."""
    node = obj(("next",), next={"$ref": "#/components/schemas/Node"}, value=STRING)
    old = document(
        answer({"$ref": "#/components/schemas/Node"}),
        schemas={"Node": node, "Loop": {"$ref": "#/components/schemas/Loop"}},
    )
    new_node = obj(("next",), next={"$ref": "#/components/schemas/Node"})
    new = document(
        answer({"$ref": "#/components/schemas/Node"}),
        schemas={"Node": new_node},
    )
    loop = document(
        answer({"$ref": "#/components/schemas/Loop"}),
        schemas={"Loop": {"$ref": "#/components/schemas/Loop"}},
    )
    elsewhere = document(answer({"$ref": "other.json#/Node"}))

    assert changes(old, new) == ["POST /things/{thing_id} 200: removed field 'value'"]
    assert changes(loop, loop) == []
    assert changes(elsewhere, loop) == []
    assert changes(document(answer({"$ref": "#/components/nope/x"})), loop) == []


def test_composed_schemas_are_compared_by_what_they_allow() -> None:
    """``oneOf``, single ``allOf``, ``const``, and listed types."""
    old = document(
        answer(
            {
                "allOf": [
                    obj(
                        kind={"oneOf": [STRING, INTEGER]},
                        flag={"const": True},
                        size={"type": ["integer", "null"]},
                        empty={"enum": []},
                        thing={"allOf": [{"const": 1.5}, {"const": None}]},
                    )
                ]
            }
        )
    )
    new = document(
        answer(
            obj(
                kind={"oneOf": [STRING]},
                flag={"enum": [True, None, [1], {"a": 1}]},
                size=INTEGER,
                empty={"enum": []},
                thing={"anyOf": [{"const": 1.5}]},
            )
        )
    )

    where = "POST /things/{thing_id} 200 field"
    assert changes(old, new) == [
        f"{where} 'flag' may now be array, null, object",
        f"{where} 'flag' may now be [1], null, {{\"a\": 1}}",
    ]


def test_request_cycles_and_new_optional_fields_are_fine() -> None:
    """A self-referencing body is compared once; an optional field is no break."""
    tree = {
        "properties": {
            "child": {"$ref": "#/components/schemas/Tree"},
            "rank": {"enum": [1, 2]},
        }
    }
    old = document(body({"$ref": "#/components/schemas/Tree"}), schemas={"Tree": tree})
    grown = {"properties": {**tree["properties"], "label": STRING}}
    new = document(body({"$ref": "#/components/schemas/Tree"}), schemas={"Tree": grown})
    fewer = {"properties": {**tree["properties"], "rank": {"enum": [1]}}}
    narrowed = document(
        body({"$ref": "#/components/schemas/Tree"}), schemas={"Tree": fewer}
    )

    assert changes(old, new) == []
    assert changes(old, narrowed) == [
        "POST /things/{thing_id}: request body field 'rank' no longer accepts 2"
    ]


def test_schemas_made_of_themselves_end() -> None:
    """A composition that refers to itself allows anything, and ends."""
    looping = {
        "Any": {"anyOf": [{"$ref": "#/components/schemas/Any"}, STRING]},
        "All": {"allOf": [{"$ref": "#/components/schemas/All"}]},
    }
    old = document(answer(obj(a=STRING)), schemas=looping)
    new = document(
        answer(
            obj(
                a={"$ref": "#/components/schemas/Any"},
                b={"$ref": "#/components/schemas/All"},
            )
        ),
        schemas=looping,
    )

    assert changes(old, new) == [
        "POST /things/{thing_id} 200 field 'a' may now be other types"
    ]
    assert changes(new, new) == []


def test_deep_schemas_stop_at_the_depth_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nesting past ``MAX_DEPTH`` is not followed."""
    monkeypatch.setattr(openapi_module, "MAX_DEPTH", 1)
    old = document(answer(obj(a=obj(b=obj(c=STRING)))))
    new = document(answer(obj(a=obj(b=obj()))))

    assert changes(old, new) == []


def test_bounds_compare_numbers_only() -> None:
    """A new bound is tighter; a looser or non-numeric one is not."""
    tightened = openapi_module._tightened
    assert tightened(None, 5, min) is True
    assert tightened(10, 20, min) is False
    assert tightened(10, 10, min) is False
    assert tightened(1, 2, max) is True
    assert tightened(1, True, max) is False
    assert tightened(True, 2, max) is True
    assert tightened(1, "2", max) is False


def nested(levels: int, leaf: dict[str, Any], key: str) -> dict[str, Any]:
    """``leaf`` inside ``levels`` objects (``properties``) or arrays (``items``)."""
    schema = leaf
    for _ in range(levels):
        schema = obj(a=schema) if key == "properties" else {"items": schema}
    return schema


def composed(levels: int, leaf: dict[str, Any], key: str) -> dict[str, Any]:
    """``leaf`` inside ``levels`` single-member ``anyOf`` (or ``allOf``) lists."""
    schema = leaf
    for _ in range(levels):
        schema = {key: [schema]}
    return schema


def parameter(schema: dict[str, Any]) -> dict[str, Any]:
    """An operation taking one query parameter."""
    return {"parameters": [{"name": "q", "in": "query", "schema": schema}]}


@pytest.mark.parametrize("key", ["properties", "items"])
def test_nesting_is_followed_sixty_four_levels_deep(key: str) -> None:
    """Parameters, bodies, and responses: level 64 is compared, 65 is not."""
    for levels, expected in ((MAX_DEPTH, 1), (MAX_DEPTH + 1, 0)):
        old, new = nested(levels, STRING, key), nested(levels, INTEGER, key)
        for operation in (parameter, body, answer):
            found = changes(document(operation(old)), document(operation(new)))
            assert len(found) == expected, (levels, operation.__name__, found)


@pytest.mark.parametrize("key", ["anyOf", "allOf"])
def test_compositions_are_followed_sixty_four_levels_deep(key: str) -> None:
    """Types and properties of compositions nested up to ``MAX_DEPTH`` count."""
    where = "POST /things/{thing_id} 200"
    deep = document(answer(composed(MAX_DEPTH, obj(gone=STRING), key)))
    deeper = document(answer(composed(MAX_DEPTH + 1, obj(gone=STRING), key)))
    integer = document(answer(INTEGER))
    emptied = document(answer(obj()))

    removed = [f"{where}: removed field 'gone'"] if key == "allOf" else []
    assert changes(deep, integer) == [f"{where} may now be integer", *removed]
    assert changes(deeper, integer) == []
    assert changes(deep, emptied) == removed
    assert changes(deeper, emptied) == []


def test_every_way_to_name_a_type_counts() -> None:
    """``oneOf``, ``properties`` alone, enum values, and malformed lists."""
    old = document(
        body(
            obj(
                one={"oneOf": [STRING, INTEGER]},
                shape={"properties": {"x": STRING}},
                values={"enum": [True, 1, 2.5]},
                nothing={"anyOf": []},
                untyped={"type": []},
                both={"allOf": [STRING, INTEGER]},
                loose={"anyOf": [{}, STRING]},
            )
        )
    )
    new = document(
        body(
            obj(
                one={"oneOf": [STRING]},
                shape=STRING,
                values=STRING,
                nothing=STRING,
                untyped=STRING,
                both=INTEGER,
                loose=INTEGER,
            )
        )
    )

    where = "POST /things/{thing_id}: request body field"
    assert changes(old, new) == [
        f"{where} 'one' no longer accepts integer",
        f"{where} 'shape' no longer accepts object",
        f"{where} 'values' no longer accepts boolean, integer, number",
        f"{where} 'nothing' no longer accepts other types",
        f"{where} 'untyped' no longer accepts other types",
        f"{where} 'both' no longer accepts other types",
        f"{where} 'loose' no longer accepts other types",
    ]


def test_references_inside_compositions_are_followed() -> None:
    """``anyOf`` and ``allOf`` members and their properties may be ``$ref``s."""
    schemas = {"Text": STRING, "Shape": obj(x=STRING, y=STRING)}
    old = document(
        body(
            obj(
                text={"anyOf": [{"anyOf": [{"$ref": "#/components/schemas/Text"}]}]},
                shape={"allOf": [{"allOf": [{"$ref": "#/components/schemas/Shape"}]}]},
            )
        ),
        schemas=schemas,
    )
    new = document(
        body(
            obj(
                text={"anyOf": [INTEGER]},
                shape={
                    "allOf": [
                        {**obj(x=INTEGER, y=STRING), "additionalProperties": False}
                    ]
                },
            )
        ),
        schemas=schemas,
    )
    answers = document(
        answer(
            obj(shape={"allOf": [{"allOf": [{"$ref": "#/components/schemas/Shape"}]}]})
        ),
        schemas=schemas,
    )
    narrower = document(answer(obj(shape={"allOf": [obj(x=STRING)]})))

    where = "POST /things/{thing_id}: request body field"
    assert changes(old, new) == [
        f"{where} 'text' no longer accepts string",
        f"{where} 'shape' field 'x' no longer accepts string",
    ]
    assert changes(answers, narrower) == [
        "POST /things/{thing_id} 200 field 'shape': removed field 'y'"
    ]


def test_each_pair_of_references_is_compared_once() -> None:
    """Pairs are told apart by both of their references, inline schemas too."""
    text = {"$ref": "#/components/schemas/Text"}
    number = {"$ref": "#/components/schemas/Number"}
    schemas = {"Text": STRING, "Number": INTEGER}

    def fields(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        return document(body(obj(a=a, b=b)), schemas=schemas)

    where = "POST /things/{thing_id}: request body field 'b'"
    assert changes(fields(STRING, INTEGER), fields(text, text)) == [
        f"{where} no longer accepts integer"
    ]
    assert changes(fields(text, number), fields(text, text)) == [
        f"{where} no longer accepts integer"
    ]
    assert changes(fields(text, text), fields(text, number)) == [
        f"{where} no longer accepts string"
    ]


def test_boolean_schemas_and_missing_items_are_tolerated() -> None:
    """``true`` as a schema allows anything; arrays without items are not compared."""
    old = document(body(obj(flag=True, list={"type": "array", "items": STRING})))
    new = document(body(obj(flag=STRING, list={"type": "array"})))

    flag = "POST /things/{thing_id}: request body field 'flag'"
    assert changes(old, new) == [f"{flag} no longer accepts other types"]
    assert changes(new, old) == []


def test_escaped_pointers_are_followed() -> None:
    """``~1`` is a slash and ``~0`` a tilde in a ``$ref``."""
    old = document(
        answer({"$ref": "#/components/schemas/a~1b~0c"}),
        schemas={"a/b~c": obj(x=STRING)},
    )

    assert changes(old, document(answer(obj()))) == [
        "POST /things/{thing_id} 200: removed field 'x'"
    ]


def test_referenced_responses_bodies_and_parameters_are_followed() -> None:
    """``components`` responses, request bodies, and parameters resolve."""
    referenced = document(
        {
            "parameters": [{"$ref": "#/components/parameters/Lang"}],
            "requestBody": {"$ref": "#/components/requestBodies/Thing"},
            "responses": {"200": {"$ref": "#/components/responses/Thing"}},
        },
        parameters={
            "Lang": {"name": "lang", "in": "header", "required": True, "schema": STRING}
        },
    )
    referenced["components"]["requestBodies"] = {
        "Thing": {"content": {"application/json": {"schema": obj(a=STRING)}}}
    }
    referenced["components"]["responses"] = {
        "Thing": {"content": {"application/json": {"schema": obj(x=STRING)}}}
    }
    inline = document(
        {
            "parameters": [
                {"name": "lang", "in": "header", "required": True, "schema": STRING}
            ],
            **body(obj(a=INTEGER), required=False),
            **answer(obj()),
        }
    )

    assert changes(referenced, inline) == [
        "POST /things/{thing_id}: request body field 'a' no longer accepts string",
        "POST /things/{thing_id} 200: removed field 'x'",
    ]
    assert changes(inline, referenced) == [
        "POST /things/{thing_id}: request body field 'a' no longer accepts integer",
    ]


def test_parameter_values_are_compared() -> None:
    """A parameter accepting fewer values breaks; malformed ones are skipped."""
    old = document(
        {
            "parameters": [
                {"name": "limit", "in": "query", "schema": {"maximum": 100}},
            ]
        }
    )
    new = document(
        {
            "parameters": [
                {"name": "limit", "in": "query", "schema": {"maximum": 50}},
                {"name": "nowhere", "required": True},
                {"in": "query", "required": True},
            ]
        }
    )

    assert changes(old, new) == [
        "POST /things/{thing_id}: query parameter 'limit' has a tighter maximum (50)"
    ]


def test_bodies_compare_every_media_type() -> None:
    """Each media type of the old body is looked for; bodies may lack content."""
    old = document(
        {
            "requestBody": {
                "content": {
                    "application/xml": {"schema": STRING},
                    "application/json": {"schema": obj(a=STRING)},
                }
            }
        }
    )
    new = document(body(obj(a=INTEGER), required=False))
    bare = document({"requestBody": {"required": False}})

    assert changes(old, new) == [
        "POST /things/{thing_id}: no longer accepts application/xml",
        "POST /things/{thing_id}: request body field 'a' no longer accepts string",
    ]
    assert changes(old, bare) == []


def test_every_success_status_is_compared() -> None:
    """Statuses after an error or a removed one are still compared."""
    old = document(
        {
            "responses": {
                "default": {"description": "error"},
                "201": {"description": "created"},
                "200": {"content": {"application/json": {"schema": obj(x=STRING)}}},
                "202": {"content": {"application/json": {"schema": STRING}}},
                "203": {"content": {"application/problem+json": {"schema": STRING}}},
            }
        }
    )
    new = document(
        {
            "responses": {
                "200": {"content": {"application/json": {"schema": obj()}}},
                "202": {"content": {"text/plain": {"schema": STRING}}},
                "203": {"content": {"application/problem+json": {}}},
            }
        }
    )

    assert changes(old, new) == [
        "POST /things/{thing_id}: no longer answers 201",
        "POST /things/{thing_id} 200: removed field 'x'",
        "POST /things/{thing_id}: no longer answers 202 with JSON",
        "POST /things/{thing_id} 203 may now be other types",
    ]
    assert changes(document({}), new) == []
    assert changes(old, document({})) == [
        f"POST /things/{{thing_id}}: no longer answers {status}"
        for status in ("201", "200", "202", "203")
    ]


def test_paths_and_operations_after_a_removed_one_are_compared() -> None:
    """A removed path or operation does not end the comparison."""
    old = {
        "paths": {
            "/gone": {"get": {}},
            "/kept": {"put": {}, "post": answer(STRING), "patch": answer(STRING)},
        }
    }
    new = {"paths": {"/kept": {"post": answer(INTEGER), "patch": answer(INTEGER)}}}

    assert changes(old, new) == [
        "/gone: removed",
        "PUT /kept: removed",
        "POST /kept 200 may now be integer",
        "PATCH /kept 200 may now be integer",
    ]


def test_clashes_name_every_method_of_a_route() -> None:
    """Mounted apps are skipped; a route's methods are listed together."""
    with override_settings(OPENAPI_OPERATION_IDS="route_name"):
        app = FastAPI(generate_unique_id_function=openapi_module.operation_id)
        app.mount("/static", FastAPI())
        app.add_api_route("/a", same, methods=["PUT", "POST"])
        app.add_api_route("/b", same, methods=["GET"])

        assert duplicate_operation_ids(app) == [
            (
                "Operation id 'same' names POST PUT /a and GET /b; give the routes "
                "distinct names (name=...)."
            )
        ]


def test_versioned_routers_put_their_deprecation_first() -> None:
    """Errors raised by a version's own guards carry its headers too."""

    async def refuse() -> None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in.")

    async def thing() -> dict[str, bool]:
        return {"ok": True}

    old = versioned_router("v3", deprecation=OLD, dependencies=[Depends(refuse)])
    new = versioned_router("v4", dependencies=[Depends(lambda: None)], tags=["v4"])
    app = FastAPI()
    for version in (old, new):
        version.add_api_route("/thing", thing, methods=["GET"])
        app.include_router(version)
    app.add_middleware(DeprecationHeadersMiddleware)
    paths = app.openapi()["paths"]

    with TestClient(app) as test_client:
        refused = test_client.get("/v3/thing")
        answered = test_client.get("/v4/thing")

    assert refused.status_code == 401
    assert refused.headers["Deprecation"] == "@1767225600"
    assert answered.json() == {"ok": True}
    assert "Deprecation" not in answered.headers
    assert paths["/v3/thing"]["get"]["deprecated"] is True
    assert paths["/v3/thing"]["get"]["operationId"] == "v3_thing"
    assert "deprecated" not in paths["/v4/thing"]["get"]
    assert paths["/v4/thing"]["get"]["operationId"] == "v4_thing"
    assert paths["/v4/thing"]["get"]["tags"] == ["v4"]


def test_deprecation_links_are_http_or_https() -> None:
    """Both schemes are accepted, and sent as ``Link``."""
    for link in ("http://example.com/old", "https://example.com/old"):
        deprecation = Deprecation(since=SINCE, link=link)
        assert deprecation.headers()[-1] == ("Link", f'<{link}>; rel="deprecation"')


def test_finishing_tolerates_sparse_schemas() -> None:
    """Schemas without paths, components, responses, or content."""
    app = FastAPI()
    app.include_router(v1)
    schema: dict[str, Any] = {
        "paths": {
            "/v1/items": {"get": {}},
            "/v1/items/{item_id}": {"delete": {"responses": {"204": {}}}},
        }
    }

    with override_settings(ERROR_FORMAT="problem+json"):
        finished = openapi_module.finish_schema(app, schema)
        bare = openapi_module.finish_schema(app, {})

    assert finished["paths"]["/v1/items"]["get"]["deprecated"] is True
    assert finished["paths"]["/v1/items/{item_id}"]["delete"]["responses"] == {
        "204": {
            "headers": {
                name: {
                    "description": openapi_module.DEPRECATION_HEADERS[name],
                    "schema": {"type": "string"},
                }
                for name in ("Deprecation", "Sunset", "Link")
            }
        }
    }
    for document in (finished, bare):
        assert {"ProblemDetails", "FieldError"} <= set(
            document["components"]["schemas"]
        )
        assert "ErrorResponse" not in document["components"]["schemas"]


def test_openapi_keeps_text_readable(tmp_path: Path) -> None:
    """Two spaces a level by default, ``-o`` for short, text not escaped."""
    path = tmp_path / "openapi.json"
    with override_settings(PROJECT_NAME="Café"):
        printed = run()
        run("-o", str(path))

    assert printed.startswith('{\n  "openapi": ')
    assert '"title": "Café"' in printed
    assert path.read_text(encoding="utf-8") == printed


def test_openapi_diff_says_why_a_file_cannot_be_read(tmp_path: Path) -> None:
    """The reason, not the exception."""
    missing = tmp_path / "missing.json"

    with pytest.raises(CommandError) as refused:
        run("--diff", str(missing))

    assert str(refused.value) == f"Cannot read {missing}: No such file or directory."


def test_fields_required_before_stay_quiet() -> None:
    """Only fields newly required are reported."""
    old = document(body(obj(("a",), a=STRING, b=STRING)))
    new = document(body(obj(("a", "b"), a=STRING, b=STRING)))

    assert changes(old, new) == [
        "POST /things/{thing_id}: request body field 'b' is now required"
    ]


def test_references_on_either_side_are_followed() -> None:
    """Compositions and properties resolve ``$ref``s in their own document."""
    schemas = {"Text": STRING, "Shape": obj(x=STRING)}
    text = {"anyOf": [{"$ref": "#/components/schemas/Text"}]}
    shape = {"allOf": [{"$ref": "#/components/schemas/Shape"}]}
    plain = document(
        body(obj(text={"type": ["string", "integer"]}, shape=obj(x=INTEGER))),
        schemas=schemas,
    )
    referenced = document(body(obj(text=text, shape=shape)), schemas=schemas)
    answered = document(answer(obj(text=text, shape=shape)), schemas=schemas)
    answered_plain = document(answer(obj(text=STRING, shape=obj(x=STRING, y=STRING))))

    where = "POST /things/{thing_id}: request body field"
    assert changes(plain, referenced) == [
        f"{where} 'text' no longer accepts integer",
        f"{where} 'shape' field 'x' no longer accepts integer",
    ]
    assert changes(answered, document(answer(obj(text=INTEGER, shape=obj())))) == [
        "POST /things/{thing_id} 200 field 'shape': removed field 'x'",
        "POST /things/{thing_id} 200 field 'text' may now be integer",
    ]
    assert changes(answered_plain, answered) == [
        "POST /things/{thing_id} 200 field 'shape': removed field 'y'"
    ]


def test_statuses_without_json_before_are_skipped() -> None:
    """A success status that had no JSON body does not end the comparison."""
    old = document(
        {
            "responses": {
                "201": {"description": "created"},
                "200": {"content": {"application/json": {"schema": obj(x=STRING)}}},
            }
        }
    )
    new = document(
        {
            "responses": {
                "201": {"description": "created"},
                "200": {"content": {"application/json": {"schema": obj()}}},
            }
        }
    )

    assert changes(old, new) == ["POST /things/{thing_id} 200: removed field 'x'"]


def test_hidden_routes_neither_clash_nor_end_the_check() -> None:
    """A route left out of the schema is skipped; the routes after it are checked."""
    with override_settings(OPENAPI_OPERATION_IDS="route_name"):
        app = FastAPI(generate_unique_id_function=openapi_module.operation_id)
        app.add_api_route("/hidden", same, methods=["GET"], include_in_schema=False)
        app.add_api_route("/a", same, methods=["GET"])
        app.add_api_route("/b", same, methods=["GET"])

        assert duplicate_operation_ids(app) == [
            (
                "Operation id 'same' names GET /a and GET /b; give the routes "
                "distinct names (name=...)."
            )
        ]
