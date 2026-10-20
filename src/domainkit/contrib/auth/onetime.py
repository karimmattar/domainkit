"""One-time tokens for links sent by email: password resets, email verification.

A token names a user, and is signed (``domainkit.signing``) under a key
derived from ``SECRET_KEY`` and from the state of the user the link acts
on (Django's ``PasswordResetTokenGenerator``). Nothing is stored:

- a reset token rests on the password hash, the last login, the email,
  and whether the account is active, so it stops working once it is used,
  or once the user logs in or any of those changes;
- a verification token rests on the email and whether it is verified, so
  it works once, and only for the address it was sent to.

Both expire (``AUTH.PASSWORD_RESET_TIMEOUT``,
``AUTH.EMAIL_VERIFICATION_TIMEOUT``). Checking a token for no user costs
what checking one for a user does, so timing tells no user ids.
"""

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import timedelta
from typing import ClassVar, Final
from uuid import UUID

from domainkit.signing import BadSignature, TimestampSigner

from .models import AbstractBaseUser, UserId
from .settings import auth_settings

TOKEN_MAX_LENGTH: Final = 256
"""Longer input is refused before any work."""
_USER: Final = re.compile(r"[0-9a-f]{32}")


def email_of(user: AbstractBaseUser) -> str | None:
    """The user's address (``EMAIL_FIELD``), if the model has one and it is set."""
    field = user.EMAIL_FIELD
    email = getattr(user, field, None) if field else None
    return email if isinstance(email, str) and email else None


class UserTokenGenerator(ABC):
    """Makes and checks signed tokens bound to a user's state."""

    salt: ClassVar[str]
    """Keeps each kind of token to its purpose."""

    @abstractmethod
    def state(self, user: AbstractBaseUser) -> Sequence[object]:
        """What the token rests on: when any of it changes, the token ends."""

    @abstractmethod
    def max_age(self) -> timedelta:
        """How long a token works."""

    def make_token(self, user: AbstractBaseUser) -> str:
        """A token for ``user``: their id, the time, and a signature."""
        return self._signer(user).sign(user.id.hex)

    def user_id(self, token: str) -> UserId | None:
        """The user ``token`` names, unchecked: only to look them up.

        None for what is not a token's form.
        """
        value = token.partition(":")[0]
        if len(token) > TOKEN_MAX_LENGTH or not _USER.fullmatch(value):
            return None
        return UserId(UUID(hex=value))

    def check_token(self, user: AbstractBaseUser | None, token: str) -> bool:
        """Whether ``token`` holds for ``user``, now.

        Without a user (no account has the id), a token is checked anyway,
        and refused, so the answer takes the same time.
        """
        try:
            value = self._signer(user).unsign(token, max_age=self.max_age())
        except BadSignature:
            return False
        return user is not None and value == user.id.hex

    def _signer(self, user: AbstractBaseUser | None) -> TimestampSigner:
        """A signer for this kind of token and ``user``'s current state."""
        parts = () if user is None else self.state(user)
        state = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()
        return TimestampSigner(salt=f"{self.salt}:{state}")


class PasswordResetTokenGenerator(UserTokenGenerator):
    """Reset tokens: they end when used, on login, or after the timeout."""

    salt = "domainkit.auth.password_reset"

    def state(self, user: AbstractBaseUser) -> Sequence[object]:
        """The password hash, last login, email, and whether the user is active."""
        return (user.password.value, user.last_login, email_of(user), user.is_active)

    def max_age(self) -> timedelta:
        """``AUTH.PASSWORD_RESET_TIMEOUT``."""
        return auth_settings().PASSWORD_RESET_TIMEOUT


class EmailVerificationTokenGenerator(UserTokenGenerator):
    """Verification tokens: they work once, for the address they went to."""

    salt = "domainkit.auth.email_verification"

    def state(self, user: AbstractBaseUser) -> Sequence[object]:
        """The email, and whether it is verified."""
        return (email_of(user), getattr(user, "email_verified", None))

    def max_age(self) -> timedelta:
        """``AUTH.EMAIL_VERIFICATION_TIMEOUT``."""
        return auth_settings().EMAIL_VERIFICATION_TIMEOUT


password_reset_tokens: Final = PasswordResetTokenGenerator()
email_verification_tokens: Final = EmailVerificationTokenGenerator()
