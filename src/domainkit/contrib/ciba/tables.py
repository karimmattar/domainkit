"""CIBA clients, sign-in requests, and the client assertions already used."""

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    Uuid,
)

from domainkit.db import UTCDateTime, metadata

__all__ = ["assertions", "clients", "sign_in_requests"]

clients = Table(
    "domainkit_ciba_clients",
    metadata,
    Column("client_id", String(64), primary_key=True),
    Column("name", String(100), nullable=False),
    # client_secret_basic, client_secret_post, or private_key_jwt.
    Column("auth_method", String(32), nullable=False),
    # The SHA-256 of a generated 256-bit secret: the secret is never kept.
    Column("secret_digest", String(64), nullable=True),
    # The client's public keys (a JWK Set), for private_key_jwt.
    Column("jwks", Text, nullable=True),
    # The permission keys its tokens may carry, space separated.
    Column("scopes", String(1000), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    info={"domain": "ciba", "fixtures": False},
)
"""The OIDC clients that may ask a user's phone (``domainkit ciba clients``)."""

sign_in_requests = Table(
    "domainkit_ciba_requests",
    metadata,
    Column("id", Uuid, primary_key=True),
    # The SHA-256 of the poll handle (auth_req_id) only the initiator holds.
    Column("handle_digest", String(64), nullable=False, unique=True),
    # The SHA-256 of a QR code's secret, until it is claimed.
    Column("qr_digest", String(64), nullable=True, unique=True),
    # None: the project's own web pages started it.
    Column("client_id", String(64), nullable=True),
    # No foreign key: the user model, and its table, may be swapped. None:
    # a QR code no phone claimed yet, or a decoy.
    Column("user_id", Uuid, nullable=True),
    Column("channel", String(4), nullable=False),
    Column("scope", String(1000), nullable=False),
    Column("binding_message", String(64), nullable=True),
    Column("match_code", String(2), nullable=True),
    Column("requester_ip", String(45), nullable=True),
    Column("requester_agent", String(200), nullable=True),
    # Answers like any request, and nobody can approve it.
    Column("decoy", Boolean, nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("interval", Integer, nullable=False),
    Column("last_polled_at", UTCDateTime, nullable=True),
    Column("claimed_at", UTCDateTime, nullable=True),
    Column("claimed_ip", String(45), nullable=True),
    Column("approved_at", UTCDateTime, nullable=True),
    Column("device_id", Uuid, nullable=True),
    Column("denied_at", UTCDateTime, nullable=True),
    Column("reported", Boolean, nullable=False),
    Column("redeemed_at", UTCDateTime, nullable=True),
    # sign_in, step_up, or transaction: a handle redeems as its kind only.
    Column("kind", String(16), nullable=False, server_default="sign_in"),
    # The SHA-256 of the credentials of the session a step-up, or a
    # transaction, was asked from: only that session redeems it.
    Column("credential_digest", String(64), nullable=True),
    # The auth.sca transaction a request confirms. No foreign key: auth's.
    Column("sca_challenge_id", Uuid, nullable=True),
    Index("ix_domainkit_ciba_requests_user", "user_id", "expires_at"),
    Index("ix_domainkit_ciba_requests_expires_at", "expires_at"),
    info={"domain": "ciba", "fixtures": False},
)
"""Each sign-in, step-up, or transaction a phone is asked to approve, until purged."""

assertions = Table(
    "domainkit_ciba_assertions",
    metadata,
    Column("client_id", String(64), nullable=False),
    Column("jti", String(255), nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("client_id", "jti", name="pk_domainkit_ciba_assertions"),
    Index("ix_domainkit_ciba_assertions_expires_at", "expires_at"),
    info={"domain": "ciba", "fixtures": False},
)
"""The ``jti`` of each ``private_key_jwt`` assertion, until it expires: once."""
