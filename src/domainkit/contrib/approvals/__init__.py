"""Maker-checker: commands that run only once other people approve them.

Install it (with messaging and audit), then ``makemigrations`` and
``migrate``::

    INSTALLED_DOMAINS = [
        "domainkit.contrib.messaging",
        "domainkit.contrib.audit",
        "domainkit.contrib.approvals",
        ...,
    ]

Guard a use case: decorate its service, and register its policies in
``ready()``::

    @requires_approval("payments.refund")
    async def refund(command: Refund, uow: PaymentsUnitOfWork) -> Payment: ...

    guard("payments.refund", refund, unit_of_work=PaymentsUnitOfWork,
          policies=[Policy("payments.refunds", approvals=2,
                           permission="payments.approve_refund")])

Called directly, the service refuses a command that needs approval
(``ApprovalRequired``). A maker submits it instead (``submit_command``,
or ``POST /requests`` of ``api.router``); people the policy names, other
than the maker and each once, approve it, stating the fingerprint they
were shown; the last approval queues it through the outbox, and the
worker runs it once, after checking it is what was approved. Requests
expire (``APPROVALS.EXPIRES_AFTER``), are rejected with a reason, or
cancelled by their maker; every change is a domain event the audit trail
records with its actor. See ``docs/contrib/approvals.md`` and ADR 0121.
"""

from .builtin import ENTRIES, SUPERUSERS, GrantSuperuser, refuse_if_guarded
from .exceptions import (
    AlreadyDecided,
    ApprovalNotNeeded,
    ApprovalPending,
    ApprovalRequired,
    FingerprintMismatch,
    InvalidArguments,
    MakerCannotDecide,
    NotTheMaker,
    RequestNotFound,
    RequestNotPending,
    UnknownAction,
)
from .guards import (
    Guard,
    Policy,
    Threshold,
    approved,
    guard,
    guards,
    on_guard,
    requires_approval,
)
from .models import (
    ApprovalGiven,
    ApprovalRequest,
    Decision,
    RequestApproved,
    RequestCancelled,
    RequestExecuted,
    RequestExpired,
    RequestFailed,
    RequestId,
    RequestRejected,
    RequestStatus,
    RequestSubmitted,
    Verdict,
    fingerprint,
)
from .services import (
    ApproveRequest,
    CancelRequest,
    RejectRequest,
    SubmitRequest,
    approve_request,
    cancel_request,
    execute_request,
    expire_requests,
    reject_request,
    submit_command,
    submit_request,
)
from .settings import ApprovalsSettings
from .uow import ApprovalsUnitOfWork

__all__ = [
    "ENTRIES",
    "SUPERUSERS",
    "AlreadyDecided",
    "ApprovalGiven",
    "ApprovalNotNeeded",
    "ApprovalPending",
    "ApprovalRequest",
    "ApprovalRequired",
    "ApprovalsSettings",
    "ApprovalsUnitOfWork",
    "ApproveRequest",
    "CancelRequest",
    "Decision",
    "FingerprintMismatch",
    "GrantSuperuser",
    "Guard",
    "InvalidArguments",
    "MakerCannotDecide",
    "NotTheMaker",
    "Policy",
    "RejectRequest",
    "RequestApproved",
    "RequestCancelled",
    "RequestExecuted",
    "RequestExpired",
    "RequestFailed",
    "RequestId",
    "RequestNotFound",
    "RequestNotPending",
    "RequestRejected",
    "RequestStatus",
    "RequestSubmitted",
    "SubmitRequest",
    "Threshold",
    "UnknownAction",
    "Verdict",
    "approve_request",
    "approved",
    "cancel_request",
    "execute_request",
    "expire_requests",
    "fingerprint",
    "guard",
    "guards",
    "on_guard",
    "refuse_if_guarded",
    "reject_request",
    "requires_approval",
    "submit_command",
    "submit_request",
]
