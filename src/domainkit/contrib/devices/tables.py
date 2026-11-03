"""Passkeys, their challenges, and bound devices."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Uuid,
)

from domainkit.db import UTCDateTime, metadata

__all__ = ["challenges", "devices", "passkeys", "push_tokens"]

passkeys = Table(
    "domainkit_passkeys",
    metadata,
    Column("id", Uuid, primary_key=True),
    # No foreign key: the user model, and its table, may be swapped.
    Column("user_id", Uuid, nullable=False, index=True),
    # The credential id's SHA-256, found by; the id itself, offered back.
    Column("digest", String(64), nullable=False, unique=True),
    Column("credential_id", String(1400), nullable=False),
    Column("algorithm", Integer, nullable=False),
    Column("public_key", LargeBinary(2048), nullable=False),
    Column("sign_count", BigInteger, nullable=False),
    Column("backed_up", Boolean, nullable=False),
    Column("name", String(100), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_used_at", UTCDateTime, nullable=True),
    info={"domain": "devices"},
)
"""Each user's passkeys: a WebAuthn credential's public key and counter."""

challenges = Table(
    "domainkit_passkey_challenges",
    metadata,
    # The challenge itself: random, 256 bits, used once.
    Column("id", String(64), primary_key=True),
    Column("purpose", String(10), nullable=False),
    Column("user_id", Uuid, nullable=True),
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_passkey_challenges_expires_at", "expires_at"),
    info={"domain": "devices", "fixtures": False},
)
"""Challenges handed to a browser, until its passkey answers one."""

devices = Table(
    "domainkit_devices",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False, index=True),
    Column("name", String(100), nullable=False),
    Column("algorithm", Integer, nullable=False),
    Column("public_key", LargeBinary(2048), nullable=False),
    # The time of the last signed request, in milliseconds: each is later.
    Column("last_time", BigInteger, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_used_at", UTCDateTime, nullable=True),
    info={"domain": "devices"},
)
"""Each user's bound devices: a key the device holds, which signs requests."""

push_tokens = Table(
    "domainkit_push_tokens",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False, index=True),
    # The token's SHA-256: one row per token, whoever holds it now.
    Column("digest", String(64), nullable=False, unique=True),
    Column("token", String(4096), nullable=False),
    Column("platform", String(20), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("last_seen_at", UTCDateTime, nullable=False),
    info={"domain": "devices"},
)
"""The tokens push services gave each user's apps: where notifications go."""
