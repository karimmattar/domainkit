"""Build the FastAPI application from settings (Django's ``get_asgi_application``)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import APIRouter, FastAPI
from fastapi.telemetry import TelemetryConfig
from starlette.types import Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.checks import CheckFailed, CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.drivers import connect_args
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import route_rule_problems
from domainkit.http.apidocs import OPENAPI_PATH, docs_routes
from domainkit.http.blocking import watch_event_loop
from domainkit.http.errors import ErrorResponse, install_exception_handlers
from domainkit.http.openapi import (
    duplicate_operation_ids,
    install_openapi,
    operation_id,
)
from domainkit.http.routing import flatten_routes, index_routes
from domainkit.http.servers import unix_socket_warnings
from domainkit.http.versioning import DeprecationHeadersMiddleware
from domainkit.http.websockets import server_warnings
from domainkit.lifecycle import serving
from domainkit.utils.module_loading import import_string

__all__ = [
    "Application",
    "get_asgi_application",
]

logger = logging.getLogger(__name__)

FRAMEWORK_ERRORS: Final = {
    400: "Malformed request",
    413: "Request body too large",
    422: "Invalid request",
    429: "Too many requests",
    500: "Internal server error",
}
FASTAPI_TELEMETRY: Final[TelemetryConfig] = {
    "tracing": False,
    "metrics": False,
    "logs": False,
}
"""FastAPI's native telemetry, off: domainkit traces its own work."""
MEASURED: Final = "fastapi.telemetry"
"""The scope key by which FastAPI knows a request is measured already.

A FastAPI application (0.142 and later) skips its own telemetry for a
request whose scope has it, as it does when an enclosing one measures it.
"""
REQUESTS: Final = frozenset({"http", "websocket"})
"""The scope types FastAPI measures."""
BUILT_PROBLEMS: Final = "domainkit_problems"
"""``app.state`` key: the problems found when the application was built."""


class Application(FastAPI):
    """The application ``get_asgi_application`` builds: telemetry off below it too.

    domainkit's own FastAPI is built with its telemetry off
    (``FASTAPI_TELEMETRY``), but a FastAPI application a project mounts in it
    keeps FastAPI's defaults: once an OpenTelemetry SDK is set, it would
    record each request's query string in a span, exception messages in
    logs, and metrics. Before any application or middleware sees a request,
    this marks it as measured already (``MEASURED``, to None), so every
    FastAPI application below skips its telemetry, whatever ``MIDDLEWARE``
    lists. A mark set by an application above, which measures the request
    itself, is kept.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Mark an HTTP or WebSocket request as measured, then serve it."""
        if scope["type"] in REQUESTS:
            scope.setdefault(MEASURED, None)
        await super().__call__(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the domains; then stop them, and close what they opened, in budget.

    Once the domains have started, the application is checked again
    (``check_started``): routes added after it was built, in ``asgi.py`` or
    by a domain's startup, must say who may call them too. With ``DEBUG``,
    the loop logs steps that hold it for ``SLOW_CALLBACK_DURATION``
    (``domainkit.http.blocking``) while the application runs.
    """
    loop = asyncio.get_running_loop()
    debug, slow = loop.get_debug(), loop.slow_callback_duration
    if settings.DEBUG and settings.SLOW_CALLBACK_DURATION is not None:
        watch_event_loop(loop, settings.SLOW_CALLBACK_DURATION)
    try:
        async with serving():
            check_started(app)
            yield
    finally:
        loop.set_debug(debug)
        loop.slow_callback_duration = slow


def get_asgi_application() -> FastAPI:
    """Set up the framework and return the configured application.

    Raises:
        CheckFailed: The application has problems (``application_problems``),
            in staging or production; every one is listed.
    """
    app = build_application()
    problems = application_problems(app)
    _report(problems, "The application has problems")
    setattr(app.state, BUILT_PROBLEMS, tuple(problems))
    if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
        # The server's own command line, here.
        for warning in server_warnings(app):
            logger.warning(warning)
        for warning in unix_socket_warnings(settings.TRUSTED_PROXIES):
            logger.warning(warning)
    return app


def build_application() -> FastAPI:
    """Set up the framework and build the application, without checking it.

    ``get_asgi_application`` checks what this returns; ``check`` collects
    the application's problems with every other one instead.
    """
    setup()
    # Refuse bad driver arguments (CONNECT_ARGS, TLS files) now, not at first use.
    for database in settings.DATABASES.values():
        connect_args(database)
    docs = api_docs()
    app = Application(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
        # The pages are domainkit's own, with their assets (no CDN).
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_PATH if docs else None,
        # Errors the framework itself can return, on every route, with the
        # real body instead of FastAPI's default validation schema.
        responses={
            code: {"model": ErrorResponse, "description": description}
            for code, description in FRAMEWORK_ERRORS.items()
        },
        generate_unique_id_function=operation_id,
        # FastAPI's own OpenTelemetry (0.142+) stays off, as before: its spans
        # record query strings and its logs raw exception messages, which
        # domainkit keeps out of logs and error reports. Application turns it
        # off for the FastAPI applications mounted below too.
        telemetry=FASTAPI_TELEMETRY,
    )
    install_exception_handlers(app)
    install_openapi(app)
    if docs:  # Where FastAPI puts its own: first, after /openapi.json.
        app.router.routes.extend(docs_routes(settings.PROJECT_NAME))
    if settings.ROOT_ROUTER:
        app.include_router(_load(settings.ROOT_ROUTER, APIRouter, "ROOT_ROUTER"))
        if settings.FLATTEN_ROUTERS:
            # Each route matched once, at the top, not once per router (ADR 0060).
            flatten_routes(app.router)
    if settings.FLATTEN_ROUTERS:
        # A request tries the routes its first segment can reach (PERF-05).
        index_routes(app.router)
    # Innermost: it sees the responses routes answer, error handlers' too.
    app.add_middleware(DeprecationHeadersMiddleware)
    # add_middleware() wraps the stack, so add the outermost entry last.
    for path in reversed(settings.MIDDLEWARE):
        middleware: Any = _load(path, type, "MIDDLEWARE")
        app.add_middleware(middleware)
    return app


def api_docs() -> bool:
    """Whether the application serves its documentation: ``API_DOCS``.

    Unset (None), it follows ``DEBUG``: on in development, off when
    deployed. True serves it anywhere, False nowhere.
    """
    explicit = settings.API_DOCS
    return settings.DEBUG if explicit is None else explicit


def application_problems(app: FastAPI) -> list[str]:
    """What is wrong with ``app``: clashing ids, open routes, what domains find.

    Every route must declare who may call it (``REQUIRE_ROUTE_RULES``),
    whatever authenticates requests. Domains report through
    ``DomainConfig.check_application``.
    """
    problems: list[str] = [
        CheckMessage("openapi.E001", problem)
        for problem in duplicate_operation_ids(app)
    ]
    if settings.REQUIRE_ROUTE_RULES:
        problems.extend(
            CheckMessage("security.E101", problem)
            for problem in route_rule_problems(app)
        )
    problems.extend(
        problem for config in registry for problem in config.check_application(app)
    )
    return problems


def check_started(app: FastAPI) -> None:
    """Check ``app`` again as it starts serving, routes added since included.

    Routes included after ``get_asgi_application`` returned (in ``asgi.py``,
    by a plugin, or by a domain's startup) were not there when it was
    checked. Staging and production refuse to start with a problem, as at
    build time; elsewhere, problems not reported then are logged. It runs
    once per start, never per request.

    Raises:
        CheckFailed: A problem, in staging or production.
    """
    built = getattr(app.state, BUILT_PROBLEMS, ())
    found = [problem for problem in application_problems(app) if problem not in built]
    _report(found, "The application has problems since it was built")


def _report(problems: list[str], heading: str) -> None:
    """Refuse ``problems`` in staging and production; log them elsewhere.

    Raises:
        CheckFailed: A problem, in staging or production.
    """
    if not problems:
        return
    failed = CheckFailed(heading, problems)
    if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
        raise failed
    logger.warning("%s", failed)


def _load[T](path: str, expected: type[T], setting: str) -> T:
    """Import ``path`` named in ``setting`` and check it is an ``expected``."""
    try:
        value = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"{setting}: {exc}") from exc
    if not isinstance(value, expected):
        raise ImproperlyConfigured(f"{setting}: {path!r} is not a {expected.__name__}.")
    return value
