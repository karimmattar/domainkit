"""The limits' tables: the limits, the subjects that moved money, and their usage.

A movement counted against a subject's limits is a row of ``usage``, with
its amount, its time on the database's clock, and the key it was counted
under, which releases it. Each subject has a row of ``subjects``: a
movement locks it before it reads the subject's usage, so two movements
of one subject are counted one after the other (ADR 0123). Amounts are
``ExactDecimal(18, 4)``, exact on PostgreSQL and SQLite.
"""

from typing import Final

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from domainkit.db import EnumValue, ExactDecimal, MoneyColumns, UTCDateTime, metadata
from domainkit.db.orm import map_imperatively

from .exceptions import LimitTaken
from .models import MAX_NOTE, MAX_ZONE, SCALE, Direction, Limit, Window

PRECISION: Final = 18
"""Digits an amount column holds, ``SCALE`` of them decimal."""
MAX_KEY: Final = 200
"""The longest key a movement is counted under."""
USAGE: Final = {"domain": "limits", "fixtures": False, "flush": False}
"""Usage is counted, not loaded: fixtures and flush leave it alone."""

limits = Table(
    "domainkit_limits_limits",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("code", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("direction", EnumValue(Direction), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("subject", String(255), nullable=False),
    *MoneyColumns("per_transaction", PRECISION, SCALE, nullable=True),
    Column("window", EnumValue(Window)),
    *MoneyColumns("max_amount", PRECISION, SCALE, nullable=True),
    Column("max_count", Integer),
    Column("time_zone", String(MAX_ZONE), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("note", String(MAX_NOTE), nullable=False),
    Column("changed_at", UTCDateTime, nullable=False),
    Column("version", Integer, nullable=False),
    UniqueConstraint(
        "kind",
        "direction",
        "currency",
        "subject",
        "code",
        info={"duplicate": LimitTaken},
    ),
    info={"domain": "limits"},
)
"""The limits, by their key; the unique index serves the look-up by subject."""

subjects = Table(
    "domainkit_limits_subjects",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("direction", EnumValue(Direction), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("subject", String(255), nullable=False),
    Column("touched_at", UTCDateTime, nullable=False),
    UniqueConstraint("kind", "direction", "currency", "subject"),
    Index("ix_domainkit_limits_subjects_touched_at", "touched_at"),
    info=USAGE,
)
"""One row per subject, direction, and currency that moved money: the lock."""

usage = Table(
    "domainkit_limits_usage",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject_id", Uuid, ForeignKey(subjects.c.id), nullable=False),
    Column("amount", ExactDecimal(PRECISION, SCALE), nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("key", String(MAX_KEY), index=True),
    Column("released_at", UTCDateTime),
    Index("ix_domainkit_limits_usage_subject", "subject_id", "at"),
    Index("ix_domainkit_limits_usage_at", "at"),
    info=USAGE,
)
"""Each movement counted against a subject's limits; released ones count no more."""


def start_mappers() -> None:
    """Map the limits (idempotent); usage is read and written as rows."""
    map_imperatively(Limit, limits)
