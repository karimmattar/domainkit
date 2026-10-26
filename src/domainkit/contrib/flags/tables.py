"""The flags and dynamic settings tables."""

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, Integer, String, Table

from domainkit.db import map_imperatively, metadata

from .models import DynamicSetting, Flag

NAME_LENGTH = 100

flags = Table(
    "domainkit_flags",
    metadata,
    Column("id", String(NAME_LENGTH), primary_key=True, info={"field": "name"}),
    Column("everyone", Boolean),
    Column("percentage", Integer, nullable=False),
    Column("users", JSON, nullable=False),
    Column("groups", JSON, nullable=False),
    Column("note", String(255), nullable=False),
    Column("version", Integer, nullable=False),
    CheckConstraint("percentage BETWEEN 0 AND 100", name="percentage"),
    info={"domain": "flags"},
)

dynamic_settings = Table(
    "domainkit_dynamic_settings",
    metadata,
    Column("id", String(NAME_LENGTH), primary_key=True, info={"field": "key"}),
    Column("value", JSON, nullable=False),
    Column("version", Integer, nullable=False),
    # A setting may be secret (a provider's key): fixtures and flush leave
    # these rows alone unless told (guarded, see domainkit.db.fixtures).
    info={"domain": "flags", "guarded": True},
)


def start_mappers() -> None:
    """Map the aggregates (idempotent)."""
    map_imperatively(Flag, flags)
    map_imperatively(DynamicSetting, dynamic_settings)
