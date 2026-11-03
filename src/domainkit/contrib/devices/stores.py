"""The device tables, read and written in a ``DevicesUnitOfWork``."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.tokens import utc_now

from .tables import challenges, devices, passkeys
from .webauthn import b64encode

__all__ = ["BoundDevice", "Challenge", "DevicesUnitOfWork", "Passkey"]


@dataclass(frozen=True, slots=True)
class Passkey:
    """A user's passkey: a WebAuthn credential's public key and counter."""

    id: UUID
    user_id: UUID
    digest: str
    credential_id: str
    """Base64url, as the browser names it."""
    algorithm: int
    public_key: bytes
    sign_count: int
    backed_up: bool
    name: str
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class BoundDevice:
    """A user's device: a key it holds, which signs requests."""

    id: UUID
    user_id: UUID
    name: str
    algorithm: int
    public_key: bytes
    last_time: int
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class Challenge:
    """A challenge handed to a browser: for what, and for whom."""

    id: str
    purpose: str
    user_id: UUID | None
    expires_at: datetime


def credential_digest(credential_id: bytes) -> str:
    """A credential id's SHA-256, in hex: what passkeys are found by."""
    return hashlib.sha256(credential_id).hexdigest()


class PasskeyStore:
    """Passkeys."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def of_user(self, user_id: UUID) -> list[Passkey]:
        """The user's passkeys, oldest first."""
        rows = await self.session.execute(
            select(passkeys)
            .where(passkeys.c.user_id == user_id)
            .order_by(passkeys.c.created_at, passkeys.c.id)
        )
        return [Passkey(**row._mapping) for row in rows]

    async def count(self, user_id: UUID) -> int:
        """How many passkeys the user has."""
        return int(
            await self.session.scalar(
                select(func.count()).where(passkeys.c.user_id == user_id)
            )
            or 0
        )

    async def find(self, credential_id: bytes) -> Passkey | None:
        """The passkey ``credential_id`` names."""
        row = (
            await self.session.execute(
                select(passkeys).where(
                    passkeys.c.digest == credential_digest(credential_id)
                )
            )
        ).first()
        return None if row is None else Passkey(**row._mapping)

    async def add(
        self,
        user_id: UUID,
        credential_id: bytes,
        algorithm: int,
        public_key: bytes,
        sign_count: int,
        *,
        backed_up: bool,
        name: str,
    ) -> Passkey:
        """Store a new passkey of the user."""
        passkey = Passkey(
            id=uuid.uuid4(),
            user_id=user_id,
            digest=credential_digest(credential_id),
            credential_id=b64encode(credential_id),
            algorithm=algorithm,
            public_key=public_key,
            sign_count=sign_count,
            backed_up=backed_up,
            name=name,
            created_at=utc_now(),
            last_used_at=None,
        )
        await self.session.execute(insert(passkeys).values(**_fields(passkey)))
        return passkey

    async def used(self, passkey: Passkey, sign_count: int) -> bool:
        """Record a use with ``sign_count``, unless another use counted past it."""
        counted = (
            passkeys.c.sign_count == 0  # A passkey that counts nothing.
            if sign_count == 0
            else passkeys.c.sign_count < sign_count
        )
        result = await self.session.execute(
            update(passkeys)
            .where(passkeys.c.id == passkey.id, counted)
            .values(sign_count=sign_count, last_used_at=utc_now())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def remove(self, user_id: UUID, passkey_id: UUID) -> bool:
        """Remove the user's passkey; whether there was one."""
        result = await self.session.execute(
            delete(passkeys).where(
                passkeys.c.user_id == user_id, passkeys.c.id == passkey_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def forget_user(self, user_id: UUID) -> None:
        """Remove every passkey of a user who is gone."""
        await self.session.execute(
            delete(passkeys).where(passkeys.c.user_id == user_id)
        )


class ChallengeStore:
    """Challenges on their way to a passkey and back."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def issue(self, purpose: str, user_id: UUID | None, ttl: timedelta) -> str:
        """A new challenge, random; expired ones are cleared first."""
        now = utc_now()
        await self.session.execute(
            delete(challenges).where(challenges.c.expires_at <= now)
        )
        challenge = Challenge(secrets.token_urlsafe(32), purpose, user_id, now + ttl)
        await self.session.execute(insert(challenges).values(**_fields(challenge)))
        return challenge.id

    async def take(self, challenge: str) -> Challenge | None:
        """The challenge, deleted: it works once, until it expires."""
        row = (
            await self.session.execute(
                delete(challenges)
                .where(challenges.c.id == challenge)
                .returning(*challenges.c)
            )
        ).first()
        if row is None:
            return None
        taken = Challenge(**row._mapping)
        return None if taken.expires_at <= utc_now() else taken


class DeviceStore:
    """Bound devices."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def of_user(self, user_id: UUID) -> list[BoundDevice]:
        """The user's devices, oldest first."""
        rows = await self.session.execute(
            select(devices)
            .where(devices.c.user_id == user_id)
            .order_by(devices.c.created_at, devices.c.id)
        )
        return [BoundDevice(**row._mapping) for row in rows]

    async def count(self, user_id: UUID) -> int:
        """How many devices the user has."""
        return int(
            await self.session.scalar(
                select(func.count()).where(devices.c.user_id == user_id)
            )
            or 0
        )

    async def get(self, device_id: UUID) -> BoundDevice | None:
        """The device ``device_id`` names."""
        row = (
            await self.session.execute(select(devices).where(devices.c.id == device_id))
        ).first()
        return None if row is None else BoundDevice(**row._mapping)

    async def add(
        self, user_id: UUID, name: str, algorithm: int, public_key: bytes
    ) -> BoundDevice:
        """Bind a new device to the user."""
        device = BoundDevice(
            id=uuid.uuid4(),
            user_id=user_id,
            name=name,
            algorithm=algorithm,
            public_key=public_key,
            last_time=0,
            created_at=utc_now(),
            last_used_at=None,
        )
        await self.session.execute(insert(devices).values(**_fields(device)))
        return device

    async def advance(self, device_id: UUID, time: int) -> bool:
        """Record a signed request of ``time``, unless one as late was recorded."""
        result = await self.session.execute(
            update(devices)
            .where(devices.c.id == device_id, devices.c.last_time < time)
            .values(last_time=time, last_used_at=utc_now())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def remove(self, user_id: UUID, device_id: UUID) -> bool:
        """Unbind the user's device; whether there was one."""
        result = await self.session.execute(
            delete(devices).where(
                devices.c.user_id == user_id, devices.c.id == device_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def forget_user(self, user_id: UUID) -> None:
        """Unbind every device of a user who is gone."""
        await self.session.execute(delete(devices).where(devices.c.user_id == user_id))


class DevicesUnitOfWork(AuthUnitOfWork):
    """The auth unit of work, with the device tables (on the same database)."""

    passkeys: PasskeyStore
    challenges: ChallengeStore
    devices: DeviceStore

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the auth repositories, then the device stores."""
        super().create_repositories(session)
        self.passkeys = PasskeyStore(session)
        self.challenges = ChallengeStore(session)
        self.devices = DeviceStore(session)


def _fields(item: object) -> dict[str, object]:
    """A dataclass's fields, by name."""
    return {name: getattr(item, name) for name in item.__dataclass_fields__}  # type: ignore[attr-defined]
