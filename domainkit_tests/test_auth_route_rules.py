"""Deny by default: every route says who may call it, or the app reports it."""

import io
import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from domainkit.conf import override_settings
from domainkit.contrib.auth import IsAuthenticated, Public, Require, open_routes
from domainkit.contrib.auth.access import routes_need_users
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.application import application_problems
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit_tests import sample_auth_routes, sample_open_routes
from domainkit_tests.conftest import MIDDLEWARE, UseAuth

ROUTES = "domainkit_tests.sample_open_routes"
FIX = "add Depends(Require(...)), or dependencies=[Public] if anyone may call it."
OPEN = [
    f"GET /open declares no access rule: {FIX}",
    f"GET POST /both declares no access rule: {FIX}",
    f"WEBSOCKET /socket declares no access rule: {FIX}",
]
LACKS = (
    "Routes check users, but MIDDLEWARE lacks "
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware."
)


def app_of(*routers: object) -> FastAPI:
    """An application with ``routers``."""
    app = FastAPI()
    for router in routers:
        app.include_router(router)  # type: ignore[arg-type]
    return app


async def raw(request: Request) -> PlainTextResponse:
    """A plain Starlette route."""
    return PlainTextResponse("raw")


def test_routes_without_rules_are_listed() -> None:
    """``Require``, the user dependencies, and ``Public`` count, at any depth."""
    app = FastAPI()
    app.include_router(sample_open_routes.router, prefix="/v1")
    app.add_api_route("/direct", sample_open_routes.open_route)
    app.add_api_websocket_route("/direct-socket", sample_open_routes.socket)
    app.add_route("/raw", raw)

    assert open_routes(app) == [
        "GET /v1/open",
        "GET POST /v1/both",
        "WEBSOCKET /v1/socket",
        "GET /direct",
        "WEBSOCKET /direct-socket",
    ]
    assert routes_need_users(app)
    public = app_of(sample_open_routes.public)
    assert open_routes(public) == []
    assert not routes_need_users(public)
    assert open_routes(app_of(sample_auth_routes.router)) == []


def alone(endpoint: Callable[..., Any], **options: Any) -> APIRouter:  # noqa: ANN401 - route options.
    """A router with one route, ``GET /alone``."""
    router = APIRouter()
    router.add_api_route("/alone", endpoint, **options)
    return router


SAMPLES = sample_open_routes
ALONE: dict[str, tuple[APIRouter, bool]] = {
    "require": (
        alone(SAMPLES.required, dependencies=[Depends(Require(IsAuthenticated))]),
        True,
    ),
    "current": (alone(SAMPLES.current), True),
    "authenticated": (alone(SAMPLES.authenticated), True),
    "nested": (alone(SAMPLES.nested), True),
    "router": (SAMPLES.guarded, True),
    "public": (alone(SAMPLES.public_route, dependencies=[Public]), False),
    "open": (alone(SAMPLES.open_route), False),
}


@pytest.mark.parametrize("kind", ALONE)
def test_each_way_of_reading_the_user(kind: str) -> None:
    """Each route alone: does it need ``AuthenticationMiddleware``?"""
    router, needs = ALONE[kind]
    app = app_of(router)

    assert routes_need_users(app) is needs
    assert open_routes(app) == (["GET /alone"] if kind == "open" else [])


@pytest.fixture
def open_project(use_auth: UseAuth) -> None:
    """Auth installed, with routes some of which declare no rule."""
    use_auth(settings={"ROOT_ROUTER": f"{ROUTES}.router", "MIDDLEWARE": MIDDLEWARE[:3]})


@pytest.mark.usefixtures("open_project")
def test_open_routes_are_logged_where_people_develop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Development and tests start, and log the problems."""
    with caplog.at_level(logging.WARNING, logger="domainkit.http.application"):
        app = get_asgi_application()

    assert application_problems(app) == OPEN
    listed = "\n".join(f"  {problem}" for problem in OPEN)
    assert caplog.messages == [f"The application has problems:\n{listed}"]


@pytest.mark.usefixtures("open_project")
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_refuse_open_routes(environment: str) -> None:
    """Staging and production do not start with a problem."""
    listed = "\n".join(f"  {problem}" for problem in OPEN)
    with (
        override_settings(ENVIRONMENT=environment),
        pytest.raises(ImproperlyConfigured) as raised,
    ):
        get_asgi_application()
    assert str(raised.value) == f"The application has problems:\n{listed}"


@pytest.mark.usefixtures("open_project")
def test_check_reports_open_routes() -> None:
    """``check`` fails with every problem."""
    with pytest.raises(CommandError) as raised:
        call_command("check", stdout=io.StringIO())

    assert str(raised.value) == "System check found problems:\n" + "\n".join(
        f"  Application: {problem}" for problem in OPEN
    )


def test_projects_can_allow_open_routes(use_auth: UseAuth) -> None:
    """``AUTH.REQUIRE_ROUTE_RULES = False``: routes may stay open."""
    use_auth(
        REQUIRE_ROUTE_RULES=False,
        settings={"ROOT_ROUTER": f"{ROUTES}.router", "MIDDLEWARE": MIDDLEWARE[:3]},
    )
    with override_settings(ENVIRONMENT="production"):
        assert application_problems(get_asgi_application()) == []
    output = io.StringIO()
    call_command("check", stdout=output)
    assert output.getvalue() == (
        "System check identified no issues. Domains: contenttypes, auth, alpha.\n"
    )


def test_rules_need_the_middleware(use_auth: UseAuth) -> None:
    """Routes that read the user need ``AuthenticationMiddleware``."""
    use_auth(
        REQUIRE_ROUTE_RULES=False,
        settings={"ROOT_ROUTER": f"{ROUTES}.router", "MIDDLEWARE": MIDDLEWARE[:2]},
    )
    assert application_problems(get_asgi_application()) == [LACKS]
    with override_settings(ROOT_ROUTER=f"{ROUTES}.public"):
        assert application_problems(get_asgi_application()) == []
    with override_settings(MIDDLEWARE=MIDDLEWARE[:3]):
        assert application_problems(get_asgi_application()) == []
