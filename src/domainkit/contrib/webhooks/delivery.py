"""Deliveries: queued from committed events, sent by tasks, retried, and replayed.

An exposed event committed with a change becomes a durable handler call in
the outbox (``fan_out``), so nothing is sent unless the change commits. The
worker then writes one delivery per subscription that wants the event, and
one ``deliver`` task each, in a single transaction. Each attempt signs the
body anew and is logged (status code, latency, error). A failed attempt is
retried with the task's backoff, up to ``WEBHOOKS.MAX_ATTEMPTS``; then the
delivery is marked failed, and ``webhooks replay`` can send it again.

Each attempt decrypts the secrets it signs with, and stores again those a
key in ``SECRET_KEY_FALLBACKS`` encrypted, under ``SECRET_KEY``. A secret
no key decrypts fails the delivery: nothing is sent unsigned.

A delivery is tagged with the people its event is about (the subjects of
its ``Personal`` fields), so ``privacy erase`` blanks its body and cancels
it if pending (``erase_subject``), and ``privacy export`` lists it
(``export_subject``).

A subscription made through the self-service API has an owner, and
receives only the events ``expose(..., owner=...)`` gives that owner;
staff's subscriptions have none, and receive every event they want.

Each attempt's host name lookups are its subscription's owner's
(``guard.lookups_for("webhook-owner:<owner>")``), or for staff's, its
own (``"webhook:<id>"``), not those of whoever caused the event: one
owner's subscriptions whose hosts' DNS never answers take at most a
quarter of the guard's lookup threads together, however many it made,
and other subscribers' deliveries of the same events still find threads
free.
"""

import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Executable,
    Row,
    delete,
    insert,
    or_,
    select,
    update,
)
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
from domainkit.db.query import contains_text
from domainkit.db.routing import router
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.ddd.personal import event_subjects, subject_tag, tagged_with
from domainkit.encryption import DecryptionFailed
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.guard import lookups_for
from domainkit.reporting import report_error
from domainkit.utils.decimals import jsonable

from . import client
from .models import ALL_EVENTS, EVENT_TYPE, previous_signs, secret_box
from .settings import WebhookSettings
from .signing import signed_headers
from .tables import (
    CANCELLED,
    ERASED,
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
ERASED_ERROR: Final = "The people this delivery was about were erased."
KEY_LOST: Final = (
    "No key decrypts the subscription's secret: SECRET_KEY changed without the "
    "old key in SECRET_KEY_FALLBACKS. Put it back, or rotate the secret, then "
    "replay. Nothing was sent."
)

type Payload = Callable[[Any], Mapping[str, Any]]
type Owner = Callable[[Any], object]


class PayloadTooLarge(ValueError):
    """An event's webhook body is over ``WEBHOOKS.MAX_PAYLOAD_BYTES``."""


class DeliveryFailed(RuntimeError):
    """An attempt failed; the worker retries the delivery after its backoff."""


@dataclass(frozen=True, slots=True)
class Exposure:
    """An event class sent to subscribers as an event type."""

    name: str
    payload: Payload | None
    owner: Owner | None = None
    """Whose event it is, for owned subscriptions; None: theirs never get it."""


_exposed: dict[type[DomainEvent], Exposure] = {}


def expose[E: DomainEvent](
    event_type: type[E],
    name: str,
    *,
    payload: Callable[[E], Mapping[str, Any]] | None = None,
    owner: Callable[[E], object] | None = None,
    bus: EventBus = event_bus,
) -> None:
    """Send committed ``event_type`` events to the subscribers of ``name``.

    Call it in ``DomainConfig.ready()``. ``payload`` picks the event's
    ``data`` (by default every field, as JSON): leave personal data out.
    Subclasses of ``event_type`` are sent too. ``owner`` names whose
    event it is (``lambda e: e.customer_id``): subscriptions that subject
    made through the self-service API get it too. Without one, or when it
    gives None, only staff's subscriptions do.
    """
    if not EVENT_TYPE.fullmatch(name):
        raise ImproperlyConfigured(
            f"Webhook event types are up to 128 letters, digits, '_', '.', ':', "
            f"or '-', not {name!r}."
        )
    _exposed[event_type] = Exposure(name, payload, owner)
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
        jsonable(document), separators=(",", ":"), ensure_ascii=False
    ).encode()


async def fan_out(event: DomainEvent) -> None:
    """Queue a delivery of ``event`` to each subscriber (a durable handler)."""
    exposure = exposure_for(type(event))
    if exposure is None:  # No longer exposed since it was committed.
        return
    data = exposure.payload(event) if exposure.payload else dump_event(event)
    owner = exposure.owner(event) if exposure.owner else None
    try:
        await queue_deliveries(
            exposure.name,
            data,
            key=str(event.event_id),
            occurred_at=event.occurred_at,
            subjects=event_subjects(event),
            owner=None if owner is None else str(owner),
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
    event_type: str,
    data: Mapping[str, Any],
    *,
    key: str,
    occurred_at: datetime,
    subjects: Iterable[str] = (),
    owner: str | None = None,
) -> int:
    """Queue a delivery of one event to each subscriber; return how many.

    ``key`` identifies the event: queueing it again (a retried fan-out)
    adds nothing. ``subjects`` are the people ``data`` is about: erasing
    one of them blanks these deliveries (``erase_subject``). Staff's
    subscriptions get it, and with ``owner``, that subject's own too.

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
        whose: ColumnElement[bool] = subscriptions.c.owner.is_(None)
        if owner is not None:
            whose = or_(whose, subscriptions.c.owner == owner)
        found = await connection.execute(
            select(subscriptions.c.id, subscriptions.c.event_types).where(
                subscriptions.c.active.is_(True), whose
            )
        )
        wanted = [
            row.id
            for row in found
            if ALL_EVENTS in row.event_types or event_type in row.event_types
        ]
        if not wanted:
            return 0
        tag = subject_tag(subjects)
        rows = [
            {
                "id": uuid.uuid5(NAMESPACE, f"{key}:{subscription_id}"),
                "subscription_id": subscription_id,
                "event_type": event_type,
                "body": body.decode(),
                "status": PENDING,
                "attempts": 0,
                "created_at": now,
                "subjects": tag,
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
    """Hand committed messages on; the relay publishes them, not this caller.

    A publication cut short by a stopping process is given back once its
    short lease ends (ADR 0042), and the relay picks the messages up then.
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
        if delivery is None or delivery.status != PENDING or delivery.body == ERASED:
            return  # Gone, finished (a duplicate run), or erased meanwhile.
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
    try:
        secrets, stale = _unsealed(subscription, started)
    except DecryptionFailed as exc:  # Never send it unsigned.
        await _finish(engine, delivery_id, FAILED, KEY_LOST)
        logger.error(
            "Webhook delivery %s to subscription %s failed: %s",
            delivery_id,
            subscription.id,
            KEY_LOST,
        )
        report_error(exc, subscription_id=str(subscription.id))
        return
    if stale:
        await _reseal(engine, subscription.id, stale)
    body = delivery.body.encode()
    headers = signed_headers(
        secrets, f"msg_{delivery_id.hex}", int(started.timestamp()), body
    )
    with lookups_for(lookup_owner(subscription.id, subscription.owner)):
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


def lookup_owner(subscription_id: uuid.UUID, owner: str | None) -> str:
    """Whose share of the guard's lookup threads a delivery draws on.

    An owned subscription's owner's, so many subscriptions of one owner
    share one quarter of the threads; a staff subscription's own.
    """
    return f"webhook:{subscription_id}" if owner is None else f"webhook-owner:{owner}"


def _unsealed(
    subscription: Row[Any], now: datetime
) -> tuple[list[str], dict[str, tuple[bytes, bytes]]]:
    """Decrypt the secrets a delivery made at ``now`` is signed with.

    Returns:
        The secrets, and for each column a fallback key encrypted, its value
        and the value encrypted again under ``SECRET_KEY``.

    Raises:
        DecryptionFailed: no key decrypts one of them.
    """
    box = secret_box()
    associated = subscription.id.bytes
    columns = ["secret"]
    if previous_signs(
        subscription.previous_secret, subscription.previous_secret_expires_at, now
    ):
        columns.append("previous_secret")
    secrets: list[str] = []
    stale: dict[str, tuple[bytes, bytes]] = {}
    for column in columns:
        sealed = subscription._mapping[column]
        plaintext, current = box.decrypt(sealed, associated=associated)
        secrets.append(plaintext.decode())
        if not current:
            stale[column] = (sealed, box.encrypt(plaintext, associated=associated))
    return secrets, stale


async def _reseal(
    engine: AsyncEngine,
    subscription_id: uuid.UUID,
    stale: Mapping[str, tuple[bytes, bytes]],
) -> None:
    """Store secrets encrypted again, unless a rotation replaced them meanwhile."""
    async with engine.begin() as connection:
        await connection.execute(
            update(subscriptions)
            .where(
                subscriptions.c.id == subscription_id,
                *(subscriptions.c[column] == old for column, (old, _) in stale.items()),
            )
            .values({column: new for column, (_, new) in stale.items()})
        )
    # The subscription's id only: the word "secrets" is what the rule matched.
    logger.info(  # nosemgrep: python-logger-credential-disclosure
        "Encrypted the secrets of webhook subscription %s again, under SECRET_KEY.",
        subscription_id,
    )


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
    """Close a pending delivery without sending it."""
    async with engine.begin() as connection:
        await connection.execute(
            update(deliveries)
            .where(deliveries.c.id == delivery_id, deliveries.c.status == PENDING)
            .values(status=status, last_error=error, finished_at=_now())
        )


async def replay(delivery_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Send finished deliveries again, under the same ``webhook-id``; return them.

    Deliveries still being tried are left alone, and so are erased ones:
    they have nothing left to send.
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
                        deliveries.c.body != ERASED,
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


async def erase_subject(subject: str) -> dict[str, int]:
    """Blank the bodies of deliveries about ``subject``; cancel the pending ones.

    The subscriptions ``subject`` owns are deleted (audited as any
    deletion), and their deliveries blanked too. Returns how many
    deliveries were blanked, how many of those were cancelled, and how
    many subscriptions went. Their rows and attempt logs stay, without
    what they would have sent; ``replay`` skips them.
    """
    from .uow import WebhooksUnitOfWork  # Imports the models, as this module.

    owned: list[uuid.UUID] = []
    async with WebhooksUnitOfWork() as uow:
        while True:
            found = await uow.subscriptions.page(
                limit=PURGE_BATCH, offset=len(owned), owner=subject
            )
            for subscription in found:
                subscription.delete()
                await uow.subscriptions.remove(subscription)
                owned.append(subscription.id)
            if len(found) < PURGE_BATCH:
                break
        await uow.commit()
    alias = webhooks_alias()
    async with connections.engine(alias).begin() as connection:
        named = or_(
            contains_text(
                deliveries.c.subjects, tagged_with(subject), connection.dialect.name
            ),
            deliveries.c.subscription_id.in_(owned),
        )
        cancelled = await connection.execute(
            update(deliveries)
            .where(named, deliveries.c.status == PENDING)
            .values(status=CANCELLED, finished_at=_now(), last_error=ERASED_ERROR)
        )
        blanked = await connection.execute(
            update(deliveries)
            .where(named, deliveries.c.body != ERASED)
            .values(body=ERASED, subjects=None)
        )
    return {
        "deliveries": blanked.rowcount,
        "cancelled": cancelled.rowcount,
        "subscriptions": len(owned),
    }


async def export_subject(subject: str) -> list[dict[str, Any]]:
    """The deliveries about ``subject``, oldest first, with what each sends."""
    alias = webhooks_alias()
    async with connections.engine(alias).connect() as connection:
        named = contains_text(
            deliveries.c.subjects, tagged_with(subject), connection.dialect.name
        )
        rows = (
            await connection.execute(
                select(deliveries)
                .where(named)
                .order_by(deliveries.c.created_at, deliveries.c.id)
            )
        ).all()
    return [
        {
            "id": str(row.id),
            "subscription_id": str(row.subscription_id),
            "event_type": row.event_type,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
            "body": json.loads(row.body),
        }
        for row in rows
    ]


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
