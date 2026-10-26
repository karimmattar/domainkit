"""Device proofs: a client that logged in keeps a count of its own (SEC-r1-2).

``AUTH.LOGIN_RATE`` counts wrong passwords per account, from any address,
and per address, for any account: that bounds guessing one account from
many addresses, but anyone could fill an account's count and keep its user
out, or fill an address's and keep an office out (ADR 0068's amendment).

A login that works now hands its client a device proof: an HttpOnly cookie
(``__Host-dk_device`` where the prefix holds, ``dk_device`` elsewhere),
signed with ``SECRET_KEY`` for that account, that works for
``AUTH.DEVICE_TTL``. A later login to the same account that sends it is
counted against that device's own count instead (``LOGIN_RATE`` per
device), and against neither the account's nor the address's: what
strangers send cannot fill it, and only a client that once knew the
password holds one. A proof for another account, a forged or expired one,
counts as none. Each login that works hands out a new proof, for the
same device. ADR 0087 compares this with a stored lockout.
"""

import secrets
from typing import Final

from fastapi import Request, Response

from domainkit.conf import settings
from domainkit.signing import BadSignature, TimestampSigner

from .settings import auth_settings
from .throttling import account_digest

__all__ = [
    "DEVICE_COOKIE",
    "device_key",
    "hand_out",
    "proven_device",
]

DEVICE_COOKIE: Final = "dk_device"
"""The proof's cookie, ``__Host-`` prefixed where the prefix holds."""
HOST_PREFIX: Final = "__Host-"
DEVICE_BYTES: Final = 16
"""Random bytes naming a device: 128 bits."""
MAX_PROOF_LENGTH: Final = 200
"""Longer cookies are not read."""


def cookie_name() -> str:
    """``__Host-dk_device`` for Secure cookies, else ``dk_device``."""
    return HOST_PREFIX + DEVICE_COOKIE if _secure() else DEVICE_COOKIE


def proven_device(request: Request, username: str) -> str | None:
    """The device the request proves for ``username``'s account, if any.

    None without ``AUTH.DEVICE_TTL``, without the cookie, or with one that
    does not hold for this account (another account's, forged, or expired).
    """
    ttl = auth_settings().DEVICE_TTL
    proof = request.cookies.get(cookie_name())
    if ttl is None or proof is None or len(proof) > MAX_PROOF_LENGTH:
        return None
    try:
        return _signer(username).unsign(proof, max_age=ttl)
    except BadSignature:
        return None


def device_key(device: str) -> str:
    """The count of wrong passwords sent with ``device``'s proof."""
    return f"login:device:{device}"


def hand_out(response: Response, username: str, device: str | None) -> None:
    """Set a new proof for ``username``'s account: for ``device``, or a new one.

    Nothing without ``AUTH.DEVICE_TTL``.
    """
    ttl = auth_settings().DEVICE_TTL
    if ttl is None:
        return
    device = device or secrets.token_urlsafe(DEVICE_BYTES)
    response.set_cookie(
        cookie_name(),
        _signer(username).sign(device),
        max_age=int(ttl.total_seconds()),
        path="/",
        secure=_secure(),
        httponly=True,
        samesite="lax",
    )


def _signer(username: str) -> TimestampSigner:
    """Signs proofs for one account alone: its name's digest is in the salt."""
    return TimestampSigner(salt=f"domainkit.auth.device:{account_digest(username)}")


def _secure() -> bool:
    """``AUTH.COOKIE.SECURE``, or HTTPS-only unless ``DEBUG``, as the session's."""
    secure = auth_settings().COOKIE.SECURE
    return not settings.DEBUG if secure is None else secure
