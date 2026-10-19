"""The unit of work over webhook subscriptions."""

from sqlalchemy import select
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

    async def page(self, *, limit: int, offset: int = 0) -> list[Subscription]:
        """Up to ``limit`` subscriptions, from the ``offset``-th, oldest first."""
        statement = (
            select(Subscription)
            .order_by(subscriptions.c.created_at, subscriptions.c.id)
            .limit(limit)
            .offset(offset)
        )
        found = list(await self.session.scalars(statement))
        for subscription in found:
            self._track(subscription)
        return found


class WebhooksUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the webhook tables, wherever ``webhooks`` is routed."""

    subscriptions: SubscriptionRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository to the session."""
        self.subscriptions = SubscriptionRepository(session)
