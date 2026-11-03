"""What happens to a user's phone numbers (audited as ``phones.*``).

The events name the number by its id: the number itself stays in its table.
"""

from uuid import UUID

from domainkit.ddd import DomainEvent

__all__ = ["PhoneRemoved", "PhoneVerified"]


class PhoneVerified(DomainEvent):
    """A user proved they receive messages at a number."""

    user_id: UUID
    phone_id: UUID


class PhoneRemoved(DomainEvent):
    """A user removed a verified number."""

    user_id: UUID
    phone_id: UUID
