"""Business errors raised by domain and application code.

The domain never mentions HTTP: ``domainkit.http.errors`` maps each base class
below to a status code. Subclass the closest one, for example
``class ProductNotFound(NotFound)``. Each class gets a machine-readable
``code`` (``product_not_found``) unless it sets its own.
"""

from collections.abc import Sequence
from typing import ClassVar

from domainkit.exceptions import DomainkitError
from domainkit.utils.text import snake_case


class DomainError(DomainkitError):
    """A business rule refused the operation; the message is safe to show."""

    code: ClassVar[str] = "domain_error"

    def __init_subclass__(cls) -> None:
        """Derive ``code`` from the class name unless the class sets one."""
        super().__init_subclass__()
        if "code" not in cls.__dict__:
            cls.code = snake_case(cls.__name__)

    def __init__(self, message: str) -> None:
        """Store the user-facing ``message``."""
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    """The requested aggregate does not exist."""


class Conflict(DomainError):
    """The operation clashes with the current state, such as a duplicate."""


class ConcurrentUpdate(Conflict):
    """Someone else changed the aggregate first; reload it and try again.

    ``model`` names the aggregate's class (``module.QualName``), where the
    unit of work could tell it; it is for logs, never for the answer.
    """

    def __init__(self, message: str, *, model: str | None = None) -> None:
        """Store the message, and the model that lost the race if known."""
        super().__init__(message)
        self.model = model


class DuplicateValue(Conflict):
    """A value that must be unique is taken (a unique constraint refused it)."""

    def __init__(
        self,
        message: str,
        *,
        constraint: str | None = None,
        fields: Sequence[str] = (),
    ) -> None:
        """Store the message, and the constraint and fields that refused it."""
        super().__init__(message)
        self.constraint = constraint
        self.fields = tuple(fields)


class PreconditionFailed(DomainError):
    """The caller expected another version of the aggregate than the stored one.

    Raised for a write whose ``If-Match`` names a version that is no longer
    current: the caller works from a stale copy, so the change is refused
    rather than applied over someone else's. Read the aggregate again, then
    decide. (``ConcurrentUpdate`` is the same race, found at commit.)
    """


class BusinessRuleViolation(DomainError):
    """The request is well-formed but breaks an invariant of the model."""


class PermissionDenied(DomainError):
    """The actor may not perform the operation."""
