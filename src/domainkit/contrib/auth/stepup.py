"""Stepping up: authenticating again, with a second factor, for one act.

Some acts ask more than a login: adding a payee, changing an email
address, approving a payment. ``StepUp(max_age=...)`` (``auth.access``)
guards them, and refuses credentials that did not authenticate again
within ``max_age`` with ``401 step_up_required``. Mount the router next to
the one that logs in::

    from domainkit.contrib.auth.stepup import router as step_up_router

    router.include_router(step_up_router, prefix="/auth")

``POST /step-up`` takes a code of the user's authenticator app (and the
password, with ``AUTH.STEP_UP.METHODS = ("pwd", "otp")``) and records the
step-up where the credentials keep it:

- a login token or a browser session: in the table of step-ups, by the
  token's digest, read by ``StepUp`` on the routes that ask for it;
- a JSON Web Token: in a new access token of the same login, with the
  ``auth_time`` and ``amr`` claims, returned in the response. The step-up
  lasts as long as that token: a refresh issues tokens without them.

A step-up needs the second factor turned on (``auth.mfa``); recovery codes
do not step up. Wrong codes count against ``AUTH.MFA.RATE`` for the user,
as every code check does, and wrong passwords against ``AUTH.LOGIN_RATE``;
step-ups, and refusals, go to the audit trail.
"""

from collections.abc import Awaitable, Callable, Collection
from datetime import datetime
from typing import Final, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.http.access import IsUnscoped, Require
from domainkit.http.errors import APIError

from .access import AuthenticatedUser
from .api import PASSWORD_MAX_LENGTH, _credentials, _errors, _no_store, _throttle
from .api import password_holds as _password_holds
from .authentication import (
    JWT_SCHEME,
    STEPPING_SCHEMES,
    Credentials,
    JWTAuthentication,
    Proof,
    get_authenticators,
)
from .events import SteppedUp
from .mfa import InvalidCode, _limit_user, spend_code
from .models import AbstractBaseUser, UserId
from .settings import auth_settings
from .throttling import audit_refusal
from .tokens import utc_now
from .uow import AuthUnitOfWork

__all__ = [
    "router",
]

ACTION: Final = "auth.step_up"
"""The audit trail's action for refused step-ups."""

router = APIRouter(tags=["auth"])


class StepUpIn(BaseModel):
    """A code of the authenticator app, and the password where it is asked."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64, repr=False)
    password: str | None = Field(
        default=None, min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False
    )
    """Needed with ``AUTH.STEP_UP.METHODS = ("pwd", "otp")``; ignored otherwise."""


class StepUpOut(BaseModel):
    """When the credentials stepped up, and with what (OpenID Connect's names)."""

    auth_time: datetime
    amr: list[str]
    """RFC 8176's names: ``otp``, and ``pwd`` where the password was asked."""
    access_token: str | None = Field(default=None, repr=False)
    """A JSON Web Token login's new access token, which carries the step-up."""
    token_type: Literal["Bearer"] | None = None
    expires_in: int | None = None
    """Seconds the new access token works."""


class NoSecondFactor(APIError):
    """The user has no second factor to step up with (409)."""

    def __init__(self) -> None:
        """Use status 409 and the code ``no_second_factor``."""
        super().__init__(
            status.HTTP_409_CONFLICT,
            "Turn on a second factor first: confirming it is you needs one.",
            code="no_second_factor",
        )


async def verify_factors[T](
    request: Request,
    user: AbstractBaseUser,
    methods: Collection[str],
    *,
    code: str,
    password: str | None,
    action: str,
    then: Callable[[AuthUnitOfWork], Awaitable[T]],
) -> T:
    """Check ``methods`` of ``user`` again, then run ``then`` in the same commit.

    The password (``pwd``) is checked first, counted against
    ``AUTH.LOGIN_RATE`` with the user's other password checks; the code
    (``otp``, an authenticator's code, never a recovery code) against
    ``AUTH.MFA.RATE`` with the user's other code checks. Only failures use
    the counts up; each is audited under ``action``. The code is spent in
    the transaction that ``then`` writes in.

    Raises:
        NoSecondFactor: The user has no confirmed authenticator.
        APIError: 400 ``password_required`` or ``wrong_password``; 429 past
            a rate.
        InvalidCode: The code is wrong, or was used.
    """
    async with AuthUnitOfWork() as uow:
        if not await uow.mfa.enabled(user.id):
            raise NoSecondFactor
    if "pwd" in methods:
        await _check_password(request, user, password, action)
    counted = await _limit_user(request, user)
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
        spent = await spend_code(uow, stored, code, recovery=False)
        if spent is not None:
            result = await then(uow)
            await uow.commit()
    if spent is None:
        await audit_refusal(action, "invalid_code")
        raise InvalidCode
    await counted.succeeded()
    return result


@router.post(
    "/step-up",
    dependencies=[Depends(Require(IsUnscoped()))],
    response_model_exclude_none=True,
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_409_CONFLICT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def step_up(
    body: StepUpIn, request: Request, response: Response, user: AuthenticatedUser
) -> StepUpOut:
    """Authenticate again with a second factor, for routes that ask (``StepUp``)."""
    _no_store(response)
    credentials = _credentials(request)
    if credentials.scheme != JWT_SCHEME and (
        credentials.scheme not in STEPPING_SCHEMES or credentials.token_id is None
    ):
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "These credentials cannot step up: log in, then step up.",
            code="step_up_unsupported",
        )
    methods = tuple(sorted(auth_settings().STEP_UP.METHODS))
    proof = Proof(utc_now(), frozenset(methods))

    async def record(uow: AuthUnitOfWork) -> None:
        if credentials.token_id is not None and credentials.scheme != JWT_SCHEME:
            await uow.step_ups.record(
                credentials.token_id, user.id, proof.auth_time, methods
            )
        stored = await uow.users.get(UserId(user.id))
        stored.record(SteppedUp(user_id=user.id, methods=methods))

    await verify_factors(
        request,
        user,
        methods,
        code=body.code,
        password=body.password,
        action=ACTION,
        then=record,
    )
    out = StepUpOut(auth_time=proof.auth_time, amr=list(methods))
    if credentials.scheme == JWT_SCHEME:
        _issue_jwt(out, credentials, proof)
    return out


def _issue_jwt(out: StepUpOut, credentials: Credentials, proof: Proof) -> None:
    """Give ``out`` a new access token of the login, carrying ``proof``."""
    signer = next(
        each for each in get_authenticators() if isinstance(each, JWTAuthentication)
    )
    assert credentials.session is not None  # noqa: S101 - every JWT names its login.
    out.access_token = signer.encode(credentials.user, credentials.session, proof)
    out.token_type = "Bearer"  # noqa: S105 - a scheme, not a secret.
    out.expires_in = int(signer.config.ACCESS_TOKEN_TTL.total_seconds())


async def _check_password(
    request: Request, user: AbstractBaseUser, password: str | None, action: str
) -> None:
    """Check the user's password again, counted with their other password checks.

    Raises:
        APIError: 400 ``password_required`` or ``wrong_password``; 429 past
            ``AUTH.LOGIN_RATE``.
    """
    if password is None:
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "Enter your password as well.",
            code="password_required",
        )
    counted = await _throttle(request, user)
    if not await _password_holds(user, password):
        await audit_refusal(action, "wrong_password")
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "Your password was entered incorrectly.",
            code="wrong_password",
        )
    await counted.succeeded()
