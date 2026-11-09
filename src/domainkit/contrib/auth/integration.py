"""What apps built on auth use: log a user in, step them up, hear of them.

``domainkit.contrib.identity``, ``domainkit.contrib.devices`` and
``domainkit.contrib.ciba`` log users in through providers, passkeys and
phones, run and add the login gates, and step them up with keys; they reach
auth through these names, which keep their meaning across releases as
the rest of the public API does.
"""

from starlette.requests import HTTPConnection

from .access import _impersonated, proof_of
from .api import PASSWORD_MAX_LENGTH, MfaChallengeOut, TokenOut, issue_login
from .codes import target_of
from .cookies import CsrfOut, check_origin, start_browser_session
from .events import SteppedUp, UserDeleted, UserLoggedOut, UserSignedUp
from .gates import gates_passed, on_login_gate, on_manage_logins, pass_login_gates
from .mfa import (
    challenge,
    first_use_after_code,
    needs_second_factor,
    second_factor_holds,
)
from .recovery import send_code
from .sca import confirm_transaction_with, sca_transaction
from .settings import auth_settings
from .stepup import (
    REAUTH_AGE,
    ReauthenticationRequired,
    StepUpOut,
    check_password_again,
    reauthenticate,
    record_step_up,
    step_up_answer,
    stepping_credentials,
)
from .throttling import account_key, address_key, attempt, audit_refusal
from .tokens import LoginMethod, utc_now

__all__ = [
    "PASSWORD_MAX_LENGTH",
    "REAUTH_AGE",
    "CsrfOut",
    "LoginMethod",
    "MfaChallengeOut",
    "ReauthenticationRequired",
    "StepUpOut",
    "SteppedUp",
    "TokenOut",
    "UserDeleted",
    "UserLoggedOut",
    "UserSignedUp",
    "account_key",
    "address_key",
    "attempt",
    "audit_refusal",
    "auth_settings",
    "challenge",
    "check_origin",
    "check_password_again",
    "confirm_transaction_with",
    "first_use_after_code",
    "gates_passed",
    "impersonated",
    "issue_login",
    "needs_second_factor",
    "on_login_gate",
    "on_manage_logins",
    "pass_login_gates",
    "proof_of",
    "reauthenticate",
    "record_step_up",
    "sca_transaction",
    "second_factor_holds",
    "send_code",
    "start_browser_session",
    "step_up_answer",
    "stepping_credentials",
    "target_of",
    "utc_now",
]


def impersonated(connection: HTTPConnection) -> bool:
    """Whether staff act as the user on ``connection`` (an impersonation)."""
    return _impersonated(connection)
