"""What happens to a user's sign-in requests (audited as ``ciba.*``).

The events name the request by its id; who asked, from where, stays in
its table.
"""

from uuid import UUID

from domainkit.ddd import DomainEvent

__all__ = ["SignInApproved", "SignInDenied", "SignInReported"]


class SignInApproved(DomainEvent):
    """A user approved a sign-in from one of their bound devices."""

    user_id: UUID
    request_id: UUID
    client_id: str | None
    """The CIBA client that asked; None: the project's own web pages."""
    device_id: UUID


class SignInDenied(DomainEvent):
    """A user denied a sign-in."""

    user_id: UUID
    request_id: UUID
    client_id: str | None


class SignInReported(DomainEvent):
    """A user denied a sign-in they did not start: someone else knows them."""

    user_id: UUID
    request_id: UUID
    client_id: str | None
