"""Errors of ``domainkit.contrib.flags``."""

from domainkit.ddd import BusinessRuleViolation, NotFound


class FlagNotFound(NotFound):
    """No flag has that name."""

    code = "flag_not_found"


class UnknownSetting(NotFound):
    """No dynamic setting is declared with that key."""

    code = "unknown_setting"


class InvalidSettingValue(BusinessRuleViolation):
    """The value does not fit the dynamic setting's type."""

    code = "invalid_setting_value"
