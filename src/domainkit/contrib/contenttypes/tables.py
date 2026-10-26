"""The content type table, mapped onto ``ContentType``."""

from sqlalchemy import Column, Integer, String, Table, UniqueConstraint

from domainkit.db import map_imperatively, metadata

from .models import ContentType

content_types = Table(
    "domainkit_content_types",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("domain", String(100), nullable=False),
    Column("model", String(100), nullable=False),
    UniqueConstraint("domain", "model"),
    # Permissions hang from these rows: replacing one re-points every
    # permission on it, and deleting one deletes them (guarded, see fixtures).
    info={"domain": "contenttypes", "guarded": True},
)


def start_mappers() -> None:
    """Map ``ContentType`` (idempotent)."""
    map_imperatively(ContentType, content_types)
