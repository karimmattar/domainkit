"""The profile's tables, and the relationships the auth app does not map."""

from sqlalchemy import Column, ForeignKey, String, Table, Uuid
from sqlalchemy.orm import class_mapper, relationship

from domainkit.contrib.auth.tables import abstract_user_columns
from domainkit.db import children, map_imperatively, metadata

from .models import Address, Note, Profile

profiles = Table(
    "profiles_users",
    metadata,
    *abstract_user_columns(),
    info={"domain": "profiles"},
)
addresses = Table(
    "profiles_addresses",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("profile_id", ForeignKey(profiles.c.id), nullable=False),
    Column("city", String(100), nullable=False),
    info={"domain": "profiles"},
)
notes = Table(
    "profiles_notes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("profile_id", ForeignKey(profiles.c.id), nullable=False),
    Column("text", String(100), nullable=False),
    info={"domain": "profiles"},
)


def start_mappers() -> None:
    """Map the entities, and add them to the profile the auth app mapped."""
    map_imperatively(Address, addresses)
    map_imperatively(Note, notes)
    mapper = class_mapper(Profile)
    if "addresses" not in mapper.relationships:
        mapper.add_property(
            "notes", relationship(Note, lazy="raise", collection_class=set)
        )
        mapper.add_property("addresses", children(Address, collection_class=set))
