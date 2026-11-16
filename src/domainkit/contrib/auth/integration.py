"""What apps built on auth use: log a user in, step them up, hear of them.

``domainkit.contrib.identity``, ``domainkit.contrib.devices`` and
``domainkit.contrib.ciba`` log users in through providers, passkeys and
phones, run and add the login gates, and step them up with keys; they reach
auth through these names, which keep their meaning across releases as
the rest of the public API does.

An app that confirms action authorizations its own way (the phone, a
passkey: ADR 0144) registers an ``ActionConfirmation`` in ``ready()``
(``on_action_confirmation``), reads what to show (``action_authorization``),
links what confirms it (``link_action_authorization``, and finds it again
by that link: ``linked_action_authorization``), confirms it with what it
proved (``confirm_action_with``), and hands over what it confirmed in
another request (``confirmed_action_authorization``).
"""

from ._http import PASSWORD_MAX_LENGTH
from .access import impersonated, proof_of
from .actions import (
    ActionAuthorization,
    ActionConfirmation,
    AuthorizationChanged,
    AuthorizationNeedsOtherFactors,
    action_authorization,
    action_for,
    confirm_action_with,
    confirmed_action_authorization,
    link_action_authorization,
    linked_action_authorization,
    login_digest,
    on_action_confirmation,
    register_action,
)
from .api import (
    MfaChallengeOut,
    TokenOut,
    attach_login_device,
    check_first_use,
    end_device_logins,
    issue_login,
)
from .codes import target_of
from .cookies import CsrfOut, check_origin, start_browser_session
from .events import (
    EmailChanged,
    MobileNumberChanged,
    SteppedUp,
    UserDeleted,
    UserLoggedOut,
    UserSignedUp,
)
from .gates import (
    MANAGES_LOGINS,
    check_manage_logins,
    gates_passed,
    on_login_gate,
    on_manage_logins,
    on_password_login,
    pass_login_gates,
)
from .management import user_for_staff
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
from .throttling import (
    RefusalHook,
    account_key,
    address_key,
    attempt,
    audit_refusal,
    on_refusal,
)
from .tokens import LoginMethod, note_login_device, utc_now

__all__ = [
    "MANAGES_LOGINS",
    "PASSWORD_MAX_LENGTH",
    "REAUTH_AGE",
    "ActionAuthorization",
    "ActionConfirmation",
    "AuthorizationChanged",
    "AuthorizationNeedsOtherFactors",
    "CsrfOut",
    "EmailChanged",
    "LoginMethod",
    "MfaChallengeOut",
    "MobileNumberChanged",
    "ReauthenticationRequired",
    "RefusalHook",
    "StepUpOut",
    "SteppedUp",
    "TokenOut",
    "UserDeleted",
    "UserLoggedOut",
    "UserSignedUp",
    "account_key",
    "action_authorization",
    "action_for",
    "address_key",
    "attach_login_device",
    "attempt",
    "audit_refusal",
    "auth_settings",
    "challenge",
    "check_first_use",
    "check_manage_logins",
    "check_origin",
    "check_password_again",
    "confirm_action_with",
    "confirm_transaction_with",
    "confirmed_action_authorization",
    "end_device_logins",
    "first_use_after_code",
    "gates_passed",
    "impersonated",
    "issue_login",
    "link_action_authorization",
    "linked_action_authorization",
    "login_digest",
    "mail_user",
    "needs_second_factor",
    "note_login_device",
    "on_action_confirmation",
    "on_login_gate",
    "on_manage_logins",
    "on_password_login",
    "on_refusal",
    "pass_login_gates",
    "proof_of",
    "reauthenticate",
    "record_step_up",
    "register_action",
    "sca_transaction",
    "second_factor_holds",
    "send_code",
    "start_browser_session",
    "step_up_answer",
    "stepping_credentials",
    "target_of",
    "user_for_staff",
    "utc_now",
]
