"""Stored responses, keyed by a hash of the caller and the key."""

from sqlalchemy import JSON, Column, Integer, LargeBinary, String, Table

from domainkit.db import UTCDateTime, metadata

idempotency_keys = Table(
    "domainkit_idempotency_keys",
    metadata,
    Column("key_hash", String(64), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("status_code", Integer),
    Column("headers", JSON),
    Column("body", LargeBinary),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False, index=True),
    info={"domain": "idempotency"},
)
"""A row without ``status_code`` is a request still in progress."""
