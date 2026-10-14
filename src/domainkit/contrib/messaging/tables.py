"""The outbox: one row per durable handler call or background task."""

from typing import Final

from sqlalchemy import JSON, Column, Index, Integer, String, Table, Text, Uuid

from domainkit.db import UTCDateTime, metadata

DEFAULT_QUEUE: Final = "default"
"""Where durable handlers, and tasks that name no queue, go."""
PENDING: Final = "pending"
DONE: Final = "done"
DEAD: Final = "dead"

messages = Table(
    "domainkit_messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("target", String(255), nullable=False),
    Column("queue", String(64), nullable=False, server_default=DEFAULT_QUEUE),
    Column("payload", JSON, nullable=False),
    Column("headers", JSON, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", UTCDateTime, nullable=False),
    Column("locked_until", UTCDateTime),
    Column("lock_id", Uuid),
    Column("created_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("last_error", Text),
    Column("dedupe_key", String(255), unique=True),
    Index("ix_domainkit_messages_ready", "status", "available_at"),
    info={"domain": "messaging"},
)
"""``kind`` is ``event`` (a durable handler) or ``task``; ``target`` names it.

``queue`` picks the workers that run it (``runworker --queue``).
"""
