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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.context import bind_request, current_request
from domainkit.http.errors import APIError, ErrorResponse
from domainkit.http.ratelimit import Backend
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
from .validators import validate_password

AUDIT = "domainkit.contrib.audit"
PASSWORD_MAX_LENGTH = 4096
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
async def login(body: LoginIn, request: Request) -> TokenOut:
    """Check a username and password; return tokens."""
    await _throttle(request, body.username)
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
async def refresh(body: RefreshIn) -> TokenOut:
    """Exchange a refresh token, once, for new tokens."""
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
    await _throttle(request, user.get_username())
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


async def _throttle(request: Request, username: str) -> None:
    """Refuse a login past ``AUTH.LOGIN_RATE`` for the address or username."""
    rate = auth_settings().LOGIN_RATE
    if rate is None:
        return
    limit, period = parse_rate(rate)
    limiter = _limiter(request)
    client = request.client.host if request.client else "unknown"
    user_key = hashlib.sha256(username.casefold().encode()).hexdigest()
    for key in (f"login:ip:{client}", f"login:user:{user_key}"):
        decision = await limiter.hit(key, limit, period)
        if not decision.allowed:
            await _audit_refusal("throttled")
            raise APIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many login attempts; try again later.",
                code="login_throttled",
                headers={"Retry-After": str(math.ceil(decision.retry_after))},
            )


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
