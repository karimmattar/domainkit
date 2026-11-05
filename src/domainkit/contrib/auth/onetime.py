"""Signed one-time tokens bound to a user's state (``UserTokenGenerator``).

A token names a user, and is signed (``domainkit.signing``) under a key
derived from ``SECRET_KEY`` and from the state of the user it acts on
(Django's ``PasswordResetTokenGenerator``). Nothing is stored: a token
stops working once any of that state changes, and expires. Checking a
token for no user costs what checking one for a user does, so timing
tells no user ids.

1.x mailed password resets and email verification as links with such
tokens; since 2.0 they are codes (``codes``), and second factors are what
uses these tokens (``mfa``).
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
