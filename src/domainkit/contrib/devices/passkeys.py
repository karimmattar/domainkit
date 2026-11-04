"""Passkeys: registering one, and logging in or stepping up with it.

Every ceremony starts with a challenge the server makes and keeps: 256
random bits, for one purpose (``register``, ``login``, ``step_up``) and,
but for a login, one user, used once within ``DEVICES.CHALLENGE_TTL``.
The browser's answer names its challenge in its client data, which finds
the record; the record is deleted before anything else is checked, so a
refused answer spends it too. Users are verified by the authenticator
(``userVerification: required``): a passkey so used is two factors on
its own, so a login with one asks no second factor, and a step-up with
one proves whatever ``StepUp`` asks (``mfa``).

Adding a passkey, or a device, needs the user to authenticate again: the
password, or a step-up within ``REAUTH_AGE``. Without it, whoever held a
stolen access token could add a key of their own and keep the account.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from domainkit.apps import registry
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.integration import (
    reauthenticate,
)
from domainkit.contrib.auth.models import UserId

from .events import PasskeyAdded
from .exceptions import (
    PasskeyRefused,
    PasskeysOff,
    TooManyKeys,
)
from .settings import DevicesSettings
from .stores import DevicesUnitOfWork, Passkey
from .webauthn import (
    ALGORITHMS,
    WebAuthnError,
    b64decode,
    b64encode,
    client_challenge,
    verify_assertion,
    verify_registration,
)

__all__ = [
    "Assertion",
    "authenticate",
    "creation_options",
    "reauthenticate",
    "register",
    "request_options",
]

logger = logging.getLogger(__name__)
REGISTER: Final = "register"
LOGIN: Final = "login"
STEP_UP: Final = "step_up"


@dataclass(frozen=True, slots=True, repr=False)
class Assertion:
    """A passkey's answer to a challenge, base64url as the browser sends it."""

    credential_id: str
    client_data_json: str
    authenticator_data: str
    signature: str
    user_handle: str | None = None


def passkey_settings() -> DevicesSettings:
    """``DEVICES``, with passkeys on.

    Raises:
        PasskeysOff: ``RP_ID`` is not set.
    """
    config = registry.settings(DevicesSettings)
    if config.RP_ID is None:
        raise PasskeysOff("Passkeys are not set up here.")
    return config


async def creation_options(user: AbstractBaseUser) -> dict[str, Any]:
    """What ``navigator.credentials.create()`` needs, as WebAuthn's JSON.

    Raises:
        PasskeysOff: Passkeys are not set up.
        TooManyKeys: The user has ``MAX_PASSKEYS``.
    """
    config = passkey_settings()
    async with DevicesUnitOfWork() as uow:
        held = await uow.passkeys.of_user(user.id)
        if len(held) >= config.MAX_PASSKEYS:
            raise TooManyKeys("Remove a passkey before adding another.")
        challenge = await uow.challenges.issue(REGISTER, user.id, config.CHALLENGE_TTL)
        await uow.commit()
    name = user.get_username()
    return {
        "rp": {"id": config.RP_ID, "name": config.RP_NAME or config.RP_ID},
        "user": {"id": b64encode(user.id.bytes), "name": name, "displayName": name},
        "challenge": challenge,
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in ALGORITHMS],
        "timeout": _milliseconds(config.CHALLENGE_TTL),
        "excludeCredentials": [_descriptor(each) for each in held],
        "authenticatorSelection": {
            "residentKey": "required",
            "userVerification": "required",
        },
        "attestation": "none",
    }


async def register(
    user: AbstractBaseUser,
    *,
    client_data_json: str,
    attestation_object: str,
    name: str,
) -> Passkey:
    """Store the passkey a registration's response proves.

    Raises:
        PasskeyRefused: The response does not hold, answers another
            challenge, or names a passkey registered already.
        TooManyKeys: The user has ``MAX_PASSKEYS``.
    """
    config = passkey_settings()
    client_data = _decoded(client_data_json)
    await _take(client_data, REGISTER, user.id)
    try:
        credential = verify_registration(
            client_data_json=client_data,
            attestation_object=_decoded(attestation_object),
            challenge=client_challenge(client_data),
            rp_id=config.RP_ID or "",
            origins=config.ORIGINS,
        )
    except WebAuthnError as exc:
        raise _refused(exc) from None
    async with DevicesUnitOfWork() as uow:
        if await uow.passkeys.find(credential.credential_id) is not None:
            raise _refused("registered already")
        if await uow.passkeys.count(user.id) >= config.MAX_PASSKEYS:
            raise TooManyKeys("Remove a passkey before adding another.")
        passkey = await uow.passkeys.add(
            user.id,
            credential.credential_id,
            credential.algorithm,
            credential.public_key,
            credential.sign_count,
            backed_up=credential.backed_up,
            name=name,
        )
        stored = await uow.users.get(UserId(user.id))
        stored.record(PasskeyAdded(user_id=user.id, passkey_id=passkey.id))
        await uow.commit()
    return passkey


async def request_options(
    purpose: str, user: AbstractBaseUser | None
) -> dict[str, Any]:
    """What ``navigator.credentials.get()`` needs, as WebAuthn's JSON.

    A login offers any passkey of this site (the browser lists them); a
    step-up offers the user's.

    Raises:
        PasskeysOff: Passkeys are not set up.
    """
    config = passkey_settings()
    async with DevicesUnitOfWork() as uow:
        held = [] if user is None else await uow.passkeys.of_user(user.id)
        challenge = await uow.challenges.issue(
            purpose, None if user is None else user.id, config.CHALLENGE_TTL
        )
        await uow.commit()
    return {
        "challenge": challenge,
        "rpId": config.RP_ID,
        "timeout": _milliseconds(config.CHALLENGE_TTL),
        "allowCredentials": [_descriptor(each) for each in held],
        "userVerification": "required",
    }


async def authenticate(
    assertion: Assertion, purpose: str, user: AbstractBaseUser | None = None
) -> AbstractBaseUser:
    """The active user whose passkey answered a challenge of ``purpose``.

    With ``user`` (a step-up), it must be theirs, and the challenge too.

    Raises:
        PasskeyRefused: It does not hold, or is not theirs.
    """
    config = passkey_settings()
    client_data = _decoded(assertion.client_data_json)
    await _take(client_data, purpose, None if user is None else user.id)
    try:
        credential_id = b64decode(assertion.credential_id)
    except WebAuthnError as exc:
        raise _refused(exc) from None
    async with DevicesUnitOfWork() as uow:
        passkey = await uow.passkeys.find(credential_id)
        if passkey is None:
            raise _refused("no such passkey")
        if user is not None and passkey.user_id != user.id:
            raise _refused("another user's passkey")
        if (
            assertion.user_handle
            and _decoded(assertion.user_handle) != passkey.user_id.bytes
        ):
            raise _refused("another user's handle")
        try:
            count = verify_assertion(
                client_data_json=client_data,
                authenticator_data=_decoded(assertion.authenticator_data),
                signature=_decoded(assertion.signature),
                challenge=client_challenge(client_data),
                rp_id=config.RP_ID or "",
                origins=config.ORIGINS,
                algorithm=passkey.algorithm,
                public_key=passkey.public_key,
                sign_count=passkey.sign_count,
            )
        except WebAuthnError as exc:
            raise _refused(exc) from None
        if not await uow.passkeys.used(passkey, count):
            raise _refused("used meanwhile with a later count")
        found = await uow.users.find(UserId(passkey.user_id))
        if found is None or not found.is_active:
            raise _refused("its user cannot log in")
        await uow.commit()
    return found


async def _take(client_data: bytes, purpose: str, user_id: UUID | None) -> None:
    """Spend the challenge the client data names, if it is for ``purpose``.

    Raises:
        PasskeyRefused: It is unknown, used, expired, for another purpose,
            or another user's.
    """
    try:
        challenge = client_challenge(client_data)
    except WebAuthnError as exc:
        raise _refused(exc) from None
    async with DevicesUnitOfWork() as uow:
        taken = await uow.challenges.take(challenge)
        await uow.commit()
    if taken is None or taken.purpose != purpose or taken.user_id != user_id:
        raise _refused("an unknown, used, or expired challenge")


def _decoded(value: str) -> bytes:
    """Base64url from the browser.

    Raises:
        PasskeyRefused: It is not.
    """
    try:
        return b64decode(value)
    except WebAuthnError as exc:
        raise _refused(exc) from None


def _refused(reason: object) -> PasskeyRefused:
    """``PasskeyRefused``, its reason logged."""
    logger.info("A passkey was refused: %s.", reason)
    return PasskeyRefused()


def _descriptor(passkey: Passkey) -> Mapping[str, str]:
    """A passkey as ``allowCredentials`` and ``excludeCredentials`` list it."""
    return {"type": "public-key", "id": passkey.credential_id}


def _milliseconds(span: timedelta) -> int:
    """``span`` in milliseconds."""
    return int(span.total_seconds() * 1000)
