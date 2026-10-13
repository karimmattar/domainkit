"""The audit trail: append-only, one row per recorded action."""

from sqlalchemy import JSON, Column, Index, String, Table, Uuid

from domainkit.db import UTCDateTime, metadata

audit_log = Table(
    "domainkit_audit_log",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("occurred_at", UTCDateTime, nullable=False),
    Column("actor", String(255), nullable=False),
    Column("action", String(255), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("target", String(255)),
    Column("request_id", String(128)),
    Column("client_ip", String(64)),
    Column("event_id", Uuid),
    Column("details", JSON, nullable=False),
    Index("ix_domainkit_audit_log_occurred_at", "occurred_at"),
    Index("ix_domainkit_audit_log_actor", "actor", "occurred_at"),
    Index("ix_domainkit_audit_log_action", "action", "occurred_at"),
    info={"domain": "audit"},
)
