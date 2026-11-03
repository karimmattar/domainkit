"""What the worker learns of the tokens it sends to."""

import hashlib

from domainkit.ddd import DomainEvent

__all__ = ["PushTokenExpired", "token_digest"]


class PushTokenExpired(DomainEvent):
    """A push service no longer knows a token: forget it (by its digest)."""

    token_digest: str
    """The token's SHA-256, in hex: the token itself is not passed around."""


def token_digest(token: str) -> str:
    """A push token's SHA-256, in hex: what ``PushTokenExpired`` names."""
    return hashlib.sha256(token.encode()).hexdigest()
