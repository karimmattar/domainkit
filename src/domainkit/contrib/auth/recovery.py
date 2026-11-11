"""Password resets, and email and mobile verification, by codes.

Install ``domainkit.contrib.messaging`` and ``domainkit.contrib.mail``, and
mount the router next to the one that logs in::

    from domainkit.contrib.auth.recovery import router as recovery_router

    router.include_router(recovery_router, prefix="/auth")

A code is 6 digits, works for ``AUTH.CODE_TTL``, and is spent after
``AUTH.CODE_ATTEMPTS`` wrong tries (ADR 0126). Codes are made in the
worker, so no code is ever in the outbox. Since 2.0, codes are the only
way: the links of 1.x are gone.

- ``POST /password-reset`` (anyone) always answers 202, whether or not an
  account uses the value, and takes the same time either way. It takes an
  ``identifier`` (an email address, a mobile number, or a username; or
  ``email``), and the worker sends a code to the user's address, or by
  text message to their number. ``POST /password-reset/code`` takes the
  identifier, the code, and a new password; every session of the user
  ends.
- ``POST /email-verification`` (a user, with credentials not limited to
  scopes) mails a code to the user's address; ``POST
  /email-verification/code`` takes it.
- ``POST /mobile-verification`` (a user) texts a code to the user's
  ``mobile_number``; ``POST /mobile-verification/code`` takes it. Texts
  need ``domainkit.contrib.notifications``.

Requests that send a code count against ``AUTH.EMAIL_RATE`` per client
address and per recipient (or user); wrong codes count against
``AUTH.LOGIN_RATE`` per client address. Refusals, refusals by the rate,
resets, and verifications go to the audit trail.
"""

import logging
import math
from datetime import timedelta
from typing import Annotated, Final, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from domainkit.contrib.messaging import enqueue, task
from domainkit.http.access import IsUnscoped, Public, Require
from domainkit.http.errors import APIError, error_responses

from ._http import PASSWORD_MAX_LENGTH
from .access import AuthenticatedUser
from .api import device_logins_kept
from .captcha import captcha
from .codes import EMAIL_VERIFICATION, MOBILE_VERIFICATION, PASSWORD_RESET, target_of
from .events import PasswordReset, PasswordResetRequested
from .exceptions import InvalidEmail
from .gates import check_manage_logins
from .models import (
    EMAIL_MAX_LENGTH,
    AbstractBaseUser,
    AbstractUser,
    UserId,
    as_mobile_number,
    same_email,
    validate_email,
)
from .onetime import email_of
from .settings import auth_settings
from .throttling import address_key, audit_refusal, count_failure, email_key, throttle
from .uow import AuthUnitOfWork
from .validators import validate_password

__all__ = [
    "router",
]

EMAIL_PATTERN: Final = r"^[^@\s\x00-\x1f\x7f]{1,64}@[^@\s\x00-\x1f\x7f]{1,253}$"
"""The shape of an address, checked first; ``EmailAddress`` checks the rest."""
RESET_CODE_TASK: Final = "domainkit.auth.send_password_reset_code"
MOBILE_VERIFICATION_TASK: Final = "domainkit.auth.send_mobile_verification"
NOTIFICATIONS: Final = "domainkit.contrib.notifications"
IDENTIFIER_MAX_LENGTH: Final = 255
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
    """What names the account: an ``identifier``, or its ``email``."""

    model_config = ConfigDict(extra="forbid")

    email: EmailAddress | None = None
    identifier: str | None = Field(
        default=None, min_length=1, max_length=IDENTIFIER_MAX_LENGTH
    )
    """An email address, a mobile number, or a username."""


class PasswordResetCodeIn(BaseModel):
    """The identifier the code was sent for, the code, and the new password."""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=IDENTIFIER_MAX_LENGTH)
    code: str = Field(min_length=1, max_length=32, repr=False)
    new_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False)
    keep_device_logins: bool | None = None
    """With ``AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS = "ask"``: keep the logins of
    the user's bound devices (true, the default), or end them too."""


class CodeIn(BaseModel):
    """A code that was sent: 6 digits."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32, repr=False)


class AcceptedOut(BaseModel):
    """What happens next."""

    detail: str


class InvalidCode(APIError):
    """The code is wrong, used, or expired: which one is not said (400)."""

    def __init__(self) -> None:
        """Use status 400 and the code ``invalid_verification_code``."""
        super().__init__(
            status.HTTP_400_BAD_REQUEST,
            "The code is wrong or has expired; ask for a new one.",
            code="invalid_verification_code",
        )


@router.post(
    "/password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Public, captcha("password_reset")],
    responses=error_responses(status.HTTP_429_TOO_MANY_REQUESTS),
)
async def request_password_reset(
    body: PasswordResetIn, request: Request
) -> AcceptedOut:
    """Send a code to the one account the value names, if any.

    By mail, or by text message for a mobile number.

    Raises:
        APIError: 422 ``missing_value`` without ``email`` or ``identifier``.
    """
    value = body.identifier or body.email
    if value is None:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Give an email address, or an identifier.",
            code="missing_value",
        )
    value = value.strip()
    recipient = as_mobile_number(value) or value
    await _limit_mail(request, "password-reset", email_key("password-reset", recipient))
    await enqueue(send_password_reset_code, identifier=value)
    return AcceptedOut(detail="If an account uses it, a code is on its way.")


@router.post(
    "/password-reset/code",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Public],
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_password_reset_code(
    body: PasswordResetCodeIn, request: Request
) -> None:
    """Set a new password with the code sent for ``identifier``; sessions end.

    A wrong code, and one for an identifier no account uses, get the same
    answer. A password the validators refuse leaves the code working. The
    logins of the user's bound devices end too, or stay, as
    ``AUTH.CREDENTIAL_CHANGE_DEVICE_LOGINS`` says (ADR 0131).
    """
    keep_devices = device_logins_kept(body.keep_device_logins)
    if body.keep_device_logins is False:
        # Ending the devices' logins is managing logins: checked once the
        # code names the user, in a transaction that spends nothing.
        async with AuthUnitOfWork() as uow:
            named = await _proven_user(uow, body)
        if named is not None:
            await check_manage_logins(request, named)
    user = None
    async with AuthUnitOfWork() as uow:
        user = await _proven_user(uow, body)
        if user is not None:
            validate_password(body.new_password, user)  # Raises: nothing is spent.
            await uow.users.set_password(user, body.new_password)
            await uow.tokens.revoke_user(user.id, keep_devices=keep_devices)
            _prove_reached(user, body.identifier)
            user.record(PasswordReset(user_id=user.id))
        await uow.commit()  # A wrong try stays counted.
    if user is None:
        await _count_failed_check(request, "password-reset")
        await audit_refusal("auth.password_reset", "invalid_verification_code")
        raise InvalidCode


def _prove_reached(user: AbstractBaseUser, identifier: str) -> None:
    """Mark proved the address or the number a reset code reached.

    The code went to the user's number when ``identifier`` is it, and to
    their address when ``identifier`` is it: whoever used it reads there
    (ADR 0133). A code asked for by username went to an address that may
    have changed since: nothing is marked.
    """
    number = as_mobile_number(identifier)
    if number is not None and getattr(user, "mobile_number", None) == number:
        verify = getattr(user, "verify_mobile_number", None)
    elif "@" in identifier and same_email(email_of(user) or "", identifier):
        verify = getattr(user, "verify_email", None)
    else:
        return
    if verify is not None:
        verify()


async def _proven_user(
    uow: AuthUnitOfWork, body: PasswordResetCodeIn
) -> AbstractBaseUser | None:
    """The active user ``body``'s code was sent for, if the code holds."""
    proven = await uow.verification_codes.check(
        PASSWORD_RESET, target_of(body.identifier), body.code
    )
    if proven is None or proven.user_id is None:
        return None
    user = await uow.users.find(UserId(proven.user_id))
    return user if user is not None and user.is_active else None


@router.post(
    "/mobile-verification",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(Require(IsUnscoped()))],
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def request_mobile_verification(
    request: Request, user: AuthenticatedUser
) -> AcceptedOut:
    """Text a code that verifies the user's mobile number (none if verified)."""
    await _limit_mail(
        request, "mobile-verification", f"mobile-verification:user:{user.id}"
    )
    await enqueue(send_mobile_verification, user_id=user.id)
    return AcceptedOut(detail="A code to verify your number is on its way.")


@router.post(
    "/mobile-verification/code",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(Require(IsUnscoped()))],
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_mobile_verification_code(
    body: CodeIn, request: Request, user: AuthenticatedUser
) -> None:
    """Mark the user's mobile number verified with the code texted to it."""
    verified = False
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
        number = getattr(stored, "mobile_number", None)
        if number:
            target = target_of(number)
            proven = await uow.verification_codes.check(
                MOBILE_VERIFICATION, target, body.code
            )
            if proven is not None and proven.user_id == stored.id:
                cast(AbstractUser, stored).verify_mobile_number()
                verified = True
        await uow.commit()  # A wrong try stays counted.
    if not verified:
        await _count_failed_check(request, "mobile-verification")
        await audit_refusal("auth.mobile_verification", "invalid_verification_code")
        raise InvalidCode


@router.post(
    "/email-verification",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(Require(IsUnscoped()))],
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def request_email_verification(
    request: Request, user: AuthenticatedUser
) -> AcceptedOut:
    """Mail a code that verifies the user's address.

    Nothing goes to an address that is verified already.
    """
    await _limit_mail(
        request, "email-verification", f"email-verification:user:{user.id}"
    )
    await enqueue(send_email_verification, user_id=user.id)
    return AcceptedOut(detail="A code to verify your address is on its way.")


@router.post(
    "/email-verification/code",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(Require(IsUnscoped()))],
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def confirm_email_verification_code(
    body: CodeIn, request: Request, user: AuthenticatedUser
) -> None:
    """Mark the user's address verified with the code mailed to it.

    The code holds for the address it went to: once the address changes,
    it no longer works.
    """
    async with AuthUnitOfWork() as uow:
        stored = await uow.users.get(UserId(user.id))
        address = email_of(stored)
        proven = None
        if address is not None:
            target = target_of(address)
            proven = await uow.verification_codes.check(
                EMAIL_VERIFICATION, target, body.code
            )
        if proven is not None and proven.user_id == stored.id:
            # Startup checks that the user model can (AbstractUser can).
            cast(AbstractUser, stored).verify_email()
        else:
            proven = None
        await uow.commit()  # A wrong try stays counted.
    if proven is None:
        await _count_failed_check(request, "email-verification")
        await audit_refusal("auth.email_verification", "invalid_verification_code")
        raise InvalidCode


async def send_password_reset_code(identifier: str) -> None:
    """Send a reset code to the account ``identifier`` names, if any.

    The ``domainkit.auth.send_password_reset_code`` task. The account must
    be active, with a usable password. An email address names the one
    account that has it, even where addresses do not log in. A mobile number gets a text
    message; an email address, or any other identifier, a mail to the
    account's address. Nothing goes where nothing can be sent.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find_by_identifier(identifier)
        if user is None and "@" in identifier:  # By any address, as 1.x's links.
            found = await uow.users.find_by_email(identifier)
            user = found[0] if len(found) == 1 else None
        if user is None or not (user.is_active and user.has_usable_password()):
            return
        number = as_mobile_number(identifier)
        address = email_of(user)
        texted = number is not None and getattr(user, "mobile_number", None) == number
        if not texted and (address is None or not _mailable(user, address)):
            return  # Texts can go: auth.E107 needs notifications for numbers.
        target = target_of(identifier)
        code = await uow.verification_codes.issue(
            PASSWORD_RESET, target, user_id=user.id
        )
        user.record(PasswordResetRequested(user_id=user.id))
        if texted:
            await text_code(cast(str, number), code, "reset your password", uow)
        await uow.commit()
    if not texted:
        await _mail_code(user, cast(str, address), code, "auth/password_reset_code")


async def send_code(
    user: AbstractBaseUser,
    identifier: str,
    *,
    purpose: str,
    wording: str,
    template: str,
) -> bool:
    """Issue a code for ``purpose`` and send it where ``identifier`` reaches ``user``.

    A mobile number of the user's gets a text message, with ``wording``
    ("reset your password"), through the outbox; anything else, a mail to
    the user's address from ``template`` (given ``username``, ``code``,
    ``valid_for``). The code is kept for ``identifier``: check it with
    ``uow.verification_codes.check(purpose, target_of(identifier), code)``.
    False, and nothing issued, where nothing can be sent. For apps built
    on auth, from a task (``integration``).
    """
    number = as_mobile_number(identifier)
    texted = number is not None and getattr(user, "mobile_number", None) == number
    address = None if texted else email_of(user)
    if not texted and (address is None or not _mailable(user, address)):
        return False
    async with AuthUnitOfWork() as uow:
        code = await uow.verification_codes.issue(
            purpose, target_of(identifier), user_id=user.id
        )
        if texted:
            await text_code(cast(str, number), code, wording, uow)
        await uow.commit()
    if not texted:
        await _mail_code(user, cast(str, address), code, template)
    return True


async def send_mobile_verification(user_id: UUID) -> None:
    """Text a verification code to the user's mobile number, unless verified.

    The ``domainkit.auth.send_mobile_verification`` task.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
        if user is None or not user.is_active:
            return
        number = getattr(user, "mobile_number", None)
        if not number or getattr(user, "mobile_number_verified", True):
            return
        if not can_text(f"User {user.id}"):
            return
        code = await uow.verification_codes.issue(
            MOBILE_VERIFICATION, target_of(number), user_id=user.id
        )
        await text_code(number, code, "verify your number", uow)
        await uow.commit()


def can_text(label: str) -> bool:
    """Whether text messages can go out; a warning names ``label`` if not.

    ``label`` names who waits for the code, by id: never the number.
    """
    from domainkit.apps import registry

    if registry.is_installed(NOTIFICATIONS):
        return True
    logger.warning(
        "%s has a code to text, and %s is not installed.", label, NOTIFICATIONS
    )
    return False


async def text_code(number: str, code: str, purpose: str, uow: AuthUnitOfWork) -> None:
    """Queue a text message with ``code``; it leaves with ``uow``'s commit."""
    from domainkit.contrib.notifications import send_sms

    valid_for = describe(auth_settings().CODE_TTL)
    body = (
        f"{code} is your code to {purpose}. It works for {valid_for}. "
        "Never give it to anyone."
    )
    await send_sms(number, body, uow=uow)


async def _mail_code(
    user: AbstractBaseUser, address: str, code: str, template: str
) -> None:
    """Mail ``code`` to ``address`` with ``template``."""
    from domainkit.contrib.mail import deliver, render_mail

    context = code_context(code, user)
    await deliver([render_mail(template, context, to=[address])])


async def send_email_verification(user_id: UUID) -> None:
    """Mail a verification code to the user's address.

    The ``domainkit.auth.send_email_verification`` task. Nothing goes to an
    address that is verified already. A new code replaces the one before.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
    if user is None or not user.is_active or getattr(user, "email_verified", True):
        return
    address = email_of(user)
    if address is None or not _mailable(user, address):
        return
    async with AuthUnitOfWork() as uow:
        code = await uow.verification_codes.issue(
            EMAIL_VERIFICATION, target_of(address), user_id=user.id
        )
        await uow.commit()
    await _mail_code(user, address, code, "auth/email_verification_code")


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
    """Register the tasks that send the codes (with messaging installed)."""
    task(name=VERIFICATION_TASK)(send_email_verification)
    task(name=RESET_CODE_TASK)(send_password_reset_code)
    task(name=MOBILE_VERIFICATION_TASK)(send_mobile_verification)


async def _limit_mail(request: Request, scope: str, recipient: str) -> None:
    """Count a request that sends mail, per client address and per recipient."""
    await throttle(
        request,
        auth_settings().EMAIL_RATE,
        [address_key(request, scope), recipient],
        action=f"auth.{scope.replace('-', '_')}",
        detail="Too many requests for mail; try again later.",
    )


async def _count_failed_check(request: Request, scope: str) -> None:
    """Count a code that did not hold, per client address (``LOGIN_RATE``).

    Raises:
        APIError: 429, past the rate.
    """
    await count_failure(
        request,
        auth_settings().LOGIN_RATE,
        address_key(request, f"{scope}-confirm"),
        action=f"auth.{scope.replace('-', '_')}",
        detail="Too many attempts; try again later.",
    )


def code_context(code: str, user: AbstractBaseUser | None = None) -> dict[str, object]:
    """What the code templates are given (no user yet for a signup)."""
    return {
        "user": user,
        "username": "" if user is None else user.get_username(),
        "code": code,
        "valid_for": describe(auth_settings().CODE_TTL),
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
