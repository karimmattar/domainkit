"""Business errors raised by domain and application code.

The domain never mentions HTTP: ``domainkit.http.errors`` maps each base class
below to a status code. Subclass the closest one, for example
``class ProductNotFound(NotFound)``. Each class gets a machine-readable
``code`` (``product_not_found``) unless it sets its own.

Clients switch on codes, so a code is public: a class renamed must keep
the code it had. Set ``code`` on the class to keep it whatever its name.
domainkit's own classes must (``FRAMEWORK``), and
``docs/reference/problems.md`` lists every one (DSGN-20).
"""

from collections.abc import Sequence
from typing import ClassVar, Final

from domainkit.exceptions import DomainkitError
from domainkit.utils.text import snake_case

FRAMEWORK: Final = "domainkit."
"""The modules whose errors must set their ``code`` (domainkit's own)."""


class DomainError(DomainkitError):
    """A business rule refused the operation; the message is safe to show."""

    code: ClassVar[str] = "domain_error"

    def __init_subclass__(cls) -> None:
        """Derive ``code`` from the class name unless the class sets one.

        Raises:
            TypeError: A class of domainkit's own sets none: its code is
                pinned, so a rename never changes it (DSGN-20).
        """
        super().__init_subclass__()
        if "code" in cls.__dict__:
            return
        if cls.__module__.startswith(FRAMEWORK):
            raise TypeError(
                f"{cls.__module__}.{cls.__qualname__} must set its code: "
                "domainkit's problem codes are pinned, never derived."
            )
        cls.code = snake_case(cls.__name__)

    def __init__(self, message: str) -> None:
        """Store the user-facing ``message``."""
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    """The requested aggregate does not exist."""

    code = "not_found"


class Conflict(DomainError):
    """The operation clashes with the current state, such as a duplicate."""

    code = "conflict"


class ConcurrentUpdate(Conflict):
    """Someone else changed the aggregate first; reload it and try again.

    ``model`` names the aggregate's class (``module.QualName``), where the
    unit of work could tell it; it is for logs, never for the answer.
    """

    code = "concurrent_update"

    def __init__(self, message: str, *, model: str | None = None) -> None:
        """Store the message, and the model that lost the race if known."""
        super().__init__(message)
        self.model = model


class DuplicateValue(Conflict):
    """A value that must be unique is taken (a unique constraint refused it)."""

    code = "duplicate_value"

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


class MultipleFound(Conflict):
    """A lookup that expects one aggregate met several (``get_or_create``)."""

    code = "multiple_found"


class PreconditionFailed(DomainError):
    """The caller expected another version of the aggregate than the stored one.

    Raised for a write whose ``If-Match`` names a version that is no longer
    current: the caller works from a stale copy, so the change is refused
    rather than applied over someone else's. Read the aggregate again, then
    decide. (``ConcurrentUpdate`` is the same race, found at commit.)
    """

    code = "precondition_failed"


class BusinessRuleViolation(DomainError):
    """The request is well-formed but breaks an invariant of the model."""

    code = "business_rule_violation"


class InvalidField(BusinessRuleViolation):
    """A field was given a value its markers rule out (``MaxLength``, ``Range``, ...).

    ``field`` names it; the API answers 422 with an error under it.
    """

    code = "invalid_field"

    def __init__(self, message: str, *, field: str) -> None:
        """Store the message and the field it is about."""
        super().__init__(message)
        self.field = field


class CheckFailed(BusinessRuleViolation):
    """An aggregate breaks one of its ``Meta.checks`` at commit.

    ``check`` names it; the API answers 422 ``check_violation``, as when
    the database's own check constraint refuses the row.
    """

    code = "check_violation"

    def __init__(self, message: str, *, check: str) -> None:
        """Store the message and the check it is about."""
        super().__init__(message)
        self.check = check


class PermissionDenied(DomainError):
    """The actor may not perform the operation."""

    code = "permission_denied"
