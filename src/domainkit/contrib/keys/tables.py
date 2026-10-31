"""The subjects' keys: one per person, wrapped under a data key.

Deleting a subject's row is what crypto-shredding is: the values sealed
under that key can never be opened again. The table is in every database
that hosts domains, beside the rows its keys seal, and is never in a
fixture (a copy would outlive the erasure) nor emptied by ``flush``.
"""

from sqlalchemy import Column, Index, LargeBinary, String, Table

from domainkit.db import UTCDateTime, metadata

__all__ = [
    "subject_keys",
]

subject_keys = Table(
    "domainkit_keys_subject_keys",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("subject", String(255), nullable=False, unique=True),
    Column("wrapping_key", String(32), nullable=False),
    Column("key", LargeBinary(60), nullable=False),
    Column("created_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_keys_subject_keys_wrapping_key", "wrapping_key"),
    info={"domain": "keys", "flush": False, "fixtures": False},
)
"""Subjects' keys: the 32-byte key under ``wrapping_key``, nonce first (60 bytes)."""
