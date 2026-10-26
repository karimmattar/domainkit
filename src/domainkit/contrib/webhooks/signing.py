"""Signatures in the Standard Webhooks scheme (https://www.standardwebhooks.com).

Each delivery carries three headers:

- ``webhook-id``: the delivery's id, the same on every retry and replay, so
  receivers can drop duplicates;
- ``webhook-timestamp``: seconds since the epoch when this attempt was signed;
- ``webhook-signature``: ``v1,<base64 HMAC-SHA256>`` over
  ``<id>.<timestamp>.<body>``, keyed with the secret's bytes. Several
  signatures, separated by spaces, appear while a secret is being rotated.

Secrets look like ``whsec_<base64 of 32 random bytes>``.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Iterable, Mapping
from typing import Final

from domainkit import clock
from domainkit.exceptions import DomainkitError

__all__ = [
    "InvalidSignature",
    "sign",
    "verify",
]

SECRET_PREFIX: Final = "whsec_"  # noqa: S105 - a prefix, not a secret.
SECRET_BYTES: Final = 32
ID_HEADER: Final = "webhook-id"
TIMESTAMP_HEADER: Final = "webhook-timestamp"
SIGNATURE_HEADER: Final = "webhook-signature"
VERSION: Final = "v1"
DEFAULT_TOLERANCE: Final = 300
"""Seconds a receiver accepts between the timestamp and its own clock."""
MAX_SIGNATURES: Final = 10
"""Most signatures a header may carry for ``verify`` to look at."""


class InvalidSignature(ValueError, DomainkitError):
    """A delivery that is not signed with the secret, or is stale."""


def new_secret() -> str:
    """Return a new random secret, ``whsec_`` and 32 bytes in base64."""
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode()


def secret_key(secret: str) -> bytes:
    """The key bytes of ``secret`` (``whsec_`` and base64).

    Raises:
        ValueError: ``secret`` is not one; the message never quotes it.
    """
    encoded = secret.removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("A webhook secret is 'whsec_' and base64.") from None
    if not key:
        raise ValueError("A webhook secret is 'whsec_' and base64.")
    return key


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """Return ``v1,<signature>`` of ``body`` sent as ``message_id`` at ``timestamp``."""
    signed = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret_key(secret), signed, hashlib.sha256).digest()
    return f"{VERSION},{base64.b64encode(digest).decode()}"


def signed_headers(
    secrets_: Iterable[str], message_id: str, timestamp: int, body: bytes
) -> dict[str, str]:
    """The three headers of a delivery, signed with each secret in turn."""
    return {
        ID_HEADER: message_id,
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: " ".join(
            sign(secret, message_id, timestamp, body) for secret in secrets_
        ),
    }


def verify(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> str:
    """Check a delivery as a receiver would; return its id.

    ``headers`` names are matched in lower case. Signatures are compared in
    constant time. The time is ``now`` (seconds since the epoch), or else
    ``domainkit.clock``'s, so a frozen clock moves it too.

    Raises:
        InvalidSignature: a header is missing or malformed, the timestamp is
            more than ``tolerance`` seconds away, or no signature matches.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    message_id = lowered.get(ID_HEADER, "")
    stamp = lowered.get(TIMESTAMP_HEADER, "")
    signatures = lowered.get(SIGNATURE_HEADER, "").split()
    well_formed = stamp.isascii() and stamp.isdigit() and len(stamp) <= 12
    if not (message_id and well_formed and signatures):
        raise InvalidSignature("The webhook headers are missing or malformed.")
    timestamp = int(stamp)
    if abs((clock.now().timestamp() if now is None else now) - timestamp) > tolerance:
        raise InvalidSignature("The webhook timestamp is too old or too new.")
    expected = sign(secret, message_id, timestamp, body).encode()
    if not any(
        hmac.compare_digest(expected, signature.encode())
        for signature in signatures[:MAX_SIGNATURES]
    ):
        raise InvalidSignature("No webhook signature matches.")
    return message_id
