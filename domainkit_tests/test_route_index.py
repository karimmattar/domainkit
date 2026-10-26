"""Routes are tried by their first path segment, and match as before (PERF-05).

``RouteIndex`` leaves out of a request's candidates the routes whose
template starts with another literal segment. Each test compares what
trying the index's candidates selects with what trying every route in
order selects (``APIRouter.app``), on the test projects' routes and on
generated ones with overlapping, order-dependent templates, then serves
both over HTTP and WebSocket.
"""

import importlib
import random
import sys
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Host, Match, Mount, Route, WebSocketRoute
from starlette.types import Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.http import get_asgi_application
from domainkit.http.routing import (
    RouteIndex,
    Routes,
    first_segment,
    flatten_routes,
    index_routes,
)
from domainkit_tests.conftest import UseAuth

PROJECTS = (
    "domainkit_tests.nested_routes",
    "domainkit_tests.sample_auth_routes",
    "domainkit_tests.staff_routes",
    "domainkit_tests.sample_identity_routes",
    "domainkit_tests.deadline_routes",
    "domainkit_tests.sample_routes",
    "domainkit_tests.reading_routes",
)
"""Routers of the test projects that import alike under any settings.

``admin_routes`` registers what the installed domains give it when first
imported, so it is left to the tests that install them."""
SAMPLES = {"str": "abc", "int": "42", "path": "a/b/c", "uuid": "0" * 32, "float": "1.5"}
METHODS = ("GET", "POST", "DELETE", "HEAD")


def seeded(seed: int) -> random.Random:
    """Reproducible routes and paths (not secrets)."""
    return random.Random(seed)  # noqa: S311 - reproducible test data.


def chosen(routes: Sequence[BaseRoute], scope: Scope) -> tuple[int, Match] | None:
    """The route trying ``routes`` in order serves, as ``APIRouter.app`` picks it."""
    partial = None
    for route in routes:
        match, _ = route.matches(dict(scope))
        if match == Match.FULL:
            return id(route), match
        if match == Match.PARTIAL and partial is None:
            partial = (id(route), match)
    return partial


def scope_of(
    path: str, method: str = "GET", kind: str = "http", root_path: str = ""
) -> Scope:
    """A request for ``path`` (under ``root_path``), with a host."""
    return {
        "type": kind,
        "method": method,
        "path": root_path + path,
        "root_path": root_path,
        "headers": [(b"host", b"api.example.test")],
        "query_string": b"",
    }


def instances(template: str, rng: random.Random) -> Iterator[str]:
    """Paths a template matches, and near misses: slashes, extra and other parts."""
    parts = []
    for part in template.split("/"):
        while "{" in part:
            start, end = part.index("{"), part.index("}")
            _, _, kind = part[start + 1 : end].partition(":")
            part = part[:start] + SAMPLES[kind or "str"] + part[end + 1 :]
        parts.append(part)
    path = "/".join(parts) or "/"
    yield path
    yield path.rstrip("/") + "/"
    yield path.rstrip("/")
    yield path + "/extra"
    yield path.replace("/", "/x", 1)
    yield "/" + rng.choice(("unknown", "", "items", "42", "static"))


def paths_for(routes: Sequence[BaseRoute], rng: random.Random) -> list[str]:
    """Every route's paths and near misses, and a few no route names."""
    found = ["/", "", "/nothing/here", "//", "/items//x"]
    for route in routes:
        template = getattr(route, "path", None)
        if isinstance(template, str):
            found.extend(instances(template or "/", rng))
    return found


def assert_same_choices(routes: list[BaseRoute], rng: random.Random) -> int:
    """Every request picks the same route through the index; how many tried."""
    index = RouteIndex(APIRouter(routes=routes))
    tried = 0
    for path in paths_for(routes, rng):
        for root_path in ("", "/api"):
            for kind, method in [("websocket", "GET")] + [("http", m) for m in METHODS]:
                scope = scope_of(path, method, kind, root_path)
                expected = chosen(routes, scope)
                assert chosen(index.candidates(scope), scope) == expected, scope
                tried += 1
    return tried


def test_the_test_projects_routes_match_alike(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each project's flattened routes: the same route for every request.

    The routers this test imports first are forgotten after it, so the
    tests that import them under their own settings get their own.
    """
    use_auth()
    for module in PROJECTS:
        if module not in sys.modules:
            monkeypatch.setitem(sys.modules, module, None)
            monkeypatch.delitem(sys.modules, module)
    rng = seeded(5)
    tried = 0
    for module in PROJECTS:
        app = FastAPI()
        app.include_router(importlib.import_module(module).router)
        flatten_routes(app.router)
        tried += assert_same_choices(app.router.routes, rng)
    assert tried > 5_000


async def answer(request: Request) -> PlainTextResponse:
    """Name the route that answered."""
    route = request.scope["route"]
    return PlainTextResponse(f"{route.path} {request.scope.get('path_params')}")


async def talk(websocket: WebSocket) -> None:
    """Accept, name the route, and close."""
    await websocket.accept()
    await websocket.send_text(websocket.scope["path"])
    await websocket.close()


async def other_app(scope: Scope, receive: Receive, send: Send) -> None:
    """A mounted application that names the path it was given."""
    await PlainTextResponse(f"mounted {scope['path']}")(scope, receive, send)


SEGMENTS = (
    "items",
    "users",
    "a",
    "static",
    "{id}",
    "{id:int}",
    "{rest:path}",
    "{name}.json",
    "items{suffix}",
    "",
)


class OwnMatching(APIRoute):
    """A route class that matches in its own way: tried for every request."""

    def matches(self, scope: Scope) -> Any:  # noqa: ANN401 - Starlette's.
        """Match as an ``APIRoute``."""
        return super().matches(scope)


def generated(rng: random.Random, count: int) -> list[BaseRoute]:
    """``count`` routes of every kind, templates overlapping, in random order."""
    routes: list[BaseRoute] = []
    for number in range(count):
        parts = [rng.choice(SEGMENTS) for _ in range(rng.randrange(1, 4))]
        names: set[str] = set()
        for index, part in enumerate(parts):
            if "{" in part:  # Each parameter named once.
                name = part[part.index("{") + 1 : part.index("}")].partition(":")[0]
                if name in names:
                    parts[index] = f"p{number}{index}"
                names.add(name)
        path = "/" + "/".join(parts)
        if "{rest:path}" in path and not path.endswith("{rest:path}"):
            path = path.split("{rest:path}")[0] + "{rest:path}"
        kind = rng.randrange(10)
        methods = [rng.choice(METHODS[:3])]
        if kind < 5:
            routes.append(APIRoute(path, answer, methods=methods))
        elif kind == 5:
            routes.append(Route(path, answer, methods=methods))
        elif kind == 6:
            routes.append(WebSocketRoute(path, talk))
        elif kind == 7:
            routes.append(Mount(path.rstrip("/").split("{")[0].rstrip("/"), other_app))
        elif kind == 8:
            routes.append(OwnMatching(path, answer, methods=methods))
        else:
            routes.append(Host("api.example.test", other_app))
    return routes


@pytest.mark.parametrize("seed", range(12))
def test_generated_routes_match_alike(seed: int) -> None:
    """Random overlapping routes, 10 to 400 of them: the same route each time."""
    rng = seeded(seed)
    routes = generated(rng, rng.choice((10, 40, 100, 400)))
    assert assert_same_choices(routes, rng) > 100


def served(app: FastAPI, path: str, method: str) -> tuple[int, str, list[Any]]:
    """What ``app`` answers to ``method path``, with its headers."""
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(method, path, follow_redirects=False)
    return response.status_code, response.text, sorted(response.headers.items())


@pytest.mark.parametrize("seed", range(4))
def test_generated_routes_are_served_alike(seed: int) -> None:
    """Over HTTP: statuses, bodies, and headers, 404, 405, and redirects too."""
    rng = seeded(100 + seed)
    routes = generated(rng, 30)
    linear, indexed = FastAPI(), FastAPI()
    for app in (linear, indexed):
        app.router.routes.extend(routes)
    assert index_routes(indexed.router)
    with (
        TestClient(linear, raise_server_exceptions=False) as plain,
        TestClient(indexed, raise_server_exceptions=False) as fast,
    ):
        for path in paths_for(routes, rng)[:120]:
            if not path.startswith("/"):
                continue
            for method in ("GET", "POST"):
                first = plain.request(method, path, follow_redirects=False)
                second = fast.request(method, path, follow_redirects=False)
                assert (second.status_code, second.text, second.headers) == (
                    first.status_code,
                    first.text,
                    first.headers,
                ), (method, path)


def test_websockets_are_served_through_the_index() -> None:
    """A WebSocket route behind others: accepted, and the right one."""
    app = FastAPI()
    app.router.routes.extend(
        [
            APIRoute("/items/{id}", answer, methods=["GET"]),
            WebSocketRoute("/live/{id}", talk),
        ]
    )
    assert index_routes(app.router)
    with TestClient(app) as client, client.websocket_connect("/live/7") as socket:
        assert socket.receive_text() == "/live/7"


def test_routes_are_indexed_by_their_first_literal_segment() -> None:
    """A literal first segment, or None: any request may reach the route."""
    cases: list[tuple[BaseRoute, str | None]] = [
        (APIRoute("/items/{id}", answer), "items"),
        (APIRoute("/", answer), ""),
        (APIRoute("/items.json", answer), "items.json"),
        (APIRoute("/{id}", answer), None),
        (APIRoute("/items{suffix}", answer), None),
        (Route("/users", answer), "users"),
        (WebSocketRoute("/live", talk), "live"),
        (Mount("/static", other_app), "static"),
        (Mount("", other_app), None),
        (Mount("/{tenant}/x", other_app), None),
        (Host("example.test", other_app), None),
        (OwnMatching("/items", answer), None),
    ]
    for route, segment in cases:
        assert first_segment(route) == segment, route
    route = APIRoute("/items", answer)
    route.matches = lambda scope: (Match.NONE, {})  # type: ignore[method-assign]
    assert first_segment(route) is None


def test_routes_added_after_indexing_are_tried(use_auth: UseAuth) -> None:
    """The index follows the router's list: a route added later is served."""
    use_auth()
    with override_settings(ROOT_ROUTER="domainkit_tests.nested_routes.router"):
        app = get_asgi_application()
    assert isinstance(app.router.middleware_stack, RouteIndex)
    app.router.add_api_route("/late/{id}", answer, dependencies=[])
    with TestClient(app) as client:
        assert client.get("/late/3").text == "/late/{id} {'id': '3'}"
    app.router.routes[:] = [APIRoute("/again", answer)]  # Replaced in place.
    with TestClient(app) as client:
        assert client.get("/again").status_code == 200
        assert client.get("/late/3").status_code == 404


def test_lifespans_reach_the_router() -> None:
    """Startup and shutdown pass through the index."""
    events: list[str] = []
    app = FastAPI(
        on_startup=[lambda: events.append("up")],
        on_shutdown=[lambda: events.append("down")],
    )
    assert index_routes(app.router)
    with TestClient(app):
        pass
    assert events == ["up", "down"]


def test_routers_that_serve_in_their_own_way_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Their own ``app``, middleware, or no telemetry hook: no index."""

    class Own(APIRouter):
        async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
            """Serve its own way."""
            await super().app(scope, receive, send)

    assert not index_routes(Own())
    given = APIRouter()
    given.app = given.app  # type: ignore[method-assign]
    assert not index_routes(given)
    wrapped = APIRouter()
    wrapped.middleware_stack = Starlette()
    assert not index_routes(wrapped)
    monkeypatch.setattr("domainkit.http.routing._SELECTED", None)
    assert not index_routes(APIRouter())


def test_without_flattening_there_is_no_index(use_auth: UseAuth) -> None:
    """``FLATTEN_ROUTERS = False`` keeps FastAPI's own matching, index included."""
    use_auth()
    with override_settings(
        ROOT_ROUTER="domainkit_tests.nested_routes.router", FLATTEN_ROUTERS=False
    ):
        app = get_asgi_application()
    assert not isinstance(app.router.middleware_stack, RouteIndex)


def test_a_known_path_with_another_method_is_refused_alike() -> None:
    """A partial match (405) through the index, as without it, and HEAD."""
    app = FastAPI()
    app.router.routes.extend(
        [
            APIRoute("/{name}", answer, methods=["DELETE"]),
            APIRoute("/items/{id}", answer, methods=["GET"]),
            APIRoute("/items/{id}", answer, methods=["PUT"]),
        ]
    )
    plain = FastAPI()
    plain.router.routes.extend(app.router.routes)
    assert index_routes(app.router)

    for method in ("POST", "HEAD", "PUT", "DELETE"):
        assert served(app, "/items/3", method) == served(plain, "/items/3", method)
    assert served(app, "/items/3", "POST")[0] == 405


def test_an_index_made_by_hand_serves_without_fastapis_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a FastAPI without its telemetry hook, a ``RouteIndex`` still serves."""
    monkeypatch.setattr("domainkit.http.routing._SELECTED", None)
    app = FastAPI()
    app.router.routes.append(APIRoute("/items/{id}", answer, methods=["GET"]))
    app.router.middleware_stack = RouteIndex(app.router)

    assert served(app, "/items/3", "GET")[:2] == (200, "/items/{id} {'id': '3'}")


def test_a_route_replaced_in_place_is_served_as_replaced(use_auth: UseAuth) -> None:
    """Hardened after the build, same length: the new route answers (SEC-10a-6)."""
    use_auth()
    from fastapi import Depends

    from domainkit.http.access import Public, Require
    from domainkit_tests.test_scoped_credentials import HeaderAuthentication

    def build(indexed: bool) -> FastAPI:
        app = FastAPI()

        @app.get("/reports", dependencies=[Public])
        async def reports() -> dict[str, str]:
            return {"secret": "report"}

        if indexed:
            assert index_routes(app.router)
        place = next(
            n
            for n, r in enumerate(app.router.routes)
            if getattr(r, "path", "") == "/reports"
        )
        app.router.routes[place] = APIRoute(
            "/reports", reports, methods=["GET"], dependencies=[Depends(Require())]
        )
        app.add_middleware(HeaderAuthentication)
        return app

    assert served(build(indexed=True), "/reports", "GET")[0] == 401
    assert served(build(indexed=False), "/reports", "GET")[0] == 401


@pytest.mark.parametrize(
    "change",
    [
        lambda routes, route: routes.__setitem__(0, route),
        lambda routes, route: routes.__setitem__(slice(0, 1), [route]),
        lambda routes, route: routes.__delitem__(0),
        lambda routes, route: routes.__iadd__([route]),
        lambda routes, route: routes.__imul__(2),
        lambda routes, route: routes.append(route),
        lambda routes, route: routes.extend([route]),
        lambda routes, route: routes.insert(0, route),
        lambda routes, route: routes.pop(),
        lambda routes, route: routes.remove(routes[0]),
        lambda routes, route: routes.clear(),
        lambda routes, route: routes.sort(key=lambda each: each.path),
        lambda routes, route: routes.reverse(),
    ],
)
def test_every_change_to_the_routes_is_counted(change: Any) -> None:  # noqa: ANN401
    """Any list method that changes the routes moves their version."""
    router = APIRouter()
    router.routes.extend([APIRoute("/b", answer), APIRoute("/a", answer)])
    index = RouteIndex(router)
    routes = router.routes
    assert isinstance(routes, Routes)
    before = routes.version

    change(routes, APIRoute("/c", answer))

    assert routes.version == before + 1
    index.candidates(scope_of("/a"))
    assert index._index.version == routes.version


def test_a_new_list_given_to_the_router_is_indexed() -> None:
    """``router.routes = [...]``: indexed, and counted from then on."""
    router = APIRouter()
    index = RouteIndex(router)
    router.routes = [APIRoute("/late", answer)]

    assert list(index.candidates(scope_of("/late"))) == router.routes
    assert isinstance(router.routes, Routes)


async def asgi(app: Any, path: str, method: str) -> tuple[int, bytes]:  # noqa: ANN401
    """Send ``method path`` straight to ``app``, in this task; status and body."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {**scope_of(path, method), "query_string": b"", "headers": []}, receive, send
    )
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return sent[0]["status"], body


@pytest.mark.anyio
async def test_another_method_on_a_starlette_route_is_a_partial_match() -> None:
    """A Starlette route answers 405 through the index as without it."""
    app = FastAPI()
    app.router.routes.append(Route("/items/{id}", answer, methods=["GET"]))
    plain = FastAPI()
    plain.router.routes.extend(app.router.routes)
    assert index_routes(app.router)

    assert await asgi(app, "/items/3", "POST") == await asgi(plain, "/items/3", "POST")
    assert (await asgi(app, "/items/3", "POST"))[0] == 405


@pytest.mark.anyio
async def test_an_indexed_application_mounted_in_another_keeps_the_outer_router() -> (
    None
):
    """``scope["router"]`` set above is left as it is, as FastAPI leaves it."""
    inner = FastAPI()
    seen: list[object] = []

    async def router_of(request: Request) -> PlainTextResponse:
        seen.append(request.scope["router"])
        return PlainTextResponse("ok")

    inner.router.routes.append(APIRoute("/here", router_of))
    assert index_routes(inner.router)
    outer = FastAPI()
    outer.mount("/inner", inner)

    assert (await asgi(outer, "/inner/here", "GET"))[0] == 200
    assert seen == [outer.router]


@pytest.mark.anyio
async def test_paths_no_route_matches_are_left_to_the_router() -> None:
    """A redirect to the slashed path, and a 404, as FastAPI answers them."""
    app = FastAPI()
    app.router.routes.append(APIRoute("/items/", answer))
    plain = FastAPI()
    plain.router.routes.extend(app.router.routes)
    assert index_routes(app.router)

    for path in ("/items", "/nothing"):
        assert await asgi(app, path, "GET") == await asgi(plain, path, "GET")
    assert (await asgi(app, "/items", "GET"))[0] == 307
    assert (await asgi(app, "/nothing", "GET"))[0] == 404
