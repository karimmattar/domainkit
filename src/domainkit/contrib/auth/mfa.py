"""Logins with a second factor: an authenticator app (TOTP), and recovery codes.

Needs the ``mfa`` extra (``cryptography``). Mount the router next to the
one that logs in::

    from domainkit.contrib.auth.mfa import router as mfa_router

    router.include_router(mfa_router, prefix="/auth")

- ``POST /mfa/totp`` (a user, with the password) starts enrolment: a new
  secret, in base 32 and as an ``otpauth://`` URI for a QR code.
- ``POST /mfa/totp/confirm`` with a first code from the app turns the
  second factor on, and returns recovery codes, shown once.
- From then on, ``/login`` (and ``/session``) answer 202 with an
  ``mfa_token``: a signed challenge that works for ``AUTH.MFA.CHALLENGE_TTL``
  and ends once the login is done. ``POST /mfa/login`` with it and a code
  (or a recovery code) returns the tokens.
- ``POST /mfa/totp/disable`` and ``POST /mfa/recovery-codes`` need the
  password and a code; ``GET /mfa`` says whether it is on.

A code works once: its step is recorded, and an earlier or equal step is
refused. Recovery codes are stored as digests and work once. Secrets are
encrypted under a key derived from ``SECRET_KEY`` (``encryption``). Codes
and passwords checked count against ``AUTH.LOGIN_RATE`` per user (and per
client address at ``/mfa/login``). Turning it on or off, new recovery
codes, used ones, and refusals are audited.
"""

import hashlib
import secrets
from datetime import timedelta
from typing import Final, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit import clock
from domainkit.conf import settings
from domainkit.http.errors import APIError

from . import totp
from .access import AuthenticatedUser, Public
from .api import (
    PASSWORD_MAX_LENGTH,
    MfaChallengeOut,
    TokenOut,
    _errors,
    _no_store,
    _throttle,
    _token_out,
    issue_login,
)
from .encryption import SecretBox
from .events import MfaDisabled, MfaEnabled, RecoveryCodesRegenerated, RecoveryCodeUsed
from .hashers import acheck_password
from .models import AbstractBaseUser, UserId
from .onetime import TOKEN_MAX_LENGTH, UserTokenGenerator
from .repositories import TotpDevice
from .settings import auth_settings
from .throttling import address_key, audit_refusal, throttle
from .uow import AuthUnitOfWork

RECOVERY_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
RECOVERY_LENGTH: Final = 16
"""Characters of a recovery code: 80 random bits, in groups of four."""
BOX_PURPOSE: Final = "domainkit.auth.totp"
"""What the key that encrypts TOTP secrets is derived for."""

type Method = Literal["totp", "recovery"]

router = APIRouter(tags=["auth"])


class MfaChallengeTokenGenerator(UserTokenGenerator):
    """The challenge between a password and a code: it ends with the login."""

    salt = "domainkit.auth.mfa_challenge"

    def state(self, user: AbstractBaseUser) -> tuple[object, ...]:
        """The password hash, last login, and status: logging in ends it."""
        return (user.password.value, user.last_login, user.is_active)

    def max_age(self) -> timedelta:
        """``AUTH.MFA.CHALLENGE_TTL``."""
        return auth_settings().MFA.CHALLENGE_TTL


challenges: Final = MfaChallengeTokenGenerator()


class MfaLoginIn(BaseModel):
    """The challenge from logging in, and a code."""

    model_config = ConfigDict(extra="forbid")

    mfa_token: str = Field(min_length=1, max_length=TOKEN_MAX_LENGTH)
    code: str = Field(min_length=1, max_length=64)


class PasswordIn(BaseModel):
    """The user's password, again."""

    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class CodeIn(BaseModel):
    """A code from the authenticator app."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)


class ReauthenticateIn(PasswordIn):
    """The password, and a code (or a recovery code)."""

    code: str = Field(min_length=1, max_length=64)


class EnrolmentOut(BaseModel):
    """The secret to put in the authenticator app, shown once."""

    secret: str
    """In base 32, for typing in."""
    otpauth_uri: str
    """For a QR code."""


class RecoveryCodesOut(BaseModel):
    """Recovery codes, shown once: each stands in for the app, once."""

    recovery_codes: list[str]


class MfaStatusOut(BaseModel):
    """Whether logins need a second factor, and recovery codes left."""

    enabled: bool
    recovery_codes_left: int


class InvalidChallenge(APIError):
    """The challenge is wrong, used, or expired: log in again (400)."""

    def __init__(self) -> None:
        """Use status 400 and the code ``invalid_token``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The login has expired; log in again.",
            code="invalid_token",
        )


class InvalidCode(APIError):
    """The code is wrong, or was used already (401)."""

    def __init__(self) -> None:
        """Use status 401 and the code ``invalid_code``."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "The code is wrong, or was used already.",
            code="invalid_code",
        )


async def challenge(user: AbstractBaseUser) -> MfaChallengeOut | None:
    """A challenge, if ``user`` has a second factor; None if not."""
    async with AuthUnitOfWork() as uow:
        if not await uow.mfa.enabled(user.id):
            return None
    return MfaChallengeOut(
        mfa_token=challenges.make_token(user),
        expires_in=int(auth_settings().MFA.CHALLENGE_TTL.total_seconds()),
    )


async def verify_challenge(request: Request, token: str, code: str) -> AbstractBaseUser:
    """The user a challenge and its code prove; the code is spent.

    Raises:
        InvalidChallenge: The challenge is wrong, used, or expired.
        InvalidCode: The code is wrong, or was used.
        APIError: 429, past ``AUTH.LOGIN_RATE`` for the address or the user.
    """
    user_id = challenges.user_id(token)
    keys = [address_key(request, "mfa")]
    if user_id is not None:
        keys.append(f"mfa:user:{user_id}")
    await _limit(request, keys)
    async with AuthUnitOfWork() as uow:
        user = None if user_id is None else await uow.users.find(user_id)
        held = challenges.check_token(user, token)  # As long without a user.
        method = None
        if user is not None and held:
            method = await spend_code(uow, user, code)
            if method is not None:
                await uow.commit()
    if user is None or not held:
        await audit_refusal("auth.mfa", "invalid_token")
        raise InvalidChallenge
    if method is None:
        await audit_refusal("auth.mfa", "invalid_code")
        raise InvalidCode
    return user


async def spend_code(
    uow: AuthUnitOfWork, user: AbstractBaseUser, code: str
) -> Method | None:
    """Spend ``code`` of ``user``: a TOTP code, or a recovery code; None if wrong.

    The caller commits. A recovery code records ``RecoveryCodeUsed``.
    """
    device = await uow.mfa.device(user.id)
    if device is None or not device.confirmed:
        return None
    entered = _normalize(code)
    if entered.isdigit():
        return "totp" if await _spend_totp(uow, user, device, entered) else None
    if len(entered) != RECOVERY_LENGTH or not set(entered) <= set(RECOVERY_ALPHABET):
        return None
    if not await uow.mfa.use_code(user.id, recovery_digest(user.id, entered)):
        return None
    left = await uow.mfa.codes_left(user.id)
    stored = await uow.users.get(UserId(user.id))
    stored.record(RecoveryCodeUsed(user_id=user.id, remaining=left))
    return "recovery"


def new_recovery_codes(count: int) -> list[str]:
    """``count`` new recovery codes, such as ``abcd-efgh-ijkl-mnop``."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        codes.append("-".join(raw[index : index + 4] for index in range(0, 16, 4)))
    return codes


def recovery_digest(user_id: UUID, code: str) -> str:
    """What the table keeps of a recovery code: SHA-256 with the user's id."""
    return hashlib.sha256(f"{user_id.hex}:{_normalize(code)}".encode()).hexdigest()


def secret_box() -> SecretBox:
    """The box TOTP secrets are encrypted in."""
    return SecretBox(BOX_PURPOSE)


@router.get("/mfa", responses=_errors(status.HTTP_401_UNAUTHORIZED))
async def mfa_status(user: AuthenticatedUser) -> MfaStatusOut:
    """Whether the user's logins need a second factor."""
    async with AuthUnitOfWork() as uow:
        return MfaStatusOut(
            enabled=await uow.mfa.enabled(user.id),
            recovery_codes_left=await uow.mfa.codes_left(user.id),
        )


@router.post(
    "/mfa/totp",
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def start_enrolment(
    body: PasswordIn, request: Request, response: Response, user: AuthenticatedUser
) -> EnrolmentOut:
    """A new secret for the authenticator app; confirm it with a code."""
    _no_store(response)
    await _reauthenticate(request, user, body.password)
    secret = totp.new_secret()
    sealed = secret_box().encrypt(secret, associated=user.id.bytes)
    async with AuthUnitOfWork() as uow:
        device = await uow.mfa.device(user.id)
        if device is not None and device.confirmed:
            raise APIError(
                status.HTTP_409_CONFLICT,
                "A second factor is on; turn it off first.",
                code="mfa_enabled",
            )
        await uow.mfa.start(user.id, sealed)
        await uow.commit()
    issuer = auth_settings().MFA.ISSUER or settings.PROJECT_NAME
    return EnrolmentOut(
        secret=totp.encode_secret(secret),
        otpauth_uri=totp.otpauth_uri(
            secret, account=user.get_username(), issuer=issuer
        ),
    )


@router.post(
    "/mfa/totp/confirm",
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_enrolment(
    body: CodeIn, request: Request, response: Response, user: AuthenticatedUser
) -> RecoveryCodesOut:
    """Turn the second factor on with a first code; return recovery codes."""
    _no_store(response)
    await _limit(request, [f"mfa:user:{user.id}"])
    codes = new_recovery_codes(auth_settings().MFA.RECOVERY_CODES)
    async with AuthUnitOfWork() as uow:
        device = await uow.mfa.device(user.id)
        if device is None or device.confirmed:
            raise APIError(
                status.HTTP_400_BAD_REQUEST,
                "No enrolment is waiting for a code; start one first.",
                code="no_enrolment",
            )
        secret, _ = secret_box().decrypt(device.secret, associated=user.id.bytes)
        step = totp.match(
            secret, _normalize(body.code), _now(), window=auth_settings().MFA.WINDOW
        )
        confirmed = step is not None and await uow.mfa.confirm(device.id, step)
        if confirmed:
            await uow.mfa.replace_codes(
                user.id, [recovery_digest(user.id, code) for code in codes]
            )
            stored = await uow.users.get(UserId(user.id))
            stored.record(MfaEnabled(user_id=user.id))
            await uow.commit()
    if not confirmed:
        await audit_refusal("auth.mfa", "invalid_code")
        raise InvalidCode
    return RecoveryCodesOut(recovery_codes=codes)


@router.post(
    "/mfa/totp/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def disable(
    body: ReauthenticateIn, request: Request, user: AuthenticatedUser
) -> None:
    """Turn the second factor off: the password and a code are needed."""
    await _reauthenticate(request, user, body.password, body.code)
    async with AuthUnitOfWork() as uow:
        await uow.mfa.remove(user.id)
        stored = await uow.users.get(UserId(user.id))
        stored.record(MfaDisabled(user_id=user.id))
        await uow.commit()


@router.post(
    "/mfa/recovery-codes",
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def regenerate_recovery_codes(
    body: ReauthenticateIn,
    request: Request,
    response: Response,
    user: AuthenticatedUser,
) -> RecoveryCodesOut:
    """Replace the recovery codes: the password and a code are needed."""
    _no_store(response)
    await _reauthenticate(request, user, body.password, body.code)
    codes = new_recovery_codes(auth_settings().MFA.RECOVERY_CODES)
    async with AuthUnitOfWork() as uow:
        await uow.mfa.replace_codes(
            user.id, [recovery_digest(user.id, code) for code in codes]
        )
        stored = await uow.users.get(UserId(user.id))
        stored.record(RecoveryCodesRegenerated(user_id=user.id))
        await uow.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post(
    "/mfa/login",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def mfa_login(body: MfaLoginIn, request: Request, response: Response) -> TokenOut:
    """Finish a login with the challenge and a code; return tokens."""
    _no_store(response)
    user = await verify_challenge(request, body.mfa_token, body.code)
    return _token_out(await issue_login(user))


async def _reauthenticate(
    request: Request, user: AbstractBaseUser, password: str, code: str | None = None
) -> None:
    """Check the password (and a code) again before a change to the factors.

    Raises:
        APIError: 400 ``wrong_password``; 429 past ``AUTH.LOGIN_RATE``.
        InvalidCode: The code is wrong, or was used.
    """
    await _throttle(request, f"password:user:{user.id}")
    if not await acheck_password(password, user.password.value):
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "Your password was entered incorrectly.",
            code="wrong_password",
        )
    if code is None:
        return
    await _limit(request, [f"mfa:user:{user.id}"])
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
        method = await spend_code(uow, stored, code)
        if method is not None:
            await uow.commit()
    if method is None:
        await audit_refusal("auth.mfa", "invalid_code")
        raise InvalidCode


async def _spend_totp(
    uow: AuthUnitOfWork, user: AbstractBaseUser, device: TotpDevice, code: str
) -> bool:
    """Spend a TOTP code; a secret under a fallback key is sealed again."""
    box = secret_box()
    secret, current = box.decrypt(device.secret, associated=user.id.bytes)
    step = totp.match(secret, code, _now(), window=auth_settings().MFA.WINDOW)
    if step is None or not await uow.mfa.use_step(device.id, step):
        return False
    if not current:
        await uow.mfa.reseal(device.id, box.encrypt(secret, associated=user.id.bytes))
    return True


async def _limit(request: Request, keys: list[str]) -> None:
    """Count code checks against ``AUTH.LOGIN_RATE``."""
    await throttle(
        request,
        auth_settings().LOGIN_RATE,
        keys,
        action="auth.mfa",
        detail="Too many attempts; try again later.",
        code="throttled",
    )


def _normalize(code: str) -> str:
    """A code as typed, without spaces or dashes, in lower case."""
    return "".join(code.split()).replace("-", "").lower()


def _now() -> float:
    """Seconds since the epoch, on ``domainkit.clock``."""
    return clock.now().timestamp()
