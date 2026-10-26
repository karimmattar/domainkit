"""Tables of subscriptions, deliveries, and the attempts of each delivery."""

from typing import Final

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    Uuid,
)

from domainkit.db import UTCDateTime, map_imperatively, metadata

from .models import Subscription

PENDING: Final = "pending"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"
"""The subscription was gone or inactive when the delivery's turn came."""
DELIVERY_STATUSES: Final = (PENDING, SUCCEEDED, FAILED, CANCELLED)
ERASED: Final = ""
"""The body of a delivery whose subjects were erased."""
INFO: Final = {"domain": "webhooks"}

subscriptions = Table(
    "domainkit_webhook_subscriptions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("url", String(2048), nullable=False),
    Column("event_types", JSON, nullable=False),
    # Encrypted (models.secret_box), bound to the subscription's id.
    Column("secret", LargeBinary(256), nullable=False),
    Column("previous_secret", LargeBinary(256)),
    Column("previous_secret_expires_at", UTCDateTime),
    Column("active", Boolean, nullable=False),
    Column("description", String(255), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("version", Integer, nullable=False),
    Column("owner", String(255), index=True),
    info=INFO,
)
"""Where events go. ``owner`` is NULL for a subscription staff made, which
receives every event it wants; else the subject that made it through the
self-service API (``webhooks.owned``), which receives only events
``expose(..., owner=...)`` gives that owner."""

deliveries = Table(
    "domainkit_webhook_deliveries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("subscription_id", Uuid, nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_attempt_at", UTCDateTime),
    Column("finished_at", UTCDateTime),
    Column("last_status_code", Integer),
    Column("last_error", String(500)),
    Column("subjects", Text),
    Index(
        "ix_domainkit_webhook_deliveries_subscription", "subscription_id", "created_at"
    ),
    Index("ix_domainkit_webhook_deliveries_finished", "status", "finished_at"),
    info=INFO,
)
"""One event for one subscription; its id is the ``webhook-id`` header.

``body`` is the exact JSON sent on every attempt; ``""`` once the people
it was about were erased (``ERASED``), and it is never sent again.
``subjects`` names them (``domainkit.ddd.personal.subject_tag``): the
subjects of the event's ``Personal`` fields.
"""

attempts = Table(
    "domainkit_webhook_attempts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("delivery_id", Uuid, nullable=False, index=True),
    Column("number", Integer, nullable=False),
    Column("started_at", UTCDateTime, nullable=False),
    Column("status_code", Integer),
    Column("duration_ms", Integer, nullable=False),
    Column("error", String(500)),
    info=INFO,
)
"""The delivery log: one row per attempt, with its status code and latency."""


def start_mappers() -> None:
    """Map subscriptions (idempotent)."""
    map_imperatively(Subscription, subscriptions)
