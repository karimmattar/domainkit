"""Opaque tokens: random secrets that name a user until they expire.

A token is ``AUTH.TOKEN_PREFIX`` followed by 256 random bits. Only its
SHA-256 digest is stored, so the table never holds a working token, and a
token is found by its digest without comparing secrets.

Access tokens authenticate requests (``TokenAuthentication``). Refresh
tokens only buy new access tokens (``JWTAuthentication``): each one is
used once, and the tokens one login leads to share a ``family``, revoked
together if a used token comes back.
"""

import hashlib
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from domainkit import clock
from domainkit.ddd import Entity

TOKEN_BYTES: Final = 32
_BODY: Final = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(TOKEN_BYTES)
"""Random bytes in a token: 256 bits, beyond guessing."""
LAST_USED_EVERY: Final = timedelta(minutes=5)
"""How often a token's ``last_used_at`` is written, at most."""
LOGIN_NAME: Final = "login"
"""The name of the tokens logging in issues."""
IDLE_PRECISION: Final = 10
"""Last uses are written this many times per ``AUTH.IDLE_TIMEOUT``."""
AGENT_MAX_LENGTH: Final = 200
"""A login keeps this much of its ``User-Agent`` at most."""
MIN_IDLE: Final = timedelta(minutes=5)
"""The shortest idle timeout: last uses are written a tenth as often."""
MAX_IDLE: Final = timedelta(days=30)
"""The longest idle timeout."""
DEVICE_KEY: Final = "hwk"
"""What a login a bound device made or signed proved (RFC 8176): its key."""
PWD: Final = frozenset({"pwd"})
"""What a password login proved (RFC 8176's names)."""
PWD_OTP: Final = frozenset({"pwd", "otp"})
"""What a password login finished with a second factor's code proved."""


class TokenKind(StrEnum):
    """What a token may be used for."""

    ACCESS = "access"
    """Authenticates requests."""
    REFRESH = "refresh"
    """Exchanged, once, for a new access token and refresh token."""
    SESSION = "session"
    """A browser's session cookie (``CookieAuthentication``), never a header."""
    CERTIFICATE = "certificate"
    """A client certificate, by its SHA-256 (``ClientCertificateAuthentication``)."""
    DEVICE = "device"
    """A client that logged in, by the SHA-256 of its device proof's id
    (``auth.devices``): it never authenticates a request."""


class LoginMethod(StrEnum):
    """How a login signed in (ADR 0130): shown with it, to tell logins apart."""

    PASSWORD = "password"  # noqa: S105 - a method's name, not a secret.
    """A password, and the second factor where the user has one."""
    PASSKEY = "passkey"
    """A passkey (WebAuthn)."""
    DEVICE = "device"
    """A bound device's signature: the phone's own login, or its enrollment."""
    PUSH = "push"
    """Approved on the user's phone, after a push."""
    QR = "qr"
    """Approved on the user's phone, which scanned the page's QR code."""
    PROVIDER = "provider"
    """An identity provider (OpenID Connect, OAuth 2, SAML, LDAP)."""
    IMPERSONATION = "impersonation"
    """Staff acting as the user."""


@dataclass(frozen=True, slots=True)
class LoginOrigin:
    """How a login was made: its method, what it proved, when, and its device.

    A login gate sees the login it judges (``gates.login_origin``); a
    request sees the login its credentials belong to (``Credentials.origin``).
    """

    method: str | None = None
    """How it signed in (``LoginMethod``); None where its issuer did not say."""
    device_id: UUID | None = None
    """The bound device that made it, or signed its request."""
    amr: frozenset[str] = frozenset()
    """What it proved, in RFC 8176's names: ``pwd`` for the password,
    ``otp`` for the authenticator's code (or a recovery code, which stands
    in for it), ``hwk`` for a bound device's key, ``mfa`` with ``hwk`` for
    a passkey verified with its user. Empty for an identity provider's
    login, and for logins made before 2.4."""
    at: datetime | None = None
    """When it was made; None where it is not known."""


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """Shorter limits for one login than the settings give every login.

    Returned by an ``on_session_limits`` hook when the login is made, and
    kept with it: a limit longer than the setting's changes nothing.

    Raises:
        ValueError: ``idle_timeout`` is not from 5 minutes to 30 days, or
            ``lifetime`` is not positive.
    """

    idle_timeout: timedelta | None = None
    """End the login once unused for this long, as ``AUTH.IDLE_TIMEOUT``
    does; None: the setting's."""
    lifetime: timedelta | None = None
    """End the login this long after it was made, whatever its use: a
    shorter ``TOKEN_TTL``, ``COOKIE.TTL``, or ``JWT.REFRESH_TOKEN_TTL``.
    None: the setting's."""

    def __post_init__(self) -> None:
        """Refuse limits no login could keep."""
        idle = self.idle_timeout
        if idle is not None and not MIN_IDLE <= idle <= MAX_IDLE:
            raise ValueError(
                f"SessionLimits.idle_timeout must be from 5 minutes to 30 days, "
                f"not {idle}"
            )
        if self.lifetime is not None and self.lifetime <= timedelta(0):
            raise ValueError(
                f"SessionLimits.lifetime must be positive, not {self.lifetime}"
            )

    def within(self, other: "SessionLimits") -> "SessionLimits":
        """The shorter of each limit of these and ``other``."""
        return SessionLimits(
            shortest(self.idle_timeout, other.idle_timeout),
            shortest(self.lifetime, other.lifetime),
        )


@dataclass(frozen=True, slots=True)
class Issuing:
    """A login being issued: how it was made, and the limits it keeps."""

    origin: LoginOrigin
    limits: SessionLimits = field(default_factory=SessionLimits)


_issuing: ContextVar[Issuing | None] = ContextVar("login_issuing", default=None)


@contextmanager
def logging_in(
    origin: LoginOrigin, limits: SessionLimits | None = None
) -> Iterator[None]:
    """Record the logins issued inside as made as ``origin`` says, to ``limits``."""
    reset = _issuing.set(Issuing(origin, limits or SessionLimits()))
    try:
        yield
    finally:
        _issuing.reset(reset)


def issuing() -> Issuing | None:
    """The login being issued, if its issuer said how it was made (``logging_in``)."""
    return _issuing.get()


def shortest(*durations: timedelta | None) -> timedelta | None:
    """The shortest of ``durations`` that are set; None if none is."""
    return min((each for each in durations if each is not None), default=None)


def signed_by_device(origin: LoginOrigin, scope: Mapping[str, Any]) -> LoginOrigin:
    """``origin``, with the bound device that signed the request, if one did.

    The device is ``origin``'s, or the one ``note_login_device`` put in the
    request's ``scope``. A login a device made or signed proved its key
    (``hwk``).
    """
    device = origin.device_id or scope.get(LOGIN_DEVICE)
    if device is None:
        return origin
    return replace(origin, device_id=device, amr=origin.amr | {DEVICE_KEY})


LOGIN_DEVICE: Final = "domainkit.auth.login_device"
"""Where a request keeps the bound device that signed it, for its login."""


def note_login_device(connection: object, device_id: UUID) -> None:
    """Say that the user's bound device ``device_id`` signed this request.

    A login issued while serving it records the device, so a password
    change can keep it (ADR 0131). The devices app calls it once a
    signature holds; ``connection`` is the request.
    """
    connection.scope[LOGIN_DEVICE] = device_id  # type: ignore[attr-defined]


class AuthToken(Entity[str]):
    """A stored token: its digest (the ``id``), owner, kind, and lifetime."""

    user_id: UUID
    kind: str
    name: str = ""
    """What it is for (``login``, ``ci``), shown when listing tokens."""
    family: UUID | None = None
    """The login a refresh token descends from."""
    created_at: datetime
    expires_at: datetime | None = None
    """None: works until revoked."""
    used_at: datetime | None = None
    """When a refresh token was exchanged; it never works again."""
    scopes: frozenset[str] | None = None
    """The permission keys it is limited to; None: all of its user's."""
    last_used_at: datetime | None = None
    """When it last authenticated a request, to ``LAST_USED_EVERY``."""
    method: str | None = None
    """How its login signed in (``LoginMethod``); None for other tokens, and
    logins made before 2.2."""
    agent: str | None = None
    """The ``User-Agent`` its login was made from, cut to ``AGENT_MAX_LENGTH``."""
    ip: str | None = None
    """The address its login was made from (``TRUSTED_PROXIES`` resolves it)."""
    device_id: UUID | None = None
    """The bound device that made its login: the phone's own."""
    amr: frozenset[str] | None = None
    """What its login proved (``LoginOrigin.amr``); None for other tokens,
    and logins made before 2.4."""
    idle_timeout: int | None = None
    """Seconds its login may idle, where a hook made it shorter than
    ``AUTH.IDLE_TIMEOUT`` (``on_session_limits``); None: the setting's."""

    class Meta:
        """Its content type's name."""

        verbose_name = "token"

    def expired(self, now: datetime | None = None) -> bool:
        """Whether the token's lifetime is over."""
        return self.expires_at is not None and (now or utc_now()) >= self.expires_at

    def lifetime(self) -> timedelta | None:
        """How long it works from when it was made; None: until revoked."""
        return None if self.expires_at is None else self.expires_at - self.created_at


def idle_limit(seconds: int | None = None) -> timedelta | None:
    """How long a login may idle: ``AUTH.IDLE_TIMEOUT``, or its own if shorter.

    ``seconds``: the login's own (``AuthToken.idle_timeout``). None: it
    never idles out.
    """
    from .settings import auth_settings  # Imports nothing of this module.

    own = None if seconds is None else timedelta(seconds=seconds)
    return shortest(auth_settings().IDLE_TIMEOUT, own)


def last_used_every(seconds: int | None = None) -> timedelta:
    """How often a login's ``last_used_at`` is written, at most.

    ``LAST_USED_EVERY``, or a tenth of its idle timeout (``idle_limit``)
    if shorter.
    """
    idle = idle_limit(seconds)
    return (
        LAST_USED_EVERY if idle is None else min(LAST_USED_EVERY, idle / IDLE_PRECISION)
    )


def is_login(kind: str, name: str) -> bool:
    """Whether a token of ``kind`` named ``name`` is a login's (not a script's).

    Browser sessions, and the access and refresh tokens logging in issues.
    """
    return kind == TokenKind.SESSION or (
        kind in {TokenKind.ACCESS, TokenKind.REFRESH} and name == LOGIN_NAME
    )


def idle_since(last: datetime, now: datetime, seconds: int | None = None) -> bool:
    """Whether a login last used at ``last`` has idled past its idle timeout.

    ``AUTH.IDLE_TIMEOUT``, or the login's own ``seconds`` if shorter. Its
    last use may be written up to ``last_used_every()`` late, so the login
    ends that much after the timeout at most, never before.
    """
    idle = idle_limit(seconds)
    return idle is not None and now - last >= idle + last_used_every(seconds)


def utc_now() -> datetime:
    """The current time in UTC."""
    return clock.now()


def new_token(prefix: str) -> str:
    """A new random token: ``prefix`` and 43 URL-safe characters."""
    return prefix + secrets.token_urlsafe(TOKEN_BYTES)


def well_formed(token: str, prefix: str) -> bool:
    """Whether ``token`` has the form of ``new_token(prefix)``'s tokens."""
    body = token.removeprefix(prefix)
    return body != token and _BODY.fullmatch(body) is not None


def token_digest(token: str) -> str:
    """The SHA-256 of ``token`` in hex: what the table stores."""
    return hashlib.sha256(token.encode()).hexdigest()
