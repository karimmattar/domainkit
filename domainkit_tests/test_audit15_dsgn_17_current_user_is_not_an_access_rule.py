"""DSGN-17: reading the current user does not say who may call the route.

Deny by default (ADR 0017, ``REQUIRE_ROUTE_RULES``) refuses to start a
deployed application with a route that declares no access rule. A route
whose only dependency is ``CurrentUser`` counts as declared, yet
``CurrentUser`` lets anyone in (``AnonymousPrincipal``): a developer who
reaches for the user, expecting a login, ships a route anyone may call, and
``check`` does not list it. ``Public`` and ``Require(AllowAny())`` already
say "anyone" on purpose; 1.0 should not freeze a third, implicit way.
"""

from fastapi import FastAPI

from domainkit.http.access import CurrentUser, Public, open_routes


def application() -> FastAPI:
    """One route that only reads the user, one public on purpose."""
    app = FastAPI()

    @app.get("/me", name="me")
    async def me(user: CurrentUser) -> dict[str, bool]:
        return {"authenticated": user.is_authenticated}

    @app.get("/hello", name="hello", dependencies=[Public])
    async def hello() -> dict[str, str]:
        return {"hello": "world"}

    return app


def test_a_route_that_only_reads_the_current_user_is_listed_as_open() -> None:
    """``open_routes`` names ``GET /me``: nothing there says anonymous callers may."""
    assert open_routes(application()) == ["GET /me"]
