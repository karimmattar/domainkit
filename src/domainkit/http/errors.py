"""One error format for every failure, and the mapping from domain errors.

Every error body is ``{"detail", "code", "request_id"}``; validation errors add
``errors`` with the location, message, and type of each problem, and so do
taken unique values, under the fields' API names. Submitted values are never
echoed back, because they may hold secrets.

With ``ERROR_FORMAT = "problem+json"`` the same errors are RFC 9457 problem
details instead: ``type``, ``title``, ``status``, ``detail``, and
``instance`` (the request's path), with ``code``, ``request_id``, and
``errors`` as extension members, as ``application/problem+json``.

The domain layer raises ``DomainError`` subclasses and never mentions HTTP;
this module is the only place that maps them to status codes. Database
refusals that are not bugs are mapped here too (``database_refusal``).

Details and validation messages are translated into the request's language
here, as the body is built (``gettext`` and ``validation_message``, see
``domainkit.i18n``): pass English text, and add its translations to the
catalogs.
"""

import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Final
from urllib.parse import quote

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException
from starlette.routing import Match

from domainkit.conf import settings
from domainkit.context import RequestContext, current_request
from domainkit.db.errors import (
    IntegrityViolation,
    duplicate_value,
    integrity_violation,
)
from domainkit.db.orm import metadata
from domainkit.ddd.exceptions import (
    BusinessRuleViolation,
    Conflict,
    DomainError,
    DuplicateValue,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
)
from domainkit.http.responses import BuiltJSONResponse, JSONResponse
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

PROBLEM_FORMAT: Final = "problem+json"
"""The ``ERROR_FORMAT`` of RFC 9457 problem details."""
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
ABOUT_BLANK: Final = "about:blank"
"""The problem type that adds nothing to the status (RFC 9457, section 4.2.1)."""
_PATH_SAFE: Final = "/:@!$&'()*+,;=-._~"
"""Characters a URI path keeps as they are; ``instance`` escapes the rest."""


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


class ProblemDetails(BaseModel):
    """An error as RFC 9457 problem details (``ERROR_FORMAT = "problem+json"``).

    ``code``, ``request_id``, and ``errors`` are extension members, the
    same as in ``ErrorResponse``.
    """

    type: str = ABOUT_BLANK
    """``about:blank``, or ``ERROR_TYPE_BASE_URL`` followed by the code."""
    title: str
    """The same for every occurrence of the type: the status phrase for
    ``about:blank``, else the code in words."""
    status: int
    detail: str
    instance: str | None = None
    """The path of the request that failed."""
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
        errors: Sequence[FieldError] | None = None,
    ) -> None:
        """Carry a machine-readable ``code``, and any field ``errors``."""
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers=dict(headers) if headers else None,
        )
        self.code = code
        self.errors = list(errors) if errors else None


def error_response(
    status_code: int,
    detail: str,
    *,
    code: str,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    """Build an error body that carries the request id for support cases.

    It is problem details when ``ERROR_FORMAT`` says so.
    """
    request = current_request()
    detail = gettext(detail)
    if settings.ERROR_FORMAT == PROBLEM_FORMAT:
        problem = problem_details(
            status_code, detail, code=code, errors=errors, request=request
        )
        return BuiltJSONResponse(
            problem.model_dump(exclude_none=True),
            status_code=status_code,
            media_type=PROBLEM_MEDIA_TYPE,
        )
    body = ErrorResponse(
        detail=detail,
        code=code,
        request_id=request.request_id if request else None,
        errors=errors,
    )
    return BuiltJSONResponse(
        body.model_dump(exclude_none=True), status_code=status_code
    )


def problem_details(
    status_code: int,
    detail: str,
    *,
    code: str,
    errors: list[FieldError] | None = None,
    request: RequestContext | None = None,
) -> ProblemDetails:
    """The RFC 9457 problem for an error answered with ``status_code``.

    Without ``ERROR_TYPE_BASE_URL`` the type is ``about:blank`` and the
    title the status phrase, as RFC 9457 asks; with it, each code is a
    type of its own.
    """
    base = settings.ERROR_TYPE_BASE_URL
    if base is None:
        problem_type, title = ABOUT_BLANK, status_phrase(status_code)
    else:
        problem_type, title = f"{base}{code}", code.replace("_", " ").capitalize()
    return ProblemDetails(
        type=problem_type,
        title=title,
        status=status_code,
        detail=detail,
        instance=quote(request.path, safe=_PATH_SAFE) if request else None,
        code=code,
        request_id=request.request_id if request else None,
        errors=errors,
    )


def status_phrase(status_code: int) -> str:
    """The reason phrase of a status (409: ``Conflict``); ``Error`` if unknown."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


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
    errors = duplicate_errors(exc) if isinstance(exc, DuplicateValue) else None
    return error_response(
        status_code_for(exc), exc.message, code=exc.code, errors=errors
    )


def duplicate_errors(error: DuplicateValue) -> list[FieldError] | None:
    """One error per field of a taken value, under ``body``; None if unnamed."""
    if not error.fields:
        return None
    message = (
        "This value is already taken."
        if len(error.fields) == 1
        else "This combination of values is already taken."
    )
    return [
        FieldError(loc=["body", field], msg=message, type=error.code)
        for field in error.fields
    ]


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
        errors=exc.errors if isinstance(exc, APIError) else None,
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
    or text too long for its column) is 422 ``invalid_value``. Integrity
    errors are answered as ``integrity_refusal`` says.
    """
    if isinstance(exc, PoolTimeout):
        return _busy()
    if isinstance(exc, IntegrityError):
        return integrity_refusal(exc)
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


def integrity_refusal(exc: IntegrityError) -> APIError | None:
    """The answer to a constraint the database enforced; None if unknown.

    A taken unique value is 409 with the constraint's code (outside a unit
    of work, which raises ``DuplicateValue`` itself). A foreign key is 409
    ``referenced``: the row is still referenced, or refers to a row that
    does not exist. A NOT NULL column is 422 ``missing_value``, and a CHECK
    constraint 422 ``check_violation``. Fields are named where the
    database says which; values never are.
    """
    duplicate = duplicate_value(exc, metadata)
    if duplicate is not None:
        return APIError(
            status.HTTP_409_CONFLICT,
            duplicate.message,
            code=duplicate.code,
            errors=duplicate_errors(duplicate),
        )
    violation = integrity_violation(exc, metadata)
    if violation is None:
        return None
    if violation.kind == "foreign_key":
        return _referenced(violation)
    if violation.kind == "not_null":
        return APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A required value is missing.",
            code="missing_value",
            errors=_field_errors(violation, "This field is required.", "missing_value"),
        )
    return APIError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "A value breaks a rule of the data: it is out of the range or set the "
        "database accepts.",
        code="check_violation",
        errors=_field_errors(
            violation, "This value is not allowed.", "check_violation"
        ),
    )


def _referenced(violation: IntegrityViolation) -> APIError:
    """409 ``referenced``, saying which side of the reference broke, if known."""
    if violation.still_referenced is True:
        detail = "Other records still refer to this one; change or remove them first."
    elif violation.still_referenced is False:
        detail = "A record this one refers to does not exist."
    else:
        detail = (
            "A reference between records would break: a record this one refers "
            "to does not exist, or other records still refer to it."
        )
    return APIError(
        status.HTTP_409_CONFLICT,
        detail,
        code="referenced",
        errors=_field_errors(violation, "No such record.", "referenced"),
    )


def _field_errors(
    violation: IntegrityViolation, message: str, kind: str
) -> list[FieldError] | None:
    """One error per field the violation names, under ``body``."""
    return [
        FieldError(loc=["body", field], msg=message, type=kind)
        for field in violation.fields
    ] or None


def sqlstate(exc: DBAPIError) -> str | None:
    """The SQLSTATE of a driver's error, from asyncpg or psycopg."""
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state if isinstance(state, str) else None


def refusal_reason(exc: BaseException) -> str:
    """How logs name a refused statement: its SQLSTATE, or SQLite's error name.

    Constraints are named too; values never are.
    """
    if not isinstance(exc, DBAPIError):
        return type(exc).__name__
    state = sqlstate(exc)
    name = getattr(exc.orig, "sqlite_errorname", None)
    reason = f"SQLSTATE {state}" if state or not isinstance(name, str) else name
    if isinstance(exc, IntegrityError):
        violation = integrity_violation(exc, metadata)
        constraint = violation.constraint if violation else None
        if constraint is None:
            duplicate = duplicate_value(exc, metadata)
            constraint = duplicate.constraint if duplicate else None
        if constraint:
            reason += f", constraint {constraint}"
    return reason


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
