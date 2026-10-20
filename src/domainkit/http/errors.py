"""One error format for every failure, and the mapping from domain errors.

Every error body is ``{"detail", "code", "request_id"}``; validation errors add
``errors`` with the location, message, and type of each problem. Submitted
values are never echoed back, because they may hold secrets.

The domain layer raises ``DomainError`` subclasses and never mentions HTTP;
this module is the only place that maps them to status codes.

Details and validation messages are translated into the request's language
here, as the body is built (``gettext`` and ``validation_message``, see
``domainkit.i18n``): pass English text, and add its translations to the
catalogs.
"""

import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException
from starlette.routing import Match

from domainkit.context import current_request
from domainkit.ddd.exceptions import (
    BusinessRuleViolation,
    Conflict,
    DomainError,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
)
from domainkit.i18n import gettext, validation_message

STATUS_CODES: Final[tuple[tuple[type[DomainError], int], ...]] = (
    (PreconditionFailed, status.HTTP_412_PRECONDITION_FAILED),
    (NotFound, status.HTTP_404_NOT_FOUND),
    (Conflict, status.HTTP_409_CONFLICT),
    (PermissionDenied, status.HTTP_403_FORBIDDEN),
    (BusinessRuleViolation, status.HTTP_422_UNPROCESSABLE_CONTENT),
)
"""Status for each base error; any other ``DomainError`` is a 400."""

DATA_EXCEPTION: Final = "22"
"""SQLSTATE class of values the database cannot take (a NUL, an overflow)."""
BUSY_STATES: Final = frozenset(
    {
        "57014",  # query_canceled: STATEMENT_TIMEOUT ran out.
        "55P03",  # lock_not_available: LOCK_TIMEOUT ran out.
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
    }
)
"""SQLSTATEs of statements worth trying again later."""

_BODILESS: Final = frozenset({status.HTTP_204_NO_CONTENT, status.HTTP_304_NOT_MODIFIED})
_METHODS: Final = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


class FieldError(BaseModel):
    """One invalid part of a request; the submitted value is left out."""

    loc: list[str | int]
    msg: str
    type: str


class ErrorResponse(BaseModel):
    """Body of every error response; also the schema documented in OpenAPI."""

    detail: str
    code: str
    request_id: str | None = None
    errors: list[FieldError] | None = None


class APIError(HTTPException):
    """An HTTP error outside the domain (a bad cursor, an oversized body)."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Carry a machine-readable ``code`` next to the message."""
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers=dict(headers) if headers else None,
        )
        self.code = code


def error_response(
    status_code: int,
    detail: str,
    *,
    code: str,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    """Build an error body that carries the request id for support cases."""
    request = current_request()
    body = ErrorResponse(
        detail=gettext(detail),
        code=code,
        request_id=request.request_id if request else None,
        errors=errors,
    )
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status_code)


def default_code(status_code: int) -> str:
    """Name a status in snake case (404: ``not_found``); unknown ones are generic."""
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        return "http_error"
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def status_code_for(error: DomainError) -> int:
    """Return the HTTP status for ``error`` from its closest mapped base class."""
    for error_type, status_code in STATUS_CODES:
        if isinstance(error, error_type):
            return status_code
    return status.HTTP_400_BAD_REQUEST


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Answer a ``DomainError`` with its status, message, and error code."""
    return error_response(status_code_for(exc), exc.message, code=exc.code)


async def http_error_handler(request: Request, exc: HTTPException) -> Response:
    """Answer ``HTTPException`` (unknown routes, 405, ``APIError``) uniformly."""
    if exc.status_code in _BODILESS:
        return Response(status_code=exc.status_code, headers=exc.headers)
    detail = exc.detail if isinstance(exc.detail, str) else None
    code = exc.code if isinstance(exc, APIError) else default_code(exc.status_code)
    response = error_response(
        exc.status_code,
        detail or code.replace("_", " ").capitalize(),
        code=code,
    )
    if exc.headers:
        response.headers.update(exc.headers)
    if exc.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
        # Starlette lists only the first matching route's methods.
        response.headers["Allow"] = ", ".join(allowed_methods(request))
    return response


def allowed_methods(request: Request) -> list[str]:
    """Return every method some route accepts for the request's path."""
    return [
        method
        for method in _METHODS
        if any(
            route.matches({**request.scope, "method": method})[0] is Match.FULL
            for route in request.app.router.routes
        )
    ]


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Answer invalid requests with what is wrong where, never with the input."""
    errors = [
        FieldError(
            loc=list(error.get("loc", ())),
            msg=validation_message(error),
            type=str(error.get("type", "")),
        )
        for error in exc.errors()
    ]
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "The request is invalid.",
        code="validation_error",
        errors=errors,
    )


def database_refusal(exc: BaseException) -> APIError | None:
    """The answer to a database error that is not a bug; None for any other.

    The pool out of connections, and statements cancelled by a timeout or
    lost to another transaction, are 503 ``database_busy``: try again
    shortly. A value the database cannot store (a NUL character, a number
    or text too long for its column) is 422 ``invalid_value``.
    """
    if isinstance(exc, PoolTimeout):
        return _busy()
    if not isinstance(exc, DBAPIError):
        return None
    state = sqlstate(exc)
    if state in BUSY_STATES:
        return _busy()
    if state is not None and state.startswith(DATA_EXCEPTION):
        return APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A value cannot be stored: it is too long, out of range, or holds "
            "a character the database refuses.",
            code="invalid_value",
        )
    return None


def sqlstate(exc: DBAPIError) -> str | None:
    """The SQLSTATE of a driver's error, from asyncpg or psycopg."""
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state if isinstance(state, str) else None


def _busy() -> APIError:
    """503: the database cannot take the request now."""
    return APIError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The database is busy; try again shortly.",
        code="database_busy",
        headers={"Retry-After": "1"},
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register the domain, HTTP, and validation error handlers on ``app``."""
    app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )


def error_responses(*codes: int) -> dict[int | str, dict[str, object]]:
    """OpenAPI ``responses`` for the error statuses a route may answer with.

    ``@router.get("/x", responses=error_responses(401, 403, 404))`` documents
    each with the ``ErrorResponse`` body. 400, 413, 422, 429, and 500 are
    documented on every route by the application already.
    """
    return {code: {"model": ErrorResponse} for code in codes}
