"""The soft-deletable user's table."""

from sqlalchemy import Column, Table

from domainkit.contrib.auth.tables import abstract_user_columns
from domainkit.db import UTCDateTime, metadata

archived_users = Table(
    "archives_users",
    metadata,
    *abstract_user_columns(),
    Column("deleted_at", UTCDateTime),
    info={"domain": "archives"},
)
