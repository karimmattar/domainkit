"""What happens to a user's identities at providers (audited as ``identity.*``)."""

from uuid import UUID

from domainkit.ddd import DomainEvent

__all__ = ["IdentityLinked", "IdentityUnlinked"]


class IdentityLinked(DomainEvent):
    """A user linked their identity at a provider: a new way into the account."""

    user_id: UUID
    provider: str


class IdentityUnlinked(DomainEvent):
    """A user unlinked their identity at a provider."""

    user_id: UUID
    provider: str
