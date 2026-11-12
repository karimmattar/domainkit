"""Signups waiting for their code."""

import dataclasses
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..tables import pending_signups
from ..tokens import utc_now
from ._shared import changed_rows


@dataclass(frozen=True, slots=True)
class PendingSignup:
    """An account someone asked for at ``/signup``, not made yet.

    It holds no password: whoever confirms the address chooses one. Its
    ``repr`` names its id alone: the rest is personal data.
    """

    id: UUID
    username: str | None = dataclasses.field(repr=False)
    email: str | None = dataclasses.field(repr=False)
    created_at: datetime = dataclasses.field(repr=False)
    expires_at: datetime = dataclasses.field(repr=False)
    mobile_number: str | None = dataclasses.field(default=None, repr=False)
    """The number a signup by code verifies (ADR 0126); None for others."""
    fields: dict[str, Any] | None = dataclasses.field(default=None, repr=False)
    """The user model's other ``REQUIRED_FIELDS``, as the client gave them."""

    def expired(self, now: datetime | None = None) -> bool:
        """Whether its link no longer works."""
        return self.expires_at <= (now or utc_now())


class SignupStore:
    """Pending signups, by id; nothing here makes or reserves an account."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(
        self,
        *,
        username: str | None,
        email: str | None,
        ttl: timedelta,
        fields: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        mobile_number: str | None = None,
    ) -> PendingSignup:
        """Store an account to make once its address or number is confirmed."""
        moment = now or utc_now()
        pending = PendingSignup(
            id=uuid.uuid4(),
            username=username,
            email=email,
            created_at=moment,
            expires_at=moment + ttl,
            mobile_number=mobile_number,
            fields=dict(fields) if fields else None,
        )
        await self.session.execute(
            insert(pending_signups).values(
                {
                    field.name: getattr(pending, field.name)
                    for field in dataclasses.fields(pending)
                }
            )
        )
        return pending

    async def get(self, signup_id: UUID) -> PendingSignup | None:
        """The pending signup ``signup_id``, expired or not."""
        row = (
            await self.session.execute(
                select(pending_signups).where(pending_signups.c.id == signup_id)
            )
        ).first()
        return None if row is None else PendingSignup(**row._mapping)

    async def remove(self, signup_id: UUID) -> bool:
        """Delete the pending signup; False if it was gone (taken by another)."""
        result = await self.session.execute(
            delete(pending_signups).where(pending_signups.c.id == signup_id)
        )
        return changed_rows(result) == 1

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete pending signups whose link no longer works; return how many."""
        result = await self.session.execute(
            delete(pending_signups).where(
                pending_signups.c.expires_at <= (now or utc_now())
            )
        )
        return changed_rows(result)
