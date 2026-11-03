"""Send a code to a number, check it, and keep the number as verified.

Each runs in a ``PhonesUnitOfWork`` the caller opens and commits, so a
script or a use case of the project's verifies numbers as the routes do::

    async with PhonesUnitOfWork() as uow:
        pending = await send_code(uow, user.id, "+33 6 12 34 56 78")
        await uow.commit()  # the text message goes out with the commit

    async with PhonesUnitOfWork() as uow:
        verified = await confirm_code(uow, user.id, "+33612345678", "123456")
        await uow.commit()  # a wrong try counts even when it is refused
    if verified is None:
        raise WrongCode()
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from domainkit import signing
from domainkit.contrib.auth.integration import utc_now
from domainkit.contrib.auth.models import UserId

from .events import PhoneRemoved, PhoneVerified
from .exceptions import (
    CodeExpired,
    CodeTooSoon,
    InvalidPhone,
    PhoneAlreadyVerified,
    PhoneNotFound,
    TooManyCodes,
    TooManyPhones,
)
from .settings import PhonesSettings
from .stores import PhoneCode, PhonesUnitOfWork, VerifiedPhone

__all__ = [
    "PendingCode",
    "confirm_code",
    "normalize_phone",
    "phones_settings",
    "remove_phone",
    "send_code",
]

E164: Final = re.compile(r"\+[1-9]\d{6,14}")
"""A number in international form: ``+``, a country code, at most 15 digits."""
_SEPARATORS: Final = re.compile(r"[\s().-]")
PURPOSE: Final = "contrib.phones.code"
"""What the key that digests codes is derived for."""


@dataclass(frozen=True, slots=True)
class PendingCode:
    """A code on its way: the number, until when it works, and when to resend."""

    phone: str
    expires_at: datetime
    resend_at: datetime


def phones_settings() -> PhonesSettings:
    """``PHONES``, validated (the defaults when it is unset)."""
    from domainkit.apps import registry

    return registry.settings(PhonesSettings)


def normalize_phone(text: str) -> str:
    """``text`` as ``+33612345678``: spaces, dots, dashes, brackets dropped.

    Raises:
        InvalidPhone: It is not a number in international form.
    """
    phone = _SEPARATORS.sub("", text)
    if not E164.fullmatch(phone):
        raise InvalidPhone(
            "Give the number in international form, such as +33612345678."
        )
    return phone


def code_digest(code_id: UUID, code: str, key: bytes) -> str:
    """What the table keeps of a code: an HMAC with a key of its own."""
    return hmac.new(key, f"{code_id.hex}:{code}".encode(), hashlib.sha256).hexdigest()


async def send_code(uow: PhonesUnitOfWork, user_id: UUID, phone: str) -> PendingCode:
    """Make a code for ``phone`` and queue the text message holding it.

    The message leaves with ``uow``'s commit, through the outbox.

    Raises:
        InvalidPhone: ``phone`` is not a number in international form.
        PhoneAlreadyVerified: The user verified it already.
        CodeTooSoon: A code went to it for this user within ``RESEND_AFTER``.
        TooManyCodes: ``MAX_SENDS`` codes went to it, or were asked by the
            user, within ``SEND_WINDOW``.
    """
    from domainkit.contrib.notifications import send_sms

    config = phones_settings()
    phone = normalize_phone(phone)
    if await uow.phones.find(user_id, phone) is not None:
        raise PhoneAlreadyVerified("This number is verified already.")
    now = utc_now()
    # Codes older than the window count for nothing: forget them as we go.
    await uow.codes.purge(now - config.SEND_WINDOW - config.CODE_TTL)
    last = await uow.codes.latest(user_id, phone)
    if last is not None and now < last.created_at + config.RESEND_AFTER:
        raise CodeTooSoon(last.created_at + config.RESEND_AFTER - now)
    sent, oldest = await uow.codes.sent_since(user_id, phone, now - config.SEND_WINDOW)
    if sent >= config.MAX_SENDS and oldest is not None:
        raise TooManyCodes(oldest + config.SEND_WINDOW - now)
    code = "".join(secrets.choice("0123456789") for _ in range(config.CODE_LENGTH))
    code_id = uuid4()
    digest = code_digest(code_id, code, signing.derive_key(PURPOSE))
    await uow.codes.expire(user_id, phone, now)  # Only the last code works.
    stored = await uow.codes.add(
        code_id, user_id, phone, digest, now=now, expires_at=now + config.CODE_TTL
    )
    await send_sms(phone, config.MESSAGE.replace("{code}", code), uow=uow)
    return PendingCode(phone, stored.expires_at, now + config.RESEND_AFTER)


async def confirm_code(
    uow: PhonesUnitOfWork, user_id: UUID, phone: str, code: str
) -> VerifiedPhone | None:
    """Check ``code`` against the last one sent; keep the number if it matches.

    None for a wrong code: the try is counted, so commit ``uow`` before
    answering (``WrongCode``). Only the last code sent works; a match
    spends every code of the number.

    Raises:
        InvalidPhone: ``phone`` is not a number in international form.
        CodeExpired: No code works: none was sent, it is older than
            ``CODE_TTL``, or it was tried ``MAX_ATTEMPTS`` times.
        TooManyPhones: The user holds ``MAX_PHONES`` numbers already.
    """
    config = phones_settings()
    phone = normalize_phone(phone)
    last = await uow.codes.live(user_id, phone, utc_now())
    if last is None or last.attempts >= config.MAX_ATTEMPTS:
        raise CodeExpired("No code works for this number; ask for a new one.")
    if not _matches(last, code.strip()):
        await uow.codes.tried(last.id)
        return None
    await uow.codes.spend(user_id, phone)
    if await uow.phones.find(user_id, phone) is not None:
        raise PhoneAlreadyVerified("This number is verified already.")
    if await uow.phones.count(user_id) >= config.MAX_PHONES:
        raise TooManyPhones("Remove a number before verifying another.")
    verified = await uow.phones.add(user_id, phone, utc_now())
    user = await uow.users.get(UserId(user_id))
    user.record(PhoneVerified(user_id=user_id, phone_id=verified.id))
    return verified


async def remove_phone(uow: PhonesUnitOfWork, user_id: UUID, phone_id: UUID) -> None:
    """Forget the user's verified number ``phone_id``.

    Raises:
        PhoneNotFound: The user has no number with that id.
    """
    if not await uow.phones.remove(user_id, phone_id):
        raise PhoneNotFound("You have no verified number with that id.")
    user = await uow.users.get(UserId(user_id))
    user.record(PhoneRemoved(user_id=user_id, phone_id=phone_id))


def _matches(stored: PhoneCode, code: str) -> bool:
    """Whether ``code`` is the one ``stored`` keeps, under any current key."""
    return any(
        hmac.compare_digest(stored.digest, code_digest(stored.id, code, key))
        for key in signing.derive_keys(PURPOSE)
    )
