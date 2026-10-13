"""Build the FastAPI application from settings (Django's ``get_asgi_application``)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import APIRouter, FastAPI

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.db.connections import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import ErrorResponse, install_exception_handlers
from domainkit.utils.module_loading import import_string

FRAMEWORK_ERRORS: Final = {
    400: "Malformed request",
    413: "Request body too large",
    422: "Invalid request",
    429: "Too many requests",
    500: "Internal server error",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the domains, then stop them and close database connections."""
    try:
        async with registry.lifespan():
            yield
    finally:
        await connections.close_all()


def get_asgi_application() -> FastAPI:
    """Set up the framework and return the configured application."""
    setup()
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
    )
    install_exception_handlers(app)
    if settings.ROOT_ROUTER:
        app.include_router(_load(settings.ROOT_ROUTER, APIRouter, "ROOT_ROUTER"))
    # add_middleware() wraps the stack, so add the outermost entry last.
    for path in reversed(settings.MIDDLEWARE):
        middleware: Any = _load(path, type, "MIDDLEWARE")
        app.add_middleware(middleware)
    return app


def _load[T](path: str, expected: type[T], setting: str) -> T:
    """Import ``path`` named in ``setting`` and check it is an ``expected``."""
    try:
        value = import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f"{setting}: {exc}") from exc
    if not isinstance(value, expected):
        raise ImproperlyConfigured(f"{setting}: {path!r} is not a {expected.__name__}.")
    return value
