"""The unit of work over webhook subscriptions."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.ddd import NotFound

from .models import Subscription, SubscriptionId, SubscriptionNotFound
from .tables import subscriptions


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
