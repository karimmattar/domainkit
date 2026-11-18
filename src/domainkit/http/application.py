"""Build the FastAPI application from settings (Django's ``get_asgi_application``)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import APIRouter, FastAPI
from fastapi.telemetry import TelemetryConfig
from starlette.types import Message, Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_check_application
from domainkit.checks import CheckFailed, CheckMessage, describe
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
from domainkit.http.ratelimit import rate_limit_backend
from domainkit.http.routing import (
    flatten_routes,
    index_routes,
    serve_without_documentation,
)
from domainkit.http.servers import unix_socket_warnings
from domainkit.http.versioning import DeprecationHeadersMiddleware
from domainkit.http.websockets import server_warnings
from domainkit.lifecycle import serving
from domainkit.tenancy import path_tenant_problems
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
    503: "Busy: overloaded, past its deadline, or its database is; retry after "
    "Retry-After",
}
FASTAPI_TELEMETRY: Final[TelemetryConfig] = {
    "tracing": False,
    "metrics": False,
    "logs": False,
}
"""FastAPI's native telemetry, off: domainkit traces its own work, and each
request with ``domainkit.http.tracing.TracingMiddleware``."""
MEASURED: Final = "fastapi.telemetry"
"""The scope key by which FastAPI knows a request is measured already.

A FastAPI application (0.142 and later) skips its own telemetry for a
request whose scope has it, as it does when an enclosing one measures it.
"""
REQUESTS: Final = frozenset({"http", "websocket"})
"""The scope types FastAPI measures."""
BUILT_PROBLEMS: Final = "domainkit_problems"
"""``app.state`` key: the problems found when the application was built."""
SERVER: Final = b"server"
"""The ``Server`` header's name, as ASGI carries it."""
CONTRIB: Final = "domainkit.contrib."
"""The packages of the optional apps, each a domain to install."""


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

    It also gives every HTTP answer the ``Server`` header of
    ``SERVER_HEADER``, once set (``server_header``): above every middleware,
    and above the handler of unhandled errors, whose answers no middleware
    of ``MIDDLEWARE`` sees.
    """

    server_header: list[tuple[bytes, bytes]] | None = None
    """The ``Server`` header each answer gets in place of its own (none if
    empty); None leaves answers as they are."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Mark an HTTP or WebSocket request as measured, then serve it."""
        if scope["type"] in REQUESTS:
            scope.setdefault(MEASURED, None)
            if self.server_header is not None:
                send = _with_server_header(send, self.server_header)
        await super().__call__(scope, receive, send)


def _with_server_header(send: Send, header: list[tuple[bytes, bytes]]) -> Send:
    """``send``, with ``header`` in place of each answer's own ``Server`` header."""

    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = [
                (name, value)
                for name, value in message.get("headers", ())
                if name.lower() != SERVER
            ] + header
        await send(message)

    return sending


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
        # Imported where deployed only: a process imports what it uses.
        from domainkit.http.live_events import fanout_warnings

        # The server's own command line, here.
        for warning in server_warnings(app):
            logger.warning(warning)
        for warning in unix_socket_warnings(settings.TRUSTED_PROXIES):
            logger.warning(warning)
        for warning in fanout_warnings(app.routes):
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
        # off for the FastAPI applications mounted below too; TracingMiddleware
        # gives requests a span without them (ADR 0158).
        telemetry=FASTAPI_TELEMETRY,
    )
    if (server := settings.SERVER_HEADER) is not None:
        app.server_header = [(SERVER, server.encode())] if server else []
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
        # A request tries the routes its first segment can reach (PERF-05),
        # and resolves no dependency that only documents a route.
        index_routes(app.router)
        serve_without_documentation(app.router)
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
    whatever authenticates requests; a route that takes its tenant from
    its path must name a parameter it has, before its rules
    (``tenancy.E001``, ``tenancy.E002``); an optional app's middleware
    needs the app (``apps.E003``), and ``RATE_LIMIT_BACKEND`` a class with
    ``hit`` (``backends.E001``). Domains report through
    ``domainkit.apps.hooks.on_check_application``.
    """
    problems: list[str] = [
        CheckMessage("openapi.E001", problem)
        for problem in duplicate_operation_ids(app)
    ]
    problems.extend(middleware_without_its_app())
    problems.extend(backend_problems())
    if settings.REQUIRE_ROUTE_RULES:
        problems.extend(
            CheckMessage("security.E101", problem)
            for problem in route_rule_problems(app)
        )
    problems.extend(path_tenant_problems(app))
    problems.extend(
        problem for hook in on_check_application for problem in hook.call(app)
    )
    return problems


def middleware_without_its_app() -> list[str]:
    """``apps.E003``: each ``MIDDLEWARE`` entry of an optional app not installed.

    Its settings, tables, and startup are the app's: without the app the
    middleware fails on its first call, and every request with it.
    """
    problems: list[str] = []
    for path in settings.MIDDLEWARE:
        if not path.startswith(CONTRIB):
            continue
        app = ".".join(path.split(".")[:3])
        if not registry.is_installed(app):
            problems.append(
                CheckMessage(
                    "apps.E003",
                    f"MIDDLEWARE lists {path!r}, but {app!r} is not in "
                    f"INSTALLED_DOMAINS: every request would fail. Install "
                    f"{app!r}, or remove the middleware.",
                )
            )
    return problems


def backend_problems() -> list[str]:
    """``backends.E001``: ``RATE_LIMIT_BACKEND`` names no rate-limit backend.

    The global limit, each ``Throttle``, auth's counts of attempts, and the
    worker's task rates make one where they first count, and would fail
    there: a path that does not import, or a class without ``hit``, is
    refused before then (since 2.9, ADR 0196).
    """
    try:
        rate_limit_backend.backend_class()
    except ImproperlyConfigured as exc:
        return [
            CheckMessage(
                "backends.E001",
                str(exc),
                "Name a class with hit(key, limit, period), such as "
                "domainkit.http.ratelimit.RedisBackend, or a name an installed "
                "package offers in its domainkit.backends entry points.",
            )
        ]
    return []


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
    if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
        raise CheckFailed(heading, problems)
    # One record a problem: a record of several lines would be escaped onto
    # one, as the log keeps a line break in a message from forging a line.
    for problem in problems:
        lines = describe(problem, indent="").splitlines()
        logger.warning("%s: %s", heading, " ".join(line.strip() for line in lines))


def _load[T](path: str, expected: type[T], setting: str) -> T:
    """Import ``path`` named in ``setting`` and check it is an ``expected``."""
    try:
        value = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"{setting}: {exc}") from exc
    if not isinstance(value, expected):
        raise ImproperlyConfigured(f"{setting}: {path!r} is not a {expected.__name__}.")
    return value
