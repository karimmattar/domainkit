"""Deliveries: queued from committed events, sent by tasks, retried, and replayed.

An exposed event committed with a change becomes a durable handler call in
the outbox (``fan_out``), so nothing is sent unless the change commits. The
worker then writes one delivery per subscription that wants the event, and
one ``deliver`` task each, in a single transaction. Each attempt signs the
body anew and is logged (status code, latency, error). A failed attempt is
retried with the task's backoff, up to ``WEBHOOKS.MAX_ATTEMPTS``; then the
delivery is marked failed, and ``webhooks replay`` can send it again.
"""

import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic_core import to_jsonable_python
from sqlalchemy import Executable, delete, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from domainkit import clock
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.contrib.messaging import Backoff, current_attempt, periodic, task
from domainkit.contrib.messaging.outbox import (
    context_headers,
    message_row,
    notify_committed,
    write,
)
from domainkit.contrib.messaging.registry import dump_event, spec_for
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.db import connections
from domainkit.db.routing import router
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.reporting import report_error

from . import client
from .models import ALL_EVENTS, EVENT_TYPE, signing_secrets
from .settings import WebhookSettings
from .signing import signed_headers
from .tables import (
    CANCELLED,
    FAILED,
    PENDING,
    SUCCEEDED,
    attempts,
    deliveries,
    subscriptions,
)

logger = logging.getLogger(__name__)

DELIVER: Final = "domainkit.contrib.webhooks.deliver"
"""The task key of one delivery's attempts."""
PURGE: Final = "domainkit.contrib.webhooks.purge"
"""The hourly task that deletes old finished deliveries."""
DATABASE_MARGIN: Final = 30.0
"""Seconds a delivery task may take beyond ``TIMEOUT``, for its database work."""
NAMESPACE: Final = uuid.UUID("5d0a6c2e-6a43-4e58-9a59-5f1e3c3bd2a1")
"""Delivery ids are derived from the event and subscription in this namespace."""
PURGE_BATCH: Final = 1000

type Payload = Callable[[Any], Mapping[str, Any]]


class PayloadTooLarge(ValueError):
    """An event's webhook body is over ``WEBHOOKS.MAX_PAYLOAD_BYTES``."""


class DeliveryFailed(RuntimeError):
    """An attempt failed; the worker retries the delivery after its backoff."""


@dataclass(frozen=True, slots=True)
class Exposure:
    """An event class sent to subscribers as an event type."""

    name: str
    payload: Payload | None


_exposed: dict[type[DomainEvent], Exposure] = {}


def expose[E: DomainEvent](
    event_type: type[E],
    name: str,
    *,
    payload: Callable[[E], Mapping[str, Any]] | None = None,
    bus: EventBus = event_bus,
) -> None:
    """Send committed ``event_type`` events to the subscribers of ``name``.

    Call it in ``DomainConfig.ready()``. ``payload`` picks the event's
    ``data`` (by default every field, as JSON): leave personal data out.
    Subclasses of ``event_type`` are sent too.
    """
    if not EVENT_TYPE.fullmatch(name):
        raise ImproperlyConfigured(
            f"Webhook event types are up to 128 letters, digits, '_', '.', ':', "
            f"or '-', not {name!r}."
        )
    _exposed[event_type] = Exposure(name, payload)
    bus.subscribe(event_type, fan_out, durable=True)


def exposure_for(event_type: type[DomainEvent]) -> Exposure | None:
    """How ``event_type`` is sent, from its own class or a base class."""
    for cls in event_type.__mro__:
        if cls in _exposed:
            return _exposed[cls]
    return None


def webhooks_alias() -> str:
    """The SQL alias holding the webhook tables (one database)."""
    return router.require_standalone(
        router.target_for("webhooks"), "DATABASE_ROUTING['webhooks']"
    )


def _now() -> datetime:
    """Return the current time in UTC (``domainkit.clock``)."""
    return clock.now()


def body_of(event_type: str, data: Mapping[str, Any], occurred_at: datetime) -> bytes:
    """The JSON a delivery sends: ``type``, ``timestamp``, and ``data``."""
    stamp = occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    document = {"type": event_type, "timestamp": stamp, "data": data}
    return json.dumps(
        to_jsonable_python(document), separators=(",", ":"), ensure_ascii=False
    ).encode()


async def fan_out(event: DomainEvent) -> None:
    """Queue a delivery of ``event`` to each subscriber (a durable handler)."""
    exposure = exposure_for(type(event))
    if exposure is None:  # No longer exposed since it was committed.
        return
    data = exposure.payload(event) if exposure.payload else dump_event(event)
    try:
        await queue_deliveries(
            exposure.name, data, key=str(event.event_id), occurred_at=event.occurred_at
        )
    except PayloadTooLarge as exc:  # Trying again cannot help: report it once.
        logger.error("Dropped a webhook event: %s", exc)
        report_error(exc, event_type=exposure.name)


def _insert_new(dialect: str, rows: Sequence[dict[str, Any]]) -> Executable:
    """Insert delivery rows, skipping those already there (a second fan-out)."""
    statement: Any
    if dialect == "postgresql":
        statement = postgresql.insert(deliveries)
    elif dialect == "sqlite":
        statement = sqlite.insert(deliveries)
    else:
        raise ImproperlyConfigured(
            f"Webhooks need PostgreSQL or SQLite, not {dialect}."
        )
    inserted: Executable = statement.values(rows).on_conflict_do_nothing(
        index_elements=["id"]
    )
    return inserted


def _deliver_message(delivery_id: uuid.UUID, *, dedupe: bool) -> dict[str, Any]:
    """The outbox row of a ``deliver`` task for ``delivery_id``."""
    spec = spec_for(deliver)
    return message_row(
        kind="task",
        target=spec.key,
        payload={"delivery_id": str(delivery_id)},
        headers=context_headers(),
        max_attempts=spec.max_attempts or 1,
        queue=spec.queue,
        dedupe_key=f"webhook:{delivery_id}" if dedupe else None,
        priority=spec.priority,
    )


async def queue_deliveries(
    event_type: str, data: Mapping[str, Any], *, key: str, occurred_at: datetime
) -> int:
    """Queue a delivery of one event to each subscriber; return how many.

    ``key`` identifies the event: queueing it again (a retried fan-out)
    adds nothing.

    Raises:
        PayloadTooLarge: the body is over ``MAX_PAYLOAD_BYTES``.
    """
    config = registry.settings(WebhookSettings)
    body = body_of(event_type, data, occurred_at)
    if len(body) > config.MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(
            f"The {event_type} webhook body has {len(body)} bytes; "
            f"WEBHOOKS.MAX_PAYLOAD_BYTES is {config.MAX_PAYLOAD_BYTES}."
        )
    alias = webhooks_alias()
    now = _now()
    async with connections.engine(alias).begin() as connection:
        found = await connection.execute(
            select(subscriptions.c.id, subscriptions.c.event_types).where(
                subscriptions.c.active.is_(True)
            )
        )
        wanted = [
            row.id
            for row in found
            if ALL_EVENTS in row.event_types or event_type in row.event_types
        ]
        if not wanted:
            return 0
        rows = [
            {
                "id": uuid.uuid5(NAMESPACE, f"{key}:{subscription_id}"),
                "subscription_id": subscription_id,
                "event_type": event_type,
                "body": body.decode(),
                "status": PENDING,
                "attempts": 0,
                "created_at": now,
            }
            for subscription_id in wanted
        ]
        dialect = connection.dialect.name
        await connection.execute(_insert_new(dialect, rows))
        messages = [_deliver_message(row["id"], dedupe=True) for row in rows]
        await write(connection, dialect, messages)
    await _committed(alias, messages)
    return len(wanted)


async def _committed(alias: str, messages: Sequence[dict[str, Any]]) -> None:
    """Hand committed messages on; the relay publishes them.

    Publishing here would leave them locked for ``REPUBLISH_AFTER`` if a
    stopping process cut the publication short.
    """
    await notify_committed(
        registry.settings(MessagingSettings), alias, messages, publish=False
    )


async def deliver(delivery_id: uuid.UUID) -> None:
    """Make one attempt at a delivery (the task behind every delivery).

    Raises:
        DeliveryFailed: the attempt failed and attempts are left.
    """
    config = registry.settings(WebhookSettings)
    engine = connections.engine(webhooks_alias())
    async with engine.connect() as connection:
        delivery = (
            await connection.execute(
                select(
                    deliveries.c.status, deliveries.c.body, deliveries.c.subscription_id
                ).where(deliveries.c.id == delivery_id)
            )
        ).first()
        if delivery is None or delivery.status != PENDING:
            return  # Gone, or finished (a duplicate run).
        subscription = (
            await connection.execute(
                select(subscriptions).where(
                    subscriptions.c.id == delivery.subscription_id
                )
            )
        ).first()
    if subscription is None or not subscription.active:
        await _finish(
            engine, delivery_id, CANCELLED, "The subscription is gone or inactive."
        )
        return
    started = _now()
    body = delivery.body.encode()
    headers = signed_headers(
        signing_secrets(
            subscription.secret,
            subscription.previous_secret,
            subscription.previous_secret_expires_at,
            started,
        ),
        f"msg_{delivery_id.hex}",
        int(started.timestamp()),
        body,
    )
    outcome = await client.post(
        subscription.url, body, headers, config, allow_http=settings.DEBUG
    )
    attempt = current_attempt()
    final = outcome.ok or outcome.refused or attempt is None or attempt.last
    status = SUCCEEDED if outcome.ok else FAILED if final else PENDING
    number = await _record(engine, delivery_id, started, outcome, status)
    if outcome.ok:
        return
    logger.warning(
        "Webhook delivery %s to subscription %s failed (attempt %d%s): %s",
        delivery_id,
        subscription.id,
        number,
        "; giving up" if final else "; retrying",
        outcome.error,
    )
    if not final:
        raise DeliveryFailed(outcome.error or "The attempt failed.")


async def _record(
    engine: AsyncEngine,
    delivery_id: uuid.UUID,
    started: datetime,
    outcome: client.Outcome,
    status: str,
) -> int:
    """Log the attempt and update its delivery; return the attempt's number."""
    async with engine.begin() as connection:
        number = (
            await connection.execute(
                update(deliveries)
                .where(deliveries.c.id == delivery_id)
                .values(
                    attempts=deliveries.c.attempts + 1,
                    status=status,
                    last_attempt_at=started,
                    last_status_code=outcome.status,
                    last_error=outcome.error,
                    finished_at=None if status == PENDING else _now(),
                )
                .returning(deliveries.c.attempts)
            )
        ).scalar_one()
        await connection.execute(
            insert(attempts).values(
                id=uuid.uuid4(),
                delivery_id=delivery_id,
                number=number,
                started_at=started,
                status_code=outcome.status,
                duration_ms=round(outcome.seconds * 1000),
                error=outcome.error,
            )
        )
    return int(number)


async def _finish(
    engine: AsyncEngine, delivery_id: uuid.UUID, status: str, error: str
) -> None:
    """Close a pending delivery without an attempt."""
    async with engine.begin() as connection:
        await connection.execute(
            update(deliveries)
            .where(deliveries.c.id == delivery_id, deliveries.c.status == PENDING)
            .values(status=status, last_error=error, finished_at=_now())
        )


async def replay(delivery_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Send finished deliveries again, under the same ``webhook-id``; return them.

    Deliveries still being tried are left alone.
    """
    if not delivery_ids:
        return []
    alias = webhooks_alias()
    async with connections.engine(alias).begin() as connection:
        replayed = list(
            (
                await connection.execute(
                    update(deliveries)
                    .where(
                        deliveries.c.id.in_(delivery_ids),
                        deliveries.c.status != PENDING,
                    )
                    .values(status=PENDING, finished_at=None)
                    .returning(deliveries.c.id)
                )
            ).scalars()
        )
        messages = [_deliver_message(id_, dedupe=False) for id_ in replayed]
        if messages:
            await write(connection, connection.dialect.name, messages)
    await _committed(alias, messages)
    return replayed


async def failed_deliveries(
    limit: int, subscription_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """The ids of the most recent failed deliveries."""
    statement = (
        select(deliveries.c.id)
        .where(deliveries.c.status == FAILED)
        .order_by(deliveries.c.finished_at.desc())
        .limit(limit)
    )
    if subscription_id is not None:
        statement = statement.where(deliveries.c.subscription_id == subscription_id)
    async with connections.engine(webhooks_alias()).connect() as connection:
        return list((await connection.execute(statement)).scalars())


async def purge(retention: timedelta) -> int:
    """Delete finished deliveries older than ``retention``, and their attempts."""
    cutoff = _now() - retention
    removed = 0
    async with connections.engine(webhooks_alias()).begin() as connection:
        while True:
            batch = await _old(connection, cutoff)
            if batch:
                await connection.execute(
                    delete(attempts).where(attempts.c.delivery_id.in_(batch))
                )
                await connection.execute(
                    delete(deliveries).where(deliveries.c.id.in_(batch))
                )
                removed += len(batch)
            if len(batch) < PURGE_BATCH:
                return removed


async def _old(connection: AsyncConnection, cutoff: datetime) -> list[uuid.UUID]:
    """Up to ``PURGE_BATCH`` finished deliveries older than ``cutoff``."""
    statement = (
        select(deliveries.c.id)
        .where(
            deliveries.c.status != PENDING,
            deliveries.c.finished_at < cutoff,
        )
        .limit(PURGE_BATCH)
    )
    return list((await connection.execute(statement)).scalars())


async def purge_deliveries() -> None:
    """Delete finished deliveries older than ``RETENTION_SECONDS`` (hourly)."""
    config = registry.settings(WebhookSettings)
    await purge(timedelta(seconds=config.RETENTION_SECONDS))


def register(config: WebhookSettings) -> None:
    """Register ``deliver`` with the queue, attempts, and backoff of ``config``.

    And the hourly purge of old deliveries.
    """
    periodic(timedelta(hours=1), name=PURGE)(purge_deliveries)
    task(
        name=DELIVER,
        queue=config.QUEUE,
        max_attempts=config.MAX_ATTEMPTS,
        backoff=Backoff(config.RETRY_BASE_SECONDS, config.RETRY_MAX_SECONDS),
        timeout=config.TIMEOUT + DATABASE_MARGIN,
    )(deliver)
