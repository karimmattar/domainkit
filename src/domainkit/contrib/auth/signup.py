"""Signing up: anyone may create an account, if ``AUTH.SIGNUP_ENABLED`` says so.

Off by default: the route answers 404 until it is on. Install messaging
and mail, and mount the router::

    from domainkit.contrib.auth.signup import router as signup_router

    router.include_router(signup_router, prefix="/auth")

``POST /signup`` (anyone) takes a username, an email address, and a
password. The fields and the password rules (``AUTH.PASSWORD_VALIDATORS``)
are checked at once: a refusal there says nothing about other accounts.
Then it answers 202 whatever happens next, in the same time: the worker
creates the account and mails a verification link, or, when the username
or the address is taken, mails the address that it is. Whether an address
has an account is told only to its owner; a username taken is told to
whoever gives their own address, as any signup must. Attempts count
against ``AUTH.EMAIL_RATE`` per client address and per email address.
"""

from typing import Final, cast

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from domainkit.contrib.mail import deliver, render_mail
from domainkit.contrib.messaging import enqueue, task
from domainkit.http.errors import APIError

from .access import Public
from .api import PASSWORD_MAX_LENGTH, _errors
from .events import UserSignedUp
from .hashers import amake_password
from .models import USERNAME_MAX_LENGTH, PasswordHash
from .recovery import AcceptedOut, EmailAddress, send_email_verification
from .settings import auth_settings
from .throttling import address_key, email_key, throttle
from .uow import AuthUnitOfWork
from .users import get_user_model
from .validators import validate_password

SIGNUP_TASK: Final = "domainkit.auth.complete_signup"

router = APIRouter(tags=["auth"])


class SignupIn(BaseModel):
    """The account to create: its username, email address, and password."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH)
    email: EmailAddress
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


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
    """Check the account's fields and password, then create it in the worker."""
    config = auth_settings()
    if not config.SIGNUP_ENABLED:
        raise APIError(status.HTTP_404_NOT_FOUND, "Not Found", code="not_found")
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
    # Builds a user the way the worker will, so its fields are checked now.
    candidate = model.create(password=PasswordHash.unusable(), **fields)
    validate_password(body.password, candidate)
    password_hash = await amake_password(body.password)
    await enqueue(
        complete_signup,
        username=candidate.get_username(),
        email=email,
        password_hash=password_hash,
    )
    return AcceptedOut(detail="Check your email to finish signing up.")


async def complete_signup(username: str, email: str, password_hash: str) -> None:
    """Create the account and mail a verification link, or say it is taken.

    The ``domainkit.auth.complete_signup`` task. If another signup takes
    the username first, the commit fails and the next try says it is taken.
    """
    async with AuthUnitOfWork() as uow:
        taken = await uow.users.username_taken(
            username
        ) or await uow.users.find_by_email(email)
        if not taken:
            model = uow.users.model
            fields = {
                model.USERNAME_FIELD: username,
                cast(str, model.EMAIL_FIELD): email,
            }
            user = model.create(password=PasswordHash(password_hash), **fields)
            await uow.users.add(user)
            user.record(UserSignedUp(user_id=user.id))
            uow.defer(send_email_verification, user_id=user.id)
            await uow.commit()
            return
    await deliver([render_mail("auth/signup_exists", {"email": email}, to=[email])])


def register_tasks() -> None:
    """Register the task that creates accounts (with messaging installed)."""
    task(name=SIGNUP_TASK)(complete_signup)
