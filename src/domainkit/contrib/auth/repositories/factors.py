"""Second factors, step-ups, and the challenges of confirmed operations."""

import dataclasses
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.money import Money

from ..tables import mfa_devices, recovery_codes, sca_challenges, step_ups
from ..tokens import utc_now
from ._shared import changed_rows


@dataclass(frozen=True, slots=True)
class TotpDevice:
    """A user's authenticator: its encrypted secret, and the last step used."""

    id: UUID
    user_id: UUID
    secret: bytes = dataclasses.field(repr=False)
    """Encrypted; ``mfa.secret_box()`` opens it."""
    created_at: datetime
    confirmed_at: datetime | None
    last_step: int | None

    @property
    def confirmed(self) -> bool:
        """Whether a code proved the app holds the secret."""
        return self.confirmed_at is not None


class MfaStore:
    """Authenticators and recovery codes, by user."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def device(self, user_id: UUID) -> TotpDevice | None:
        """The user's authenticator, confirmed or not."""
        row = (
            await self.session.execute(
                select(mfa_devices).where(mfa_devices.c.user_id == user_id)
            )
        ).first()
        return None if row is None else TotpDevice(**row._mapping)

    async def enabled(self, user_id: UUID) -> bool:
        """Whether the user has a confirmed authenticator."""
        statement = select(
            exists().where(
                mfa_devices.c.user_id == user_id,
                mfa_devices.c.confirmed_at.is_not(None),
            )
        )
        return bool(await self.session.scalar(statement))

    async def start(self, user_id: UUID, secret: bytes) -> UUID:
        """Store a new, unconfirmed authenticator, replacing an unconfirmed one."""
        await self.session.execute(
            delete(mfa_devices).where(
                mfa_devices.c.user_id == user_id, mfa_devices.c.confirmed_at.is_(None)
            )
        )
        device_id = uuid.uuid4()
        await self.session.execute(
            insert(mfa_devices).values(
                id=device_id, user_id=user_id, secret=secret, created_at=utc_now()
            )
        )
        return device_id

    async def confirm(self, device_id: UUID, step: int) -> bool:
        """Confirm the authenticator with the step of its first code."""
        result = await self.session.execute(
            update(mfa_devices)
            .where(mfa_devices.c.id == device_id, mfa_devices.c.confirmed_at.is_(None))
            .values(confirmed_at=utc_now(), last_step=step)
        )
        return changed_rows(result) == 1

    async def use_step(self, device_id: UUID, step: int) -> bool:
        """Record ``step`` as used, unless it, or a later one, already was."""
        result = await self.session.execute(
            update(mfa_devices)
            .where(
                mfa_devices.c.id == device_id,
                mfa_devices.c.last_step.is_(None) | (mfa_devices.c.last_step < step),
            )
            .values(last_step=step)
        )
        return changed_rows(result) == 1

    async def sealed(
        self, *, after: UUID | None = None, limit: int
    ) -> list[TotpDevice]:
        """Authenticators by id, after ``after``: a page of their sealed secrets."""
        statement = select(mfa_devices).order_by(mfa_devices.c.id).limit(limit)
        if after is not None:
            statement = statement.where(mfa_devices.c.id > after)
        rows = await self.session.execute(statement)
        return [TotpDevice(**row._mapping) for row in rows]

    async def reseal(self, device_id: UUID, secret: bytes) -> None:
        """Store the secret encrypted again (under the current key)."""
        await self.session.execute(
            update(mfa_devices)
            .where(mfa_devices.c.id == device_id)
            .values(secret=secret)
        )

    async def remove(self, user_id: UUID) -> int:
        """Delete the user's authenticator and recovery codes; return how many."""
        removed = await self.session.execute(
            delete(mfa_devices).where(mfa_devices.c.user_id == user_id)
        )
        await self.session.execute(
            delete(recovery_codes).where(recovery_codes.c.user_id == user_id)
        )
        return changed_rows(removed)

    async def replace_codes(self, user_id: UUID, digests: Iterable[str]) -> None:
        """Make ``digests`` the user's only recovery codes."""
        await self.session.execute(
            delete(recovery_codes).where(recovery_codes.c.user_id == user_id)
        )
        await self.session.execute(
            insert(recovery_codes),
            [{"id": digest, "user_id": user_id} for digest in digests],
        )

    async def use_code(self, user_id: UUID, digest: str) -> bool:
        """Spend the user's unused recovery code with ``digest``, once."""
        result = await self.session.execute(
            update(recovery_codes)
            .where(
                recovery_codes.c.id == digest,
                recovery_codes.c.user_id == user_id,
                recovery_codes.c.used_at.is_(None),
            )
            .values(used_at=utc_now())
        )
        return changed_rows(result) == 1

    async def codes_left(self, user_id: UUID) -> int:
        """How many of the user's recovery codes are unused."""
        statement = select(func.count()).where(
            recovery_codes.c.user_id == user_id, recovery_codes.c.used_at.is_(None)
        )
        return int(await self.session.scalar(statement) or 0)


class StepUpStore:
    """The last step-up of each login token and browser session, by digest."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def record(
        self, digest: str, user_id: UUID, when: datetime, methods: Iterable[str]
    ) -> None:
        """Keep that the credential ``digest`` of ``user_id`` stepped up at ``when``.

        One statement: concurrent step-ups of one credential each write, and
        the latest proof stays; an older one, written after, changes nothing.
        """
        dialect = self.session.get_bind().dialect.name
        statement = (postgresql if dialect == "postgresql" else sqlite).insert(step_ups)
        proof = {"auth_time": when, "amr": " ".join(sorted(set(methods)))}
        await self.session.execute(
            statement.values(id=digest, user_id=user_id, **proof).on_conflict_do_update(
                index_elements=[step_ups.c.id],
                set_=proof,
                where=step_ups.c.auth_time <= when,
            )
        )

    async def last(
        self, digest: str, user_id: UUID
    ) -> tuple[datetime, frozenset[str]] | None:
        """When credential ``digest`` of ``user_id`` last stepped up, and with what."""
        row = (
            await self.session.execute(
                select(step_ups.c.auth_time, step_ups.c.amr).where(
                    step_ups.c.id == digest, step_ups.c.user_id == user_id
                )
            )
        ).first()
        return None if row is None else (row.auth_time, frozenset(row.amr.split()))

    async def clear_older(self, before: datetime) -> int:
        """Delete step-ups from before ``before``: no rule takes them; how many."""
        result = await self.session.execute(
            delete(step_ups).where(step_ups.c.auth_time < before)
        )
        return changed_rows(result)


@dataclass(frozen=True, slots=True)
class ScaChallenge:
    """A transaction waiting for its user's authorization, or holding it.

    Its ``repr`` names its id and state alone: the payee may be personal data.
    """

    id: UUID
    user_id: UUID
    amount: Money = dataclasses.field(repr=False)
    payee: str = dataclasses.field(repr=False)
    purpose: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    amr: str | None = None
    """What confirmed it, RFC 8176's names separated by spaces."""
    consumed_at: datetime | None = None

    def expired(self, now: datetime | None = None) -> bool:
        """Whether it no longer works."""
        return self.expires_at <= (now or utc_now())


class ScaStore:
    """Transactions' authorizations, by id (``auth.sca``)."""

    def __init__(self, session: AsyncSession) -> None:
        """Work in ``session``."""
        self.session = session

    async def add(
        self,
        user_id: UUID,
        amount: Money,
        payee: str,
        purpose: str,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> ScaChallenge:
        """Store a transaction for ``user_id`` to authorize within ``ttl``."""
        moment = now or utc_now()
        challenge = ScaChallenge(
            id=uuid.uuid4(),
            user_id=user_id,
            amount=amount,
            payee=payee,
            purpose=purpose,
            created_at=moment,
            expires_at=moment + ttl,
        )
        await self.session.execute(
            insert(sca_challenges).values(
                id=challenge.id,
                user_id=user_id,
                amount=amount.amount,
                amount_currency=amount.currency.code,
                payee=payee,
                purpose=purpose,
                created_at=moment,
                expires_at=challenge.expires_at,
            )
        )
        return challenge

    async def get(self, challenge_id: UUID) -> ScaChallenge | None:
        """The transaction ``challenge_id``, in any state."""
        row = (
            await self.session.execute(
                select(sca_challenges).where(sca_challenges.c.id == challenge_id)
            )
        ).first()
        if row is None:
            return None
        values = dict(row._mapping)
        amount = Money(values.pop("amount"), values.pop("amount_currency"))
        return ScaChallenge(amount=amount, **values)

    async def confirm(
        self, challenge_id: UUID, when: datetime, methods: Iterable[str]
    ) -> bool:
        """Mark it confirmed at ``when``, if it is unconfirmed, unused, and live."""
        result = await self.session.execute(
            update(sca_challenges)
            .where(
                sca_challenges.c.id == challenge_id,
                sca_challenges.c.confirmed_at.is_(None),
                sca_challenges.c.consumed_at.is_(None),
                sca_challenges.c.expires_at > when,
            )
            .values(confirmed_at=when, amr=" ".join(sorted(set(methods))))
        )
        return changed_rows(result) == 1

    async def consume(self, challenge_id: UUID, when: datetime) -> bool:
        """Mark it used at ``when``, if it is unused; one caller wins."""
        result = await self.session.execute(
            update(sca_challenges)
            .where(
                sca_challenges.c.id == challenge_id,
                sca_challenges.c.consumed_at.is_(None),
            )
            .values(consumed_at=when)
        )
        return changed_rows(result) == 1

    async def clear_expired(self, now: datetime | None = None) -> int:
        """Delete transactions whose authorization no longer works; how many."""
        result = await self.session.execute(
            delete(sca_challenges).where(
                sca_challenges.c.expires_at <= (now or utc_now())
            )
        )
        return changed_rows(result)
