"""The grants table: in every database that hosts a domain, beside the objects.

``VisibleTo`` filters a query with ``EXISTS`` on this table, so it must
live in the database of the objects it is about. Grants may be dumped in
fixtures: they hold no secrets. They are guarded, though: ``loaddata``
writes them only with ``--include-guarded``, and ``flush`` keeps them
unless it is given, but for the grants on the objects it deletes, which go
with them (``about_model``), as with a repository's ``remove``.
"""

from sqlalchemy import CheckConstraint, Index, Table

from domainkit.db import map_imperatively, table_for

from .models import ObjectPermission

object_permissions: Table = table_for(
    ObjectPermission,
    "domainkit_object_permissions",
    CheckConstraint("(user_id IS NULL) <> (group_id IS NULL)", name="whom"),
    CheckConstraint(
        "(CASE WHEN object_uuid IS NULL THEN 0 ELSE 1 END"
        " + CASE WHEN object_int IS NULL THEN 0 ELSE 1 END"
        " + CASE WHEN object_text IS NULL THEN 0 ELSE 1 END) = 1",
        name="object",
    ),
    Index(
        "ix_domainkit_object_permissions_uuid",
        "content_type",
        "permission",
        "object_uuid",
    ),
    Index(
        "ix_domainkit_object_permissions_int",
        "content_type",
        "permission",
        "object_int",
    ),
    Index(
        "ix_domainkit_object_permissions_text",
        "content_type",
        "permission",
        "object_text",
    ),
    Index("ix_domainkit_object_permissions_user", "user_id"),
    Index("ix_domainkit_object_permissions_group", "group_id"),
    domain="objectpermissions",
)
object_permissions.info["guarded"] = True
object_permissions.info["about_model"] = "content_type"


def start_mappers() -> None:
    """Map the aggregate (idempotent)."""
    map_imperatively(ObjectPermission, object_permissions)
