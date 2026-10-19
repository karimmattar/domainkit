"""Errors of ``domainkit.contrib.auth``; each maps to an HTTP status."""

from collections.abc import Sequence

from domainkit.ddd import BusinessRuleViolation, Conflict, DuplicateValue, NotFound


class InvalidUsername(BusinessRuleViolation):
    """The username is empty, too long, or uses characters it may not."""


class UsernameTaken(DuplicateValue):
    """Another user has that username."""


class InvalidGroupName(BusinessRuleViolation):
    """The group name is empty or too long."""


class GroupNameTaken(DuplicateValue):
    """Another group has that name."""


class UnknownPermission(NotFound):
    """No permission has that key (``<domain>.<codename>``)."""


class UserNotFound(NotFound):
    """No user has that id."""


class GroupNotFound(NotFound):
    """No group has that id."""


class UnknownGroup(NotFound):
    """No group has that name."""


class UnknownUserField(BusinessRuleViolation):
    """The user model has no such field, or it changes another way."""


class InvalidUserField(BusinessRuleViolation):
    """A value does not fit the type of the user field it is for."""


class InvalidPassword(BusinessRuleViolation):
    """The password breaks the configured rules (``PASSWORD_VALIDATORS``)."""

    def __init__(self, errors: Sequence[str]) -> None:
        """Keep every rule the password breaks; the message lists them."""
        super().__init__(" ".join(errors))
        self.errors = list(errors)


class LastSuperuser(Conflict):
    """The step would leave no active superuser to run the system."""
