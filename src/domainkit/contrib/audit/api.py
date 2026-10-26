"""Records' history over HTTP, for staff (Django admin's "History").

Mount it where staff tools live::

    from domainkit.contrib.audit.api import router as audit_router

    router.include_router(audit_router, prefix="/admin/audit")

``GET /history?target=r-1`` lists the audit rows about a target, and
``GET /history?field=product_id&id=...`` those about an aggregate, newest
first, with what each changed. Callers need to be staff (or superusers)
with ``audit.view_history``; ``domainkit.contrib.auth`` must be installed.
Personal values are the pseudonyms the trail keeps, and the client's
address is left out.
"""

from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, JsonValue

from domainkit.contrib.auth import IsAdminUser, IsSuperuser
from domainkit.contrib.auth.models import AnyUser
from domainkit.http import (
    CURSOR_PATTERN,
    MAX_CURSOR_LENGTH,
    APIError,
    InvalidCursor,
    decode_cursor,
    encode_cursor,
    error_responses,
)
from domainkit.http.access import HasPermissions, Require

from .history import FIELD, MAX_LIMIT, HistoryEntry, Position, history_page

__all__ = [
    "router",
]

PAGE_SIZE: Final = 50
VIEW_HISTORY: Final = "audit.view_history"

router = APIRouter(tags=["audit"])
staff = Require(IsAdminUser() | IsSuperuser(), HasPermissions(VIEW_HISTORY))
"""Staff, or superusers, granted ``audit.view_history``."""


class FieldChangeOut(BaseModel):
    """A field an entry changed; ``before`` is null when unknown."""

    field: str
    before: JsonValue
    after: JsonValue


class HistoryEntryOut(BaseModel):
    """One audit row about the record."""

    id: UUID
    occurred_at: datetime
    actor: str
    action: str
    outcome: str
    target: str | None
    request_id: str | None
    details: dict[str, JsonValue]
    changes: list[FieldChangeOut]

    @classmethod
    def of(cls, entry: HistoryEntry) -> "HistoryEntryOut":
        """Describe ``entry``."""
        return cls(
            id=entry.id,
            occurred_at=entry.occurred_at,
            actor=entry.actor,
            action=entry.action,
            outcome=entry.outcome,
            target=entry.target,
            request_id=entry.request_id,
            details=dict(entry.details),
            changes=[
                FieldChangeOut(field=c.field, before=c.before, after=c.after)
                for c in entry.changes
            ],
        )


class HistoryPageOut(BaseModel):
    """Entries, newest first, and the cursor of older ones."""

    items: list[HistoryEntryOut]
    next_cursor: str | None


@router.get(
    "/history",
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
)
async def read_history(
    actor: Annotated[AnyUser, Depends(staff)],
    target: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    field: Annotated[str | None, Query(pattern=f"^{FIELD.pattern}$")] = None,
    id: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = PAGE_SIZE,
    cursor: Annotated[
        str | None, Query(max_length=MAX_CURSOR_LENGTH, pattern=CURSOR_PATTERN)
    ] = None,
) -> HistoryPageOut:
    """A record's history: ``target``, or an aggregate's ``field`` and ``id``."""
    aggregate = None if field is None or id is None else (field, id)
    if (target is None) == (aggregate is None) or (field is None) != (id is None):
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Name a target, or an aggregate's field and id.",
            code="invalid_history_query",
        )
    page = await history_page(
        target=target, aggregate=aggregate, limit=limit, before=_position(cursor)
    )
    last = page.entries[-1] if page.more else None
    return HistoryPageOut(
        items=[HistoryEntryOut.of(entry) for entry in page.entries],
        next_cursor=None
        if last is None
        else encode_cursor([last.occurred_at.isoformat(), str(last.id)]),
    )


def _position(cursor: str | None) -> Position | None:
    """The entry a cursor continues after."""
    if cursor is None:
        return None
    at, entry = decode_cursor(cursor, 2)
    try:
        moment = datetime.fromisoformat(at)
        identifier = UUID(entry)
    except ValueError:
        raise InvalidCursor from None
    if moment.tzinfo is None:
        raise InvalidCursor
    return moment, identifier
