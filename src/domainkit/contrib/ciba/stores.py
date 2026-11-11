"""The CIBA tables, read and written in a ``CibaUnitOfWork``.

Every change of a request's state is one guarded update, so two phones,
two polls, or a phone and a poll racing never both win: the update that
matched no row lost.
"""

from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.contrib.devices import DevicesUnitOfWork

from .tables import assertions, clients, sign_in_requests

__all__ = ["CibaClient", "CibaUnitOfWork", "SignInRequest"]

PUSH = "push"
QR = "qr"
SIGN_IN = "sign_in"
STEP_UP = "step_up"
TRANSACTION = "transaction"
DEVICE = "device"
"""A request to bind the asking phone's key (ADR 0137)."""


@dataclass(frozen=True, slots=True, repr=False)
class CibaClient:
    """An OIDC client that may ask users' phones: who, how it proves it, what for."""

    client_id: str
    name: str
    auth_method: str
    secret_digest: str | None
    jwks: str | None
    scopes: str
    created_at: datetime

    def __repr__(self) -> str:
        """The id, name and method: the digest and keys stay out of logs."""
        return (
            f"CibaClient(client_id={self.client_id!r}, name={self.name!r}, "
            f"auth_method={self.auth_method!r})"
        )

    @property
    def allowed_scopes(self) -> frozenset[str]:
        """The permission keys its tokens may carry."""
        return frozenset(self.scopes.split())


@dataclass(frozen=True, slots=True, repr=False)
class SignInRequest:
    """A sign-in a phone is asked to approve, as its row keeps it."""

    id: UUID
    handle_digest: str
    qr_digest: str | None
    client_id: str | None
    user_id: UUID | None
    channel: str
    scope: str
    binding_message: str | None
    match_code: str | None
    requester_ip: str | None
    requester_agent: str | None
    decoy: bool
    created_at: datetime
    expires_at: datetime
    interval: int
    last_polled_at: datetime | None
    claimed_at: datetime | None
    claimed_ip: str | None
    approved_at: datetime | None
    device_id: UUID | None
    denied_at: datetime | None
    reported: bool
    redeemed_at: datetime | None
    kind: str = SIGN_IN
    """``sign_in``, ``step_up``, or ``transaction``."""
    credential_digest: str | None = None
    """A step-up's or a transaction's: the session that asked, which redeems it."""
    sca_challenge_id: UUID | None = None
    """A transaction's: the ``auth.sca`` transaction it confirms."""

    def __repr__(self) -> str:
        """The id, kind, channel and state: digests, addresses and codes stay out."""
        return (
            f"SignInRequest(id={self.id!r}, kind={self.kind!r}, "
            f"channel={self.channel!r}, state={self.state!r})"
        )

    @property
    def state(self) -> str:
        """``redeemed``, ``approved``, ``denied``, ``claimed``, or ``pending``."""
        if self.redeemed_at is not None:
            return "redeemed"
        if self.approved_at is not None:
            return "approved"
        if self.denied_at is not None:
            return "denied"
        if self.claimed_at is not None:
            return "claimed"
        return "pending"

    def expired(self, now: datetime) -> bool:
        """Whether its time is up at ``now``."""
        return now >= self.expires_at


def _row(row: object) -> SignInRequest:
    """The request a row of ``sign_in_requests`` keeps."""
    return SignInRequest(**row._mapping)  # type: ignore[attr-defined]


def _answerable() -> object:
    """Neither approved, denied, nor redeemed: the phone may still answer."""
    columns = sign_in_requests.c
    return and_(
        columns.approved_at.is_(None),
        columns.denied_at.is_(None),
        columns.redeemed_at.is_(None),
        columns.decoy.is_(False),
    )


class SignInRequestStore:
    """Sign-in requests."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(self, request: SignInRequest) -> SignInRequest:
        """Keep a new request."""
        await self.session.execute(
            insert(sign_in_requests).values(
                {field.name: getattr(request, field.name) for field in fields(request)}
            )
        )
        return request

    async def get(self, request_id: UUID) -> SignInRequest | None:
        """The request ``request_id`` names."""
        row = (
            await self.session.execute(
                select(sign_in_requests).where(sign_in_requests.c.id == request_id)
            )
        ).first()
        return None if row is None else _row(row)

    async def by_handle(self, digest: str) -> SignInRequest | None:
        """The request whose poll handle has the digest ``digest``."""
        row = (
            await self.session.execute(
                select(sign_in_requests).where(
                    sign_in_requests.c.handle_digest == digest
                )
            )
        ).first()
        return None if row is None else _row(row)

    async def by_qr(self, digest: str) -> SignInRequest | None:
        """The request whose QR code has the digest ``digest``, if unclaimed."""
        row = (
            await self.session.execute(
                select(sign_in_requests).where(sign_in_requests.c.qr_digest == digest)
            )
        ).first()
        return None if row is None else _row(row)

    async def waiting_for(self, user_id: UUID, now: datetime) -> list[SignInRequest]:
        """The user's requests their phone may still answer, oldest first."""
        rows = await self.session.execute(
            select(sign_in_requests)
            .where(
                sign_in_requests.c.user_id == user_id,
                sign_in_requests.c.expires_at > now,
                _answerable(),  # type: ignore[arg-type]
            )
            .order_by(sign_in_requests.c.created_at, sign_in_requests.c.id)
        )
        return [_row(row) for row in rows]

    async def live_pushes(self, user_id: UUID, now: datetime) -> int:
        """How many push requests of the user the phone may still answer."""
        return int(
            await self.session.scalar(
                select(func.count()).where(
                    sign_in_requests.c.user_id == user_id,
                    sign_in_requests.c.channel == PUSH,
                    sign_in_requests.c.expires_at > now,
                    _answerable(),  # type: ignore[arg-type]
                )
            )
            or 0
        )

    async def reported_since(self, user_id: UUID, since: datetime) -> bool:
        """Whether the user reported a request after ``since``."""
        found = await self.session.scalar(
            select(sign_in_requests.c.id)
            .where(
                sign_in_requests.c.user_id == user_id,
                sign_in_requests.c.reported.is_(True),
                sign_in_requests.c.denied_at > since,
            )
            .limit(1)
        )
        return found is not None

    async def claim(
        self, request_id: UUID, user_id: UUID, address: str | None, now: datetime
    ) -> bool:
        """Make the unclaimed QR request the user's; whether this claim won.

        Its code works once: the digest is cleared with the claim.
        """
        result = await self.session.execute(
            update(sign_in_requests)
            .where(
                sign_in_requests.c.id == request_id,
                sign_in_requests.c.user_id.is_(None),
                sign_in_requests.c.claimed_at.is_(None),
                sign_in_requests.c.expires_at > now,
                _answerable(),  # type: ignore[arg-type]
            )
            .values(user_id=user_id, claimed_at=now, claimed_ip=address, qr_digest=None)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def approve(
        self, request_id: UUID, user_id: UUID, device_id: UUID, now: datetime
    ) -> bool:
        """Approve the user's request from ``device_id``; whether this answer won."""
        result = await self.session.execute(
            update(sign_in_requests)
            .where(
                sign_in_requests.c.id == request_id,
                sign_in_requests.c.user_id == user_id,
                sign_in_requests.c.expires_at > now,
                _answerable(),  # type: ignore[arg-type]
            )
            .values(approved_at=now, device_id=device_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def deny(
        self, request_id: UUID, user_id: UUID, now: datetime, *, reported: bool
    ) -> bool:
        """Deny the user's request; whether this answer won."""
        result = await self.session.execute(
            update(sign_in_requests)
            .where(
                sign_in_requests.c.id == request_id,
                sign_in_requests.c.user_id == user_id,
                sign_in_requests.c.expires_at > now,
                _answerable(),  # type: ignore[arg-type]
            )
            .values(denied_at=now, reported=reported)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def deny_waiting(self, user_id: UUID, now: datetime) -> int:
        """Deny every request of the user still waiting; how many."""
        result = await self.session.execute(
            update(sign_in_requests)
            .where(
                sign_in_requests.c.user_id == user_id,
                sign_in_requests.c.expires_at > now,
                _answerable(),  # type: ignore[arg-type]
            )
            .values(denied_at=now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def polled(self, request_id: UUID, now: datetime, interval: int) -> bool:
        """Record a poll at ``now``; False if the last came within ``interval`` s."""
        columns = sign_in_requests.c
        result = await self.session.execute(
            update(sign_in_requests)
            .where(
                columns.id == request_id,
                columns.interval == interval,
                (columns.last_polled_at.is_(None))
                | (columns.last_polled_at <= now - timedelta(seconds=interval)),
            )
            .values(last_polled_at=now)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def slow_down(self, request_id: UUID, now: datetime, step: int) -> None:
        """Make the initiator wait ``step`` seconds more between polls, from now."""
        await self.session.execute(
            update(sign_in_requests)
            .where(sign_in_requests.c.id == request_id)
            .values(interval=sign_in_requests.c.interval + step, last_polled_at=now)
        )

    async def redeem(self, request_id: UUID, now: datetime) -> bool:
        """Spend the approved request; whether this poll is the one that did."""
        result = await self.session.execute(
            update(sign_in_requests)
            .where(
                sign_in_requests.c.id == request_id,
                sign_in_requests.c.approved_at.is_not(None),
                sign_in_requests.c.redeemed_at.is_(None),
            )
            .values(redeemed_at=now)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def purge(self, before: datetime) -> int:
        """Forget requests that ended before ``before``; how many."""
        result = await self.session.execute(
            delete(sign_in_requests).where(sign_in_requests.c.expires_at < before)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def forget_user(self, user_id: UUID) -> int:
        """Forget every request of a user who is gone; how many."""
        result = await self.session.execute(
            delete(sign_in_requests).where(sign_in_requests.c.user_id == user_id)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class ClientStore:
    """CIBA clients."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(self, client: CibaClient) -> CibaClient:
        """Keep a new client."""
        await self.session.execute(
            insert(clients).values(
                {field.name: getattr(client, field.name) for field in fields(client)}
            )
        )
        return client

    async def get(self, client_id: str) -> CibaClient | None:
        """The client ``client_id`` names."""
        row = (
            await self.session.execute(
                select(clients).where(clients.c.client_id == client_id)
            )
        ).first()
        return None if row is None else CibaClient(**row._mapping)

    async def all(self) -> list[CibaClient]:
        """Every client, oldest first."""
        rows = await self.session.execute(
            select(clients).order_by(clients.c.created_at, clients.c.client_id)
        )
        return [CibaClient(**row._mapping) for row in rows]

    async def replace(self, client: CibaClient, **changes: object) -> CibaClient:
        """Change the client's stored fields."""
        changed = replace(client, **changes)  # type: ignore[arg-type]
        await self.session.execute(
            update(clients)
            .where(clients.c.client_id == client.client_id)
            .values(**changes)
        )
        return changed

    async def remove(self, client_id: str) -> bool:
        """Forget the client; whether there was one."""
        result = await self.session.execute(
            delete(clients).where(clients.c.client_id == client_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


class AssertionStore:
    """The ``jti`` of client assertions already used."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def use(self, client_id: str, jti: str, expires_at: datetime) -> bool:
        """Keep that ``jti`` was used; False if it was before (a replay)."""
        dialect = self.session.get_bind().dialect.name
        statement = (postgresql if dialect == "postgresql" else sqlite).insert(
            assertions
        )
        result = await self.session.execute(
            statement.values(client_id=client_id, jti=jti, expires_at=expires_at)
            .on_conflict_do_nothing(index_elements=["client_id", "jti"])
            .returning(assertions.c.jti)
        )
        return result.first() is not None

    async def purge(self, before: datetime) -> int:
        """Forget assertions that expired before ``before``; how many."""
        result = await self.session.execute(
            delete(assertions).where(assertions.c.expires_at < before)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class CibaUnitOfWork(DevicesUnitOfWork):
    """The devices unit of work, with the CIBA tables (on the same database)."""

    requests: SignInRequestStore
    clients: ClientStore
    assertions: AssertionStore

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the auth and device repositories, then the CIBA stores."""
        super().create_repositories(session)
        self.requests = SignInRequestStore(session)
        self.clients = ClientStore(session)
        self.assertions = AssertionStore(session)
