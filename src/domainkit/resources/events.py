"""Events the generic create, change, and delete routes record (``crud=True``).

Those routes change an aggregate without a method of its own, so they
record what happened themselves; with ``domainkit.contrib.audit``
installed, the audit trail shows who changed which fields of what.
"""

from domainkit.ddd import DomainEvent


class RecordCreated(DomainEvent):
    """An aggregate was created through a generic route."""

    model: str
    """Its content type (``catalog.category``), or its class name."""
    record_id: str
    fields: tuple[str, ...]
    """The fields the request set."""


class RecordChanged(DomainEvent):
    """Fields of an aggregate were changed through a generic route."""

    model: str
    record_id: str
    fields: tuple[str, ...]
    """The fields whose values changed."""


class RecordDeleted(DomainEvent):
    """An aggregate was deleted through a generic route."""

    model: str
    record_id: str
