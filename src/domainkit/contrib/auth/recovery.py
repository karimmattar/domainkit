"""Password resets and email verification, by links sent by email.

Install ``domainkit.contrib.messaging`` and ``domainkit.contrib.mail``, and
mount the router next to the one that logs in::

    from domainkit.contrib.auth.recovery import router as recovery_router

    router.include_router(recovery_router, prefix="/auth")

- ``POST /password-reset`` (anyone) always answers 202, whether or not an
  account uses the address, and takes the same time either way: the worker
  looks the address up, and mails each active user with a usable password
  a link (``AUTH.PASSWORD_RESET_URL``).
- ``POST /password-reset/confirm`` (anyone) takes the link's token and a
  new password; every session of the user ends.
- ``POST /email-verification`` (a user, with credentials not limited to
  scopes) mails a link to the user's address.
- ``POST /email-verification/confirm`` (anyone) takes the link's token.

Requests that send mail count against ``AUTH.EMAIL_RATE`` per client
address and per email address (or user); tokens that do not hold count
against ``AUTH.LOGIN_RATE`` per client address, once checked: not per
user, and not before the check, since a signature cannot be guessed, and
a count anyone could use up with forged tokens would lock people out of
their links. Refused tokens, refusals by
the rate, resets, and verifications go to the audit trail. Links are made
in the worker, so no token is ever stored, not even in the outbox.
"""

import logging
import math
from datetime import timedelta
from typing import Annotated, Final, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from domainkit.contrib.mail import deliver, render_mail
from domainkit.contrib.messaging import enqueue, task
from domainkit.http.errors import APIError

from .access import AuthenticatedUser, IsUnscoped, Public, Require
from .api import PASSWORD_MAX_LENGTH, _errors
from .events import PasswordReset, PasswordResetRequested
from .exceptions import InvalidEmail
from .models import (
    EMAIL_MAX_LENGTH,
    AbstractBaseUser,
    AbstractUser,
    UserId,
    validate_email,
)
from .onetime import (
    TOKEN_MAX_LENGTH,
    UserTokenGenerator,
    email_of,
    email_verification_tokens,
    password_reset_tokens,
)
from .settings import TOKEN_PLACEHOLDER, auth_settings
from .throttling import address_key, audit_refusal, count_failure, email_key, throttle
from .uow import AuthUnitOfWork
from .validators import validate_password

EMAIL_PATTERN: Final = r"^[^@\s\x00-\x1f\x7f]{1,64}@[^@\s\x00-\x1f\x7f]{1,253}$"
"""The shape of an address, checked first; ``EmailAddress`` checks the rest."""
RESET_TASK: Final = "domainkit.auth.send_password_reset"
VERIFICATION_TASK: Final = "domainkit.auth.send_email_verification"

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _plain_address(value: str) -> str:
    """``validate_email``, for pydantic: a refusal is a ``ValueError``."""
    try:
        return validate_email(value)
    except InvalidEmail as exc:
        raise ValueError(exc.message) from None


EmailAddress = Annotated[
    str,
    Field(max_length=EMAIL_MAX_LENGTH, pattern=EMAIL_PATTERN),
    AfterValidator(_plain_address),
]
"""One plain address, trimmed (``validate_email``): no display name, ASCII."""


class PasswordResetIn(BaseModel):
    """The email address of the account."""

    model_config = ConfigDict(extra="forbid")

    email: EmailAddress


class PasswordResetConfirmIn(BaseModel):
    """A reset link's token, and the new password."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=TOKEN_MAX_LENGTH, repr=False)
    new_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False)


class TokenIn(BaseModel):
    """A verification link's token."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=TOKEN_MAX_LENGTH, repr=False)


class AcceptedOut(BaseModel):
    """What happens next."""

    detail: str


class InvalidLink(APIError):
    """The link's token is wrong, used, or expired: which one is not said (400)."""

    def __init__(self) -> None:
        """Use status 400 and the code ``invalid_token``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The link is invalid or has expired.",
            code="invalid_token",
        )


@router.post(
    "/password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Public],
    responses=_errors(status.HTTP_429_TOO_MANY_REQUESTS),
)
async def request_password_reset(
    body: PasswordResetIn, request: Request
) -> AcceptedOut:
    """Mail a reset link to the accounts that use the address, if any."""
    email = body.email
    await _limit_mail(request, "password-reset", email_key("password-reset", email))
    await enqueue(send_password_reset, email=email)
    return AcceptedOut(
        detail="If an account uses this address, a reset link is on its way."
    )


@router.post(
    "/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Public],
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_password_reset(
    body: PasswordResetConfirmIn, request: Request
) -> None:
    """Set a new password with a reset link's token; every session ends."""
    user_id = password_reset_tokens.user_id(body.token)
    async with AuthUnitOfWork() as uow:
        user = await _holder(uow, password_reset_tokens, user_id, body.token)
        if user is not None:
            validate_password(body.new_password, user)
            await uow.users.set_password(user, body.new_password)
            await uow.tokens.revoke_user(user.id)
            user.record(PasswordReset(user_id=user.id))
            await uow.commit()
    if user is None:
        await _count_failed_check(request, "password-reset")
        await audit_refusal("auth.password_reset", "invalid_token")
        raise InvalidLink


@router.post(
    "/email-verification",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(Require(IsUnscoped()))],
    responses=_errors(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def request_email_verification(
    request: Request, user: AuthenticatedUser
) -> AcceptedOut:
    """Mail a link that verifies the user's address (none if it is verified)."""
    await _limit_mail(
        request, "email-verification", f"email-verification:user:{user.id}"
    )
    await enqueue(send_email_verification, user_id=user.id)
    return AcceptedOut(detail="A link to verify your address is on its way.")


@router.post(
    "/email-verification/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Public],
    responses=_errors(status.HTTP_400_BAD_REQUEST, status.HTTP_429_TOO_MANY_REQUESTS),
)
async def confirm_email_verification(body: TokenIn, request: Request) -> None:
    """Mark the user's address verified with a link's token."""
    user_id = email_verification_tokens.user_id(body.token)
    async with AuthUnitOfWork() as uow:
        user = await _holder(uow, email_verification_tokens, user_id, body.token)
        if user is not None:
            # Startup checks that the user model can (AbstractUser can).
            cast(AbstractUser, user).verify_email()
            await uow.commit()
    if user is None:
        await _count_failed_check(request, "email-verification")
        await audit_refusal("auth.email_verification", "invalid_token")
        raise InvalidLink


async def send_password_reset(email: str) -> None:
    """Mail a reset link to each active user of ``email`` who has a password.

    The ``domainkit.auth.send_password_reset`` task. Nobody uses the
    address: nothing is sent. A user whose stored address cannot take mail
    (from before addresses were checked) is skipped, and logged by id, so
    the others still get their links.
    """
    config = auth_settings()
    async with AuthUnitOfWork() as uow:
        messages = []
        for user in await uow.users.find_by_email(email):
            address = email_of(user)
            if address is None or not (user.is_active and user.has_usable_password()):
                continue
            if not _mailable(user, address):
                continue
            token = password_reset_tokens.make_token(user)
            context = _context(
                user, config.PASSWORD_RESET_URL, token, config.PASSWORD_RESET_TIMEOUT
            )
            messages.append(render_mail("auth/password_reset", context, to=[address]))
            user.record(PasswordResetRequested(user_id=user.id))
        await uow.commit()
    if messages:
        await deliver(messages)


async def send_email_verification(user_id: UUID) -> None:
    """Mail a verification link to the user's address, unless it is verified.

    The ``domainkit.auth.send_email_verification`` task.
    """
    config = auth_settings()
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
    if user is None or not user.is_active or getattr(user, "email_verified", True):
        return
    address = email_of(user)
    if address is None or not _mailable(user, address):
        return
    token = email_verification_tokens.make_token(user)
    context = _context(
        user, config.EMAIL_VERIFICATION_URL, token, config.EMAIL_VERIFICATION_TIMEOUT
    )
    await deliver([render_mail("auth/email_verification", context, to=[address])])


def _mailable(user: AbstractBaseUser, address: str) -> bool:
    """Whether ``address`` can take mail; a warning names the user if not.

    The warning gives the user's id, never the address: logs keep no
    personal data.
    """
    try:
        validate_email(address)
    except InvalidEmail:
        logger.warning("User %s has an address that cannot take mail.", user.id)
        return False
    return True


def register_tasks() -> None:
    """Register the tasks that send the links (with messaging installed)."""
    task(name=RESET_TASK)(send_password_reset)
    task(name=VERIFICATION_TASK)(send_email_verification)


async def _limit_mail(request: Request, scope: str, recipient: str) -> None:
    """Count a request that sends mail, per client address and per recipient."""
    await throttle(
        request,
        auth_settings().EMAIL_RATE,
        [address_key(request, scope), recipient],
        action=f"auth.{scope.replace('-', '_')}",
        detail="Too many requests for mail; try again later.",
        code="throttled",
    )


async def _count_failed_check(request: Request, scope: str) -> None:
    """Count a token that did not hold, per client address (``LOGIN_RATE``).

    Raises:
        APIError: 429, past the rate.
    """
    await count_failure(
        request,
        auth_settings().LOGIN_RATE,
        address_key(request, f"{scope}-confirm"),
        action=f"auth.{scope.replace('-', '_')}",
        detail="Too many attempts; try again later.",
        code="throttled",
    )


async def _holder(
    uow: AuthUnitOfWork, tokens: UserTokenGenerator, user_id: UserId | None, token: str
) -> AbstractBaseUser | None:
    """The user ``token`` holds for, or None, checked in the same time either way."""
    user = None if user_id is None else await uow.users.find(user_id)
    return user if tokens.check_token(user, token) else None


def _context(
    user: AbstractBaseUser, url: str | None, token: str, valid_for: timedelta
) -> dict[str, object]:
    """What the link templates are given."""
    link = (
        token if url is None else url.replace(TOKEN_PLACEHOLDER, quote(token, safe=""))
    )
    return {
        "user": user,
        "username": user.get_username(),
        "link": link,
        "valid_for": describe(valid_for),
    }


def describe(duration: timedelta) -> str:
    """``duration`` in words: ``3 days``, ``1 hour``, or ``90 minutes``."""
    seconds = math.ceil(duration.total_seconds())
    if seconds % 86400 == 0:
        count, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        count, unit = seconds // 3600, "hour"
    else:
        count, unit = math.ceil(seconds / 60), "minute"
    return f"{count} {unit}{'' if count == 1 else 's'}"
