"""Linked identities, logins on their way back from a provider, and tickets."""

from sqlalchemy import Column, Index, String, Table, UniqueConstraint, Uuid

from domainkit.db import UTCDateTime, metadata

__all__ = ["flows", "identities", "tickets"]

identities = Table(
    "domainkit_identities",
    metadata,
    Column("id", Uuid, primary_key=True),
    # No foreign key: the user model, and its table, may be swapped.
    Column("user_id", Uuid, nullable=False, index=True),
    Column("provider", String(50), nullable=False),
    Column("subject", String(255), nullable=False),
    Column("email", String(254), nullable=True),
    Column("created_at", UTCDateTime, nullable=False),
    UniqueConstraint("provider", "subject", name="uq_domainkit_identities_subject"),
    info={"domain": "identity"},
)
"""Each provider's account a user logs in with: one user per provider subject."""

flows = Table(
    "domainkit_identity_flows",
    metadata,
    # The state sent to the provider: random, 256 bits, used once.
    Column("id", String(64), primary_key=True),
    Column("provider", String(50), nullable=False),
    Column("purpose", String(10), nullable=False),
    Column("user_id", Uuid, nullable=True),
    # PKCE's verifier and OIDC's nonce stay here, never in the browser.
    Column("verifier", String(128), nullable=False),
    Column("nonce", String(64), nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_identity_flows_expires_at", "expires_at"),
    info={"domain": "identity", "fixtures": False},
)
"""Logins and links sent to a provider, until it sends the browser back."""

tickets = Table(
    "domainkit_identity_tickets",
    metadata,
    # The ticket's SHA-256: a stolen database names no ticket.
    Column("id", String(64), primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    info={"domain": "identity", "fixtures": False},
)
"""Logins a posted response (SAML) proved, until the page redeems them."""
