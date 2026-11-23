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

The project's own code (a task, a command, an event handler) submits,
approves, rejects, and cancels with ``record_submission``,
``record_approval``, ``record_rejection``, and ``record_cancellation``:
what the services do, asking for no authorization of the act whatever
``APPROVALS`` says (ADR 0163). Never from a route a user reaches.
"""

from .builtin import (
    CAPTURE_HOLD,
    GRANT_SUPERUSER,
    HOLD_ORDER,
    IMPERSONATE,
    POST_ENTRY,
    REACTIVATE,
    REVERSE_ENTRY,
    SET_GROUP_PERMISSIONS,
    SET_GROUPS,
    SET_PASSWORD,
    SET_PERMISSIONS,
    SET_STAFF,
    GrantSuperuser,
    HeldGroup,
    Impersonate,
    Reactivate,
    SetGroupPermissions,
    SetGroups,
    SetPassword,
    SetPermissions,
    SetStaff,
    held_groups,
    named_keys_held,
    refuse_if_guarded,
)
from .exceptions import (
    AlreadyDecided,
    ApprovalNotNeeded,
    ApprovalPending,
    ApprovalRequired,
    FingerprintMismatch,
    InterestedParty,
    InvalidArguments,
    MakerCannotDecide,
    MakerNotAllowed,
    NotTheMaker,
    PolicyRemoved,
    RequestNotFound,
    RequestNotPending,
    RequestTampered,
    UnknownAction,
)
from .guards import (
    Guard,
    Policy,
    Threshold,
    approved,
    current_run,
    guard,
    guards,
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
    approvers_of,
    cancel_request,
    execute_request,
    expire_requests,
    record_approval,
    record_cancellation,
    record_rejection,
    record_submission,
    reject_request,
    submit_command,
    submit_request,
)
from .settings import ApprovalsSettings
from .uow import ApprovalsUnitOfWork

__all__ = [
    "CAPTURE_HOLD",
    "GRANT_SUPERUSER",
    "HOLD_ORDER",
    "IMPERSONATE",
    "POST_ENTRY",
    "REACTIVATE",
    "REVERSE_ENTRY",
    "SET_GROUPS",
    "SET_GROUP_PERMISSIONS",
    "SET_PASSWORD",
    "SET_PERMISSIONS",
    "SET_STAFF",
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
    "HeldGroup",
    "Impersonate",
    "InterestedParty",
    "InvalidArguments",
    "MakerCannotDecide",
    "MakerNotAllowed",
    "NotTheMaker",
    "Policy",
    "PolicyRemoved",
    "Reactivate",
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
    "RequestTampered",
    "SetGroupPermissions",
    "SetGroups",
    "SetPassword",
    "SetPermissions",
    "SetStaff",
    "SubmitRequest",
    "Threshold",
    "UnknownAction",
    "Verdict",
    "approve_request",
    "approved",
    "approvers_of",
    "cancel_request",
    "current_run",
    "execute_request",
    "expire_requests",
    "fingerprint",
    "guard",
    "guards",
    "held_groups",
    "named_keys_held",
    "record_approval",
    "record_cancellation",
    "record_rejection",
    "record_submission",
    "refuse_if_guarded",
    "reject_request",
    "requires_approval",
    "submit_command",
    "submit_request",
]
