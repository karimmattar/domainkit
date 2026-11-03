"""What apps built on auth use: log a user in, step them up, hear of them.

``domainkit.contrib.identity`` and ``domainkit.contrib.devices`` log users
in through providers and passkeys, and step them up with keys; they reach
auth through these names, which keep their meaning across releases as
the rest of the public API does.
"""

from starlette.requests import HTTPConnection

from .access import _impersonated, proof_of
from .api import PASSWORD_MAX_LENGTH, MfaChallengeOut, TokenOut, issue_login
from .events import SteppedUp, UserDeleted, UserSignedUp
from .mfa import challenge
from .stepup import (
    StepUpOut,
    check_password_again,
    record_step_up,
    step_up_answer,
    stepping_credentials,
)
from .tokens import utc_now

__all__ = [
    "PASSWORD_MAX_LENGTH",
    "MfaChallengeOut",
    "StepUpOut",
    "SteppedUp",
    "TokenOut",
    "UserDeleted",
    "UserSignedUp",
    "challenge",
    "check_password_again",
    "impersonated",
    "issue_login",
    "proof_of",
    "record_step_up",
    "step_up_answer",
    "stepping_credentials",
    "utc_now",
]


def impersonated(connection: HTTPConnection) -> bool:
    """Whether staff act as the user on ``connection`` (an impersonation)."""
    return _impersonated(connection)
