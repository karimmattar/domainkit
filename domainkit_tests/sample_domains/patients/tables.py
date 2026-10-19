"""Tables of the patients domain."""

from sqlalchemy import (
    Column,
    Date,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from domainkit.db import UTCDateTime, map_imperatively, metadata

from .models import Appointment, Badge, Card, Patient, Room, Visit

patients = Table(
    "patients_patients",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(100), nullable=False),
    Column("email", String(254)),
    Column("registered_at", UTCDateTime, nullable=False),
    Column("version", Integer, nullable=False),
    info={"domain": "patients"},
)

visits = Table(
    "patients_visits",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("patient_id", Uuid, nullable=False),
    Column("visited_at", UTCDateTime, nullable=False),
    Column("notes", Text),
    Column("symptoms", Text),
    Column("version", Integer, nullable=False),
    info={"domain": "patients"},
)

appointments = Table(
    "patients_appointments",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("patient_id", Uuid, nullable=False),
    Column("doctor_id", String(64), nullable=False),
    Column("booked_at", UTCDateTime, nullable=False),
    Column("patient_name", String(100), nullable=False),
    Column("doctor_phone", String(32)),
    Column("version", Integer, nullable=False),
    info={"domain": "patients"},
)

cards = Table(
    "patients_cards",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("holder_id", String(64), nullable=False),
    Column("number", String(40), nullable=False),
    Column("code", String(16), nullable=False, unique=True),
    Column("nickname", String(40), nullable=False),
    Column("version", Integer, nullable=False),
    UniqueConstraint("number"),
    info={"domain": "patients"},
)
"""Numbers are unique by a constraint of the table, codes by their column's."""

badges = Table(
    "patients_badges",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("holder_id", String(64), nullable=False),
    Column("born_on", Date, nullable=False),
    Column("erased_count", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    info={"domain": "patients"},
)

rooms = Table(
    "patients_rooms",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("name", String(50), nullable=False),
    Column("opened_on", Date, nullable=False),
    Column("version", Integer, nullable=False),
    info={"domain": "patients"},
)

access_log = Table(
    "patients_access_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("patient_id", Uuid, nullable=False),
    Column("accessed_at", UTCDateTime, nullable=False),
    info={"domain": "patients"},
)
"""Who opened which record: kept 30 days, then deleted."""

daily_counts = Table(
    "patients_daily_counts",
    metadata,
    Column("day", Date, primary_key=True),
    Column("ward", String(20), primary_key=True),
    Column("count", Integer, nullable=False),
    Column("note", Text),
    info={"domain": "patients"},
)
"""Visits per ward and day: a two-column key, dated by a Date."""

ledger = Table(
    "patients_ledger",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("written_at", UTCDateTime, nullable=False),
    info={"domain": "patients", "append_only": True},
)


def start_mappers() -> None:
    """Map the aggregates."""
    map_imperatively(Patient, patients)
    map_imperatively(Visit, visits)
    map_imperatively(Appointment, appointments)
    map_imperatively(Card, cards)
    map_imperatively(Badge, badges)
    map_imperatively(Room, rooms)
