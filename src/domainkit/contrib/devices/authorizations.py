"""Confirming an act's authorization with a bound device or a passkey (ADR 0144).

A session asks for an authorization of one act (``domainkit.contrib.auth.actions``),
and the user confirms it. This app confirms it two ways, which
``DevicesConfig.ready()`` registers with auth (``on_action_confirmation``):

- **The mobile app's own act** (``device``): ``POST
  /devices/authorizations/{id}`` with ``{"digest"}``, signed by one of the
  user's bound devices, for an act the app makes itself and shows itself.
  It proves ``hwk`` and ``phone``. The authorization is the app's own
  login's: that login asked for it, and only that login spends it. Another
  session's is the phone's to confirm (``domainkit.contrib.ciba``), which
  shows who asked and matches a number. The device's key, what it signed,
  and its signature are kept with the authorization, so the approval can
  be checked again once the device is unbound.
- **A passkey** (``passkey``): ``POST
  /passkeys/authorizations/{id}/options`` makes a challenge for that
  authorization alone, linked to it, and ``POST
  /passkeys/authorizations/{id}`` takes the assertion. It proves ``hwk``
  and ``mfa``, never ``phone``. Only the session that asked uses either.

Each answers ``{"authorization": "<id>.<code>", "expires_at"}``, for the
act's ``Action-Authorization`` header.
"""

import logging
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, Field
from starlette.requests import Request

from domainkit.apps import registry
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.integration import (
    ActionAuthorization,
    ActionConfirmation,
    AuthorizationNeedsOtherFactors,
    action_authorization,
    action_for,
    auth_settings,
    confirm_action_with,
    link_action_authorization,
    login_digest,
    stepping_credentials,
)

from .binding import device_proof
from .exceptions import DeviceSignatureRefused, PasskeyRefused
from .passkeys import AUTHORIZE, Assertion, authenticate, request_options
from .settings import DevicesSettings
from .stores import DevicesUnitOfWork

__all__ = [
    "DEVICE_CONFIRMATION",
    "PASSKEY_CONFIRMATION",
    "AuthorizationOut",
    "confirm_with_device",
    "confirm_with_passkey",
    "passkey_options",
]

logger = logging.getLogger(__name__)
DEVICE_METHODS: Final = frozenset({"hwk", "phone"})
"""What the app's own act proves: a key the device holds, on its own screen."""
PASSKEY_METHODS: Final = frozenset({"hwk", "mfa"})
"""What a passkey verified with its user proves: never the phone."""


async def _has_device(user_id: UUID) -> bool:
    """Whether the user has a bound device."""
    async with DevicesUnitOfWork() as uow:
        return await uow.devices.count(user_id) > 0


async def _has_passkey(user_id: UUID) -> bool:
    """Whether the user has a passkey, and passkeys are set up (``RP_ID``)."""
    if registry.settings(DevicesSettings).RP_ID is None:
        return False
    async with DevicesUnitOfWork() as uow:
        return await uow.passkeys.count(user_id) > 0


DEVICE_CONFIRMATION: Final = ActionConfirmation("device", DEVICE_METHODS, _has_device)
"""The app's own act, signed by a bound device: ``hwk`` and ``phone``."""
PASSKEY_CONFIRMATION: Final = ActionConfirmation(
    "passkey", PASSKEY_METHODS, _has_passkey
)
"""A passkey verified with its user: ``hwk`` and ``mfa``, never ``phone``."""


class AuthorizationOut(BaseModel):
    """The authorization: send it with the act, in ``Action-Authorization``, once."""

    authorization: str = Field(repr=False)
    """``<id>.<code>``."""
    expires_at: datetime


async def confirm_with_device(
    request: Request, user: AbstractBaseUser, authorization_id: UUID, digest: str
) -> AuthorizationOut:
    """Confirm the app's own authorization, whose act's digest its device signed.

    The request is signed by one of the user's bound devices
    (``DeviceSigned``), with the login that asked for the authorization;
    ``digest`` is the act's, as the app works it out from the act it shows.
    It proves ``hwk`` and ``phone``. The device's id, its key, what it
    signed, its signature, and when are kept with the authorization.

    Raises:
        DeviceSignatureRefused: No device of the user signed the request.
        AuthorizationNotFound: None of this login's authorizations has that id.
        AuthorizationChanged: ``digest`` is not the act's (409).
        AuthorizationNeedsOtherFactors: The act asks the password too (409).
        APIError: 400 ``step_up_unsupported``: the credentials cannot be
            told apart; 409 ``authorization_confirmed``, 409
            ``authorization_expired``.
    """
    proof = await device_proof(request, user)
    if proof is None:
        raise DeviceSignatureRefused()
    authorization = await _own(request, user, authorization_id)
    code = await confirm_action_with(
        authorization.id,
        DEVICE_METHODS,
        user_id=user.id,
        login=authorization.login,
        digest=digest,
        device_id=proof.device.id,
        algorithm=proof.device.algorithm,
        public_key=proof.device.public_key,
        signature=proof.signature,
        signed=proof.signed,
        signed_at=proof.signed_at,
    )
    return AuthorizationOut(authorization=code, expires_at=authorization.expires_at)


async def passkey_options(
    request: Request, user: AbstractBaseUser, authorization_id: UUID
) -> dict[str, Any]:
    """The options to confirm one of the session's authorizations with a passkey.

    A challenge made for this authorization alone (purpose ``authorize``,
    for the user, used once), linked to it, offering the user's passkeys:
    the options a step-up's are. Asking again makes a new one, and the
    last one alone confirms it.

    Raises:
        PasskeysOff: ``RP_ID`` is not set.
        AuthorizationNotFound: None of this login's authorizations has that id.
        AuthorizationNeedsOtherFactors: The act asks the phone, which no
            passkey proves (409).
        APIError: 400 ``step_up_unsupported``; 409
            ``authorization_confirmed``, 409 ``authorization_expired``.
    """
    authorization = await _own(request, user, authorization_id)
    if _asks_the_phone(authorization):
        raise AuthorizationNeedsOtherFactors
    options = await request_options(AUTHORIZE, user)
    await link_action_authorization(
        authorization.id, user_id=user.id, passkey_challenge=options["challenge"]
    )
    return options


async def confirm_with_passkey(
    request: Request,
    user: AbstractBaseUser,
    authorization_id: UUID,
    assertion: Assertion,
) -> AuthorizationOut:
    """Confirm one of the session's authorizations: the user's passkey answered.

    The assertion answers the challenge made for this authorization
    (``passkey_options``), which it spends; it proves ``hwk`` and ``mfa``.

    Raises:
        PasskeysOff: ``RP_ID`` is not set.
        AuthorizationNotFound: None of this login's authorizations has that id.
        AuthorizationNeedsOtherFactors: The act asks the phone (409).
        PasskeyRefused: The answer does not hold, is another user's, or
            answers another challenge than the authorization's (401).
        APIError: 400 ``step_up_unsupported``; 409
            ``authorization_confirmed``, 409 ``authorization_expired``.
    """
    authorization = await _own(request, user, authorization_id)
    if _asks_the_phone(authorization):
        raise AuthorizationNeedsOtherFactors
    if authorization.passkey_challenge is None:
        logger.info("A passkey was refused: no challenge was made for it.")
        raise PasskeyRefused()
    await authenticate(
        assertion, AUTHORIZE, user, challenge=authorization.passkey_challenge
    )
    code = await confirm_action_with(
        authorization.id, PASSKEY_METHODS, user_id=user.id, login=authorization.login
    )
    return AuthorizationOut(authorization=code, expires_at=authorization.expires_at)


async def _own(
    request: Request, user: AbstractBaseUser, authorization_id: UUID
) -> ActionAuthorization:
    """The user's authorization ``authorization_id``, asked by the request's login.

    Raises:
        APIError: 400 ``step_up_unsupported``: the credentials cannot be
            told apart.
        AuthorizationNotFound: None of the login's has that id.
    """
    login = login_digest(stepping_credentials(request))
    return await action_authorization(authorization_id, user.id, login=login)


def _asks_the_phone(authorization: ActionAuthorization) -> bool:
    """Whether confirming it must prove ``phone``, which no passkey proves."""
    action = action_for(authorization.purpose)
    required = (
        action.required()
        if action is not None
        else frozenset(auth_settings().AUTHORIZATIONS.METHODS)
    )
    return "phone" in required
