"""The phone tables, read and written in a ``PhonesUnitOfWork``."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.contrib.auth import AuthUnitOfWork

from .tables import phone_codes, phones

__all__ = ["PhoneCode", "PhonesUnitOfWork", "VerifiedPhone"]


@dataclass(frozen=True, slots=True, repr=False)
class PhoneCode:
    """A code sent to a number: its keyed digest, never the code."""

    id: UUID
    user_id: UUID
    phone: str
    digest: str
    attempts: int
    created_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        """The id and its tries: the number is personal, the digest secret."""
        return f"PhoneCode(id={self.id!r}, attempts={self.attempts})"


@dataclass(frozen=True, slots=True)
class VerifiedPhone:
    """A number a user proved they receive messages at."""

    id: UUID
    user_id: UUID
    phone: str
    verified_at: datetime


class PhoneCodeStore:
    """Codes sent."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(
        self,
        code_id: UUID,
        user_id: UUID,
        phone: str,
        digest: str,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> PhoneCode:
        """Keep a new code's digest, made with its id ``code_id``."""
        code = PhoneCode(code_id, user_id, phone, digest, 0, now, expires_at)
        await self.session.execute(
            insert(phone_codes).values(
                id=code_id,
                user_id=user_id,
                phone=phone,
                digest=digest,
                attempts=0,
                created_at=now,
                expires_at=expires_at,
            )
        )
        return code

    async def latest(self, user_id: UUID, phone: str) -> PhoneCode | None:
        """The last code sent to ``phone`` for ``user_id``, locked for update."""
        row = (
            await self.session.execute(
                select(phone_codes)
                .where(phone_codes.c.user_id == user_id, phone_codes.c.phone == phone)
                .order_by(phone_codes.c.created_at.desc(), phone_codes.c.id)
                .limit(1)
                .with_for_update()
            )
        ).first()
        return None if row is None else PhoneCode(**row._mapping)

    async def live(self, user_id: UUID, phone: str, now: datetime) -> PhoneCode | None:
        """The code of ``phone`` for ``user_id`` that works at ``now``, locked.

        At most one does: a new code expires the ones before it.
        """
        row = (
            await self.session.execute(
                select(phone_codes)
                .where(
                    phone_codes.c.user_id == user_id,
                    phone_codes.c.phone == phone,
                    phone_codes.c.expires_at > now,
                )
                .with_for_update()
            )
        ).first()
        return None if row is None else PhoneCode(**row._mapping)

    async def expire(self, user_id: UUID, phone: str, now: datetime) -> None:
        """End every code of ``phone`` for ``user_id`` at ``now``: a new one is sent.

        The rows stay, counted against ``MAX_SENDS``.
        """
        await self.session.execute(
            update(phone_codes)
            .where(
                phone_codes.c.user_id == user_id,
                phone_codes.c.phone == phone,
                phone_codes.c.expires_at > now,
            )
            .values(expires_at=now)
        )

    async def sent_since(
        self, user_id: UUID, phone: str, since: datetime
    ) -> tuple[int, datetime | None]:
        """Codes sent after ``since`` to ``phone``, or asked by ``user_id``.

        Counted either way: a user cannot spread sends over many numbers,
        nor many users over one number. With the time of the oldest.
        """
        row = (
            await self.session.execute(
                select(func.count(), func.min(phone_codes.c.created_at)).where(
                    phone_codes.c.created_at > since,
                    or_(phone_codes.c.phone == phone, phone_codes.c.user_id == user_id),
                )
            )
        ).one()
        return int(row[0] or 0), row[1]

    async def tried(self, code_id: UUID) -> None:
        """Count one wrong try of a code."""
        await self.session.execute(
            update(phone_codes)
            .where(phone_codes.c.id == code_id)
            .values(attempts=phone_codes.c.attempts + 1)
        )

    async def spend(self, user_id: UUID, phone: str) -> None:
        """Forget every code of ``phone`` for ``user_id``: one was used."""
        await self.session.execute(
            delete(phone_codes).where(
                phone_codes.c.user_id == user_id, phone_codes.c.phone == phone
            )
        )

    async def purge(self, before: datetime) -> int:
        """Forget codes sent before ``before``; return how many."""
        result = await self.session.execute(
            delete(phone_codes).where(phone_codes.c.created_at < before)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def forget_user(self, user_id: UUID) -> None:
        """Forget every code a deleted user asked for."""
        await self.session.execute(
            delete(phone_codes).where(phone_codes.c.user_id == user_id)
        )


class PhoneStore:
    """Verified numbers."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def of_user(self, user_id: UUID) -> list[VerifiedPhone]:
        """The user's verified numbers, oldest first."""
        rows = await self.session.execute(
            select(phones)
            .where(phones.c.user_id == user_id)
            .order_by(phones.c.verified_at, phones.c.id)
        )
        return [VerifiedPhone(**row._mapping) for row in rows]

    async def find(self, user_id: UUID, phone: str) -> VerifiedPhone | None:
        """The user's verified ``phone``, if they verified it."""
        row = (
            await self.session.execute(
                select(phones).where(
                    phones.c.user_id == user_id, phones.c.phone == phone
                )
            )
        ).first()
        return None if row is None else VerifiedPhone(**row._mapping)

    async def count(self, user_id: UUID) -> int:
        """How many numbers the user verified."""
        return int(
            await self.session.scalar(
                select(func.count()).where(phones.c.user_id == user_id)
            )
            or 0
        )

    async def add(self, user_id: UUID, phone: str, now: datetime) -> VerifiedPhone:
        """Keep ``phone`` as verified by ``user_id``."""
        verified = VerifiedPhone(uuid.uuid4(), user_id, phone, now)
        await self.session.execute(
            insert(phones).values(
                id=verified.id, user_id=user_id, phone=phone, verified_at=now
            )
        )
        return verified

    async def remove(self, user_id: UUID, phone_id: UUID) -> bool:
        """Forget the user's number ``phone_id``; whether they had it."""
        result = await self.session.execute(
            delete(phones).where(phones.c.id == phone_id, phones.c.user_id == user_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def forget_user(self, user_id: UUID) -> None:
        """Forget every number of a deleted user."""
        await self.session.execute(delete(phones).where(phones.c.user_id == user_id))


class PhonesUnitOfWork(AuthUnitOfWork):
    """The auth unit of work, with the phone tables (on the same database)."""

    codes: PhoneCodeStore
    phones: PhoneStore

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the auth repositories, then the phone stores."""
        super().create_repositories(session)
        self.codes = PhoneCodeStore(session)
        self.phones = PhoneStore(session)
