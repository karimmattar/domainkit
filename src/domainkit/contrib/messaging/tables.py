"""The outbox: one row per durable handler call or background task."""

from typing import Final

from sqlalchemy import JSON, Column, Index, Integer, String, Table, Text, Uuid

from domainkit.db import UTCDateTime, metadata

DEFAULT_QUEUE: Final = "default"
"""Where durable handlers, and tasks that name no queue, go."""
PENDING: Final = "pending"
DONE: Final = "done"
DEAD: Final = "dead"
CANCELLED: Final = "cancelled"
STATUSES: Final = (PENDING, DONE, DEAD, CANCELLED)
FINISHED: Final = (DONE, CANCELLED)
"""The statuses cleanup deletes once ``RETENTION_SECONDS`` have passed."""
BLANKED: Final = "payload_blanked"
"""The header set on a dead message whose arguments were blanked (``sensitive``)."""
SENSITIVE: Final = "sensitive"
"""The header of a chain's step that follows a ``sensitive`` one, at any distance.

It is handed what the sensitive step had, or made, so its arguments are
blanked when it dies, as the sensitive step's would be."""

messages = Table(
    "domainkit_messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("target", String(255), nullable=False),
    Column("queue", String(64), nullable=False, server_default=DEFAULT_QUEUE),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("payload", JSON, nullable=False),
    Column("headers", JSON, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", UTCDateTime, nullable=False),
    Column("locked_until", UTCDateTime),
    Column("lock_id", Uuid),
    Column("started_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("last_error", Text),
    Column("dedupe_key", String(255), unique=True),
    Index("ix_domainkit_messages_ready", "status", "available_at"),
    info={"domain": "messaging"},
)
"""``kind`` is ``event`` (a durable handler) or ``task``; ``target`` names it.

``queue`` picks the workers that run it (``runworker --queue``), and
``priority`` the order among a queue's due messages (higher first).
``started_at`` is when a worker last started it; it is cleared when the
message goes back to wait. ``payload`` holds the arguments until the
message is done or cancelled, then ``{}``; a dead message keeps them, unless
its task is ``sensitive``. A pending message with ``started_at`` and a live
``locked_until`` is running; with a live lock alone, a relay published it.
"""

PRIORITY_INDEX: Final = Index(
    "ix_domainkit_messages_priority",
    messages.c.status,
    messages.c.priority.desc(),
    messages.c.available_at,
)
"""Claims take due messages by priority, then by age."""

FINISHED_INDEX: Final = Index(
    "ix_domainkit_messages_finished",
    messages.c.finished_at,
    postgresql_where=messages.c.status.in_(FINISHED),
    sqlite_where=messages.c.status.in_(FINISHED),
)
"""Cleanup finds the oldest done and cancelled messages, and only those.

Partial: pending and dead messages are not in it, so it costs a write only
when a message finishes. A cleanup statement must name both statuses as
literals, or the database cannot tell that the index applies.
"""
