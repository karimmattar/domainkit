"""Batches: one command for many items in one request (``batch_route``).

``POST /invoices/batch`` takes ``{"items": [...], "atomic": false}``, at
most ``max_items`` of them, each validated before any runs. Each item is
the command's body, and the id of the aggregate it acts on for commands
on one aggregate (with an ``if_match`` of its own). The answer, ``207``,
holds one result per item, in order: its status, and the aggregate or
the error, as a single request would have answered.

Without ``atomic``, each item is a use case of its own: those that fail
leave the others done. With ``atomic``, all run in one transaction, each
service's own commit deferred to the end; the first failure undoes them
all, and every other item answers ``424 not_applied``.
"""

from types import TracebackType
from typing import Any, Final, Self

from fastapi import status
from pydantic import BaseModel, Field, JsonValue
from starlette.exceptions import HTTPException

from domainkit.ddd.exceptions import DomainError
from domainkit.ddd.uow import UnitOfWork
from domainkit.http.errors import APIError, database_refusal, status_code_for
from domainkit.i18n import gettext

MAX_ITEMS: Final = 100
"""The most items a batch takes, unless its route says otherwise."""
NOT_APPLIED: Final = "not_applied"


class BatchError(BaseModel):
    """Why one item failed, as a request of its own would have been told."""

    code: str
    detail: str


class BatchResult(BaseModel):
    """What became of one item."""

    status: int
    """The status a request of its own would have answered with."""
    item: dict[str, JsonValue] | None = None
    """The aggregate (or the command's answer), if it has one."""
    etag: str | None = None
    location: str | None = None
    """Where a created aggregate lives."""
    error: BatchError | None = None


class BatchAnswer(BaseModel):
    """One result per item, in order, and how many failed."""

    results: list[BatchResult]
    failed: int = Field(description="How many items failed, not counting 424s.")


class Enlisted:
    """An open unit of work, lent to a service; the batch commits it.

    The service's own enter, commit, and exit do nothing.
    """

    def __init__(self, uow: UnitOfWork) -> None:
        """Lend ``uow``."""
        self._uow = uow

    async def __aenter__(self) -> Self:
        """The unit of work is open already."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave it open; the batch rolls back if the service failed."""

    async def commit(self) -> None:
        """Commit later, with the rest of the batch."""

    async def rollback(self) -> None:
        """The batch rolls back, if anything failed."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - the unit of work's.
        """Repositories and the rest come from the unit of work."""
        return getattr(self._uow, name)


class Undo(Exception):
    """Raised to leave an atomic batch's savepoint once an item failed."""


def failure(exc: Exception) -> BatchResult | None:
    """The result of an item that raised ``exc``; None if it is a bug, not a refusal."""
    if isinstance(exc, DomainError):
        return _failed(status_code_for(exc), exc.code, exc.message)
    refused = exc if isinstance(exc, HTTPException) else database_refusal(exc)
    if refused is None:
        return None
    code = refused.code if isinstance(refused, APIError) else "http_error"
    return _failed(refused.status_code, code, str(refused.detail))


def not_applied() -> BatchResult:
    """An item of an atomic batch that another item's failure undid or stopped."""
    return _failed(
        status.HTTP_424_FAILED_DEPENDENCY,
        NOT_APPLIED,
        "Another item of the batch failed, so nothing was applied.",
    )


def _failed(status_code: int, code: str, detail: str) -> BatchResult:
    """A failed item's result, its detail in the request's language."""
    return BatchResult(
        status=status_code, error=BatchError(code=code, detail=gettext(detail))
    )
