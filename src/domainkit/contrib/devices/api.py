"""The routes of passkeys and bound devices.

::

    from domainkit.contrib.devices.api import router as devices_router

    router.include_router(devices_router, prefix="/auth")

Passkeys (``RP_ID`` set):

- ``POST /passkeys/register/options`` (a user, with ``{"password"}`` or a
  recent step-up): the options for ``navigator.credentials.create()``.
- ``POST /passkeys/register`` (a user): the browser's response, and a
  name; the passkey.
- ``POST /passkeys/login/options`` (anyone), ``POST /passkeys/login``:
  tokens, with no second factor asked.
- ``POST /passkeys/step-up/options``, ``POST /passkeys/step-up`` (a
  user): a step-up proving ``mfa``.
- ``GET /passkeys``, ``DELETE /passkeys/{id}`` (a user).

Devices:

- ``POST /devices`` (a user, with ``{"password"}`` or a recent step-up):
  bind a device's public key.
- ``GET /devices``, ``DELETE /devices/{id}`` (a user).
- ``POST /devices/step-up`` (a user, signed by one of their devices): a
  step-up proving ``hwk``.

Push tokens:

- ``POST /devices/push-tokens`` (a user): the token an app's push service
  gave it, and the ``platform``.
- ``GET /devices/push-tokens``, ``DELETE /devices/push-tokens/{id}``.
"""

from datetime import datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.apps import registry
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.api import PASSWORD_MAX_LENGTH, TokenOut, issue_login
from domainkit.contrib.auth.authentication import Credentials, Proof
from domainkit.contrib.auth.models import UserId
from domainkit.contrib.auth.stepup import (
    StepUpOut,
    record_step_up,
    step_up_answer,
    stepping_credentials,
)
from domainkit.contrib.auth.tokens import utc_now
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.http.access import IsUnscoped, Public, Require

from .binding import DeviceSigned, device_key
from .events import DeviceBound, DeviceUnbound, PasskeyRemoved
from .exceptions import DeviceNotFound, PasskeyNotFound, TooManyKeys
from .passkeys import (
    LOGIN,
    STEP_UP,
    Assertion,
    authenticate,
    creation_options,
    reauthenticate,
    register,
    request_options,
)
from .settings import DevicesSettings
from .stores import DevicesUnitOfWork, Passkey, PushToken

__all__ = ["router"]

router = APIRouter(tags=["devices"])
SIGNED_IN = Depends(Require(IsUnscoped()))
DEVICE_SIGNED = Depends(Require(IsUnscoped(), DeviceSigned()))
ADD_PASSKEY: Final = "devices.add_passkey"
BIND_DEVICE: Final = "devices.bind_device"
MAX_B64: Final = 16_384
"""Base64url characters a browser's field may have."""

type Encoded = Annotated[str, Field(min_length=1, max_length=MAX_B64)]


class ReauthIn(BaseModel):
    """The password, unless the user stepped up in the last ten minutes."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )


class AttestationResponse(BaseModel):
    """``AuthenticatorAttestationResponse``, as ``toJSON()`` writes it."""

    model_config = ConfigDict(extra="ignore")

    clientDataJSON: Encoded
    attestationObject: Encoded


class RegistrationCredential(BaseModel):
    """``PublicKeyCredential`` of a registration, as ``toJSON()`` writes it."""

    model_config = ConfigDict(extra="ignore")

    response: AttestationResponse


class RegistrationIn(BaseModel):
    """The browser's new credential, and a name the user knows it by."""

    model_config = ConfigDict(extra="forbid")

    credential: RegistrationCredential
    name: str = Field(default="Passkey", min_length=1, max_length=100)


class AssertionResponse(BaseModel):
    """``AuthenticatorAssertionResponse``, as ``toJSON()`` writes it."""

    model_config = ConfigDict(extra="ignore")

    clientDataJSON: Encoded
    authenticatorData: Encoded
    signature: Encoded
    userHandle: Annotated[str, Field(max_length=MAX_B64)] | None = None


class AssertionCredential(BaseModel):
    """``PublicKeyCredential`` of an assertion, as ``toJSON()`` writes it."""

    model_config = ConfigDict(extra="ignore")

    id: Annotated[str, Field(min_length=1, max_length=1400)]
    response: AssertionResponse

    def assertion(self) -> Assertion:
        """What ``authenticate`` checks."""
        return Assertion(
            credential_id=self.id,
            client_data_json=self.response.clientDataJSON,
            authenticator_data=self.response.authenticatorData,
            signature=self.response.signature,
            user_handle=self.response.userHandle,
        )


class AssertionIn(BaseModel):
    """The browser's answer to a challenge."""

    model_config = ConfigDict(extra="forbid")

    credential: AssertionCredential


class PasskeyOut(BaseModel):
    """A passkey of the user (never its key)."""

    id: UUID
    name: str
    backed_up: bool
    """Synced to the user's other devices."""
    created_at: datetime
    last_used_at: datetime | None


class DeviceIn(BaseModel):
    """A device's public key: SubjectPublicKeyInfo, or a P-256 point, base64url."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=1, max_length=1024)
    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )


class PushTokenIn(BaseModel):
    """A token a push service gave the app, and which service."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(
        min_length=1, max_length=4096, pattern=r"^[\x21-\x7e]+$", repr=False
    )
    platform: Literal["fcm", "apns", "huawei"]


class PushTokenOut(BaseModel):
    """A push token of the user (never the token)."""

    id: UUID
    platform: str
    created_at: datetime
    last_seen_at: datetime


class DeviceOut(BaseModel):
    """A bound device of the user."""

    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None


@router.post("/passkeys/register/options")
async def start_registration(
    body: ReauthIn, request: Request, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> dict[str, Any]:
    """The options to make a passkey with, once the user authenticated again."""
    await reauthenticate(request, user, body.password, ADD_PASSKEY)
    return await creation_options(user)


@router.post("/passkeys/register", status_code=status.HTTP_201_CREATED)
async def finish_registration(
    body: RegistrationIn, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> PasskeyOut:
    """Store the passkey the browser made."""
    passkey = await register(
        user,
        client_data_json=body.credential.response.clientDataJSON,
        attestation_object=body.credential.response.attestationObject,
        name=body.name,
    )
    return _passkey_out(passkey)


@router.post("/passkeys/login/options", dependencies=[Public])
async def start_login() -> dict[str, Any]:
    """The options to log in with a passkey of this site."""
    return await request_options(LOGIN, None)


@router.post("/passkeys/login", dependencies=[Public])
async def finish_login(body: AssertionIn, response: Response) -> TokenOut:
    """Tokens for the user whose passkey answered."""
    response.headers["Cache-Control"] = "no-store"
    user = await authenticate(body.credential.assertion(), LOGIN)
    issued = await issue_login(user, factor_proven=True)
    return TokenOut(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        refresh_token=issued.refresh_token,
    )


@router.post("/passkeys/step-up/options")
async def start_step_up(user: Annotated[AbstractBaseUser, SIGNED_IN]) -> dict[str, Any]:
    """The options to step up with one of the user's passkeys."""
    return await request_options(STEP_UP, user)


@router.post("/passkeys/step-up", response_model_exclude_none=True)
async def finish_step_up(
    body: AssertionIn,
    request: Request,
    response: Response,
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> StepUpOut:
    """Step up: the user's passkey answered, verifying them (``mfa``)."""
    response.headers["Cache-Control"] = "no-store"
    credentials = stepping_credentials(request)
    await authenticate(body.credential.assertion(), STEP_UP, user)
    return await _stepped_up(credentials, user, frozenset({"hwk", "mfa"}))


@router.get("/passkeys")
async def list_passkeys(
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> list[PasskeyOut]:
    """The user's passkeys."""
    async with DevicesUnitOfWork() as uow:
        return [_passkey_out(each) for each in await uow.passkeys.of_user(user.id)]


@router.delete("/passkeys/{passkey_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_passkey(
    passkey_id: UUID, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> None:
    """Remove one of the user's passkeys."""
    async with DevicesUnitOfWork() as uow:
        if not await uow.passkeys.remove(user.id, passkey_id):
            raise PasskeyNotFound("You have no passkey with that id.")
        stored = await uow.users.get(UserId(user.id))
        stored.record(PasskeyRemoved(user_id=user.id, passkey_id=passkey_id))
        await uow.commit()


@router.post("/devices", status_code=status.HTTP_201_CREATED)
async def bind_device(
    body: DeviceIn, request: Request, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> DeviceOut:
    """Bind a device's key, once the user authenticated again."""
    algorithm, key = device_key(body.public_key)
    await reauthenticate(request, user, body.password, BIND_DEVICE)
    limit = _settings().MAX_DEVICES
    async with DevicesUnitOfWork() as uow:
        if await uow.devices.count(user.id) >= limit:
            raise TooManyKeys("Unbind a device before binding another.")
        device = await uow.devices.add(user.id, body.name, algorithm, key)
        stored = await uow.users.get(UserId(user.id))
        stored.record(DeviceBound(user_id=user.id, device_id=device.id))
        await uow.commit()
    return DeviceOut(
        id=device.id, name=device.name, created_at=device.created_at, last_used_at=None
    )


@router.get("/devices")
async def list_devices(user: Annotated[AbstractBaseUser, SIGNED_IN]) -> list[DeviceOut]:
    """The user's bound devices."""
    async with DevicesUnitOfWork() as uow:
        found = await uow.devices.of_user(user.id)
    return [
        DeviceOut(
            id=d.id, name=d.name, created_at=d.created_at, last_used_at=d.last_used_at
        )
        for d in found
    ]


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_device(
    device_id: UUID, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> None:
    """Unbind one of the user's devices."""
    async with DevicesUnitOfWork() as uow:
        if not await uow.devices.remove(user.id, device_id):
            raise DeviceNotFound("You have no device with that id.")
        stored = await uow.users.get(UserId(user.id))
        stored.record(DeviceUnbound(user_id=user.id, device_id=device_id))
        await uow.commit()


@router.post("/devices/push-tokens", status_code=status.HTTP_201_CREATED)
async def register_push_token(
    body: PushTokenIn, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> PushTokenOut:
    """Keep the token the user's app was given: notifications go there."""
    async with DevicesUnitOfWork() as uow:
        token = await uow.push_tokens.register(
            user.id, body.token, body.platform, keep=_settings().MAX_PUSH_TOKENS
        )
        await uow.commit()
    return _push_token_out(token)


@router.get("/devices/push-tokens")
async def list_push_tokens(
    user: Annotated[AbstractBaseUser, SIGNED_IN],
) -> list[PushTokenOut]:
    """The user's push tokens."""
    async with DevicesUnitOfWork() as uow:
        return [_push_token_out(t) for t in await uow.push_tokens.of_user(user.id)]


@router.delete(
    "/devices/push-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_push_token(
    token_id: UUID, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> None:
    """Remove one of the user's push tokens (the app logged out)."""
    async with DevicesUnitOfWork() as uow:
        if not await uow.push_tokens.remove(user.id, token_id):
            raise DeviceNotFound("You have no push token with that id.")
        await uow.commit()


@router.post(
    "/devices/step-up", response_model_exclude_none=True, dependencies=[DEVICE_SIGNED]
)
async def device_step_up(
    request: Request, response: Response, user: Annotated[AbstractBaseUser, SIGNED_IN]
) -> StepUpOut:
    """Step up: a device of the user signed this request (``hwk``)."""
    response.headers["Cache-Control"] = "no-store"
    credentials = stepping_credentials(request)
    return await _stepped_up(credentials, user, frozenset({"hwk"}))


async def _stepped_up(
    credentials: Credentials,
    user: AbstractBaseUser,
    methods: frozenset[str],
) -> StepUpOut:
    """Record a step-up proving ``methods``; what it answers."""
    proof = Proof(utc_now(), methods)
    async with AuthUnitOfWork() as uow:
        await record_step_up(uow, credentials, user, proof)
        await uow.commit()
    return step_up_answer(credentials, proof)


def _passkey_out(passkey: Passkey) -> PasskeyOut:
    """A passkey as the API shows it."""
    return PasskeyOut(
        id=passkey.id,
        name=passkey.name,
        backed_up=passkey.backed_up,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
    )


def _push_token_out(token: PushToken) -> PushTokenOut:
    """A push token as the API shows it."""
    return PushTokenOut(
        id=token.id,
        platform=token.platform,
        created_at=token.created_at,
        last_seen_at=token.last_seen_at,
    )


def _settings() -> DevicesSettings:
    """``DEVICES``, validated."""
    return registry.settings(DevicesSettings)
