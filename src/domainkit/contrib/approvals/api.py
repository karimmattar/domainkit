"""Approval requests over HTTP: submit, list, read, approve, reject, cancel.

Mount it where the project's API lives::

    from domainkit.contrib.approvals.api import router as approvals

    router.include_router(approvals, prefix="/api/approvals")

Who may do what:

- Reading and listing need ``approvals.view_approvalrequest``.
- Submitting needs ``approvals.add_approvalrequest``, and the permission
  the guard names for its makers (``accounting.add_entry`` for postings).
- Approving and rejecting need to be one of the approvers the request's
  policy names (its permission, or its group), never its maker, once per
  person; approving also needs the policy's ``approve_rule``, or
  ``APPROVALS.APPROVE_RULE``, such as a recent second factor.
- Cancelling needs ``approvals.add_approvalrequest`` and to be the maker.

An approval states the ``fingerprint`` the approver was shown: a request
that holds another command is refused (``fingerprint_mismatch``).
"""

from datetime import datetime
from typing import Annotated, Any, Final, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.ddd import PermissionDenied
from domainkit.http import Filter, FilterSet, Pagination, error_responses
from domainkit.http.access import HasPermissions, Principal, Require
from domainkit.resources import HasModelPermission, ResourceRouter

from .guards import guard_for
from .models import MAX_REASON, ApprovalRequest, RequestStatus, Verdict
from .rules import RequestActions
from .services import (
    ApproveRequest,
    CancelRequest,
    RejectRequest,
    SubmitRequest,
    approve_request,
    cancel_request,
    reject_request,
    submit_request,
)
from .uow import ApprovalsUnitOfWork

__all__ = [
    "requests",
    "router",
]

PAGES: Final = Pagination(default_limit=50, max_limit=200)
TAGS: Final = ["approvals"]


class RequestFilters(FilterSet):
    """Requests by status, action, policy, or maker; newest first."""

    model = ApprovalRequest
    status = Filter("exact", "in")
    action = Filter("exact")
    policy = Filter("exact")
    maker = Filter("exact")
    ordering_fields = ("submitted_at", "expires_at")
    default_ordering = ("-submitted_at",)


class DecisionOut(BaseModel):
    """One person's decision about a request."""

    approver: str
    verdict: Verdict
    fingerprint: str
    decided_at: datetime


class RequestOut(BaseModel):
    """A request: the command it holds, who submitted it, and who decided it."""

    id: UUID
    action: str
    arguments: dict[str, Any]
    fingerprint: str
    """SHA-256 of the action and arguments, canonical JSON: what approvals state."""
    maker: str
    policy: str
    required: int
    approvals: int
    status: RequestStatus
    reason: str
    submitted_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
    decisions: list[DecisionOut]

    @classmethod
    def of(cls, request: ApprovalRequest) -> Self:
        """Describe ``request``."""
        return cls(
            id=request.id,
            action=request.action,
            arguments=request.arguments,
            fingerprint=request.fingerprint,
            maker=request.maker,
            policy=request.policy,
            required=request.required,
            approvals=request.approvals,
            status=request.status,
            reason=request.reason,
            submitted_at=request.submitted_at,
            expires_at=request.expires_at,
            decided_at=request.decided_at,
            executed_at=request.executed_at,
            decisions=[
                DecisionOut(
                    approver=each.approver,
                    verdict=each.verdict,
                    fingerprint=each.fingerprint,
                    decided_at=each.decided_at,
                )
                for each in request.decisions
            ],
        )


Reason = Annotated[str, Field(max_length=MAX_REASON)]


class SubmitIn(BaseModel):
    """A guarded command to submit: its action, and its arguments as JSON."""

    model_config = ConfigDict(extra="forbid")

    action: Annotated[str, Field(min_length=1, max_length=128)]
    arguments: dict[str, Any]


class ApproveIn(BaseModel):
    """What the approver was shown: the request's fingerprint."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RejectIn(BaseModel):
    """Why the request is rejected."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=1, max_length=MAX_REASON)]


class CancelIn(BaseModel):
    """Why the maker withdraws it."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason = ""


async def caller(user: Annotated[Principal, Depends(Require())]) -> str:
    """The id of the principal acting: the maker, or the approver."""
    return str(user.id)


requests = ResourceRouter(
    "/requests",
    model=ApprovalRequest,
    schema=RequestOut,
    present=RequestOut.of,
    unit_of_work=ApprovalsUnitOfWork,
    repository=lambda uow: uow.requests,
    filters=RequestFilters,
    pagination=PAGES,
    lookup="request_id",
    rule=RequestActions(),
    tags=TAGS,
)
"""Approval requests: list, read, approve, reject, and cancel."""
requests.command_route(
    ApproveRequest,
    service=approve_request,
    body=ApproveIn,
    path="/approve",
    action="approve",
    provide={"approver": caller},
)
requests.command_route(
    RejectRequest,
    service=reject_request,
    body=RejectIn,
    path="/reject",
    action="reject",
    provide={"approver": caller},
)
requests.command_route(
    CancelRequest,
    service=cancel_request,
    body=CancelIn,
    path="/cancel",
    action="cancel",
    provide={"maker": caller},
)

SUBMIT: Final = Depends(Require(HasModelPermission("add", ApprovalRequest)))


@requests.post(
    "",
    name="approvals_submit",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 404, 422),
)
async def submit(
    body: SubmitIn,
    request: Request,
    response: Response,
    user: Annotated[Principal, SUBMIT],
) -> RequestOut:
    """Submit a guarded command; it runs once its policy's approvers approve it.

    The maker needs the permission the guard names for its makers too.
    """
    guard = guard_for(body.action)
    if guard.permission is not None and not await HasPermissions(
        guard.permission
    ).has_permission(request, user):
        raise PermissionDenied("You may not submit this command.")
    made = await submit_request(
        SubmitRequest(action=body.action, arguments=body.arguments, maker=str(user.id)),
        ApprovalsUnitOfWork(),
    )
    response.headers["Location"] = f"{request.url.path}/{made.id}"
    return RequestOut.of(made)


router = APIRouter()
"""Every route of the requests (see the module)."""
router.include_router(requests)
