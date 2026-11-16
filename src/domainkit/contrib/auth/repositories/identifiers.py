"""New addresses and numbers, held until their code proves them (ADR 0153)."""

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from ..tables import pending_identifiers
from ..tokens import utc_now
from ._shared import changed_rows


@dataclass(frozen=True, slots=True)
class PendingIdentifier:
    """An address or a number a user asked to change to, not proved yet.

    The account keeps the one it has until a code sent here comes back.
    Its ``repr`` leaves the value out: it is personal data.
    """

    user_id: UUID
    field: str
    """``email`` or ``mobile_number``: what it replaces once proved."""
    value: str = dataclasses.field(repr=False)
    created_at: datetime = dataclasses.field(repr=False)
    expires_at: datetime = dataclasses.field(repr=False)


class PendingIdentifierStore:
    """Held addresses and numbers, one per user and field; the caller commits."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def hold(
        self,
        user_id: UUID,
        field: str,
        value: str,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> PendingIdentifier:
        """Hold ``value`` for ``user_id``'s ``field``, for ``ttl``.

        It replaces the one held before for that field. One statement, an
        upsert, so two requests at once both succeed, and the later wins.
        """
        moment = now or utc_now()
        held = PendingIdentifier(
            user_id=user_id,
            field=field,
            value=value,
            created_at=moment,
            expires_at=moment + ttl,
        )
        fresh = {"value": value, "created_at": moment, "expires_at": held.expires_at}
        dialect = self.session.get_bind().dialect.name
        statement = (postgresql if dialect == "postgresql" else sqlite).insert(
            pending_identifiers
        )
        columns = pending_identifiers.c
        await self.session.execute(
            statement.values(
                user_id=user_id, field=field, **fresh
            ).on_conflict_do_update(
                index_elements=[columns.user_id, columns.field], set_=fresh
            )
        )
        return held

    async def get(
        self, user_id: UUID, field: str, *, now: datetime | None = None
    ) -> PendingIdentifier | None:
        """What ``user_id`` holds for ``field``; None when nothing, or expired."""
        row = (
            await self.session.execute(
                select(pending_identifiers).where(
                    pending_identifiers.c.user_id == user_id,
                    pending_identifiers.c.field == field,
                    pending_identifiers.c.expires_at > (now or utc_now()),
                )
            )
        ).first()
        return None if row is None else PendingIdentifier(**row._mapping)

    async def extend(
        self, held: PendingIdentifier, *, until: datetime
    ) -> PendingIdentifier:
        """Keep ``held`` until ``until``, when a new code sent to it expires.

        Nothing changes when another value was held since.
        """
        await self.session.execute(
            update(pending_identifiers)
            .where(
                pending_identifiers.c.user_id == held.user_id,
                pending_identifiers.c.field == held.field,
                pending_identifiers.c.value == held.value,
            )
            .values(expires_at=until)
        )
        return dataclasses.replace(held, expires_at=until)

    async def drop(self, user_id: UUID, *fields: str) -> int:
        """Delete what ``user_id`` holds for ``fields``; return how many."""
        result = await self.session.execute(
            delete(pending_identifiers).where(
                pending_identifiers.c.user_id == user_id,
                pending_identifiers.c.field.in_(fields),
            )
        )
        return changed_rows(result)

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete what is held past its last code; return how many."""
        result = await self.session.execute(
            delete(pending_identifiers).where(
                pending_identifiers.c.expires_at <= (now or utc_now())
            )
        )
        return changed_rows(result)
