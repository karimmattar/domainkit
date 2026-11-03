"""Codes sent to phone numbers, and the numbers users verified."""

from sqlalchemy import Column, Index, Integer, String, Table, UniqueConstraint, Uuid

from domainkit.db import UTCDateTime, metadata

__all__ = ["phone_codes", "phones"]

phone_codes = Table(
    "domainkit_phone_codes",
    metadata,
    Column("id", Uuid, primary_key=True),
    # No foreign key: the user model, and its table, may be swapped.
    Column("user_id", Uuid, nullable=False),
    Column("phone", String(16), nullable=False),
    # An HMAC of the code, keyed from SECRET_KEY: the code is never kept.
    Column("digest", String(64), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_phone_codes_user_phone", "user_id", "phone", "created_at"),
    Index("ix_domainkit_phone_codes_phone", "phone", "created_at"),
    Index("ix_domainkit_phone_codes_created_at", "created_at"),
    info={"domain": "phones", "fixtures": False},
)
"""Each code sent, until it is used, spent, or old: counted to limit sends."""

phones = Table(
    "domainkit_phones",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False, index=True),
    Column("phone", String(16), nullable=False),
    Column("verified_at", UTCDateTime, nullable=False),
    UniqueConstraint("user_id", "phone", name="uq_domainkit_phones_user_phone"),
    info={"domain": "phones"},
)
"""The numbers each user proved they receive messages at."""
