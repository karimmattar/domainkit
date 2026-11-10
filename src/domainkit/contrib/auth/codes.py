"""Codes of 6 digits that prove someone reads an address or a number.

A code is for one purpose (``EMAIL_VERIFICATION``, ...) and one target: an
email address or a mobile number, kept only as an HMAC (``target_of``). The
code itself is kept only as an HMAC too, keyed from ``SECRET_KEY``. It
works for ``AUTH.CODE_TTL``, is spent after ``AUTH.CODE_ATTEMPTS`` tries,
and is used once; a new code for the same purpose and target replaces the
one before (ADR 0126)::

    target = target_of(email)
    async with AuthUnitOfWork() as uow:
        code = await uow.verification_codes.issue(EMAIL_VERIFICATION, target)
        await uow.commit()  # then send the code

    async with AuthUnitOfWork() as uow:
        proven = await uow.verification_codes.check(EMAIL_VERIFICATION, target, code)
        await uow.commit()  # a wrong try counts even when it is refused
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit import signing

from .models import as_mobile_number, email_key
from .settings import auth_settings
from .tables import auth_codes
from .tokens import utc_now

EMAIL_VERIFICATION: Final = "email_verification"
"""A code that proves a user reads mail at their address."""
MOBILE_VERIFICATION: Final = "mobile_verification"
"""A code that proves a user receives texts at their mobile number."""
SIGNUP: Final = "signup"
"""A code that finishes a signup: the address or number is the person's."""
PASSWORD_RESET: Final = "password_reset"  # noqa: S105 - a purpose, not a secret.
"""A code that lets whoever reads it choose a new password."""
CODE_LENGTH: Final = 6
CODE_PURPOSE: Final = "contrib.auth.code"
"""What the key that digests codes is derived for."""
TARGET_PURPOSE: Final = "contrib.auth.code-target"
"""What the key that digests addresses and numbers is derived for."""


@dataclass(frozen=True, slots=True)
class Proven:
    """A code that matched: the user, or the pending signup, it was sent for."""

    user_id: UUID | None
    signup_id: UUID | None = None


def target_of(identifier: str) -> str:
    """What the table keeps of an address or a number: an HMAC of it.

    Ways of writing one address (in any case) or one number (with spaces)
    give one target.
    """
    canonical = as_mobile_number(identifier) or email_key(identifier)
    key = signing.derive_key(TARGET_PURPOSE)
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


def code_digest(code_id: UUID, code: str, key: bytes) -> str:
    """What the table keeps of a code: an HMAC with a key of its own."""
    return hmac.new(key, f"{code_id.hex}:{code}".encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    """6 digits from ``secrets``, leading zeros kept."""
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


class CodeStore:
    """The codes in ``domainkit_auth_codes``; the caller commits."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def issue(
        self,
        purpose: str,
        target: str,
        *,
        user_id: UUID | None = None,
        signup_id: UUID | None = None,
        now: datetime | None = None,
    ) -> str:
        """Make a code for ``purpose`` and ``target``; return it, to send.

        The code before it, for the same purpose and target, stops working.
        One statement, an upsert: two codes issued at once for one target
        (two signups with one address, run by two workers) both succeed,
        and the later one is the code that works.
        """
        moment = now or utc_now()
        code, code_id = new_code(), uuid.uuid4()
        fresh = {
            "id": code_id,
            "user_id": user_id,
            "signup_id": signup_id,
            "digest": code_digest(code_id, code, signing.derive_key(CODE_PURPOSE)),
            "attempts": 0,
            "created_at": moment,
            "expires_at": moment + auth_settings().CODE_TTL,
        }
        dialect = self.session.get_bind().dialect.name
        statement = (postgresql if dialect == "postgresql" else sqlite).insert(
            auth_codes
        )
        await self.session.execute(
            statement.values(
                purpose=purpose, target=target, **fresh
            ).on_conflict_do_update(
                index_elements=[auth_codes.c.purpose, auth_codes.c.target],
                set_=fresh,
            )
        )
        return code

    async def check(
        self, purpose: str, target: str, code: str, *, now: datetime | None = None
    ) -> Proven | None:
        """Whether ``code`` is the live one for ``purpose`` and ``target``.

        Each try counts, the right one too, in one statement, so tries made
        at once cannot pass ``AUTH.CODE_ATTEMPTS``. A match spends the code.
        None for a wrong code, and for no live code: expired, spent, or
        never sent. Commit either way, so the try stays counted.
        """
        counted = (
            update(auth_codes)
            .where(
                auth_codes.c.purpose == purpose,
                auth_codes.c.target == target,
                auth_codes.c.expires_at > (now or utc_now()),
                auth_codes.c.attempts < auth_settings().CODE_ATTEMPTS,
            )
            .values(attempts=auth_codes.c.attempts + 1)
            .returning(
                auth_codes.c.id,
                auth_codes.c.digest,
                auth_codes.c.user_id,
                auth_codes.c.signup_id,
            )
        )
        row = (await self.session.execute(counted)).first()
        if row is None or not _matches(row.id, row.digest, code.strip()):
            return None
        await self.session.execute(delete(auth_codes).where(auth_codes.c.id == row.id))
        return Proven(user_id=row.user_id, signup_id=row.signup_id)

    async def forget(self, user_id: UUID, targets: list[str]) -> int:
        """Delete ``user_id``'s codes sent to ``targets``; return how many.

        An address or a number the user gave up (ADR 0133): a code mailed or
        texted to it before must not reach their account any more.
        """
        result = await self.session.execute(
            delete(auth_codes).where(
                auth_codes.c.user_id == user_id, auth_codes.c.target.in_(targets)
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete the codes that no longer work; return how many."""
        result = await self.session.execute(
            delete(auth_codes).where(auth_codes.c.expires_at <= (now or utc_now()))
        )
        return int(getattr(result, "rowcount", 0) or 0)


def _matches(code_id: UUID, digest: str, code: str) -> bool:
    """Whether ``code`` gives ``digest``, under any current key."""
    return any(
        hmac.compare_digest(digest, code_digest(code_id, code, key))
        for key in signing.derive_keys(CODE_PURPOSE)
    )
