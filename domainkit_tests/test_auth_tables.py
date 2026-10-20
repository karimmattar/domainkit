"""The auth tables: user columns (Django's lengths) and password hashes."""

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import sqlite

from domainkit.contrib.auth import PasswordHash
from domainkit.contrib.auth.tables import (
    PasswordHashType,
    abstract_base_user_columns,
    abstract_user_columns,
    user_link_tables,
)

type ColumnSpec = tuple[str, str, bool | None, bool, bool]
BASE_USER: list[ColumnSpec] = [
    ("id", "Uuid()", False, True, False),
    ("password", "PasswordHashType(length=255)", False, False, False),
    ("last_login", "UTCDateTime(timezone=True)", True, False, False),
    ("is_active", "Boolean()", False, False, False),
    ("is_superuser", "Boolean()", False, False, False),
    ("version", "Integer()", False, False, False),
]


def describe(columns: list[Column[object]]) -> list[ColumnSpec]:
    """Name, type, nullable, primary key, and unique, per column."""
    return [
        (
            column.name,
            repr(column.type),
            column.nullable,
            column.primary_key,
            bool(column.unique),
        )
        for column in columns
    ]


def test_base_user_columns() -> None:
    """Credentials, status, and optimistic locking."""
    assert describe(abstract_base_user_columns()) == BASE_USER


def test_user_columns_keep_djangos_lengths() -> None:
    """Usernames of 150 characters (unique) and emails of 254, as in Django."""
    assert describe(abstract_user_columns()) == [
        *BASE_USER,
        ("username", "String(length=150)", False, False, True),
        ("email", "String(length=254)", False, False, False),
        ("email_verified", "Boolean()", False, False, False),
        ("first_name", "String(length=150)", False, False, False),
        ("last_name", "String(length=150)", False, False, False),
        ("is_staff", "Boolean()", False, False, False),
        ("date_joined", "UTCDateTime(timezone=True)", False, False, False),
    ]


def test_each_call_makes_new_columns() -> None:
    """A column belongs to one table, so every table gets its own."""
    assert abstract_user_columns()[0] is not abstract_user_columns()[0]


def test_password_hashes_are_stored_as_text() -> None:
    """The value object goes in as its text and comes back wrapped."""
    column = PasswordHashType()
    dialect = sqlite.dialect()
    stored = "pbkdf2_sha256$1$salt$aGFzaA=="

    assert column.process_bind_param(PasswordHash(stored), dialect) == stored
    assert column.process_bind_param(None, dialect) is None
    assert column.process_result_value(stored, dialect) == PasswordHash(stored)
    assert column.process_result_value(None, dialect) is None


def links(table: Table) -> list[tuple[str, str, bool, str, str | None]]:
    """Name, type, primary key, target, and ``ON DELETE``, per column."""
    return [
        (
            column.name,
            repr(column.type),
            column.primary_key,
            *next((key.target_fullname, key.ondelete) for key in column.foreign_keys),
        )
        for column in table.columns
    ]


def test_link_tables_follow_the_user_table() -> None:
    """Named after it, in its metadata and domain, made once."""
    people = Table(
        "people", MetaData(), *abstract_base_user_columns(), info={"domain": "crm"}
    )

    user_groups, user_permissions = user_link_tables(people)

    assert (user_groups.name, user_permissions.name) == (
        "people_groups",
        "people_permissions",
    )
    assert user_groups.metadata is user_permissions.metadata is people.metadata
    assert user_groups.info == user_permissions.info == {"domain": "crm"}
    assert links(user_groups) == [
        ("user_id", "Uuid()", True, "people.id", "CASCADE"),
        ("group_id", "Uuid()", True, "domainkit_groups.id", "CASCADE"),
    ]
    assert links(user_permissions) == [
        ("user_id", "Uuid()", True, "people.id", "CASCADE"),
        ("permission_id", "Integer()", True, "domainkit_permissions.id", "CASCADE"),
    ]
    assert user_link_tables(people) == (user_groups, user_permissions)
