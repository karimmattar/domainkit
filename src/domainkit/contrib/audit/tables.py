"""The audit trail: append-only, one row per recorded action, sealed in a chain.

Personal values in it are pseudonyms, each made with the key of the person
it is about (the subject). Those keys live in their own table, which is not
append-only: deleting a subject's key makes their pseudonyms unlinkable.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Index,
    LargeBinary,
    String,
    Table,
    Uuid,
)

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

audit_keys = Table(
    "domainkit_audit_keys",
    metadata,
    Column("subject", String(255), primary_key=True),
    Column("key", LargeBinary(32), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    info={"domain": "audit"},
)

audit_seals = Table(
    "domainkit_audit_seals",
    metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("entry_id", Uuid, nullable=False, unique=True),
    Column("digest", String(64), nullable=False),
    Column("link", String(64), nullable=False),
    Column("sealed_at", UTCDateTime, nullable=False),
    info={"domain": "audit"},
)
"""Each row's place in the hash chain (see ``seals``); append-only too."""
