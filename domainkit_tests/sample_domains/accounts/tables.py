"""The custom user's table."""

from sqlalchemy import Column, String, Table

from domainkit.contrib.auth.tables import abstract_user_columns
from domainkit.db import metadata

accounts = Table(
    "accounts_users",
    metadata,
    *abstract_user_columns(),
    Column("phone", String(32), nullable=False),
    info={"domain": "accounts"},
)
