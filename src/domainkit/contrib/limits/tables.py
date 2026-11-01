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
from .models import MAX_NOTE, MAX_ZONE, PRECISION, SCALE, Direction, Limit, Window

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
    info=USAGE,
)
"""One row per subject, direction, and currency a window bounds: the lock.

Only the subjects a window bounds have one (PERF-26): a subject no limit
bounds moves without a row, a lock, or usage.

``touched_at`` has no index: the lock writes it on every movement, and an
indexed column would make each of those writes a new index entry in every
index (no HOT update). The hourly purge reads the subjects whole (PERF-30).
"""

usage = Table(
    "domainkit_limits_usage",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject_id", Uuid, ForeignKey(subjects.c.id), nullable=False),
    Column("amount", ExactDecimal(PRECISION, SCALE), nullable=False),
    Column("at", UTCDateTime, nullable=False),
    Column("key", String(MAX_KEY)),
    Column("released_at", UTCDateTime),
    Index("ix_domainkit_limits_usage_subject", "subject_id", "at"),
    Index("ix_domainkit_limits_usage_at", "at"),
    Index("ix_domainkit_limits_usage_key_hash", "key", postgresql_using="hash"),
    info=USAGE,
)
"""Each movement counted against a subject's limits; released ones count no more.

``key`` is only ever looked up by equality (``release``): on PostgreSQL its
index is a hash, a third of the B-tree's size (PERF-30).
"""

kept = Table(
    "domainkit_limits_kept",
    metadata,
    Column("entry_id", Uuid, primary_key=True),
    Column("at", UTCDateTime, nullable=False),
    Index("ix_domainkit_limits_kept_at", "at"),
    info=USAGE,
)
"""The ledger's reversals counted as movements, which free nothing (SEC-16b-9).

A reversal booked while an approved request runs is counted, and the
entry it reverses stays counted (FIN-88): its id is kept here, at the
time it was counted, so ``postings.count_earlier`` counts both for a
limit set later. Purged with the usage.
"""


def start_mappers() -> None:
    """Map the limits (idempotent); usage is read and written as rows."""
    map_imperatively(Limit, limits)
