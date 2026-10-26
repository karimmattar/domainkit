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
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
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

    class Meta:
        """Its content type's name."""

        verbose_name = "token"

    def expired(self, now: datetime | None = None) -> bool:
        """Whether the token's lifetime is over."""
        return self.expires_at is not None and (now or utc_now()) >= self.expires_at


def last_used_every() -> timedelta:
    """How often a token's ``last_used_at`` is written, at most.

    ``LAST_USED_EVERY``, or a tenth of ``AUTH.IDLE_TIMEOUT`` if shorter.
    """
    from .settings import auth_settings  # Imports nothing of this module.

    idle = auth_settings().IDLE_TIMEOUT
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


def idle_since(last: datetime, now: datetime) -> bool:
    """Whether a login last used at ``last`` has idled past ``AUTH.IDLE_TIMEOUT``.

    Its last use may be written up to ``last_used_every()`` late, so the
    login ends that much after the timeout at most, never before.
    """
    from .settings import auth_settings

    idle = auth_settings().IDLE_TIMEOUT
    return idle is not None and now - last >= idle + last_used_every()


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
