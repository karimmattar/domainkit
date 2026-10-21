"""Included routers are flattened when the application is built (ADR 0060).

The flattened application must answer, document, and guard exactly as
FastAPI's nesting did: each test builds both and compares them.
"""

import io
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi import routing as fastapi_routing
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.types import Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings, use_settings
from domainkit.conf.env import PROJECT_ROOT_VARIABLE, project_prefix, project_root
from domainkit.contrib.auth import Public
from domainkit.contrib.auth.access import _routes, open_routes
from domainkit.contrib.auth.authentication import BaseAuthentication, Credentials
from domainkit.contrib.auth.models import PasswordHash, User
from domainkit.http import get_asgi_application, routing
from domainkit.http.application import application_problems
from domainkit.http.metrics import route_template
from domainkit.http.routing import flatten_routes, nests_routers
from domainkit.management import call_command
from domainkit_tests import nested_routes
from domainkit_tests.conftest import ALPHA, AUTH, CONTENTTYPES, UseAuth

ROUTES = "domainkit_tests.nested_routes.router"
AUDIT = "domainkit.contrib.audit"
FLAGS = "domainkit.contrib.flags"
GATEWAY = "domainkit_tests.test_routing.GatewayAuthentication"
STACK = [
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
]
FIXED_ID = {"X-Request-ID": "routing-test"}
ALICE = {"X-User": "alice", **FIXED_ID}

type Build = Callable[[], FastAPI]


class GatewayAuthentication(BaseAuthentication):
    """Users named by ``X-User``, made up on the spot: no database."""

    async def authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """A new user named by the header, if there is one."""
        name = connection.headers.get("x-user")
        if name is None:
            return None
        user = User.create(password=PasswordHash.unusable(), username=name)
        return Credentials(user, scheme="gateway")


@pytest.fixture
def nested() -> Build:
    """Build the application as FastAPI nests it (``FLATTEN_ROUTERS = False``)."""

    def build() -> FastAPI:
        with override_settings(FLATTEN_ROUTERS=False):
            return get_asgi_application()

    return build


@pytest.fixture
def gateway(use_auth: UseAuth) -> None:
    """Auth installed, users named by ``X-User``, the nested routes mounted."""
    use_auth(
        AUTHENTICATION_CLASSES=[GATEWAY],
        settings={"ROOT_ROUTER": ROUTES, "MIDDLEWARE": STACK},
    )


@pytest.fixture(autouse=True)
def no_events() -> Iterator[None]:
    """Start each test without lifespan events."""
    nested_routes.events.clear()
    yield
    nested_routes.events.clear()


def answer(client: TestClient, method: str, path: str, **options: Any) -> object:  # noqa: ANN401 - request options.
    """Everything a client sees of one response: status, raw headers, body."""
    response = client.request(method, path, follow_redirects=False, **options)
    return response.status_code, response.headers.raw, response.content


def kinds(app: FastAPI) -> list[tuple[str, str]]:
    """Each top-level route's kind and path, in order (FastAPI's own left out)."""
    return [
        (type(route).__name__, getattr(route, "path", ""))
        for route in app.routes
        if getattr(route, "path", "") not in OWN
    ]


OWN = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
OPEN = (
    "GET /open declares no access rule: add Depends(Require(...)), or "
    "dependencies=[Public] if anyone may call it."
)


@pytest.mark.usefixtures("gateway")
def test_included_routers_become_their_routes_in_order(nested: Build) -> None:
    """Each route is matched once, at the top, with its full path."""
    app = get_asgi_application()
    included = fastapi_routing._IncludedRouter

    assert kinds(app) == [
        ("APIRoute", "/items/{name}"),
        ("StampedRoute", "/v1/items/new"),
        ("StampedRoute", "/v1/items/{item_id}"),
        ("StampedRoute", "/v1/items/{item_id}"),
        ("StampedRoute", "/v1/items/"),
        ("APIRoute", "/v1/guarded/inside"),
        ("APIWebSocketRoute", "/v1/guarded/socket"),
        ("APIRoute", "/v1/plain/text"),
        ("APIRoute", "/v1/plain/hidden"),
        ("APIWebSocketRoute", "/v1/plain/feed"),
        ("APIWebSocketRoute", "/v1/echo"),
        ("Route", "/raw"),
        ("Mount", "/sub"),
        ("APIRoute", "/staff/report"),
        ("APIRoute", "/open"),
        ("APIRoute", "/v1/items/{item_id}"),
    ]
    # The routers' own routes are copies: they still serve FastAPI's nesting.
    assert getattr(nested_routes.items.routes[0], "path", None) == "/items/new"
    assert flatten_routes(app.router) == 0
    assert not any(isinstance(route, included) for route in app.routes)
    assert [type(route) for route in nested().routes][-1] is included


@pytest.mark.usefixtures("gateway")
def test_each_route_is_matched_once_and_built_once(
    nested: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted, not timed: a match per route up to the one served, no new handler."""
    before, after = nested(), get_asgi_application()
    matched: list[str] = []
    built: list[str] = []
    matches, handler = APIRoute.matches, APIRoute.get_route_handler

    def match(route: APIRoute, scope: Scope) -> Any:  # noqa: ANN401 - FastAPI's.
        matched.append(route.path)
        return matches(route, scope)

    def build(route: APIRoute) -> Any:  # noqa: ANN401 - FastAPI's.
        built.append(route.path)
        return handler(route)

    monkeypatch.setattr(APIRoute, "matches", match)
    monkeypatch.setattr(APIRoute, "get_route_handler", build)
    flat, deep = TestClient(after), TestClient(before)
    for _ in range(3):
        assert flat.get("/v1/items/new").status_code == 200
    flattened = (list(matched), list(built))
    matched.clear()
    for _ in range(3):
        assert deep.get("/v1/items/new").status_code == 200

    assert flattened == (["/items/{name}", "/v1/items/new"] * 3, [])
    assert len(matched) > len(flattened[0])


@pytest.mark.usefixtures("gateway")
def test_rules_and_dependencies_are_the_ones_nesting_gave(nested: Build) -> None:
    """Deny by default sees the same rules, router-level ones included.

    Path operations are compared with nesting as it is read. WebSockets
    are checked by name: nested, FastAPI 0.158 hides their full path and
    the rules of the ``include_router`` calls from ``iter_route_contexts``
    readers, so ``/v1/plain/feed`` would be listed as ``/feed``, open.
    """
    before, after = nested(), get_asgi_application()

    def rules(app: FastAPI) -> list[tuple[str, list[object]]]:
        return [
            (name, [dependency.call for dependency in dependant.dependencies])
            for name, dependant in _routes(app.routes)
            if dependant is not None
        ]

    def operations(app: FastAPI) -> list[tuple[str, list[object]]]:
        return [rule for rule in rules(app) if not rule[0].startswith("WEBSOCKET")]

    assert operations(after) == operations(before)
    assert open_routes(after) == ["GET /open"]
    assert application_problems(after) == [OPEN]
    found = dict(rules(after))
    router_rule = nested_routes.guarded.dependencies[0].dependency
    public = Public.dependency
    assert found["GET /v1/guarded/inside"] == [router_rule]
    assert found["WEBSOCKET /v1/guarded/socket"] == [router_rule]
    assert found["WEBSOCKET /v1/plain/feed"] == [public]
    assert found["WEBSOCKET /v1/echo"] == [public]
    assert found["GET /staff/report"] == [nested_routes.staff_rule.dependency]


@pytest.mark.usefixtures("gateway")
@pytest.mark.parametrize(
    ("method", "path", "options"),
    [
        ("GET", "/v1/items/new", {}),
        ("GET", "/v1/items/7", {"headers": {"X-Tag": "a", **FIXED_ID}}),
        ("GET", "/v1/items/seven", {}),  # 422: not a number.
        ("PUT", "/v1/items/7", {"json": {"name": "x"}}),
        ("POST", "/v1/items/7", {}),  # The later route, past a partial match.
        ("DELETE", "/v1/items/7", {}),  # 405, with every method in Allow.
        ("POST", "/v1/items/", {"json": {"name": "x"}}),
        ("GET", "/v1/items", {}),  # Redirected to the slash.
        ("GET", "/v1/items/new/", {}),  # Redirected without it.
        ("GET", "/items/x", {}),
        ("GET", "/v1/guarded/inside", {}),  # 401: the router's rule.
        ("GET", "/v1/guarded/inside", {"headers": ALICE}),
        ("GET", "/staff/report", {}),  # 401: the include_router call's rule.
        ("GET", "/staff/report", {"headers": ALICE}),
        ("GET", "/v1/plain/text", {}),
        ("GET", "/v1/plain/hidden", {}),
        ("GET", "/raw", {}),
        ("GET", "/sub/ping", {}),
        ("GET", "/open", {}),
        ("GET", "/missing", {}),
        ("HEAD", "/v1/items/new", {}),
        ("OPTIONS", "/open", {}),
    ],
)
def test_requests_are_answered_byte_for_byte_alike(
    nested: Build, method: str, path: str, options: dict[str, Any]
) -> None:
    """Statuses, headers in order, and bodies, of answers and refusals."""
    options = {"headers": FIXED_ID, **options}
    with TestClient(nested()) as before, TestClient(get_asgi_application()) as after:
        assert answer(after, method, path, **options) == answer(
            before, method, path, **options
        )


@pytest.mark.usefixtures("gateway")
def test_route_classes_response_classes_and_overrides_stay(nested: Build) -> None:
    """The router's route class still builds the handler; overrides still reach it."""
    client = TestClient(get_asgi_application())

    stamped = client.get("/v1/items/new")
    plain = client.get("/v1/plain/text")

    assert (stamped.json(), stamped.headers[nested_routes.STAMP]) == (
        {"name": "new plain"},
        "stamped",
    )
    assert (plain.text, plain.headers["content-type"]) == (
        "plain",
        "text/plain; charset=utf-8",
    )
    for app in (nested(), client.app):
        assert isinstance(app, FastAPI)
        app.dependency_overrides[nested_routes.flavour] = lambda: "salty"
        assert TestClient(app).get("/v1/items/new").json() == {"name": "new salty"}


@pytest.mark.usefixtures("gateway")
def test_websockets_are_served_and_guarded_alike(nested: Build) -> None:
    """A router's rule guards its WebSockets after flattening too."""

    def outcomes(app: FastAPI) -> list[object]:
        client = TestClient(app)
        seen: list[object] = []
        with client.websocket_connect("/v1/echo") as socket:
            socket.send_text("hello")
            seen.append(socket.receive_text())
        with client.websocket_connect("/v1/guarded/socket", headers=ALICE) as socket:
            seen.append(socket.receive_text())
        with client.websocket_connect("/v1/plain/feed") as socket:
            seen.append(socket.receive_text())
        with (
            pytest.raises(WebSocketDenialResponse) as refused,
            client.websocket_connect("/v1/guarded/socket"),
        ):
            pass  # pragma: no cover - the handshake is refused.
        seen.append(refused.value.status_code)
        return seen

    assert (
        outcomes(get_asgi_application())
        == outcomes(nested())
        == [
            "hello",
            "alice",
            "fed",
            401,
        ]
    )


@pytest.mark.usefixtures("gateway")
def test_names_build_the_same_urls(nested: Build) -> None:
    """``url_path_for`` finds the same route, with the same full path."""
    before, after = nested(), get_asgi_application()
    names: list[tuple[str, dict[str, Any]]] = [
        ("new_item", {}),
        ("get_item", {"item_id": 7}),
        ("replace_item", {"item_id": 7}),
        ("create_item", {}),
        ("inside", {}),
        ("guarded_socket", {}),
        ("text", {}),
        ("echo", {}),
        ("raw", {}),
        ("report", {}),
        ("shadowed", {"name": "x"}),
        ("after", {"item_id": 7}),
    ]

    for name, params in names:
        assert after.url_path_for(name, **params) == before.url_path_for(
            name, **params
        ), name
    assert after.url_path_for("get_item", item_id=7) == "/v1/items/7"


@pytest.mark.usefixtures("gateway")
def test_lifespans_and_startup_hooks_still_run(nested: Build) -> None:
    """``include_router`` merged them into the application; flattening keeps them."""
    for build in (nested, get_asgi_application):
        with TestClient(build()):
            pass
    flattened = nested_routes.events[: len(nested_routes.events) // 2]

    assert flattened == nested_routes.events[len(flattened) :]
    assert sorted(flattened) == ["lifespan started", "lifespan stopped", "started"]


@pytest.mark.usefixtures("gateway")
def test_route_labels_of_path_operations_stay(nested: Build) -> None:
    """``MetricsMiddleware`` labels a flattened route by its full template.

    A Starlette route or a mount inside an included router is labelled as
    one at the top is ("unmatched", or the mounted application's route);
    nesting gave it an empty label.
    """

    def labels(app: FastAPI) -> list[str]:
        seen: list[str] = []

        async def capture(scope: Scope, receive: Receive, send: Send) -> None:
            await app(scope, receive, send)
            seen.append(route_template(scope))

        client = TestClient(capture)
        for path in ("/v1/items/7", "/v1/items/new", "/staff/report", "/open"):
            client.get(path, headers=ALICE)
        client.delete("/v1/items/7")
        client.get("/missing")
        client.get("/raw")
        client.get("/sub/ping")
        return seen

    paths = [
        "/v1/items/{item_id}",
        "/v1/items/new",
        "/staff/report",
        "/open",
        "/v1/items/{item_id}",
        "unmatched",
    ]
    assert labels(nested()) == [*paths, "", ""]
    assert labels(get_asgi_application()) == [*paths, "unmatched", "/ping"]


@pytest.mark.parametrize(
    "root",
    [
        ROUTES,
        "domainkit_tests.sample_routes.router",
        "domainkit_tests.sample_auth_routes.router",
        "domainkit_tests.staff_routes.router",
        "domainkit_tests.admin_routes.router",
        "domainkit_tests.sample_identity_routes.router",
    ],
)
def test_openapi_documents_are_byte_identical(
    use_auth: UseAuth, nested: Build, root: str
) -> None:
    """The auth, admin, and staff routers, and the tests' own, documented alike.

    Audit and flags are installed, as for the tests these routers serve:
    the admin site builds its router once, with ``/history`` only if audit
    is installed then, so another test's site must not be built without.
    """
    use_auth(
        AUTHENTICATION_CLASSES=[GATEWAY],
        settings={
            "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, AUDIT, FLAGS, ALPHA],
            "ROOT_ROUTER": root,
            "MIDDLEWARE": STACK,
            "API_DOCS": True,
        },
    )
    before, after = nested(), get_asgi_application()

    assert json.dumps(after.openapi()) == json.dumps(before.openapi())
    assert len(after.routes) > len(before.routes)
    if root == "domainkit_tests.admin_routes.router":
        histories = [path for path in kinds(after) if path[1].endswith("/history")]
        assert histories == [("APIRoute", "/admin/alpha/notes/{note_id}/history")]


def test_the_template_projects_document_and_rules_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: Build
) -> None:
    """What ``init`` writes: the same OpenAPI document, routes, and rules."""
    project = tmp_path / "billing"
    call_command("init", "billingAPI", str(project), stdout=io.StringIO())
    monkeypatch.setenv(PROJECT_ROOT_VARIABLE, str(project))
    monkeypatch.syspath_prepend(str(project / "src"))
    project_root.cache_clear()
    project_prefix.cache_clear()
    registry.clear()
    try:
        with use_settings("config.settings.test"):
            setup()
            before, after = nested(), get_asgi_application()
            documents = [json.dumps(app.openapi()) for app in (before, after)]
            served = [
                [name for name, _ in _routes(app.routes)] for app in (before, after)
            ]
            problems = [application_problems(app) for app in (before, after)]
    finally:
        registry.clear()
        project_root.cache_clear()
        project_prefix.cache_clear()
        for name, module in list(sys.modules.items()):
            if str(getattr(module, "__file__", "")).startswith(str(tmp_path)):
                del sys.modules[name]

    assert documents[0] == documents[1]
    assert served[0] == served[1]
    assert "GET /auth/me" in served[1]
    assert problems == [[], []]


def test_frontend_routers_stay_included(tmp_path: Path) -> None:
    """FastAPI matches frontend routes after every other: such a router stays."""
    (tmp_path / "index.html").write_text("<p>app</p>", encoding="utf-8")
    site = APIRouter()
    site.frontend("/", directory=tmp_path)

    @site.get("/api/ping")
    async def ping() -> dict[str, bool]:
        return {"pong": True}

    api = APIRouter()
    api.add_api_route("/hello", ping)
    app = FastAPI()
    app.include_router(site)
    app.include_router(api, prefix="/v2")

    assert flatten_routes(app.router) == 1
    client = TestClient(app)
    assert client.get("/api/ping").json() == {"pong": True}
    assert client.get("/v2/hello").json() == {"pong": True}
    html = {"Accept": "text/html"}
    assert client.get("/anything", headers=html).text == "<p>app</p>"


def test_routers_kept_otherwise_are_left_as_they_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a FastAPI that does not nest, or a route it cannot tell, nothing moves."""
    router = APIRouter()
    router.add_api_route("/hello", nested_routes.open_route)
    app = FastAPI()
    app.include_router(router)
    included = list(app.router.routes)

    with monkeypatch.context() as patch:
        patch.setattr(routing, "_INCLUDED", None)
        assert not nests_routers()
        assert flatten_routes(app.router) == 0
    with monkeypatch.context() as patch:
        patch.setattr(routing, "_INCLUDED", SimpleNamespace)
        assert not nests_routers()
    unknown = SimpleNamespace(original_route=BaseRoute(), starlette_route=None)
    not_a_dataclass = SimpleNamespace(original_route=router.routes[0])
    for context in (unknown, not_a_dataclass):
        with monkeypatch.context() as patch:
            patch.setattr(
                fastapi_routing._IncludedRouter,
                "effective_route_contexts",
                lambda self, context=context: iter([context]),
            )
            assert flatten_routes(app.router) == 0
    assert app.router.routes == included

    assert nests_routers()
    assert flatten_routes(app.router) == 1
    assert isinstance(app.router.routes[-1], APIRoute)
    assert TestClient(app).get("/hello").json() == {"open": True}


def test_routes_added_to_a_router_after_the_build_are_not_served() -> None:
    """Flattening copies what the routers hold when the application is built."""
    router = APIRouter()
    app = FastAPI()
    app.include_router(router, prefix="/late")
    app.include_router(router, prefix="/later")

    assert flatten_routes(app.router) == 2  # Both inclusions, without a route.
    router.add_api_route("/hello", nested_routes.open_route)

    assert TestClient(app).get("/late/hello").status_code == 404
    assert [getattr(route, "path", "") for route in app.routes if route] == sorted(
        OWN, key=["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"].index
    )
