"""The member table."""

from sqlalchemy import Column, String, Table

from domainkit.contrib.auth.tables import abstract_base_user_columns
from domainkit.db import metadata

members = Table(
    "members",
    metadata,
    *abstract_base_user_columns(),
    Column("email", String(254), nullable=False, unique=True),
    info={"domain": "members"},
)
