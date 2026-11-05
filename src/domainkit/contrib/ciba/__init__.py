"""Approve sign-ins from the user's mobile app: OIDC CIBA, push and QR.

Install it after auth, messaging, notifications, and devices, and mount
its routes (ADR 0128)::

    INSTALLED_DOMAINS = [
        ...,
        "domainkit.contrib.auth",
        "domainkit.contrib.messaging",
        "domainkit.contrib.notifications",
        "domainkit.contrib.devices",
        "domainkit.contrib.ciba",
    ]
    CIBA = {"APPROVE_LOGINS": True}

A sign-in request reaches a phone the user bound (``devices``) by push,
when the initiator names the user, or by a QR code its page shows. The
phone sees who asks, from where, and approves or denies with a request its
key signs; the initiator polls until then. The project's own web pages
start requests at ``/app-sign-in`` and get a login or a browser session;
OIDC clients use the CIBA endpoints and get a scoped token and an ID
token. With ``APPROVE_LOGINS``, every login of a user with a bound device
needs it: the mobile app is the user's identity. See ``docs/auth/ciba.md``.
"""

from .events import SignInApproved, SignInDenied, SignInReported
from .exceptions import (
    AccessDenied,
    AuthorizationPending,
    DifferentNetwork,
    PhoneApprovalRequired,
    PollRefused,
    QrCodeExpired,
    SignInExpired,
    SignInNotFound,
    SignInUnknown,
    SlowDown,
    WrongMatchCode,
)
from .services import (
    Requester,
    Started,
    approve,
    claim,
    deny,
    poll,
    start_push,
    start_qr,
)
from .settings import CibaSettings
from .stores import CibaClient, CibaUnitOfWork, SignInRequest

__all__ = [
    "AccessDenied",
    "AuthorizationPending",
    "CibaClient",
    "CibaSettings",
    "CibaUnitOfWork",
    "DifferentNetwork",
    "PhoneApprovalRequired",
    "PollRefused",
    "QrCodeExpired",
    "Requester",
    "SignInApproved",
    "SignInDenied",
    "SignInExpired",
    "SignInNotFound",
    "SignInReported",
    "SignInRequest",
    "SignInUnknown",
    "SlowDown",
    "Started",
    "WrongMatchCode",
    "approve",
    "claim",
    "deny",
    "poll",
    "start_push",
    "start_qr",
]
