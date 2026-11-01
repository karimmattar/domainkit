"""Limits over HTTP: list, read, make, change, and remove them; read usage.

Mount it where the project's API lives::

    from domainkit.contrib.limits.api import router as limits

    router.include_router(limits, prefix="/api/limits")

Each route needs a model permission, whatever authenticates requests:
``limits.view_limit`` to read limits and usage, ``add_limit``,
``change_limit``, and ``delete_limit`` to change them. Every change is
recorded in the audit trail (``LimitSet``, ``LimitRemoved``), with who
made it. Amounts are ``{"amount": "5000.00", "currency": "EGP"}``.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Final, Self

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from domainkit.http import Filter, FilterSet, Pagination, error_responses
from domainkit.http.access import Require
from domainkit.money import Money
from domainkit.resources import HasModelPermission, ModelActions, ResourceRouter

from .models import MAX_NOTE, MAX_ZONE, Direction, Limit, Window
from .services import (
    ChangeLimit,
    RemoveLimit,
    SetLimit,
    change_limit,
    define_limit,
    remove_limit,
)
from .uow import LimitsUnitOfWork
from .usage import Usage, usage_of

__all__ = ["limits", "router"]

PAGES: Final = Pagination(default_limit=50, max_limit=200)
TAGS: Final[list[str | Enum]] = ["limits"]
VIEW_LIMITS: Final = Depends(Require(HasModelPermission("view", Limit)))
Code = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")]
Kind = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
Subject = Annotated[str, Field(min_length=1, max_length=255)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{2,7}$")]
Zone = Annotated[str, Field(min_length=1, max_length=MAX_ZONE)]
Note = Annotated[str, Field(max_length=MAX_NOTE)]
Count = Annotated[int, Field(ge=1)]


class LimitFilters(FilterSet):
    """Limits by code, kind, direction, currency, subject, or whether enabled."""

    model = Limit
    code = Filter("exact")
    kind = Filter("exact")
    direction = Filter("exact")
    currency = Filter("exact")
    subject = Filter("exact")
    enabled = Filter("exact")
    ordering_fields = ("code", "changed_at")
    default_ordering = ("code",)


class BoundsIn(BaseModel):
    """What a limit allows; one left out is no bound."""

    model_config = ConfigDict(extra="forbid")

    per_transaction: Money | None = None
    window: Window | None = None
    max_amount: Money | None = None
    max_count: Count | None = None
    time_zone: Zone | None = None
    """For calendar windows; by default ``TIME_ZONE`` (a change keeps the
    limit's)."""
    enabled: bool = True
    note: Note = ""


class LimitIn(BoundsIn):
    """A new limit: its key, and what it allows."""

    code: Code
    kind: Kind
    currency: CurrencyCode
    subject: Annotated[str, Field(max_length=255)] = ""
    """One subject it is for; ``""``, every subject of the kind."""
    direction: Direction = Direction.OUT


limits = ResourceRouter(
    "/limits",
    model=Limit,
    unit_of_work=LimitsUnitOfWork,
    repository=lambda uow: uow.limits,
    filters=LimitFilters,
    pagination=PAGES,
    rule=ModelActions(Limit),
    tags=TAGS,
)
"""The limits: list, read, make, change, and remove."""
limits.command_route(SetLimit, service=define_limit, body=LimitIn)
limits.command_route(
    ChangeLimit, service=change_limit, body=BoundsIn, path="/change", action="change"
)
limits.command_route(RemoveLimit, service=remove_limit, method="DELETE")


class UsageOut(BaseModel):
    """What a subject used of one limit in its current window."""

    code: str
    kind: str
    direction: Direction
    subject: str
    """The limit's subject: the subject's own, or ``""`` for every one."""
    window: Window | None
    since: datetime | None
    used: Money
    count: int
    remaining: Money | None
    per_transaction: Money | None
    max_amount: Money | None
    max_count: int | None
    origin: str
    """``stored``, or ``settings`` for a default of ``LIMITS.DEFAULTS``."""

    @classmethod
    def of(cls, usage: Usage) -> Self:
        """Describe ``usage``."""
        return cls(
            code=usage.code,
            kind=usage.kind,
            direction=usage.direction,
            subject=usage.subject,
            window=usage.window,
            since=usage.since,
            used=usage.used,
            count=usage.count,
            remaining=usage.remaining,
            per_transaction=usage.per_transaction,
            max_amount=usage.max_amount,
            max_count=usage.max_count,
            origin="stored" if usage.stored else "settings",
        )


class UsagePage(BaseModel):
    """A subject's usage of each limit that bounds it."""

    items: list[UsageOut]


router = APIRouter()
"""Every route of the limits (see the module)."""


@router.get(
    "/usage",
    name="limits_usage",
    tags=TAGS,
    responses=error_responses(401, 403, 422),
    dependencies=[VIEW_LIMITS],
)
async def read_usage(
    kind: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_]{0,31}$")],
    subject: Annotated[str, Query(min_length=1, max_length=255)],
    currency: Annotated[str | None, Query(pattern=r"^[A-Z][A-Z0-9]{2,7}$")] = None,
    direction: Direction | None = None,
) -> UsagePage:
    """A subject's usage of each limit that bounds it, in its current window."""
    found = await usage_of(kind, subject, currency=currency, direction=direction)
    return UsagePage(items=[UsageOut.of(each) for each in found])


router.include_router(limits)
