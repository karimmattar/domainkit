"""What verifying a phone number may be refused."""

import math
from datetime import timedelta

from fastapi import status

from domainkit.ddd import BusinessRuleViolation, Conflict, NotFound
from domainkit.http.errors import APIError

__all__ = [
    "CodeExpired",
    "CodeTooSoon",
    "InvalidPhone",
    "PhoneAlreadyVerified",
    "PhoneNotFound",
    "TooManyCodes",
    "TooManyPhones",
    "WrongCode",
]


class InvalidPhone(BusinessRuleViolation):
    """Not a phone number in international form, such as ``+33612345678`` (422)."""

    code = "invalid_phone"


class PhoneAlreadyVerified(Conflict):
    """The user verified this number already (409)."""

    code = "phone_already_verified"


class TooManyPhones(Conflict):
    """The user holds ``PHONES.MAX_PHONES`` numbers already (409)."""

    code = "too_many_phones"


class PhoneNotFound(NotFound):
    """The user has no verified number with that id (404)."""

    code = "phone_not_found"


class CodeExpired(BusinessRuleViolation):
    """No code works for this number: none sent, too old, or tried too often (422)."""

    code = "code_expired"


class WrongCode(APIError):
    """The code is not the one sent (422); a few more tries are left."""

    def __init__(self) -> None:
        """Use status 422 and the code ``wrong_code``."""
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "That is not the code we sent.",
            code="wrong_code",
        )


class CodeTooSoon(APIError):
    """A code went to this number moments ago (429 ``rate_limited``)."""

    def __init__(self, wait: timedelta) -> None:
        """Answer ``rate_limited``, as every 429 does; retry after ``wait``."""
        super().__init__(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "A code was sent moments ago; wait before asking for another.",
            code="rate_limited",
            headers={"Retry-After": _seconds(wait)},
        )


class TooManyCodes(APIError):
    """``PHONES.MAX_SENDS`` codes went to this number, or for this user (429)."""

    def __init__(self, wait: timedelta) -> None:
        """Answer ``rate_limited``, as every 429 does; retry after ``wait``."""
        super().__init__(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many codes were sent lately; try again later.",
            code="rate_limited",
            headers={"Retry-After": _seconds(wait)},
        )


def _seconds(wait: timedelta) -> str:
    """Whole seconds to wait, rounded up, at least one."""
    return str(max(1, math.ceil(wait.total_seconds())))
