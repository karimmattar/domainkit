"""Signing up: anyone may ask for an account, if ``AUTH.SIGNUP_ENABLED`` says so.

Off by default: the routes answer 404 until it is on. Install messaging
and mail, and mount the router::

    from domainkit.contrib.auth.signup import router as signup_router

    router.include_router(signup_router, prefix="/auth")

``POST /signup`` (anyone) takes an email address (or, where numbers
identify users, a ``mobile_number`` alone), a username where the user
model wants one, and no password; a user model's other
``REQUIRED_FIELDS`` (``signup_fields()``) come under ``fields``, each
checked by its type and markers as any change to a user is. The fields
are checked at once: a refusal there says nothing about other accounts.
Then the request is kept as a pending signup, and it answers 202, doing
the same work whatever accounts exist. The worker sends a code of 6
digits (ADR 0126) to the address, or by text message to the number, or,
when the username, the address, or the number is taken, word that it
is. ``POST /signup/code`` (anyone) takes the address or the number, the
code, and a password, checked by ``AUTH.PASSWORD_VALIDATORS``, and makes
the account, its address or number verified.

Whoever reads the mail chooses the password, so whoever asked with
someone else's address gains nothing: the account made is the address
owner's, with a password only they know (no pre-account hijacking). No
account exists until then, so nothing anyone else can see differs
between an address that has an account and one that has none: whether
it has one is told only to whoever reads its mail. A pending signup
reserves nothing: several may name one username, and the first confirmed
gets it; the others' codes then answer 409. A username taken is told to
whoever gives their own address, as any signup must. Attempts count
against ``AUTH.EMAIL_RATE`` per client address and per address or
number, wrong codes against ``AUTH.LOGIN_RATE`` per client address.
Pending signups (a username and an address, no password) are kept in a
table of their own, never in fixtures, and deleted once used, when the
address is taken, or hourly once their code has expired. Since 2.0,
codes are the only way: the signup links of 1.x are gone.
"""

from typing import Final, cast
from uuid import UUID

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.contrib.messaging import task
from domainkit.http.access import Public
from domainkit.http.errors import APIError

from .api import PASSWORD_MAX_LENGTH, _errors
from .captcha import captcha
from .codes import SIGNUP, target_of
from .events import UserSignedUp
from .exceptions import InvalidUserField, UnknownUserField
from .hashers import amake_password
from .models import (
    USERNAME_MAX_LENGTH,
    AbstractBaseUser,
    AbstractUser,
    PasswordHash,
    normalize_mobile_number,
)
from .recovery import (
    IDENTIFIER_MAX_LENGTH,
    AcceptedOut,
    EmailAddress,
    InvalidCode,
    code_context,
    text_code,
)
from .repositories import PendingSignup
from .settings import AuthSettings, auth_settings, identifier_fields
from .throttling import (
    address_key,
    audit_refusal,
    count_failure,
    email_key,
    throttle,
)
from .uow import AuthUnitOfWork
from .users import FieldValue, get_user_model, typed_fields
from .validators import validate_password

__all__ = [
    "router",
]

SIGNUP_CODE_TASK: Final = "domainkit.auth.send_signup_code"
MOBILE_NUMBER: Final = "mobile_number"

router = APIRouter(tags=["auth"])


class SignupIn(BaseModel):
    """The account to ask for: its email address, or its mobile number.

    No password: whoever reads the code chooses it (``SignupCodeIn``).
    """

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(
        default=None, min_length=1, max_length=USERNAME_MAX_LENGTH
    )
    """Where the user model wants one, or the project lets people choose."""
    email: EmailAddress | None = None
    mobile_number: str | None = Field(default=None, min_length=1, max_length=32)
    """Where ``mobile_number`` identifies users: the code goes to it when no
    email address is given."""
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    """The user model's other ``REQUIRED_FIELDS`` (``signup_fields()``)."""


class SignupCodeIn(BaseModel):
    """The address or number the code went to, the code, and the password."""

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=IDENTIFIER_MAX_LENGTH)
    code: str = Field(min_length=1, max_length=32, repr=False)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False)


class SignupTaken(APIError):
    """The username or the address got an account before the code was used (409)."""

    def __init__(self) -> None:
        """Use status 409 and the code ``signup_taken``."""
        super().__init__(
            status.HTTP_409_CONFLICT,
            "An account already uses this username or this address; "
            "sign up again with another username, or log in.",
            code="signup_taken",
        )


@router.post(
    "/signup",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Public, captcha("signup")],
    responses=_errors(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def signup(body: SignupIn, request: Request) -> AcceptedOut:
    """Check the account's fields; keep it until its code is used.

    Raises:
        APIError: 422 ``missing_value`` without an address (or, where
            numbers identify users, a number).
        UnknownUserField: A number, where numbers do not identify users.
        InvalidMobileNumber: The number is not in international form.
    """
    config = _enabled()
    model = get_user_model()
    if body.mobile_number is not None and MOBILE_NUMBER not in identifier_fields(model):
        raise UnknownUserField(
            "A mobile number can be given only where it identifies users "
            "(AUTH.IDENTIFIERS)."
        )
    texted = body.email is None and body.mobile_number is not None
    if body.email is None and not texted:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Give an email address.",
            code="missing_value",
        )
    number = (
        None
        if body.mobile_number is None
        else normalize_mobile_number(body.mobile_number)
    )
    contact = cast(str, number if texted else body.email)
    await throttle(
        request,
        config.EMAIL_RATE,
        [address_key(request, "signup"), email_key("signup", contact)],
        action="auth.signup",
        detail="Too many requests for mail; try again later.",
    )
    given = _given(model, body.fields)
    fields: dict[str, object] = {}
    if body.username is not None:
        fields[model.USERNAME_FIELD] = body.username
    if body.email is not None:
        fields[cast(str, model.EMAIL_FIELD)] = body.email
    if number is not None:
        fields[MOBILE_NUMBER] = number
    # Builds a user the way confirming will, so its fields are checked now.
    candidate = model.create(
        password=PasswordHash.unusable(), **fields, **typed_fields(model, given)
    )
    async with AuthUnitOfWork() as uow:
        pending = await uow.signups.add(
            username=getattr(candidate, model.USERNAME_FIELD, None) or None,
            email=getattr(candidate, cast(str, model.EMAIL_FIELD)) or None,
            mobile_number=getattr(candidate, MOBILE_NUMBER, None),
            ttl=config.CODE_TTL,
            fields=given,
        )
        uow.defer(send_signup_code, signup_id=pending.id)
        await uow.commit()
    if texted:
        return AcceptedOut(detail="Check your text messages to finish signing up.")
    return AcceptedOut(detail="Check your email to finish signing up.")


@router.post(
    "/signup/code",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Public],
    responses=_errors(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
async def confirm_signup_code(body: SignupCodeIn, request: Request) -> None:
    """Make the account the code was sent for, with the password chosen now.

    A password the validators refuse leaves the code working, to try
    another; the address, or the number, is verified by the code.
    """
    config = _enabled()
    async with AuthUnitOfWork() as uow:
        target = target_of(body.identifier)
        proven = await uow.verification_codes.check(SIGNUP, target, body.code)
        pending = None
        if proven is not None and proven.signup_id is not None:
            pending = await uow.signups.get(proven.signup_id)
        if pending is not None and not pending.expired():
            account = _account(get_user_model(), pending, PasswordHash.unusable())
            validate_password(body.password, account)  # Raises: nothing is spent.
            password = PasswordHash(await amake_password(body.password))
            await uow.signups.remove(pending.id)  # The code works once.
            if await _taken(uow, pending):
                await uow.commit()
                raise SignupTaken
            await _make_account(uow, pending, password)
            await uow.commit()
            return
        await uow.commit()  # A wrong try stays counted.
    await count_failure(
        request,
        config.LOGIN_RATE,
        address_key(request, "signup-confirm"),
        action="auth.signup",
        detail="Too many attempts; try again later.",
    )
    await audit_refusal("auth.signup", "invalid_verification_code")
    raise InvalidCode


async def send_signup_code(signup_id: UUID) -> None:
    """Send the address, or the number, a code that makes the account.

    The ``domainkit.auth.send_signup_code`` task. An address or a number
    already taken is told so instead, and the pending signup is deleted:
    a text goes with that commit, and a mail before it, so a retry after a
    failed delivery still tells. One gone or expired is left alone.
    """
    from domainkit.contrib.mail import deliver, render_mail

    async with AuthUnitOfWork() as uow:
        pending = await uow.signups.get(signup_id)
        if pending is None or pending.expired():
            return
        number = None if pending.email else pending.mobile_number
        if await _taken(uow, pending):
            if number is None:
                context: dict[str, object] = {"email": pending.email}
                to = [cast(str, pending.email)]
                await deliver([render_mail("auth/signup_exists", context, to=to)])
            else:  # auth.E107: notifications are installed.
                await _text_taken(number, uow)
            await uow.signups.remove(pending.id)
            await uow.commit()
            return
        contact = cast(str, pending.email or number)
        code = await uow.verification_codes.issue(
            SIGNUP, target_of(contact), signup_id=pending.id
        )
        if number is not None:
            await text_code(number, code, "finish signing up", uow)
        await uow.commit()
    if number is None:
        mail = render_mail("auth/signup_code", code_context(code), to=[contact])
        await deliver([mail])


async def _text_taken(number: str, uow: AuthUnitOfWork) -> None:
    """Tell ``number`` that an account uses it already."""
    from domainkit.contrib.notifications import send_sms

    body = (
        "Someone tried to sign up with this number, and an account uses it "
        "already. If it was you, log in, or reset your password."
    )
    await send_sms(number, body, uow=uow)


def register_tasks() -> None:
    """Register the task that sends signup codes (with messaging)."""
    task(name=SIGNUP_CODE_TASK)(send_signup_code)


def _enabled() -> AuthSettings:
    """The settings, if signing up is on.

    Raises:
        APIError: 404, while ``AUTH.SIGNUP_ENABLED`` is off.
    """
    config = auth_settings()
    if not config.SIGNUP_ENABLED:
        raise APIError(status.HTTP_404_NOT_FOUND, "Not Found", code="not_found")
    return config


async def _taken(uow: AuthUnitOfWork, pending: PendingSignup) -> bool:
    """Whether an account has the signup's username, address, or number.

    The username in any case; the address in any case of its ASCII letters.
    """
    if pending.username and await uow.users.username_taken(pending.username):
        return True
    if pending.email and await uow.users.find_by_email(pending.email):
        return True
    number = pending.mobile_number
    return number is not None and await uow.users.mobile_number_taken(number)


def _account(
    model: type[AbstractBaseUser], pending: PendingSignup, password: PasswordHash
) -> AbstractBaseUser:
    """The user the signup asks for, with ``password``."""
    fields: dict[str, object] = {}
    if pending.username is not None:
        fields[model.USERNAME_FIELD] = pending.username
    if pending.email is not None:
        fields[cast(str, model.EMAIL_FIELD)] = pending.email
    if pending.mobile_number is not None:
        fields[MOBILE_NUMBER] = pending.mobile_number
    extra = typed_fields(model, pending.fields or {})
    return model.create(password=password, **fields, **extra)


def _given(
    model: type[AbstractBaseUser], fields: dict[str, FieldValue]
) -> dict[str, FieldValue]:
    """``fields``, if they are the model's ``signup_fields()``, every one.

    Raises:
        UnknownUserField: A field is not one a signup gives.
        InvalidUserField: One of them is missing.
    """
    wanted = model.signup_fields()
    if unknown := sorted(set(fields) - set(wanted)):
        raise UnknownUserField(
            f"These user fields cannot be given here: {', '.join(unknown)}."
        )
    if missing := [name for name in wanted if name not in fields]:
        raise InvalidUserField(f"These user fields are required: {', '.join(missing)}.")
    return fields


async def _make_account(
    uow: AuthUnitOfWork, pending: PendingSignup, password: PasswordHash
) -> None:
    """Add the user the signup asked for, verified by the code.

    The address is verified, or, for a signup by number alone, the number.
    The password is kept in the history, as every password stored is
    (``AUTH.PASSWORD_HISTORY``, AUTH-15).
    """
    user = _account(uow.users.model, pending, password)
    # Startup checks that the user model can (AbstractUser can).
    if pending.email is not None:
        cast(AbstractUser, user).verify_email()
    else:
        cast(AbstractUser, user).verify_mobile_number()
    await uow.users.add(user)
    await uow.passwords.record(user.id, password)
    user.record(UserSignedUp(user_id=user.id))
