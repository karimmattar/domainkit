"""Approval requests over HTTP: submit, list, read, approve, reject, cancel.

Mount it where the project's API lives::

    from domainkit.contrib.approvals.api import router as approvals

    router.include_router(approvals, prefix="/api/approvals")

Who may do what:

- Reading and listing need ``approvals.view_approvalrequest``, and show
  the requests the caller made and those whose policy names them among
  its approvers; ``approvals.view_every_approvalrequest`` shows every
  request (user report P3). Another request is ``404``.
- Submitting needs ``approvals.add_approvalrequest``, and the permission
  the guard names for its makers (``accounting.add_entry`` for postings).
- Approving and rejecting need to be one of the approvers the request's
  policy names (its permission, or its group), never its maker, once per
  person; approving also needs the policy's ``approve_rule``, or
  ``APPROVALS.APPROVE_RULE``, such as a recent second factor. With
  ``APPROVALS.APPROVE_ACTION`` on, it needs an authorization of that one
  approval too (``Action-Authorization``), which the approver asks for at
  ``POST /requests/{id}/approve/authorization``; off, that route answers
  ``404``.
- Cancelling needs ``approvals.add_approvalrequest`` and to be the maker.
- With ``APPROVALS.SUBMIT_ACTION``, ``REJECT_ACTION``, or ``CANCEL_ACTION``
  on, submitting, rejecting, or cancelling needs an authorization of that
  one act too, asked for at ``POST /requests/authorization``, ``POST
  /requests/{id}/reject/authorization``, or ``POST
  /requests/{id}/cancel/authorization`` (ADR 0163); off, those answer
  ``404``.

An approval states the ``fingerprint`` the approver was shown: a request
that holds another command is refused (``fingerprint_mismatch``).
"""

from datetime import datetime
from typing import Annotated, Any, Final, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import HTTPConnection

from domainkit.ddd import Criteria, F, PermissionDenied
from domainkit.http import Filter, FilterSet, Pagination, error_responses
from domainkit.http.access import HasPermissions, Principal, Require, current_user
from domainkit.resources import HasModelPermission, ResourceRouter

from .exceptions import UnknownAction
from .guards import guard_for, guards
from .models import MAX_REASON, ApprovalRequest, RequestStatus, Verdict
from .rules import (
    ActsForThemselves,
    ApprovalAuthorizationOut,
    RequestActions,
    SubmitAction,
    names_approver,
    rule_instance,
)
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
NAMES: Final = "approvals_"
"""What every route's name starts with, and so its operation id: a project's
resource of the same name clashes with none of them (ADR 0192)."""
VIEW_EVERY: Final = "approvals.view_every_approvalrequest"
"""The permission that shows every request, not only one's own and those one decides."""
READS: Final = frozenset({"GET", "HEAD"})
TWIN: Final = "domainkit_twin"
"""The attribute that marks a twin's endpoint: it asks for an authorization,
and makes no act (``domainkit.resources.asking_authorization``)."""


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


async def visible(connection: HTTPConnection) -> Criteria | None:
    """What a caller reads of the requests: theirs, and those they may decide.

    The requests they made, and those of a policy that names them among its
    approvers (by its permission, its group, or its approver rule); every
    request with ``approvals.view_every_approvalrequest``. Only reads are
    narrowed: approving, rejecting, and cancelling ask their own rules.
    """
    if connection.scope.get("method") not in READS:
        return None
    user = current_user(connection)
    if await HasPermissions(VIEW_EVERY).has_permission(connection, user):
        return None
    shown: Criteria = F("maker") == str(user.id)
    for action, registered in guards().items():
        for policy in registered.policies:
            if await names_approver(connection, user, policy):
                shown |= (F("action") == action) & (F("policy") == policy.name)
    return shown


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
    scope=visible,
    tags=TAGS,
    name_prefix=NAMES,
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

MAY_SUBMIT: Final = HasModelPermission("add", ApprovalRequest)
SUBMIT: Final = Depends(Require(MAY_SUBMIT, SubmitAction()))
"""Who submits: ``approvals.add_approvalrequest``, and, with
``SUBMIT_ACTION``, an authorization of the submission."""
ASKS_TO_SUBMIT: Final = Depends(Require(MAY_SUBMIT))


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

    The maker needs the permission the guard names for its makers too, and
    to pass its ``maker_rule`` (a superuser, to make superusers). The
    guard's ``maker_fields`` are set to the maker, whatever ``arguments``
    says; a guard with ``http=False`` is not submitted here (404). With
    ``APPROVALS.SUBMIT_ACTION`` on, it spends the maker's authorization of
    this submission (``Action-Authorization``).
    """
    made = await submit_request(
        await _submission(body, request, user), ApprovalsUnitOfWork()
    )
    response.headers["Location"] = f"{request.url.path}/{made.id}"
    return RequestOut.of(made)


@requests.post(
    "/authorization",
    name="approvals_submit_authorization",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 401, 403, 404, 409, 422),
)
async def ask_to_submit(
    body: SubmitIn,
    request: Request,
    response: Response,
    user: Annotated[Principal, ASKS_TO_SUBMIT],
) -> ApprovalAuthorizationOut:
    """Ask for an authorization of submitting this command (``SUBMIT_ACTION``).

    It checks what submitting checks, and stores nothing but the
    authorization. Off, it answers ``404``, as a route that is not there,
    to a caller submitting would not refuse first.
    """
    response.headers["Cache-Control"] = "no-store"
    command = await _submission(body, request, user)
    answer = await SubmitAction().ask_authorization(request, command)
    return ApprovalAuthorizationOut.model_validate(answer.model_dump())


setattr(ask_to_submit, TWIN, True)


async def _submission(
    body: SubmitIn, request: Request, user: Principal
) -> SubmitRequest:
    """The submission ``body`` asks for, if ``user`` may make it.

    Raises:
        UnknownAction: No guard has that name, or its commands are not
            submitted here (``http=False``).
        PermissionDenied: Someone acts for the user, or the user lacks the
            guard's permission or fails its ``maker_rule``.
    """
    guard = guard_for(body.action)
    if not guard.http:  # Submitted by the project's own code alone.
        raise UnknownAction("No guarded command has that name.")
    rules = [
        ActsForThemselves(),
        HasPermissions(guard.permission) if guard.permission else None,
        rule_instance(guard.maker_rule),
    ]
    for rule in rules:
        if rule is not None and not await rule.has_permission(request, user):
            raise PermissionDenied("You may not submit this command.")
    return SubmitRequest(
        action=body.action, arguments=body.arguments, maker=str(user.id)
    )


router = APIRouter()
"""Every route of the requests (see the module)."""
router.include_router(requests)
