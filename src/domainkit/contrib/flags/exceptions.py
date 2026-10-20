"""Errors of ``domainkit.contrib.flags``."""

from domainkit.ddd import BusinessRuleViolation, NotFound


class FlagNotFound(NotFound):
    """No flag has that name."""


class UnknownSetting(NotFound):
    """No dynamic setting is declared with that key."""


class InvalidSettingValue(BusinessRuleViolation):
    """The value does not fit the dynamic setting's type."""
