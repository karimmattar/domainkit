"""What auth's routes share: the password check's throttle, and small helpers.

Internal to ``domainkit.contrib.auth``: its route modules (``api``,
``cookies``, ``mfa``, ``stepup``, ``sca``, ``personal_tokens``,
``recovery``, ``signup``) import these from here, not from one another.
Other apps use ``auth.integration``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID

from fastapi import Request, Response

from domainkit.context import bind_request, current_request

from .authentication import Credentials
from .models import AbstractBaseUser
from .settings import auth_settings
from .throttling import Attempt, Limit, Window, account_key, attempt
from .tokens import logging_in

LOGIN_THROTTLED = "Too many login attempts; try again later."
"""The detail of a throttled login."""
PASSWORD_MAX_LENGTH = 4096
"""Longer passwords are refused before hashing, which they would slow down."""
DAILY = "login-daily"
"""The account's count of wrong passwords against ``ACCOUNT_LOGIN_RATE``."""


async def password_throttle(request: Request, user: AbstractBaseUser) -> Attempt:
    """Refuse a signed-in user's password check past ``AUTH.LOGIN_RATE``.

    Counted per user, at ``LOGIN_RATE`` and over each day at
    ``ACCOUNT_LOGIN_RATE`` (SEC-16a-4): a session holds no more guesses at
    the password than a stranger does. With
    ``AUTH.PASSWORD_CHECKS_COUNT_AS_LOGINS`` (the PCI preset's), against the
    account's count of wrong logins too, so the rate bounds every guess at
    the password, from wherever it comes. The
    first refusal of each key in a period is audited, not every one, so a
    flood of attempts writes no rows to the append-only trail. Give the
    result ``succeeded()`` once the password holds: only wrong ones count.
    """
    config = auth_settings()
    daily = daily_window(config.ACCOUNT_LOGIN_RATE)
    limits: list[Limit] = [
        (f"password:user:{user.id}", config.LOGIN_RATE),
        (f"password-daily:user:{user.id}", daily),
    ]
    if config.PASSWORD_CHECKS_COUNT_AS_LOGINS:
        username = user.get_username()
        limits += [
            (account_key("login", username), config.LOGIN_RATE),
            (account_key(DAILY, username), daily),
        ]
    return await attempt(
        request,
        limits,
        action="auth.login",
        detail=LOGIN_THROTTLED,
    )


def daily_window(rate: str | None) -> Window | None:
    """``ACCOUNT_LOGIN_RATE`` as a window, which no day passes (SEC-16a-2)."""
    return None if rate is None else Window(rate)


def no_store(response: Response) -> None:
    """Keep tokens out of every cache, as OAuth 2 asks (RFC 6749, 5.1).

    ``IdempotencyMiddleware`` does not store such responses either.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def credentials_of(request: Request) -> Credentials:
    """The credentials of an authenticated request."""
    credentials: Credentials = request.scope["auth"]
    return credentials


@contextmanager
def made_by(method: str | None, device_id: UUID | None) -> Iterator[None]:
    """Record the logins issued inside as made by ``method``, if one is given."""
    if method is None:
        yield
        return
    with logging_in(method, device_id):
        yield


@contextmanager
def acting_as(user: AbstractBaseUser) -> Iterator[None]:
    """Attribute what follows (the login's events) to ``user``."""
    context = current_request()
    if context is None:
        yield
        return
    with bind_request(replace(context, actor=str(user.id))):
        yield
