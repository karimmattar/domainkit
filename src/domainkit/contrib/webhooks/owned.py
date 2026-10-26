"""The self-service webhooks API: a subject manages its own subscriptions only.

Mount it where your users' API lives (it needs ``domainkit.contrib.auth``)::

    from domainkit.contrib.webhooks.owned import router as my_webhooks

    router.include_router(my_webhooks, prefix="/webhooks")

Each endpoint needs an authenticated user, whose subscriptions are those
it made here: their owner is the user's id (``owned_router(owner=...)``
names another, such as the user's tenant). Another owner's subscription
is 404, as one that does not exist. An owned subscription receives only
the events ``expose(..., owner=...)`` gives its owner, never another's
and never those exposed without an owner. An owner may have
``WEBHOOKS.MAX_SUBSCRIPTIONS_PER_OWNER``, and its deliveries share one
quarter of the guard's lookup threads, however many it has.

As in the admin API, the answers that hold a secret are marked
``Cache-Control: no-store``.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select

from domainkit.apps import registry
from domainkit.contrib.auth import AbstractBaseUser, AuthenticatedUser
from domainkit.db import connections
from domainkit.ddd import BusinessRuleViolation
from domainkit.http import error_responses

from .admin import (
    CHANGE,
    COMMON,
    ONE,
    DeliveryOut,
    NewSubscriptionOut,
    Offset,
    PageSize,
    SecretOut,
    SubscriptionIn,
    SubscriptionOut,
    SubscriptionPatch,
    _checked,
    _no_store,
)
from .delivery import webhooks_alias
from .models import MAX_OWNER, Subscription, SubscriptionId, SubscriptionNotFound
from .settings import WebhookSettings
from .tables import deliveries
from .uow import WebhooksUnitOfWork

__all__ = [
    "SubscriptionLimitReached",
    "owned_router",
    "router",
    "user_owner",
]

type OwnerOf = Callable[[AbstractBaseUser], str]
type DeliveryStatus = Literal["pending", "succeeded", "failed", "cancelled"]


class SubscriptionLimitReached(BusinessRuleViolation):
    """The owner has ``WEBHOOKS.MAX_SUBSCRIPTIONS_PER_OWNER`` subscriptions."""

    code = "webhook_subscription_limit"


def user_owner(user: AbstractBaseUser) -> str:
    """The owner of a user's subscriptions: the user's id, as text."""
    return str(user.id)


def _owned(subscription: Subscription, owner: str) -> Subscription:
    """``subscription`` if ``owner`` owns it; else 404, as if it did not exist."""
    if subscription.owner != owner:
        raise SubscriptionNotFound(f"No webhook subscription has id {subscription.id}.")
    return subscription


def owned_router(owner: OwnerOf = user_owner) -> APIRouter:
    """The self-service routes, with ``owner`` naming whose subscriptions are whose.

    ``owner`` maps the authenticated user to an owner: its id by default,
    or, say, its tenant (``lambda user: str(user.tenant_id)``); the
    events ``expose(..., owner=...)`` gives must name owners the same way.
    """
    routes = APIRouter(tags=["webhooks"])

    def owner_of(user: AuthenticatedUser) -> str:
        found = owner(user)
        if not 1 <= len(found) <= MAX_OWNER:
            raise BusinessRuleViolation(
                f"A subscription's owner has 1 to {MAX_OWNER} characters."
            )
        return found

    Owner = Annotated[str, Depends(owner_of)]

    async def mine(
        uow: WebhooksUnitOfWork, subscription_id: UUID, me: str
    ) -> Subscription:
        """The owner's subscription, or 404."""
        found = await uow.subscriptions.get(SubscriptionId(subscription_id))
        return _owned(found, me)

    @routes.get("/subscriptions", responses=error_responses(*COMMON, 422))
    async def list_own_subscriptions(
        me: Owner, limit: PageSize = 50, offset: Offset = 0
    ) -> list[SubscriptionOut]:
        """Your subscriptions, oldest first."""
        async with WebhooksUnitOfWork() as uow:
            found = await uow.subscriptions.page(limit=limit, offset=offset, owner=me)
        return [SubscriptionOut.of(subscription) for subscription in found]

    @routes.post(
        "/subscriptions",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(*CHANGE),
    )
    async def create_own_subscription(
        body: SubscriptionIn, response: Response, me: Owner
    ) -> NewSubscriptionOut:
        """Subscribe a URL to your events of the types given; returns its secret, once.

        The URL is checked as the admin API checks it.
        """
        _no_store(response)
        host = _checked(body.url)
        limit = registry.settings(WebhookSettings).MAX_SUBSCRIPTIONS_PER_OWNER
        subscription, secret = Subscription.create(
            body.url,
            host,
            body.event_types,
            body.description,
            active=body.active,
            owner=me,
        )
        async with WebhooksUnitOfWork() as uow:
            if await uow.subscriptions.count_owned(me) >= limit:
                raise SubscriptionLimitReached(
                    f"You have {limit} webhook subscriptions, the most allowed; "
                    "delete one first."
                )
            await uow.subscriptions.add(subscription)
            await uow.commit()
        return NewSubscriptionOut(
            **SubscriptionOut.of(subscription).model_dump(), secret=secret
        )

    @routes.get("/subscriptions/{subscription_id}", responses=error_responses(*ONE))
    async def read_own_subscription(
        subscription_id: UUID, me: Owner
    ) -> SubscriptionOut:
        """One of your subscriptions."""
        async with WebhooksUnitOfWork() as uow:
            return SubscriptionOut.of(await mine(uow, subscription_id, me))

    @routes.patch(
        "/subscriptions/{subscription_id}", responses=error_responses(*CHANGE)
    )
    async def update_own_subscription(
        subscription_id: UUID, body: SubscriptionPatch, me: Owner
    ) -> SubscriptionOut:
        """Change your subscription's URL, event types, description, or activity."""
        host = None if body.url is None else _checked(body.url)
        async with WebhooksUnitOfWork() as uow:
            subscription = await mine(uow, subscription_id, me)
            subscription.change(
                url=body.url,
                event_types=body.event_types,
                active=body.active,
                description=body.description,
                host=host,
            )
            await uow.commit()
        return SubscriptionOut.of(subscription)

    @routes.post(
        "/subscriptions/{subscription_id}/rotate-secret",
        responses=error_responses(*CHANGE),
    )
    async def rotate_own_secret(
        subscription_id: UUID, response: Response, me: Owner
    ) -> SecretOut:
        """Give one of your subscriptions a new secret, shown once."""
        _no_store(response)
        overlap = timedelta(
            seconds=registry.settings(WebhookSettings).SECRET_OVERLAP_SECONDS
        )
        async with WebhooksUnitOfWork() as uow:
            subscription = await mine(uow, subscription_id, me)
            secret = subscription.rotate_secret(overlap)
            await uow.commit()
        return SecretOut(
            secret=secret, previous_valid_until=subscription.previous_secret_expires_at
        )

    @routes.delete(
        "/subscriptions/{subscription_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=error_responses(*ONE),
    )
    async def delete_own_subscription(subscription_id: UUID, me: Owner) -> None:
        """Delete one of your subscriptions; its pending deliveries are cancelled."""
        async with WebhooksUnitOfWork() as uow:
            subscription = await mine(uow, subscription_id, me)
            subscription.delete()
            await uow.subscriptions.remove(subscription)
            await uow.commit()

    @routes.get(
        "/subscriptions/{subscription_id}/deliveries",
        responses=error_responses(*ONE, 422),
    )
    async def list_own_deliveries(
        subscription_id: UUID,
        me: Owner,
        limit: PageSize = 50,
        offset: Offset = 0,
        status_: Annotated[DeliveryStatus | None, Query(alias="status")] = None,
    ) -> list[DeliveryOut]:
        """One of your subscriptions' deliveries, newest first, without bodies."""
        async with WebhooksUnitOfWork() as uow:
            await mine(uow, subscription_id, me)
        statement = (
            select(deliveries)
            .where(deliveries.c.subscription_id == subscription_id)
            .order_by(deliveries.c.created_at.desc(), deliveries.c.id)
            .limit(limit)
            .offset(offset)
        )
        if status_ is not None:
            statement = statement.where(deliveries.c.status == status_)
        async with connections.engine(webhooks_alias()).connect() as connection:
            rows = (await connection.execute(statement)).all()
        return [DeliveryOut.of(row) for row in rows]

    return routes


router: Final = owned_router()
"""The self-service routes, for subscriptions owned by the user's id."""
