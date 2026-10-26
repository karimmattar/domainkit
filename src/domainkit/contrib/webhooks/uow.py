"""The unit of work over webhook subscriptions."""

from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.ddd import NotFound

from .models import Subscription, SubscriptionId, SubscriptionNotFound
from .tables import subscriptions

_OWNER_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtext('domainkit.webhooks.owner:' || :owner))"
)
"""One owner's creates, one at a time, until the transaction ends (PostgreSQL)."""


class SubscriptionRepository(SqlAlchemyRepository[Subscription, SubscriptionId]):
    """Webhook subscriptions, oldest first."""

    model = Subscription

    def not_found(self, id_: SubscriptionId) -> NotFound:
        """The error for a missing subscription."""
        return SubscriptionNotFound(f"No webhook subscription has id {id_}.")

    async def page(
        self, *, limit: int, offset: int = 0, owner: str | None = None
    ) -> list[Subscription]:
        """Up to ``limit`` subscriptions, from the ``offset``-th, oldest first.

        With ``owner``, only that subject's.
        """
        statement = (
            select(Subscription)
            .order_by(subscriptions.c.created_at, subscriptions.c.id)
            .limit(limit)
            .offset(offset)
        )
        if owner is not None:
            statement = statement.where(subscriptions.c.owner == owner)
        found = list(await self.session.scalars(statement))
        for subscription in found:
            self._track(subscription)
        return found

    async def owned_by(self, owner: str) -> list[Subscription]:
        """Every subscription ``owner`` has, oldest first."""
        statement = (
            select(Subscription)
            .where(subscriptions.c.owner == owner)
            .order_by(subscriptions.c.created_at, subscriptions.c.id)
        )
        found = list(await self.session.scalars(statement))
        for subscription in found:
            self._track(subscription)
        return found

    async def add_owned(self, subscription: Subscription, *, limit: int) -> bool:
        """Add ``subscription`` unless its owner has ``limit`` already; whether it did.

        Counted after the insert, in its transaction, under a lock of the
        owner (a transaction advisory lock on PostgreSQL; SQLite's insert
        takes the one write lock), so creates racing for the last place
        cannot all pass the count (SEC-10b-6). Refused, nothing is added
        once the transaction rolls back.
        """
        owner = subscription.owner
        assert owner is not None  # noqa: S101 - owned subscriptions only.
        if self.session.get_bind().dialect.name == "postgresql":
            await self.session.execute(_OWNER_LOCK, {"owner": owner})
        await self.add(subscription)
        await self.session.flush()
        return await self.count_owned(owner) <= limit

    async def count_owned(self, owner: str) -> int:
        """How many subscriptions ``owner`` has."""
        statement = (
            select(func.count())
            .select_from(subscriptions)
            .where(subscriptions.c.owner == owner)
        )
        return int(await self.session.scalar(statement) or 0)


class WebhooksUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the webhook tables, wherever ``webhooks`` is routed."""

    subscriptions: SubscriptionRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository to the session."""
        self.subscriptions = SubscriptionRepository(session)
