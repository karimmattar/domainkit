"""Routes serve requests without the dependencies that only document them (PERF-34).

``access.documenting_bearer`` puts the bearer token in OpenAPI; the
authentication middleware checks the token. A route keeps it in its
``dependant``, so OpenAPI shows it, and serves requests through a copy
without it. ``access.bearer``, which a project's dependencies may read, is
served as FastAPI serves it.
"""

from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.routing import Route

from domainkit.http import access
from domainkit.http.routing import serve_without_documentation


async def guarded(
    _: Annotated[
        HTTPAuthorizationCredentials | None, Depends(access.documenting_bearer)
    ] = None,
) -> str:
    """A dependency that, as ``Require`` does, names the token to document it."""
    return "allowed"


def app_with_routes() -> FastAPI:
    """A route behind ``guarded``, one without, and a plain Starlette route."""
    app = FastAPI()

    @app.get("/guarded")
    async def read(rule: Annotated[str, Depends(guarded)]) -> str:
        return rule

    @app.get("/open")
    async def open_() -> str:
        return "open"

    app.router.routes.append(Route("/plain", lambda request: None))
    return app


def calls(dependant: object) -> list[object]:
    """Every call a dependant resolves, at any depth."""
    found: list[object] = []
    for each in dependant.dependencies:  # type: ignore[attr-defined]
        found += [each.call, *calls(each)]
    return found


def test_a_route_serves_without_the_token_and_documents_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token is read by no request, and OpenAPI still shows it."""
    app = app_with_routes()
    routes = {r.path: r for r in app.router.routes if isinstance(r, Route)}
    open_handler = routes["/open"].app

    assert serve_without_documentation(app.router) == 1

    async def never(*args: object) -> None:
        raise AssertionError("the token was read")

    monkeypatch.setattr(type(access.bearer), "__call__", never)
    client = TestClient(app)
    response = client.get("/guarded", headers={"Authorization": "Bearer abc"})
    assert (response.status_code, response.json()) == (200, "allowed")
    guarded_route = routes["/guarded"]
    assert isinstance(guarded_route, APIRoute)
    assert access.documenting_bearer in calls(guarded_route.dependant)
    assert routes["/open"].app is open_handler
    operation = app.openapi()["paths"]["/guarded"]["get"]
    assert operation["security"] == [{"Token": []}]


def test_a_dependency_on_the_token_is_kept_where_nothing_documents_it() -> None:
    """Without the pass, the route resolves the token, as FastAPI does."""
    router = APIRouter()

    @router.get("/kept")
    async def kept(rule: Annotated[str, Depends(guarded)]) -> str:
        return rule

    [route] = router.routes
    assert isinstance(route, APIRoute)
    assert access.documenting_bearer in calls(route.dependant)
    assert serve_without_documentation(APIRouter()) == 0


def test_a_projects_own_dependency_on_the_token_still_reads_it() -> None:
    """Only what ``Require`` names to document is left out, not ``bearer``.

    The audit found the pass leaving out every ``Depends(access.bearer)``, so
    a project's dependency that reads the token got its default instead.
    """
    app = FastAPI()

    async def token(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(access.bearer)
        ] = None,
    ) -> str | None:
        return None if credentials is None else credentials.credentials

    @app.get("/both")
    async def both(
        rule: Annotated[str, Depends(guarded)],
        seen: Annotated[str | None, Depends(token)],
    ) -> list[str | None]:
        return [rule, seen]

    assert serve_without_documentation(app.router) == 1
    client = TestClient(app)

    response = client.get("/both", headers={"Authorization": "Bearer abc"})
    assert response.json() == ["allowed", "abc"]
    assert client.get("/both").json() == ["allowed", None]
    schemes = app.openapi()["components"]["securitySchemes"]
    assert list(schemes) == ["Token"]
