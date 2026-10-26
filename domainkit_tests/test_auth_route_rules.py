"""Deny by default: every route says who may call it, or the app reports it."""

import io
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from domainkit.conf import override_settings
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.access import (
    IsAuthenticated,
    Public,
    Require,
    mark_public,
    open_routes,
    routes_need_users,
)
from domainkit.http.application import application_problems, check_started
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


@mark_public
async def marked(request: Request) -> PlainTextResponse:
    """A plain Starlette route, public on purpose."""
    return PlainTextResponse("marked")


async def raw_socket(websocket: WebSocket) -> None:
    """A plain Starlette WebSocket."""
    await websocket.close()


mounted = APIRouter()
"""A project's router that mounts files."""
mounted.mount("/static", StaticFiles(directory="static", check_dir=False))


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
        "GET HEAD /raw",
    ]
    assert routes_need_users(app)
    public = app_of(sample_open_routes.public)
    assert open_routes(public) == []
    assert not routes_need_users(public)
    assert open_routes(app_of(sample_auth_routes.router)) == []


def test_what_takes_no_rule_is_listed_unless_marked(tmp_path: Path) -> None:
    """Mounts and Starlette's own routes cannot take ``Public``: ``mark_public``.

    Mounted applications with routes are walked, so each of their routes
    counts. Such routes, and a mounted ``StaticFiles``, used to be served to
    anyone without being listed.
    """
    sub = app_of(alone(sample_open_routes.open_route), sample_open_routes.public)
    files = StaticFiles(directory=tmp_path)
    nested = APIRouter()
    nested.add_route("/raw", raw)
    nested.mount("/files", files)
    nested.mount("/sub", sub)
    app = FastAPI()
    app.mount("/sub", sub)
    app.mount("/static", files)
    app.mount("/assets", mark_public(StaticFiles(directory=tmp_path)))
    app.add_route("/raw", raw)
    app.add_route("/marked", marked)
    app.router.routes.append(WebSocketRoute("/raw-socket", raw_socket))
    app.router.routes.append(Mount("/v2", routes=[Route("/raw", raw)]))
    app.router.routes.append(Host("files.example.org", files))
    app.include_router(nested, prefix="/nested")

    assert open_routes(app) == [
        "GET /sub/alone",
        "MOUNT /static",
        "GET HEAD /raw",
        "WEBSOCKET /raw-socket",
        "GET HEAD /v2/raw",
        "HOST files.example.org",
        "GET HEAD /nested/raw",
        "MOUNT /nested/files",
        "GET /nested/sub/alone",
    ]
    assert not routes_need_users(app)
    app.mount("/guarded", app_of(alone(sample_open_routes.authenticated)))
    assert routes_need_users(app)


def test_hosts_and_mounts_keep_the_prefix_they_are_served_under(
    tmp_path: Path,
) -> None:
    """Inside an included router or a mount, the prefix counts.

    FastAPI serves a host of an included router through a mount of the
    prefix, so one whose application has no routes is listed as that mount.
    """
    sub = app_of(alone(sample_open_routes.open_route))
    inner = APIRouter()
    inner.host("hosted.example.org", sub)
    inner.mount("/sub", sub)
    inner.host("files.example.org", StaticFiles(directory=tmp_path))
    app = FastAPI()
    app.include_router(inner, prefix="/nested")
    app.mount("/hosting", Starlette(routes=[Host("api.example.org", sub)]))

    assert open_routes(app) == [
        "GET /nested/alone",
        "GET /nested/sub/alone",
        "MOUNT /nested",
        "GET /hosting/alone",
    ]


def test_the_rules_of_an_include_cover_its_websockets() -> None:
    """Dependencies given to ``include_router`` guard its WebSockets too."""
    sockets = APIRouter()
    sockets.add_api_websocket_route("/socket", sample_open_routes.socket)
    app = FastAPI()
    app.include_router(sockets, prefix="/open")
    app.include_router(sockets, prefix="/public", dependencies=[Public])

    assert open_routes(app) == ["WEBSOCKET /open/socket"]


def test_the_check_says_how_to_mark_what_takes_no_rule(use_auth: UseAuth) -> None:
    """A mount is reported with the way to guard it, or to mark it public."""
    use_auth(
        settings={
            "ROOT_ROUTER": "domainkit_tests.test_auth_route_rules.mounted",
            "MIDDLEWARE": MIDDLEWARE[:3],
        }
    )
    assert application_problems(get_asgi_application()) == [
        (
            "MOUNT /static takes no access rule: serve it from a FastAPI route "
            "with Require(...), or wrap it in mark_public(...) if anyone may "
            "call it."
        )
    ]


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


def late_router() -> APIRouter:
    """A router with a route that declares no rule, as ``asgi.py`` might add."""
    late = APIRouter()

    @late.get("/late-report")
    async def late_report() -> dict[str, str]:
        return {"secret": "quarterly numbers"}

    return late


LATE = f"GET /late-report declares no access rule: {FIX}"


def test_routes_added_after_the_build_are_checked_as_it_starts(
    use_auth: UseAuth, caplog: pytest.LogCaptureFixture
) -> None:
    """Routes included once the factory returned escaped the check (AUTH-06).

    The application is checked again as it starts serving; where people
    develop, what is new is logged.
    """
    use_auth(settings={"ROOT_ROUTER": None, "MIDDLEWARE": MIDDLEWARE[:3]})
    app = get_asgi_application()
    app.include_router(late_router())

    with (
        caplog.at_level(logging.WARNING, logger="domainkit.http.application"),
        TestClient(app),
    ):
        pass

    assert caplog.messages == [
        f"The application has problems since it was built:\n  {LATE}"
    ]


def test_applications_built_elsewhere_are_checked_whole_as_they_start(
    use_auth: UseAuth, caplog: pytest.LogCaptureFixture
) -> None:
    """An application ``get_asgi_application`` did not build reports everything."""
    use_auth(settings={"ROOT_ROUTER": None, "MIDDLEWARE": MIDDLEWARE[:3]})
    with caplog.at_level(logging.WARNING, logger="domainkit.http.application"):
        check_started(app_of(late_router()))

    assert caplog.messages == [
        f"The application has problems since it was built:\n  {LATE}"
    ]


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployments_refuse_to_start_with_routes_added_late(
    use_auth: UseAuth, environment: str
) -> None:
    """Staging and production refuse to start, as they refuse to build."""
    use_auth(settings={"ROOT_ROUTER": None, "MIDDLEWARE": MIDDLEWARE[:3]})
    with override_settings(ENVIRONMENT=environment):
        app = get_asgi_application()
        app.include_router(late_router())
        with pytest.raises(ImproperlyConfigured) as raised, TestClient(app):
            pass  # pragma: no cover - the start is refused.

    assert str(raised.value) == (
        f"The application has problems since it was built:\n  {LATE}"
    )


@pytest.mark.usefixtures("open_project")
def test_problems_found_at_the_build_are_not_logged_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Starting logs only what the build did not."""
    app = get_asgi_application()
    assert len(caplog.messages) == 1  # The build's.
    caplog.clear()
    with (
        caplog.at_level(logging.WARNING, logger="domainkit.http.application"),
        TestClient(app),
    ):
        pass

    assert caplog.messages == []


@pytest.mark.usefixtures("open_project")
def test_check_reports_open_routes() -> None:
    """``check`` fails with every problem."""
    with pytest.raises(CommandError) as raised:
        call_command("check", stdout=io.StringIO())

    assert str(raised.value) == "System check found problems:\n" + "\n".join(
        f"  Application: {problem}" for problem in OPEN
    )


def test_projects_can_allow_open_routes(use_auth: UseAuth) -> None:
    """``REQUIRE_ROUTE_RULES = False``: routes may stay open."""
    use_auth(
        settings={
            "ROOT_ROUTER": f"{ROUTES}.router",
            "MIDDLEWARE": MIDDLEWARE[:3],
            "REQUIRE_ROUTE_RULES": False,
        },
    )
    with override_settings(ENVIRONMENT="production"):
        assert application_problems(get_asgi_application()) == []
    output = io.StringIO()
    call_command("check", stdout=output)
    assert output.getvalue() == (
        "System check identified no issues. Domains: contenttypes, auth, alpha.\n"
    )


def test_the_auth_setting_still_allows_open_routes_with_a_warning(
    use_auth: UseAuth,
) -> None:
    """``AUTH.REQUIRE_ROUTE_RULES = False`` works until 0.11, and warns."""
    with pytest.warns(
        DomainkitDeprecationWarning,
        match=r"^AUTH\.REQUIRE_ROUTE_RULES moved to REQUIRE_ROUTE_RULES, .* "
        r"removed in 0\.11\.$",
    ):
        use_auth(
            REQUIRE_ROUTE_RULES=False,
            settings={"ROOT_ROUTER": f"{ROUTES}.router", "MIDDLEWARE": MIDDLEWARE[:3]},
        )
    with override_settings(ENVIRONMENT="production"):
        assert application_problems(get_asgi_application()) == []
    with override_settings(AUTH={"REQUIRE_ROUTE_RULES": True}):
        assert application_problems(get_asgi_application()) != []
    with override_settings(REQUIRE_ROUTE_RULES=False, AUTH={}):
        assert application_problems(get_asgi_application()) == []


def test_rules_need_the_middleware(use_auth: UseAuth) -> None:
    """Routes that read the user need ``AuthenticationMiddleware``."""
    use_auth(
        settings={
            "ROOT_ROUTER": f"{ROUTES}.router",
            "MIDDLEWARE": MIDDLEWARE[:2],
            "REQUIRE_ROUTE_RULES": False,
        },
    )
    assert application_problems(get_asgi_application()) == [LACKS]
    with override_settings(ROOT_ROUTER=f"{ROUTES}.public"):
        assert application_problems(get_asgi_application()) == []
    with override_settings(MIDDLEWARE=MIDDLEWARE[:3]):
        assert application_problems(get_asgi_application()) == []
