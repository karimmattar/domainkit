"""Errors of ``domainkit.contrib.auth``; each maps to an HTTP status."""

from collections.abc import Sequence

from domainkit.ddd import BusinessRuleViolation, Conflict, NotFound


class InvalidUsername(BusinessRuleViolation):
    """The username is empty, too long, or uses characters it may not."""


class UsernameTaken(Conflict):
    """Another user has that username."""


class InvalidGroupName(BusinessRuleViolation):
    """The group name is empty or too long."""


class GroupNameTaken(Conflict):
    """Another group has that name."""


class UnknownPermission(NotFound):
    """No permission has that key (``<domain>.<codename>``)."""


class InvalidPassword(BusinessRuleViolation):
    """The password breaks the configured rules (``PASSWORD_VALIDATORS``)."""

    def __init__(self, errors: Sequence[str]) -> None:
        """Keep every rule the password breaks; the message lists them."""
        super().__init__(" ".join(errors))
        self.errors = list(errors)
