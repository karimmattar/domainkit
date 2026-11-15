"""``select_routes`` mounts part of a router, with dependencies on named routes.

A project took the framework's routers apart by path, and read the JSON
body in dependencies of its own to add a rule to one route (user report
A7). These tests hold what replaces that: copies of the routes it names,
with its dependencies on the routes it names, and the router copied left
as it was.
"""

import importlib
import re
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketException,
    status,
)
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from domainkit.conf import override_settings
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.devices.api import router as devices_router
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.access import open_routes
from domainkit.http.routers import route_names, select_routes
from domainkit_tests.conftest import API, Migrated
from domainkit_tests.support.auth import PASSWORD, bearer, create_user, login


async def refuse() -> None:
    """Refuse every request: 403."""
    raise HTTPException(status.HTTP_403_FORBIDDEN, "refused")


def shop() -> tuple[APIRouter, list[str]]:
    """Products, and orders included under ``/shop``; what ran, in order."""
    calls: list[str] = []

    async def router_check() -> None:
        calls.append("router")

    async def route_check() -> None:
        calls.append("route")

    async def orders_check() -> None:
        calls.append("orders")

    router = APIRouter(dependencies=[Depends(router_check)])

    @router.get("/products", dependencies=[Depends(route_check)])
    async def products() -> list[str]:
        calls.append("endpoint")
        return ["kettle"]

    @router.post("/products")
    async def add_product() -> dict[str, bool]:
        calls.append("endpoint")
        return {"added": True}

    @router.get("/products/{product_id}")
    async def product(product_id: int) -> dict[str, int]:
        return {"id": product_id}

    orders = APIRouter(prefix="/orders", tags=["orders"])

    @orders.get("/{order_id}", dependencies=[Depends(orders_check)])
    async def order(order_id: int) -> dict[str, int]:
        calls.append("endpoint")
        return {"id": order_id}

    router.include_router(orders, prefix="/shop")
    return router, calls


def serve(router: APIRouter) -> TestClient:
    """A client of an application that mounts ``router`` under ``/api``."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_route_names_are_methods_and_paths_as_the_router_declares_them() -> None:
    """Included routers' prefixes count, not the mount's (user report A7)."""
    router, _ = shop()

    assert route_names(router) == [
        "GET /products",
        "POST /products",
        "GET /products/{product_id}",
        "GET /shop/orders/{order_id}",
    ]


def test_only_copies_the_named_routes_in_the_routers_order() -> None:
    """The others are not served; the order is the router's (user report A7)."""
    router, _ = shop()

    selected = select_routes(
        router, only=["GET /shop/orders/{order_id}", "GET /products"]
    )
    client = serve(selected)

    assert route_names(selected) == ["GET /products", "GET /shop/orders/{order_id}"]
    assert client.get("/api/products").json() == ["kettle"]
    assert client.get("/api/shop/orders/7").json() == {"id": 7}
    assert client.post("/api/products").status_code == 405
    assert client.get("/api/products/7").status_code == 404


def test_without_leaves_the_named_routes_out() -> None:
    """Every other route is copied; ``without`` wins over ``only`` (user report A7)."""
    router, _ = shop()

    selected = select_routes(router, without=["POST /products"])
    both = select_routes(
        router, only=["GET /products", "POST /products"], without=["POST /products"]
    )

    assert route_names(selected) == [
        "GET /products",
        "GET /products/{product_id}",
        "GET /shop/orders/{order_id}",
    ]
    assert route_names(both) == ["GET /products"]
    assert serve(selected).post("/api/products").status_code == 405


def test_a_dependency_refuses_only_the_route_it_is_given_to() -> None:
    """The named route is refused before its endpoint; others serve (user report A7)."""
    router, calls = shop()

    client = serve(
        select_routes(router, dependencies={"POST /products": [Depends(refuse)]})
    )
    refused = client.post("/api/products")
    served = client.get("/api/products")

    assert refused.status_code == 403
    assert served.json() == ["kettle"]
    assert calls == ["router", "router", "route", "endpoint"]


def test_added_dependencies_run_after_the_routes_own_and_before_its_endpoint() -> None:
    """As the last of the route's ``dependencies=[...]`` (user report A7)."""
    router, calls = shop()

    async def added() -> None:
        calls.append("added")

    client = serve(
        select_routes(
            router,
            dependencies={
                "GET /products": [Depends(added)],
                "GET /shop/orders/{order_id}": [Depends(added)],
            },
        )
    )
    client.get("/api/products")
    client.get("/api/shop/orders/1")

    assert calls == [
        *("router", "route", "added", "endpoint"),
        *("router", "orders", "added", "endpoint"),
    ]


def test_openapi_shows_an_added_dependency_on_its_route_only() -> None:
    """A header the dependency reads is that operation's alone (user report A7)."""
    router, _ = shop()

    async def needs_code(x_code: Annotated[str, Header()]) -> None:
        """Read the code the client sends."""

    client = serve(
        select_routes(router, dependencies={"POST /products": [Depends(needs_code)]})
    )
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]

    assert [each["name"] for each in paths["/api/products"]["post"]["parameters"]] == [
        "x-code"
    ]
    assert "parameters" not in paths["/api/products"]["get"]
    assert paths["/api/shop/orders/{order_id}"]["get"]["tags"] == ["orders"]
    assert client.post("/api/products").status_code == 422
    assert client.post("/api/products", headers={"X-Code": "1"}).json() == {
        "added": True
    }


@pytest.mark.parametrize(
    ("argument", "name"),
    [
        ("only", "GET /nowhere"),
        ("without", "get /products"),
        ("dependencies", "GET /api/products"),
        ("only", "GET /products/{id}"),
        ("without", "GET /orders/{order_id}"),
    ],
)
def test_a_name_that_matches_no_route_is_refused(argument: str, name: str) -> None:
    """A typo neither copies every route nor drops a rule (user report A7)."""
    router, _ = shop()
    given: dict[str, object] = {
        argument: {name: [Depends(refuse)]} if argument == "dependencies" else [name]
    }

    with pytest.raises(ImproperlyConfigured, match=re.escape(repr(name))) as raised:
        select_routes(router, **given)  # type: ignore[arg-type]

    assert "GET /shop/orders/{order_id}" in str(raised.value)  # What there is.


def test_dependencies_for_a_route_left_out_are_refused() -> None:
    """They would never run: the call says two things (user report A7)."""
    router, _ = shop()

    with pytest.raises(ImproperlyConfigured, match="left out: 'POST /products'"):
        select_routes(
            router,
            without=["POST /products"],
            dependencies={"POST /products": [Depends(refuse)]},
        )
    with pytest.raises(ImproperlyConfigured, match="left out: 'POST /products'"):
        select_routes(
            router,
            only=["GET /products"],
            dependencies={"POST /products": [Depends(refuse)]},
        )


def test_names_and_dependencies_must_be_lists() -> None:
    """A string reads as letters; a bare function is no dependency (user report A7)."""
    router, _ = shop()

    with pytest.raises(ImproperlyConfigured, match=r"only takes a list"):
        select_routes(router, only="GET /products")
    with pytest.raises(ImproperlyConfigured, match=r"without takes a list"):
        select_routes(router, without="GET /products")
    with pytest.raises(ImproperlyConfigured, match=r"a list of dependencies"):
        select_routes(router, dependencies={"GET /products": Depends(refuse)})
    with pytest.raises(ImproperlyConfigured, match=r"is not a dependency"):
        select_routes(router, dependencies={"GET /products": [refuse]})  # type: ignore[list-item]


def test_the_router_copied_is_left_as_it_was() -> None:
    """Copies, not changes: the router still serves every route (user report A7)."""
    router, calls = shop()
    routes = list(router.routes)
    own = [list(route.dependencies) for route in routes if isinstance(route, APIRoute)]
    names = route_names(router)

    selected = select_routes(
        router,
        only=["GET /products", "POST /products"],
        dependencies={"GET /products": [Depends(refuse)]},
    )

    assert all(new is old for new, old in zip(router.routes, routes, strict=True))
    assert [
        list(route.dependencies)
        for route in router.routes
        if isinstance(route, APIRoute)
    ] == own
    assert route_names(router) == names
    assert not {id(route) for route in selected.routes} & {
        id(route) for route in routes
    }
    assert serve(router).get("/api/products").json() == ["kettle"]
    assert serve(selected).get("/api/products").status_code == 403
    assert calls == ["router", "route", "endpoint", "router", "route"]


def test_websocket_routes_are_named_and_take_dependencies() -> None:
    """``WEBSOCKET /path``; a refusal closes that socket alone (user report A7)."""
    router = APIRouter()

    async def closed() -> None:
        raise WebSocketException(status.WS_1008_POLICY_VIOLATION)

    @router.websocket("/feed")
    async def feed(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"feed": True})
        await websocket.close()

    @router.websocket("/news")
    async def news(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"news": True})
        await websocket.close()

    selected = select_routes(
        router, dependencies={"WEBSOCKET /feed": [Depends(closed)]}
    )
    client = serve(selected)

    assert route_names(selected) == ["WEBSOCKET /feed", "WEBSOCKET /news"]
    with client.websocket_connect("/api/news") as socket:
        assert socket.receive_json() == {"news": True}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/feed"):
        pass
    with serve(router).websocket_connect("/api/feed") as socket:
        assert socket.receive_json() == {"feed": True}


def test_starlette_routes_are_copied_as_they_are_and_take_no_dependencies() -> None:
    """The router's own are copied; an included one is refused (user report A7)."""

    async def page(request: Request) -> PlainTextResponse:
        return PlainTextResponse("page")

    router, _ = shop()
    router.add_route("/page", page)
    outer = APIRouter()
    outer.include_router(router)

    selected = select_routes(router, only=["GET HEAD /page"])

    assert serve(selected).get("/api/page").text == "page"
    with pytest.raises(ImproperlyConfigured, match="takes no dependencies"):
        select_routes(router, dependencies={"GET HEAD /page": [Depends(refuse)]})
    with pytest.raises(ImproperlyConfigured, match="'GET HEAD /page' is a Route of"):
        select_routes(outer)
    assert "GET HEAD /page" not in route_names(
        select_routes(outer, without=["GET HEAD /page"])
    )


class Gated(APIRouter):
    """Serves requests itself: only callers sending ``X-Gate: open`` get in."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse a closed gate."""
        if dict(scope["headers"]).get(b"x-gate") != b"open":
            await PlainTextResponse("closed", 403)(scope, receive, send)
            return
        await super().handle(scope, receive, send)


def test_a_router_that_serves_itself_is_not_copied_in_part(tmp_path: Path) -> None:
    """Its gate or frontend routes would be lost: include it whole (user report A7)."""
    gated = Gated()
    gated.add_api_route("/inside", refuse)
    outer = APIRouter()
    outer.include_router(gated)
    (tmp_path / "index.html").write_text("<p>app</p>", encoding="utf-8")
    site = APIRouter()
    site.frontend("/", directory=tmp_path)

    with pytest.raises(ImproperlyConfigured, match="the router serves requests"):
        select_routes(gated)
    with pytest.raises(ImproperlyConfigured, match=r"a router it includes .* serves"):
        select_routes(outer)
    with pytest.raises(ImproperlyConfigured, match="has frontend routes"):
        select_routes(site)


def test_the_copy_keeps_the_routers_lifespan() -> None:
    """The router's startup runs once where the copy is mounted (user report A7)."""
    started: list[str] = []
    router = APIRouter(on_startup=[lambda: started.append("router")])
    router.add_api_route("/ping", refuse)

    with serve(select_routes(router)):
        assert started == ["router"]


@pytest.mark.parametrize(
    "module",
    ["auth.api", "auth.admin", "auth.personal_tokens", "devices.api", "ciba.api"],
)
def test_a_copy_of_a_framework_router_documents_as_the_router_does(module: str) -> None:
    """Every route as served: paths, rules, bodies, answers (user report A7)."""
    router = importlib.import_module(f"domainkit.contrib.{module}").router
    original, copied = FastAPI(), FastAPI()
    original.include_router(router, prefix="/auth")
    copied.include_router(select_routes(router), prefix="/auth")

    assert copied.openapi() == original.openapi()
    assert route_names(select_routes(router)) == route_names(router)


def test_the_report_names_a_route_of_the_devices_router() -> None:
    """``POST /devices/enroll/code``, as named in user report A7, takes a rule."""
    selected = select_routes(
        devices_router, dependencies={"POST /devices/enroll/code": [Depends(refuse)]}
    )
    app = FastAPI()
    app.include_router(selected, prefix="/auth")
    operations = app.openapi()["paths"]

    assert "POST /devices/enroll/code" in route_names(devices_router)
    assert route_names(selected) == route_names(devices_router)
    assert "/auth/devices/enroll/code" in operations


async def not_alice(request: Request) -> None:
    """Refuse a login as ``alice``: a rule that reads the JSON body."""
    if (await request.json()).get("username") == "alice":
        raise PermissionDenied("Alice logs in on her phone.")


router = APIRouter()
router.include_router(
    select_routes(
        auth_router,
        without=["POST /password"],
        dependencies={"POST /login": [Depends(not_alice)]},
    ),
    prefix="/auth",
)


def test_a_selection_of_auths_router_serves_in_an_application(
    migrated: Migrated,
) -> None:
    """Login serves under its rule; the route left out is not (user report A7)."""
    with (
        migrated(API),
        override_settings(ROOT_ROUTER=f"{__name__}.router"),
        TestClient(get_asgi_application()) as client,
    ):
        create_user("bob")
        create_user("alice")
        token = login(client, "bob")["access_token"]
        me = client.get("/auth/me", headers=bearer(token))
        alice = client.post(
            "/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        password = client.post(
            "/auth/password",
            json={"old_password": PASSWORD, "new_password": PASSWORD + "!"},
            headers=bearer(token),
        )
        app = client.app

        assert me.json()["username"] == "bob"
        assert alice.status_code == 403
        assert alice.json()["detail"] == "Alice logs in on her phone."
        assert password.status_code == 404
        assert open_routes(app) == []  # type: ignore[arg-type]
    assert "POST /login" in route_names(auth_router)
    assert "POST /password" in route_names(auth_router)
