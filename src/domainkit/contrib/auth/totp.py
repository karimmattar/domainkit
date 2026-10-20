"""Time-based one-time passwords (RFC 6238), with the standard library's HMAC.

Authenticator apps share a random secret with the service, and both derive
a code from it and the time: HMAC-SHA1 of the 30-second step since the
epoch, truncated to six digits (RFC 4226). ``match`` accepts a code from
the current step or ``window`` steps either side, for clocks that drift,
and says which step it was, so a caller can refuse a code used before.
"""

import base64
import hashlib
import hmac
import secrets
import struct
from typing import Final
from urllib.parse import quote, urlencode

SECRET_BYTES: Final = 20
"""160 bits, the length RFC 4226 recommends for HMAC-SHA1."""
DIGITS: Final = 6
STEP: Final = 30
"""Seconds each code works."""


def new_secret() -> bytes:
    """A new random secret."""
    return secrets.token_bytes(SECRET_BYTES)


def hotp(key: bytes, counter: int, *, digits: int = DIGITS) -> str:
    """The HMAC-based one-time password for ``counter`` (RFC 4226)."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    (number,) = struct.unpack(">I", digest[offset : offset + 4])
    return str((number & 0x7FFFFFFF) % 10**digits).zfill(digits)


def time_step(at: float) -> int:
    """The step (30 seconds) that moment ``at`` falls in."""
    return int(at // STEP)


def totp(key: bytes, at: float, *, digits: int = DIGITS) -> str:
    """The code for moment ``at`` (seconds since the epoch)."""
    return hotp(key, time_step(at), digits=digits)


def match(
    key: bytes, code: str, at: float, *, window: int = 1, digits: int = DIGITS
) -> int | None:
    """The step whose code ``code`` is, within ``window`` steps of ``at``.

    The current step is tried first, then those around it. Codes are
    compared in constant time. None: no step in the window has it.
    """
    if len(code) != digits or not code.isascii() or not code.isdigit():
        return None
    now = time_step(at)
    steps = [now, *(now + delta for d in range(1, window + 1) for delta in (-d, d))]
    for step in steps:
        if hmac.compare_digest(hotp(key, step, digits=digits), code):
            return step
    return None


def encode_secret(secret: bytes) -> str:
    """The secret in base 32 without padding, as authenticator apps take it."""
    return base64.b32encode(secret).decode().rstrip("=")


def otpauth_uri(secret: bytes, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI that QR codes for authenticator apps carry."""
    label = quote(f"{issuer}:{account}", safe="")
    query = urlencode(
        {
            "secret": encode_secret(secret),
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP,
        },
        quote_via=quote,
    )
    return f"otpauth://totp/{label}?{query}"
