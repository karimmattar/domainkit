"""Build the FastAPI application from settings (Django's ``get_asgi_application``)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import APIRouter, FastAPI

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.conf.global_settings import DEPLOYED_ENVIRONMENTS
from domainkit.db.drivers import connect_args
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.blocking import watch_event_loop
from domainkit.http.errors import ErrorResponse, install_exception_handlers
from domainkit.http.openapi import (
    duplicate_operation_ids,
    install_openapi,
    operation_id,
)
from domainkit.http.servers import limit_warnings, server_limits
from domainkit.http.versioning import DeprecationHeadersMiddleware
from domainkit.lifecycle import close_resources
from domainkit.utils.module_loading import import_string

logger = logging.getLogger(__name__)

FRAMEWORK_ERRORS: Final = {
    400: "Malformed request",
    413: "Request body too large",
    422: "Invalid request",
    429: "Too many requests",
    500: "Internal server error",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the domains, then stop them and close what they opened.

    With ``DEBUG``, the loop logs steps that hold it for
    ``SLOW_CALLBACK_DURATION`` (``domainkit.http.blocking``) while the
    application runs.
    """
    loop = asyncio.get_running_loop()
    debug, slow = loop.get_debug(), loop.slow_callback_duration
    if settings.DEBUG and settings.SLOW_CALLBACK_DURATION is not None:
        watch_event_loop(loop, settings.SLOW_CALLBACK_DURATION)
    try:
        async with registry.lifespan():
            yield
    finally:
        await close_resources()
        loop.set_debug(debug)
        loop.slow_callback_duration = slow


def get_asgi_application() -> FastAPI:
    """Set up the framework and return the configured application."""
    setup()
    # Refuse bad driver arguments (CONNECT_ARGS, TLS files) now, not at first use.
    for database in settings.DATABASES.values():
        connect_args(database)
    docs = settings.API_DOCS
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        # Errors the framework itself can return, on every route, with the
        # real body instead of FastAPI's default validation schema.
        responses={
            code: {"model": ErrorResponse, "description": description}
            for code, description in FRAMEWORK_ERRORS.items()
        },
        generate_unique_id_function=operation_id,
    )
    install_exception_handlers(app)
    install_openapi(app)
    if settings.ROOT_ROUTER:
        app.include_router(_load(settings.ROOT_ROUTER, APIRouter, "ROOT_ROUTER"))
    # Innermost: it sees the responses routes answer, error handlers' too.
    app.add_middleware(DeprecationHeadersMiddleware)
    # add_middleware() wraps the stack, so add the outermost entry last.
    for path in reversed(settings.MIDDLEWARE):
        middleware: Any = _load(path, type, "MIDDLEWARE")
        app.add_middleware(middleware)
    if problems := application_problems(app):
        listed = "\n".join(f"  {problem}" for problem in problems)
        if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            raise ImproperlyConfigured(f"The application has problems:\n{listed}")
        logger.warning("The application has problems:\n%s", listed)
    if settings.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
        limits = server_limits()  # The server's own command line, here.
        for warning in limit_warnings(settings.MAX_REQUEST_BODY_SIZE, limits):
            logger.warning(warning)
    return app


def application_problems(app: FastAPI) -> list[str]:
    """What is wrong with ``app``: clashing operation ids, and what domains find.

    Domains report through ``DomainConfig.check_application``.
    """
    problems = duplicate_operation_ids(app)
    problems.extend(
        problem for config in registry for problem in config.check_application(app)
    )
    return problems


def _load[T](path: str, expected: type[T], setting: str) -> T:
    """Import ``path`` named in ``setting`` and check it is an ``expected``."""
    try:
        value = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"{setting}: {exc}") from exc
    if not isinstance(value, expected):
        raise ImproperlyConfigured(f"{setting}: {path!r} is not a {expected.__name__}.")
    return value
