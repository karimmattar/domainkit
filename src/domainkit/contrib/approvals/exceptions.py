"""What approvals refuse, each with its code and HTTP status.

Messages are fixed sentences, so the HTTP boundary translates them.
"""

from uuid import UUID

from domainkit.ddd import (
    BusinessRuleViolation,
    Conflict,
    DuplicateValue,
    NotFound,
    PermissionDenied,
)


class ApprovalRequired(PermissionDenied):
    """A guarded command was called directly: it runs only once approved (403)."""

    code = "approval_required"


class ApprovalPending(ApprovalRequired):
    """A guarded command called directly was submitted for approval instead (403).

    ``request_id`` names the request; the command runs once it is approved.
    """

    code = "approval_pending"

    def __init__(self, message: str, request_id: UUID) -> None:
        """Keep the message and the id of the request submitted."""
        super().__init__(message)
        self.request_id = request_id


class RequestNotFound(NotFound):
    """No approval request has that id."""

    code = "approval_request_not_found"


class UnknownAction(NotFound):
    """No guarded command has that name."""

    code = "unknown_action"


class InvalidArguments(BusinessRuleViolation):
    """A command's arguments do not make the command, or are too large."""

    code = "invalid_arguments"


class ApprovalNotNeeded(BusinessRuleViolation):
    """No policy asks for approval of this command: call it directly."""

    code = "approval_not_needed"


class RequestNotPending(Conflict):
    """The request was approved, rejected, cancelled, or has expired."""

    code = "request_not_pending"


class AlreadyDecided(DuplicateValue):
    """This person approved or rejected the request already (409)."""

    code = "already_decided"


class FingerprintMismatch(Conflict):
    """What was approved is not what the request holds."""

    code = "fingerprint_mismatch"


class MakerCannotDecide(PermissionDenied):
    """Whoever submitted a request may not approve or reject it."""

    code = "maker_cannot_decide"


class NotTheMaker(PermissionDenied):
    """Only whoever submitted a request may cancel it."""

    code = "not_the_maker"
