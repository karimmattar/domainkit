"""What erasing personal data tells the rest of the service, and the audit trail."""

from domainkit.ddd import DomainEvent


class PersonalDataErased(DomainEvent):
    """An aggregate's personal fields were erased, at a request or by retention.

    It names the fields, never their values, so the audit trail can say
    what was erased without keeping it.
    """

    model: str
    """The aggregate's content type, ``<domain>.<model>``."""
    aggregate_id: str
    fields: tuple[str, ...]
    reason: str
    """``request`` (``privacy erase``) or ``retention`` (``privacy purge``)."""
