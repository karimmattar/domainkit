"""The webhooks admin API: subscriptions and their deliveries, for staff.

Mount it where staff tools live (it needs ``domainkit.contrib.auth``)::

    from domainkit.contrib.webhooks.admin import router as webhooks_admin

    router.include_router(webhooks_admin, prefix="/admin/webhooks")

Every endpoint needs a staff user (``is_staff``) or a superuser, and the
permission of its action on ``webhooks.subscription``: ``view``, ``add``,
``change`` (also to rotate a secret or replay a delivery), or ``delete``.
A subscription's secret is in the answer that creates or rotates it, and
nowhere else.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select

from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.contrib.auth import AnyUser
from domainkit.contrib.auth.admin import staff
from domainkit.db import connections
from domainkit.ddd import NotFound
from domainkit.http import ErrorResponse

from .delivery import replay, webhooks_alias
from .guard import MAX_URL_LENGTH, checked_url
from .models import MAX_DESCRIPTION, MAX_EVENT_TYPES, Subscription, SubscriptionId
from .settings import WebhookSettings
from .tables import attempts, deliveries
from .uow import WebhooksUnitOfWork

router = APIRouter(tags=["webhooks admin"])

MAX_PAGE_SIZE: Final = 200
MAX_OFFSET: Final = 10_000
COMMON: Final = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
ONE: Final = (*COMMON, status.HTTP_404_NOT_FOUND)
CHANGE: Final = (*ONE, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT)

PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0, le=MAX_OFFSET)]
EventTypes = Annotated[
    list[Annotated[str, Field(max_length=128)]],
    Field(min_length=1, max_length=MAX_EVENT_TYPES),
]


class DeliveryNotFound(NotFound):
    """No webhook delivery has that id."""

    code = "webhook_delivery_not_found"


def _errors(*codes: int) -> dict[int | str, dict[str, object]]:
    """OpenAPI entries for the error statuses an endpoint returns."""
    return {code: {"model": ErrorResponse} for code in codes}


def _staff(action: str) -> Any:  # noqa: ANN401 - Depends.
    """Staff with ``action`` on webhook subscriptions."""
    return staff(action, lambda: Subscription)


class SubscriptionIn(BaseModel):
    """A new subscription."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=MAX_URL_LENGTH)
    event_types: EventTypes
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    active: bool = True


class SubscriptionPatch(BaseModel):
    """Changes to a subscription; what is left out stays."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    event_types: EventTypes | None = None
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    active: bool | None = None


class SubscriptionOut(BaseModel):
    """A subscription, without its secret."""

    id: UUID
    url: str
    event_types: list[str]
    active: bool
    description: str
    created_at: datetime
    rotating: bool
    """The previous secret still signs deliveries."""

    @classmethod
    def of(cls, subscription: Subscription) -> Self:
        """Describe ``subscription``."""
        return cls(
            id=subscription.id,
            url=subscription.url,
            event_types=list(subscription.event_types),
            active=subscription.active,
            description=subscription.description,
            created_at=subscription.created_at,
            rotating=len(subscription.signing_secrets(datetime.now(UTC))) > 1,
        )


class NewSubscriptionOut(SubscriptionOut):
    """A new subscription and its secret, shown this once."""

    secret: str


class SecretOut(BaseModel):
    """A new secret, shown this once."""

    secret: str
    previous_valid_until: datetime | None
    """Until then, deliveries carry a signature with the old secret too."""


class DeliveryOut(BaseModel):
    """A delivery and how its attempts went."""

    id: UUID
    webhook_id: str
    subscription_id: UUID
    event_type: str
    status: str
    attempts: int
    created_at: datetime
    last_attempt_at: datetime | None
    finished_at: datetime | None
    last_status_code: int | None
    last_error: str | None

    @classmethod
    def of(cls, row: Any) -> Self:  # noqa: ANN401 - a result row.
        """Describe a delivery row."""
        return cls(
            id=row.id,
            webhook_id=f"msg_{row.id.hex}",
            subscription_id=row.subscription_id,
            event_type=row.event_type,
            status=row.status,
            attempts=row.attempts,
            created_at=row.created_at,
            last_attempt_at=row.last_attempt_at,
            finished_at=row.finished_at,
            last_status_code=row.last_status_code,
            last_error=row.last_error,
        )


class AttemptOut(BaseModel):
    """One attempt of a delivery."""

    number: int
    started_at: datetime
    status_code: int | None
    duration_ms: int
    error: str | None


class DeliveryDetailOut(DeliveryOut):
    """A delivery, what it sends, and every attempt."""

    payload: JsonValue
    log: list[AttemptOut]


class ReplayOut(BaseModel):
    """Whether a delivery was queued again."""

    replayed: bool


async def _subscription(uow: WebhooksUnitOfWork, subscription_id: UUID) -> Subscription:
    """The subscription, or 404."""
    return await uow.subscriptions.get(SubscriptionId(subscription_id))


def _checked(url: str) -> str:
    """The host of ``url``, once the guard accepts it (else 422)."""
    config = registry.settings(WebhookSettings)
    parsed = checked_url(
        url, allow_http=settings.DEBUG, allowed=config.ALLOWED_PRIVATE_NETWORKS
    )
    return parsed.raw_host.decode("ascii")


@router.get("/subscriptions", responses=_errors(*COMMON, 422))
async def list_subscriptions(
    actor: Annotated[AnyUser, _staff("view")],
    limit: PageSize = 50,
    offset: Offset = 0,
) -> list[SubscriptionOut]:
    """Subscriptions, oldest first."""
    async with WebhooksUnitOfWork() as uow:
        found = await uow.subscriptions.page(limit=limit, offset=offset)
    return [SubscriptionOut.of(subscription) for subscription in found]


@router.post(
    "/subscriptions", status_code=status.HTTP_201_CREATED, responses=_errors(*CHANGE)
)
async def create_subscription(
    body: SubscriptionIn, actor: Annotated[AnyUser, _staff("add")]
) -> NewSubscriptionOut:
    """Subscribe a URL to event types (``*`` for all); returns its secret, once.

    The URL must use https and may not name a private address (see
    ``WEBHOOKS.ALLOWED_PRIVATE_NETWORKS``); its host is checked again
    before every delivery.
    """
    host = _checked(body.url)
    subscription = Subscription.create(
        body.url, host, body.event_types, body.description, active=body.active
    )
    async with WebhooksUnitOfWork() as uow:
        await uow.subscriptions.add(subscription)
        await uow.commit()
    return NewSubscriptionOut(
        **SubscriptionOut.of(subscription).model_dump(), secret=subscription.secret
    )


@router.get("/subscriptions/{subscription_id}", responses=_errors(*ONE))
async def read_subscription(
    subscription_id: UUID, actor: Annotated[AnyUser, _staff("view")]
) -> SubscriptionOut:
    """One subscription."""
    async with WebhooksUnitOfWork() as uow:
        return SubscriptionOut.of(await _subscription(uow, subscription_id))


@router.patch("/subscriptions/{subscription_id}", responses=_errors(*CHANGE))
async def update_subscription(
    subscription_id: UUID,
    body: SubscriptionPatch,
    actor: Annotated[AnyUser, _staff("change")],
) -> SubscriptionOut:
    """Change a subscription's URL, event types, description, or activity."""
    if body.url is not None:
        _checked(body.url)
    async with WebhooksUnitOfWork() as uow:
        subscription = await _subscription(uow, subscription_id)
        subscription.change(
            url=body.url,
            event_types=body.event_types,
            active=body.active,
            description=body.description,
        )
        await uow.commit()
    return SubscriptionOut.of(subscription)


@router.post(
    "/subscriptions/{subscription_id}/rotate-secret", responses=_errors(*CHANGE)
)
async def rotate_secret(
    subscription_id: UUID, actor: Annotated[AnyUser, _staff("change")]
) -> SecretOut:
    """Give a subscription a new secret, shown once.

    The old one also signs deliveries for ``WEBHOOKS.SECRET_OVERLAP_SECONDS``,
    so receivers can switch without missing any.
    """
    overlap = timedelta(
        seconds=registry.settings(WebhookSettings).SECRET_OVERLAP_SECONDS
    )
    async with WebhooksUnitOfWork() as uow:
        subscription = await _subscription(uow, subscription_id)
        secret = subscription.rotate_secret(overlap)
        await uow.commit()
    return SecretOut(
        secret=secret, previous_valid_until=subscription.previous_secret_expires_at
    )


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*ONE),
)
async def delete_subscription(
    subscription_id: UUID, actor: Annotated[AnyUser, _staff("delete")]
) -> None:
    """Delete a subscription; its pending deliveries are cancelled when due."""
    async with WebhooksUnitOfWork() as uow:
        subscription = await _subscription(uow, subscription_id)
        subscription.delete()
        await uow.subscriptions.remove(subscription)
        await uow.commit()


@router.get("/subscriptions/{subscription_id}/deliveries", responses=_errors(*ONE, 422))
async def list_deliveries(
    subscription_id: UUID,
    actor: Annotated[AnyUser, _staff("view")],
    limit: PageSize = 50,
    offset: Offset = 0,
    status_: Annotated[
        Literal["pending", "succeeded", "failed", "cancelled"] | None,
        Query(alias="status"),
    ] = None,
) -> list[DeliveryOut]:
    """A subscription's deliveries, newest first."""
    async with WebhooksUnitOfWork() as uow:
        await _subscription(uow, subscription_id)
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


@router.get("/deliveries/{delivery_id}", responses=_errors(*ONE))
async def read_delivery(
    delivery_id: UUID, actor: Annotated[AnyUser, _staff("view")]
) -> DeliveryDetailOut:
    """A delivery, its body, and its attempts."""
    async with connections.engine(webhooks_alias()).connect() as connection:
        row = (
            await connection.execute(
                select(deliveries).where(deliveries.c.id == delivery_id)
            )
        ).first()
        if row is None:
            raise DeliveryNotFound(f"No webhook delivery has id {delivery_id}.")
        log = (
            await connection.execute(
                select(attempts)
                .where(attempts.c.delivery_id == delivery_id)
                .order_by(attempts.c.number)
            )
        ).all()
    return DeliveryDetailOut(
        **DeliveryOut.of(row).model_dump(),
        payload=json.loads(row.body),
        log=[
            AttemptOut(
                number=entry.number,
                started_at=entry.started_at,
                status_code=entry.status_code,
                duration_ms=entry.duration_ms,
                error=entry.error,
            )
            for entry in log
        ],
    )


@router.post(
    "/deliveries/{delivery_id}/replay",
    status_code=status.HTTP_202_ACCEPTED,
    responses=_errors(*ONE),
)
async def replay_delivery(
    delivery_id: UUID, actor: Annotated[AnyUser, _staff("change")]
) -> ReplayOut:
    """Send a finished delivery again (same ``webhook-id``); a pending one stays."""
    async with connections.engine(webhooks_alias()).connect() as connection:
        found = await connection.scalar(
            select(deliveries.c.id).where(deliveries.c.id == delivery_id)
        )
    if found is None:
        raise DeliveryNotFound(f"No webhook delivery has id {delivery_id}.")
    return ReplayOut(replayed=bool(await replay([delivery_id])))
