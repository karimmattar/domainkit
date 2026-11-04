"""Errors of ``domainkit.contrib.auth``; each maps to an HTTP status."""

from collections.abc import Sequence

from domainkit.ddd import BusinessRuleViolation, Conflict, DuplicateValue, NotFound


class InvalidUsername(BusinessRuleViolation):
    """The username is empty, too long, or uses characters it may not."""

    code = "invalid_username"


class InvalidEmail(BusinessRuleViolation):
    """The email address is not one plain address mail can go to."""

    code = "invalid_email"


class UsernameTaken(DuplicateValue):
    """Another user has that username."""

    code = "username_taken"


class InvalidMobileNumber(BusinessRuleViolation):
    """The mobile number is not one in international form (E.164)."""

    code = "invalid_mobile_number"


class MobileNumberTaken(DuplicateValue):
    """Another user has that mobile number."""

    code = "mobile_number_taken"


class EmailTaken(DuplicateValue):
    """Another user has that email address, and it identifies users."""

    code = "email_taken"


class IdentifierRequired(BusinessRuleViolation):
    """A user needs at least one identifier: a username, an email, or a number."""

    code = "identifier_required"


class InvalidGroupName(BusinessRuleViolation):
    """The group name is empty or too long."""

    code = "invalid_group_name"


class GroupNameTaken(DuplicateValue):
    """Another group has that name."""

    code = "group_name_taken"


class UnknownPermission(NotFound):
    """No permission has that key (``<domain>.<codename>``)."""

    code = "unknown_permission"


class UserNotFound(NotFound):
    """No user has that id."""

    code = "user_not_found"


class GroupNotFound(NotFound):
    """No group has that id."""

    code = "group_not_found"


class UnknownGroup(NotFound):
    """No group has that name."""

    code = "unknown_group"


class UnknownUserField(BusinessRuleViolation):
    """The user model has no such field, or it changes another way."""

    code = "unknown_user_field"


class InvalidUserField(BusinessRuleViolation):
    """A value does not fit the type of the user field it is for."""

    code = "invalid_user_field"


class InvalidPassword(BusinessRuleViolation):
    """The password breaks the configured rules (``PASSWORD_VALIDATORS``)."""

    code = "invalid_password"

    def __init__(self, errors: Sequence[str]) -> None:
        """Keep every rule the password breaks; the message lists them."""
        super().__init__(" ".join(errors))
        self.errors = list(errors)


class LastSuperuser(Conflict):
    """The step would leave no active superuser to run the system."""

    code = "last_superuser"
