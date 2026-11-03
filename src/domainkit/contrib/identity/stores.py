"""The identity tables, read and written in an ``IdentityUnitOfWork``."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.tokens import utc_now

from .tables import flows, identities, tickets

__all__ = ["Flow", "IdentityUnitOfWork", "LinkedIdentity"]


@dataclass(frozen=True, slots=True)
class LinkedIdentity:
    """A provider's account a user logs in with."""

    id: UUID
    user_id: UUID
    provider: str
    subject: str
    email: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class Flow:
    """A login or link sent to a provider; its ``verifier`` and ``nonce`` are secret."""

    id: str
    provider: str
    purpose: str
    user_id: UUID | None
    verifier: str
    nonce: str
    expires_at: datetime

    def __repr__(self) -> str:
        """The provider and purpose only: the rest proves the flow."""
        return f"Flow(provider={self.provider!r}, purpose={self.purpose!r})"


class IdentityStore:
    """Linked identities."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def find(self, provider: str, subject: str) -> LinkedIdentity | None:
        """The identity ``subject`` of ``provider``, if linked."""
        row = (
            await self.session.execute(
                select(identities).where(
                    identities.c.provider == provider, identities.c.subject == subject
                )
            )
        ).first()
        return None if row is None else LinkedIdentity(**row._mapping)

    async def of_user(self, user_id: UUID) -> list[LinkedIdentity]:
        """The user's identities, by provider."""
        rows = await self.session.execute(
            select(identities)
            .where(identities.c.user_id == user_id)
            .order_by(identities.c.provider, identities.c.created_at)
        )
        return [LinkedIdentity(**row._mapping) for row in rows]

    async def link(
        self, user_id: UUID, provider: str, subject: str, email: str | None
    ) -> LinkedIdentity:
        """Link ``subject`` of ``provider`` to the user."""
        identity = LinkedIdentity(
            uuid.uuid4(), user_id, provider, subject, email, utc_now()
        )
        await self.session.execute(insert(identities).values(**_fields(identity)))
        return identity

    async def unlink(self, user_id: UUID, provider: str) -> int:
        """Unlink the user's identities at ``provider``; how many."""
        result = await self.session.execute(
            delete(identities).where(
                identities.c.user_id == user_id, identities.c.provider == provider
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def forget_user(self, user_id: UUID) -> None:
        """Unlink every identity of a user who is gone."""
        await self.session.execute(
            delete(identities).where(identities.c.user_id == user_id)
        )


class FlowStore:
    """Logins and links on their way to a provider and back."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def start(
        self, provider: str, purpose: str, user_id: UUID | None, ttl: timedelta
    ) -> Flow:
        """A new flow: its state, PKCE verifier, and nonce, random."""
        flow = Flow(
            id=secrets.token_urlsafe(32),
            provider=provider,
            purpose=purpose,
            user_id=user_id,
            verifier=secrets.token_urlsafe(64),
            nonce=secrets.token_urlsafe(32),
            expires_at=utc_now() + ttl,
        )
        await self.session.execute(insert(flows).values(**_fields(flow)))
        return flow

    async def take(self, state: str) -> Flow | None:
        """The flow ``state`` names, deleted: a state works once, until it expires."""
        row = (
            await self.session.execute(
                delete(flows).where(flows.c.id == state).returning(*flows.c)
            )
        ).first()
        if row is None:
            return None
        flow = Flow(**row._mapping)
        return None if flow.expires_at <= utc_now() else flow

    async def clear_expired(self) -> int:
        """Delete the flows that can no longer come back; how many."""
        result = await self.session.execute(
            delete(flows).where(flows.c.expires_at <= utc_now())
        )
        return int(result.rowcount)  # type: ignore[attr-defined]


class TicketStore:
    """One-time tickets for logins a posted response proved."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def issue(self, user_id: UUID, ttl: timedelta) -> str:
        """A new ticket for ``user_id`` (shown only now; its digest is kept)."""
        raw = secrets.token_urlsafe(32)
        await self.session.execute(
            insert(tickets).values(
                id=_digest(raw), user_id=user_id, expires_at=utc_now() + ttl
            )
        )
        return raw

    async def redeem(self, raw: str) -> UUID | None:
        """The user a ticket names, the ticket deleted; None if unknown or expired."""
        row = (
            await self.session.execute(
                delete(tickets)
                .where(tickets.c.id == _digest(raw))
                .returning(tickets.c.user_id, tickets.c.expires_at)
            )
        ).first()
        if row is None or row.expires_at <= utc_now():
            return None
        user_id: UUID = row.user_id
        return user_id


class IdentityUnitOfWork(AuthUnitOfWork):
    """The auth unit of work, with the identity tables (on the same database)."""

    identities: IdentityStore
    flows: FlowStore
    tickets: TicketStore

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the auth repositories, then the identity stores."""
        super().create_repositories(session)
        self.identities = IdentityStore(session)
        self.flows = FlowStore(session)
        self.tickets = TicketStore(session)


def _fields(item: object) -> dict[str, object]:
    """A dataclass's fields, by name."""
    return {name: getattr(item, name) for name in item.__dataclass_fields__}  # type: ignore[attr-defined]


def _digest(raw: str) -> str:
    """A ticket's SHA-256, in hex."""
    return hashlib.sha256(raw.encode()).hexdigest()
