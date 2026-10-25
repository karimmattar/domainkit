"""Signing up: anyone may ask for an account, if ``AUTH.SIGNUP_ENABLED`` says so.

Off by default: the routes answer 404 until it is on. Install messaging
and mail, and mount the router::

    from domainkit.contrib.auth.signup import router as signup_router

    router.include_router(signup_router, prefix="/auth")

``POST /signup`` (anyone) takes a username and an email address, and no
password. The fields are checked at once: a refusal there says nothing
about other accounts. Then the request is kept as a pending signup, and
it answers 202, doing the same work whatever accounts exist. The worker
mails the address: a link (``AUTH.SIGNUP_URL``), or, when the username or
the address is taken, word that it is. ``POST /signup/confirm`` (anyone)
takes the link's token and a password, checked by
``AUTH.PASSWORD_VALIDATORS``, and makes the account, its address verified.

Whoever reads the mail chooses the password, so whoever asked with
someone else's address gains nothing: the account made is the address
owner's, with a password only they know (no pre-account hijacking). No
account exists until then, so nothing anyone else can see differs
between an address that has an account and one that has none: whether
it has one is told only to whoever reads its mail. A pending signup
reserves nothing: several may name one username, and the first confirmed
gets it; the others' links then answer 409. A username taken is told to
whoever gives their own address, as any signup must. Attempts count
against ``AUTH.EMAIL_RATE`` per client address and per email address,
links that do not hold against ``AUTH.LOGIN_RATE`` per client address,
once checked (so forged ones never hold a valid one back). Pending
signups (a username and an address, no password) are kept in a table of
their own, never in fixtures, and deleted once used, when the address is
taken, or hourly once their link has expired.
"""

import re
from typing import Final, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.contrib.mail import deliver, render_mail
from domainkit.contrib.messaging import task
from domainkit.http.errors import APIError
from domainkit.signing import BadSignature, TimestampSigner

from .access import Public
from .api import PASSWORD_MAX_LENGTH, _errors
from .events import UserSignedUp
from .hashers import amake_password
from .models import (
    USERNAME_MAX_LENGTH,
    AbstractBaseUser,
    AbstractUser,
    PasswordHash,
)
from .recovery import AcceptedOut, EmailAddress, InvalidLink, TokenIn, describe
from .repositories import PendingSignup
from .settings import TOKEN_PLACEHOLDER, AuthSettings, auth_settings
from .throttling import (
    address_key,
    audit_refusal,
    count_failure,
    email_key,
    throttle,
)
from .uow import AuthUnitOfWork
from .users import get_user_model
from .validators import validate_password

SIGNUP_TASK: Final = "domainkit.auth.send_signup_link"
SALT: Final = "domainkit.auth.signup"
"""Keeps signup links to their purpose."""
_SIGNUP: Final = re.compile(r"[0-9a-f]{32}")

router = APIRouter(tags=["auth"])


class SignupIn(BaseModel):
    """The account to ask for: its username and email address.

    No password: whoever reads the mail chooses it (``SignupConfirmIn``).
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH)
    email: EmailAddress


class SignupConfirmIn(TokenIn):
    """A signup link's token, and the password of the account it makes."""

    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, repr=False)


class SignupTaken(APIError):
    """The username or the address got an account before the link was used (409)."""

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
    dependencies=[Public],
    responses=_errors(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
)
async def signup(body: SignupIn, request: Request) -> AcceptedOut:
    """Check the account's fields; keep it until its link is used."""
    config = _enabled()
    email = body.email
    await throttle(
        request,
        config.EMAIL_RATE,
        [address_key(request, "signup"), email_key("signup", email)],
        action="auth.signup",
        detail="Too many requests for mail; try again later.",
        code="throttled",
    )
    model = get_user_model()
    fields = {model.USERNAME_FIELD: body.username, cast(str, model.EMAIL_FIELD): email}
    # Builds a user the way confirming will, so its fields are checked now.
    candidate = model.create(password=PasswordHash.unusable(), **fields)
    async with AuthUnitOfWork() as uow:
        pending = await uow.signups.add(
            username=candidate.get_username(),
            email=getattr(candidate, cast(str, model.EMAIL_FIELD)),
            ttl=config.EMAIL_VERIFICATION_TIMEOUT,
        )
        uow.defer(send_signup_link, signup_id=pending.id)
        await uow.commit()
    return AcceptedOut(detail="Check your email to finish signing up.")


@router.post(
    "/signup/confirm",
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
async def confirm_signup(body: SignupConfirmIn, request: Request) -> None:
    """Make the account a signup link names, with the password chosen now.

    A password the validators refuse leaves the link working, to try
    another; the address is verified by the link.
    """
    config = _enabled()
    signup_id = signup_of(body.token)
    async with AuthUnitOfWork() as uow:
        pending = None if signup_id is None else await uow.signups.get(signup_id)
    if pending is not None and not pending.expired():
        account = _account(get_user_model(), pending, PasswordHash.unusable())
        validate_password(body.password, account)
        password = PasswordHash(await amake_password(body.password))
        async with AuthUnitOfWork() as uow:
            # Removed first: of two requests with one link, one goes on.
            if await uow.signups.remove(pending.id):
                if await _taken(uow, pending):
                    await uow.commit()
                    raise SignupTaken
                await _make_account(uow, pending, password)
                await uow.commit()
                return
    await count_failure(
        request,
        config.LOGIN_RATE,
        address_key(request, "signup-confirm"),
        action="auth.signup",
        detail="Too many attempts; try again later.",
        code="throttled",
    )
    await audit_refusal("auth.signup", "invalid_token")
    raise InvalidLink


async def send_signup_link(signup_id: UUID) -> None:
    """Mail the address a link that makes the account, or say it is taken.

    The ``domainkit.auth.send_signup_link`` task. A pending signup whose
    username or address is taken is deleted once its address is told, so
    a retry after a failed delivery tells it still; one gone or expired is
    left alone.
    """
    config = auth_settings()
    async with AuthUnitOfWork() as uow:
        pending = await uow.signups.get(signup_id)
        if pending is None or pending.expired():
            return
        taken = await _taken(uow, pending)
    if taken:
        context: dict[str, object] = {"email": pending.email}
        template = "auth/signup_exists"
    else:
        token = signup_token(pending.id)
        url = config.SIGNUP_URL
        link = (
            token
            if url is None
            else url.replace(TOKEN_PLACEHOLDER, quote(token, safe=""))
        )
        context = {
            "link": link,
            "valid_for": describe(config.EMAIL_VERIFICATION_TIMEOUT),
        }
        template = "auth/signup_confirm"
    await deliver([render_mail(template, context, to=[pending.email])])
    if taken:
        async with AuthUnitOfWork() as uow:
            await uow.signups.remove(pending.id)
            await uow.commit()


def signup_token(signup_id: UUID) -> str:
    """The token of a signup link: the pending signup's id, signed and dated."""
    return TimestampSigner(salt=SALT).sign(signup_id.hex)


def signup_of(token: str) -> UUID | None:
    """The pending signup a link's token names, if it is signed and in date."""
    try:
        value = TimestampSigner(salt=SALT).unsign(
            token, max_age=auth_settings().EMAIL_VERIFICATION_TIMEOUT
        )
    except BadSignature:
        return None
    return UUID(hex=value) if _SIGNUP.fullmatch(value) else None


def register_tasks() -> None:
    """Register the task that mails signup links (with messaging installed)."""
    task(name=SIGNUP_TASK)(send_signup_link)


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
    """Whether an account has the signup's username (in any case) or address."""
    return await uow.users.username_taken(pending.username) or bool(
        await uow.users.find_by_email(pending.email)
    )


def _account(
    model: type[AbstractBaseUser], pending: PendingSignup, password: PasswordHash
) -> AbstractBaseUser:
    """The user the signup asks for, with ``password``."""
    fields = {
        model.USERNAME_FIELD: pending.username,
        cast(str, model.EMAIL_FIELD): pending.email,
    }
    return model.create(password=password, **fields)


async def _make_account(
    uow: AuthUnitOfWork, pending: PendingSignup, password: PasswordHash
) -> None:
    """Add the user the signup asked for, its address verified by the link."""
    user = _account(uow.users.model, pending, password)
    # Startup checks that the user model can (AbstractUser can).
    cast(AbstractUser, user).verify_email()
    await uow.users.add(user)
    user.record(UserSignedUp(user_id=user.id))
