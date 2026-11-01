"""The audit trail: append-only, one row per recorded action, sealed in a chain.

Personal values in it are pseudonyms, each made with the key of the person
it is about (the subject). Those keys live in their own table, which is not
append-only: deleting a subject's key makes their pseudonyms unlinkable.

None of the three is ever in a fixture (a copy of the keys would outlive
``forget``), and ``flush`` keeps them all. Nor are the ledger states the
anchors' ledger was last verified at (``ledger_states``).
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Index,
    LargeBinary,
    String,
    Table,
    Text,
    Uuid,
    text,
)

from domainkit.db import UTCDateTime, metadata

__all__ = [
    "audit_seals",
]

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
    Index("ix_domainkit_audit_log_target", "target", "occurred_at"),
    info={"domain": "audit", "append_only": True, "fixtures": False},
)

audit_keys = Table(
    "domainkit_audit_keys",
    metadata,
    Column("subject", String(255), primary_key=True),
    Column("key", LargeBinary(32), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    info={"domain": "audit", "flush": False, "fixtures": False},
)

audit_seals = Table(
    "domainkit_audit_seals",
    metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("entry_id", Uuid, nullable=False, unique=True),
    Column("digest", String(64), nullable=False),
    Column("link", String(64), nullable=False),
    Column("sealed_at", UTCDateTime, nullable=False),
    Column("late", Boolean, nullable=False, server_default=text("false")),
    info={"domain": "audit", "append_only": True, "fixtures": False},
)
"""Each row's place in the hash chain (see ``seals``); append-only too.

``late`` marks a row sealed after it fell out of the late window
(``auditseal --late``): it was open to change unnoticed for longer.
"""

audit_checkpoints = Table(
    "domainkit_audit_checkpoints",
    metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("document", Text, nullable=False),
    Column("signature", LargeBinary(64), nullable=False),
    Column("made_at", UTCDateTime, nullable=False),
    info={"domain": "audit", "append_only": True, "fixtures": False},
)
"""Signed checkpoints of the chain (see ``checkpoints``), where it was pruned.

``document`` is the checkpoint's canonical JSON, exactly as signed:
``verify`` takes the newest one below the chain's first seal as its
start. Its signature, not the table, is what protects it.
"""

ledger_states = Table(
    "domainkit_ledger_states",
    metadata,
    Column("ledger", String(255), primary_key=True),
    Column("database", String(255), nullable=False),
    Column("transaction_id", BigInteger, nullable=False),
    Column("digest", LargeBinary(64), nullable=False),
    Column("signature", LargeBinary(256), nullable=False),
    Column("public_key", LargeBinary(256), nullable=False),
    Column("saved_at", UTCDateTime, nullable=False),
    info={"domain": "audit", "flush": False, "fixtures": False},
)
"""The newest state each ledger proved to a client (``domainkit.ledger.trust``).

Every process and host starts from it, so a ledger rolled back to an older
copy is refused by all of them, not only by the one that saw it first.
"""
