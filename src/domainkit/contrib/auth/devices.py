"""Device proofs: a client that logged in keeps a count of its own (SEC-r1-2).

``AUTH.LOGIN_RATE`` counts wrong passwords per account, from any address,
and per address, for any account: that bounds guessing one account from
many addresses, but anyone could fill an account's count and keep its user
out, or fill an address's and keep an office out (ADR 0068's amendment).

A login that works, second factor included, hands its client a device
proof: an HttpOnly cookie (``__Host-dk_device`` where the prefix holds,
``dk_device`` elsewhere) naming a random device, signed with ``SECRET_KEY``
for that account. The device is stored as one of the user's tokens (kind
``device``), for ``AUTH.DEVICE_TTL``: whatever ends every token of the user
(a password change or reset, logging out everywhere, an admin's change,
deletion) ends its devices too, and ``GET /auth/tokens`` lists them. A
user keeps at most ``MAX_DEVICES``; a new one past it ends the oldest.

A later login to the same account that sends a live proof is counted
against that device's own count (``LOGIN_RATE``), and against a ceiling of
the account's proven attempts (``PROVEN_CEILING`` times ``LOGIN_RATE``),
not the account's nor the address's: what strangers send cannot fill
those, and however many proofs one holds, an account takes at most
``LOGIN_RATE`` guesses without proofs and ``PROVEN_CEILING`` times as many
with them. A proof for another account, a forged one, or one whose device
ended counts as none. ADR 0087 has the measurements.
"""

import secrets
from typing import Final

from fastapi import Request, Response

from domainkit.conf import settings
from domainkit.signing import BadSignature, TimestampSigner
from domainkit.utils.rates import PERIODS, parse_rate

from .models import AbstractBaseUser
from .settings import auth_settings
from .throttling import account_digest, account_key
from .tokens import AuthToken, TokenKind, token_digest, utc_now
from .uow import AuthUnitOfWork

__all__ = [
    "DEVICE_COOKIE",
    "MAX_DEVICES",
    "PROVEN_CEILING",
    "device_key",
    "hand_out",
    "proven_device",
    "proven_limits",
]

DEVICE_COOKIE: Final = "dk_device"
"""The proof's cookie, ``__Host-`` prefixed where the prefix holds."""
HOST_PREFIX: Final = "__Host-"
DEVICE_BYTES: Final = 16
"""Random bytes naming a device: 128 bits."""
MAX_PROOF_LENGTH: Final = 200
"""Longer cookies are not read."""
MAX_DEVICES: Final = 20
"""Devices a user holds at once; a new one past it ends the oldest."""
PROVEN_CEILING: Final = 2
"""Proven attempts an account takes from all its devices, in ``LOGIN_RATE`` units.

Two: a copied proof cannot fill it alone and keep the user's own devices
out, and many proofs check no more than twice ``LOGIN_RATE`` (ADR 0087).
"""
DEVICE_NAME: Final = "device"
_UNITS: Final = {seconds: unit for unit, seconds in PERIODS.items()}


def cookie_name() -> str:
    """``__Host-dk_device`` for Secure cookies, else ``dk_device``."""
    return HOST_PREFIX + DEVICE_COOKIE if _secure() else DEVICE_COOKIE


async def proven_device(request: Request, username: str) -> str | None:
    """The device the request proves for ``username``'s account, if any.

    None without ``AUTH.DEVICE_TTL``, without the cookie, or with one that
    does not hold for this account (another account's, forged, or of a
    device that ended). The signature is checked first: only a proof it
    holds costs a lookup.
    """
    ttl = auth_settings().DEVICE_TTL
    proof = request.cookies.get(cookie_name())
    if ttl is None or proof is None or len(proof) > MAX_PROOF_LENGTH:
        return None
    try:
        device = _signer(username).unsign(proof, max_age=ttl)
    except BadSignature:
        return None
    async with AuthUnitOfWork() as uow:
        stored = await uow.tokens.find_digest(token_digest(device))
    if stored is None or stored.kind != TokenKind.DEVICE or stored.expired():
        return None
    return device


def device_key(device: str) -> str:
    """The count of wrong passwords sent with ``device``'s proof."""
    return f"login:device:{device}"


def proven_limits(
    device: str, username: str, rate: str | None
) -> list[tuple[str, str | None]]:
    """The counts a proven attempt takes: its device's, and the account's ceiling.

    The device's at ``rate`` (``LOGIN_RATE``); the account's proven attempts
    at ``PROVEN_CEILING`` times it.
    """
    if rate is None:
        return [(device_key(device), None)]
    limit, period = parse_rate(rate)
    ceiling = f"{limit * PROVEN_CEILING}/{_UNITS[period]}"
    return [(device_key(device), rate), (account_key("proven", username), ceiling)]


async def hand_out(
    request: Request, response: Response, user: AbstractBaseUser
) -> None:
    """Set a proof for ``user``'s account, once the whole login held.

    Call it after the second factor, if the user has one. The device the
    request proves is renewed for ``AUTH.DEVICE_TTL``; otherwise a new one
    is stored, and past ``MAX_DEVICES`` the user's oldest ends. Nothing
    without ``AUTH.DEVICE_TTL``.
    """
    ttl = auth_settings().DEVICE_TTL
    if ttl is None:
        return
    username = user.get_username()
    user_id = user.id
    device = await proven_device(request, username)
    async with AuthUnitOfWork() as uow:
        stored = (
            None
            if device is None
            else await uow.tokens.find_digest(token_digest(device))
        )
        if (
            device is not None
            and stored is not None
            and stored.kind == TokenKind.DEVICE
            and stored.user_id == user_id
        ):
            stored.expires_at = utc_now() + ttl
        else:
            device = secrets.token_urlsafe(DEVICE_BYTES)
            await uow.tokens.end_oldest(user_id, TokenKind.DEVICE, keep=MAX_DEVICES - 1)
            uow.session.add(
                AuthToken(
                    id=token_digest(device),
                    user_id=user_id,
                    kind=TokenKind.DEVICE,
                    name=DEVICE_NAME,
                    created_at=utc_now(),
                    expires_at=utc_now() + ttl,
                )
            )
        await uow.commit()
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
