"""Endpoints to log in and out, refresh tokens, and change passwords.

Mount the router where the API keeps them, ``/auth`` for example::

    from domainkit.contrib.auth.api import router as auth_router

    router.include_router(auth_router, prefix="/auth")

``POST /login`` checks a username and password with ``AUTH.BACKENDS`` and
returns tokens from the first ``AUTH.AUTHENTICATION_CLASSES`` class, in
OAuth 2's token response format. Attempts are limited to
``AUTH.LOGIN_RATE`` per client address and per username, and refusals go
to the audit trail when ``domainkit.contrib.audit`` is installed.
"""

import hashlib
import math
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.context import bind_request, current_request
from domainkit.http.errors import APIError, ErrorResponse
from domainkit.http.ratelimit import Backend, client_group
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

from .access import AuthenticatedUser, Public
from .authentication import Credentials, IssuedTokens, get_authenticators
from .backends import authenticate, get_all_permissions
from .events import UserLoggedOut
from .hashers import acheck_password
from .models import AbstractBaseUser, UserId
from .settings import auth_settings
from .uow import AuthUnitOfWork
from .users import get_user_model
from .validators import validate_password

AUDIT = "domainkit.contrib.audit"
PASSWORD_MAX_LENGTH = 4096
MAX_REFUSALS = 10_000
"""Throttled keys remembered, to audit each once per period."""
"""Longer passwords are refused before hashing, which they would slow down."""

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    """A username (the user model's ``USERNAME_FIELD``) and its password."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class RefreshIn(BaseModel):
    """A refresh token from logging in or the last refresh."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1, max_length=255)


class LogoutIn(BaseModel):
    """Whether to end every session of the user, not only this one."""

    model_config = ConfigDict(extra="forbid")

    everywhere: bool = False


class PasswordIn(BaseModel):
    """The current password, and the new one."""

    model_config = ConfigDict(extra="forbid")

    old_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class TokenOut(BaseModel):
    """Tokens (OAuth 2's token response): send ``Authorization: Bearer ...``."""

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - a scheme, not a secret.
    expires_in: int | None = None
    """Seconds the access token works; absent: until revoked."""
    refresh_token: str | None = None
    """Exchange it at ``/refresh`` before the access token expires."""


class UserOut(BaseModel):
    """Who is logged in, and what they may do."""

    id: UUID
    username: str
    email: str | None
    is_staff: bool
    is_superuser: bool
    last_login: datetime | None
    groups: list[str]
    permissions: list[str]


def _errors(*codes: int) -> dict[int | str, dict[str, object]]:
    """OpenAPI entries for the error statuses an endpoint returns."""
    return {code: {"model": ErrorResponse} for code in codes}


@router.post(
    "/login",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses=_errors(status.HTTP_401_UNAUTHORIZED, status.HTTP_429_TOO_MANY_REQUESTS),
)
async def login(body: LoginIn, request: Request, response: Response) -> TokenOut:
    """Check a username and password; return tokens."""
    _no_store(response)
    await _throttle(request, *_login_keys(request, body.username))
    user = await authenticate(username=body.username, password=body.password)
    if user is None:
        await _audit_refusal("invalid_credentials")
        raise APIError(
            status.HTTP_401_UNAUTHORIZED,
            "Unable to log in with the given credentials.",
            code="invalid_credentials",
        )
    with _acting_as(user):
        async with AuthUnitOfWork() as uow:
            stored = await uow.users.get(UserId(user.id))
            tokens = await get_authenticators()[0].issue(uow, stored)
            await uow.users.record_login(stored)
            await uow.commit()
    return _token_out(tokens)


@router.post(
    "/refresh",
    dependencies=[Public],
    response_model_exclude_none=True,
    responses=_errors(status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED),
)
async def refresh(body: RefreshIn, response: Response) -> TokenOut:
    """Exchange a refresh token, once, for new tokens."""
    _no_store(response)
    async with AuthUnitOfWork() as uow:
        tokens = await get_authenticators()[0].refresh(uow, body.refresh_token)
        await uow.commit()
    return _token_out(tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(status.HTTP_401_UNAUTHORIZED),
)
async def logout(
    request: Request,
    user: AuthenticatedUser,
    body: Annotated[LogoutIn | None, Body()] = None,
) -> None:
    """End this session's tokens, or every token of the user."""
    everywhere = body is not None and body.everywhere
    credentials = _credentials(request)
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
        if everywhere:
            await uow.tokens.revoke_user(user.id)
        elif credentials.token_id is not None:
            await uow.tokens.revoke(credentials.token_id)
        elif credentials.session is not None:
            await uow.tokens.revoke_family(credentials.session)
        stored.record(UserLoggedOut(user_id=user.id, everywhere=everywhere))
        await uow.commit()


@router.get("/me", responses=_errors(status.HTTP_401_UNAUTHORIZED))
async def me(user: AuthenticatedUser) -> UserOut:
    """The user who sent the request."""
    field = user.EMAIL_FIELD
    email = getattr(user, field, None) if field else None
    return UserOut(
        id=user.id,
        username=user.get_username(),
        email=email if isinstance(email, str) else None,
        is_staff=getattr(user, "is_staff", False) is True,
        is_superuser=user.is_superuser,
        last_login=user.last_login,
        groups=sorted(group.name for group in user.groups),
        permissions=sorted(await get_all_permissions(user)),
    )


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def change_password(
    request: Request, body: PasswordIn, user: AuthenticatedUser
) -> None:
    """Change the password; every other session of the user ends."""
    await _throttle(request, f"password:user:{user.id}")
    if not await acheck_password(body.old_password, user.password.value):
        raise APIError(
            status.HTTP_400_BAD_REQUEST,
            "Your old password was entered incorrectly.",
            code="wrong_password",
        )
    validate_password(body.new_password, user)
    credentials = _credentials(request)
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
        await uow.users.set_password(stored, body.new_password)
        await uow.tokens.revoke_user(
            user.id, keep=credentials.token_id, keep_family=credentials.session
        )
        await uow.commit()


async def _throttle(request: Request, *keys: str) -> None:
    """Refuse a password check past ``AUTH.LOGIN_RATE`` for any of ``keys``.

    The first refusal of each key in a period is audited, not every one, so
    a flood of attempts writes no rows to the append-only trail.
    """
    rate = auth_settings().LOGIN_RATE
    if rate is None:
        return
    limit, period = parse_rate(rate)
    limiter = _limiter(request)
    for key in keys:
        decision = await limiter.hit(key, limit, period)
        if not decision.allowed:
            if _first_refusal(request, key, period):
                await _audit_refusal("throttled")
            raise APIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many login attempts; try again later.",
                code="login_throttled",
                headers={"Retry-After": str(math.ceil(decision.retry_after))},
            )


def _no_store(response: Response) -> None:
    """Keep tokens out of every cache, as OAuth 2 asks (RFC 6749, 5.1).

    ``IdempotencyMiddleware`` does not store such responses either.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _login_keys(request: Request, username: str) -> tuple[str, str]:
    """The counts a login is checked against: its address's and its account's."""
    client = client_group(request.client.host) if request.client else "unknown"
    return f"login:ip:{client}", f"login:user:{_account(username)}"


def _account(username: str) -> str:
    """The key of the account ``username`` names, as logging in finds it.

    Names the lookup treats as one (``alice`` and its full-width or
    mathematical look-alikes, under NFKC) share one key, in any case.
    """
    name = get_user_model().normalize_username(username)
    folded = unicodedata.normalize("NFKC", name.casefold())
    return hashlib.sha256(folded.encode()).hexdigest()


def _first_refusal(request: Request, key: str, period: int) -> bool:
    """Whether ``key`` was refused for the first time in ``period`` here."""
    state = request.app.state
    refused: OrderedDict[str, float] | None = getattr(state, "login_refusals", None)
    if refused is None:
        refused = state.login_refusals = OrderedDict()
    now = time.monotonic()
    since = refused.get(key)
    if since is not None and now - since < period:
        return False
    refused[key] = now
    refused.move_to_end(key)
    if len(refused) > MAX_REFUSALS:
        refused.popitem(last=False)
    return True


def _limiter(request: Request) -> Backend:
    """The application's login counter (``RATE_LIMIT_BACKEND``), made once."""
    state = request.app.state
    limiter: Backend | None = getattr(state, "login_limiter", None)
    if limiter is None:
        limiter = import_string(settings.RATE_LIMIT_BACKEND)()
        state.login_limiter = limiter
    return limiter


async def _audit_refusal(reason: str) -> None:
    """Record a refused login, without the username: it may be a password."""
    if registry.is_installed(AUDIT):
        from domainkit.contrib.audit import Outcome, record

        await record("auth.login", Outcome.DENIED, reason=reason)


@contextmanager
def _acting_as(user: AbstractBaseUser) -> Iterator[None]:
    """Attribute what follows (the login's events) to ``user``."""
    context = current_request()
    if context is None:
        yield
        return
    with bind_request(replace(context, actor=str(user.id))):
        yield


def _credentials(request: Request) -> Credentials:
    """The credentials of an authenticated request."""
    credentials: Credentials = request.scope["auth"]
    return credentials


def _token_out(tokens: IssuedTokens) -> TokenOut:
    """The response body for issued tokens."""
    return TokenOut(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        refresh_token=tokens.refresh_token,
    )
