"""What apps built on auth use: log a user in, step them up, hear of them.

``domainkit.contrib.identity``, ``domainkit.contrib.devices`` and
``domainkit.contrib.ciba`` log users in through providers, passkeys and
phones, run and add the login gates, and step them up with keys; they reach
auth through these names, which keep their meaning across releases as
the rest of the public API does.
"""

from ._http import PASSWORD_MAX_LENGTH
from .access import impersonated, proof_of
from .api import (
    MfaChallengeOut,
    TokenOut,
    attach_login_device,
    end_device_logins,
    issue_login,
)
from .codes import target_of
from .cookies import CsrfOut, check_origin, start_browser_session
from .events import SteppedUp, UserDeleted, UserLoggedOut, UserSignedUp
from .gates import (
    MANAGES_LOGINS,
    check_manage_logins,
    gates_passed,
    on_login_gate,
    on_manage_logins,
    pass_login_gates,
)
from .mfa import (
    challenge,
    first_use_after_code,
    needs_second_factor,
    second_factor_holds,
)
from .recovery import mail_user, send_code
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
from .tokens import LoginMethod, note_login_device, utc_now

__all__ = [
    "MANAGES_LOGINS",
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
    "attach_login_device",
    "attempt",
    "audit_refusal",
    "auth_settings",
    "challenge",
    "check_manage_logins",
    "check_origin",
    "check_password_again",
    "confirm_transaction_with",
    "end_device_logins",
    "first_use_after_code",
    "gates_passed",
    "impersonated",
    "issue_login",
    "mail_user",
    "needs_second_factor",
    "note_login_device",
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
