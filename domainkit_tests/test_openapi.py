"""The API surface: operation ids, the ``openapi`` command, versions, deprecations."""

import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel

from domainkit.conf import override_settings
from domainkit.ddd import NotFound
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http import openapi as openapi_module
from domainkit.http.openapi import breaking_changes, duplicate_operation_ids
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
        ({"since": datetime(2026, 1, 1)}, "needs a time zone"),  # noqa: DTZ001
        ({"since": SINCE, "sunset": SINCE}, "must follow"),
        ({"since": SINCE, "link": "ftp://example.com"}, "absolute http"),
        ({"since": SINCE, "link": "https://"}, "absolute http"),
        ({"since": SINCE, "link": 'https://a.example/x>; rel="evil"'}, "absolute"),
        ({"since": SINCE, "link": "https://a.example/\r\nSet-Cookie: x"}, "absolute"),
    ],
)
def test_deprecations_refuse_what_headers_cannot_carry(
    options: dict[str, Any], message: str
) -> None:
    """Naive times, a sunset before the start, and links that break headers."""
    with pytest.raises(ImproperlyConfigured, match=message):
        Deprecation(**options)


def test_deprecation_times_are_utc() -> None:
    """Other zones are converted; dates mean midnight UTC."""
    later = timezone(timedelta(hours=2))
    deprecation = Deprecation(
        since=datetime(2026, 1, 1, 2, tzinfo=later), sunset=date(2026, 2, 1)
    )

    assert deprecation.since == SINCE
    assert deprecation.headers() == [
        ("Deprecation", "@1767225600"),
        ("Sunset", "Sun, 01 Feb 2026 00:00:00 GMT"),
    ]


@pytest.mark.parametrize("version", ["", "v 1", "/v1", "x" * 33])
def test_versions_are_plain_names(version: str) -> None:
    """A version becomes a path segment and a prefix of names."""
    with pytest.raises(ImproperlyConfigured, match="API version"):
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
        ("[" * 100_000 + "]" * 100_000, "is not a JSON document"),
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
