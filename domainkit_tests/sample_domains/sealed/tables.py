"""The sealed user's table: the address in a type of the project's, over bytes."""

from sqlalchemy import Column, Dialect, LargeBinary, Table
from sqlalchemy.types import TypeDecorator

from domainkit.contrib.auth.tables import abstract_user_columns
from domainkit.db import metadata


class SealedText(TypeDecorator[str]):
    """Text in, bytes stored: a stand-in for a project's encrypting type."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> bytes | None:
        """The text's UTF-8."""
        return None if value is None else value.encode()

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> str | None:
        """The text back."""
        return None if value is None else bytes(value).decode()


sealed = Table(
    "sealed_users",
    metadata,
    Column("email", SealedText(), nullable=False),
    *(column for column in abstract_user_columns() if column.name != "email"),
    info={"domain": "sealed"},
)
