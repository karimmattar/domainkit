"""Phone verification: a code by text message, and the numbers users proved.

Install it after auth, messaging, and notifications, and mount its routes::

    INSTALLED_DOMAINS = [
        ...,
        "domainkit.contrib.auth",
        "domainkit.contrib.messaging",
        "domainkit.contrib.notifications",
        "domainkit.contrib.phones",
    ]
    PHONES = {"CODE_TTL": timedelta(minutes=10), "MAX_ATTEMPTS": 5}

A user asks for a code (``POST /auth/phones/codes``); the text message
leaves through the outbox with the commit. The code is kept only as an
HMAC keyed from ``SECRET_KEY``, works for ``CODE_TTL``, and is spent after
``MAX_ATTEMPTS`` wrong tries; sends to a number, and by a user, are
limited. ``POST /auth/phones`` with the code keeps the number as verified
and records ``PhoneVerified``. See ``docs/auth/phones.md``.
"""

from .events import PhoneRemoved, PhoneVerified
from .exceptions import (
    CodeExpired,
    CodeTooSoon,
    InvalidPhone,
    PhoneAlreadyVerified,
    PhoneNotFound,
    TooManyCodes,
    TooManyPhones,
    WrongCode,
)
from .services import (
    PendingCode,
    confirm_code,
    normalize_phone,
    remove_phone,
    send_code,
)
from .settings import PhonesSettings
from .stores import PhonesUnitOfWork, VerifiedPhone

__all__ = [
    "CodeExpired",
    "CodeTooSoon",
    "InvalidPhone",
    "PendingCode",
    "PhoneAlreadyVerified",
    "PhoneNotFound",
    "PhoneRemoved",
    "PhoneVerified",
    "PhonesSettings",
    "PhonesUnitOfWork",
    "TooManyCodes",
    "TooManyPhones",
    "VerifiedPhone",
    "WrongCode",
    "confirm_code",
    "normalize_phone",
    "remove_phone",
    "send_code",
]
