"""The auth tables: user columns (Django's lengths), indexes, and password hashes."""

import io
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Column, MetaData, Table, event, inspect, make_url
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth import AbstractUser, PasswordHash
from domainkit.contrib.auth.exceptions import UsernameTaken
from domainkit.contrib.auth.tables import (
    PasswordHashType,
    abstract_base_user_columns,
    abstract_user_columns,
    lookup_indexes,
    user_link_tables,
    users,
)
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.db import connections
from domainkit.ddd.query import Selection
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    POSTGRES_VARIABLE,
)
from domainkit_tests.support.auth import create_user

ON_POSTGRES: Final = bool(os.environ.get(POSTGRES_VARIABLE))

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
        ("username", "String(length=150)", True, False, True),  # Optional since 1.8.
        ("email", "String(length=254)", False, False, False),
        ("email_verified", "Boolean()", False, False, False),
        ("mobile_number", "String(length=16)", True, False, True),
        ("mobile_number_verified", "Boolean()", False, False, False),
        ("first_name", "String(length=150)", False, False, False),
        ("last_name", "String(length=150)", False, False, False),
        ("is_staff", "Boolean()", False, False, False),
        ("date_joined", "UTCDateTime(timezone=True)", False, False, False),
    ]


def test_a_projects_user_table_refuses_taken_usernames_and_starts_unverified() -> None:
    """A duplicate username is ``UsernameTaken``; existing rows get ``false``.

    What a project's own table from ``abstract_user_columns()`` keeps: the
    refusal its unique constraint answers with, and the default a migration
    that adds ``email_verified`` to stored rows needs.
    """
    columns = {column.name: column for column in abstract_user_columns()}
    table = Table("shop_users", MetaData(), *columns.values())
    postgres = make_url("postgresql+asyncpg://").get_dialect()()
    ddl = str(CreateTable(table).compile(dialect=postgres))

    assert columns["username"].info == {"duplicate": UsernameTaken}
    assert "email_verified BOOLEAN DEFAULT false NOT NULL" in ddl


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
    # Guarded since 0.11, the user table too: loaddata and flush leave them
    # alone unless told (DATA-12).
    assert (
        user_groups.info == user_permissions.info == {"domain": "crm", "guarded": True}
    )
    assert people.info == {"domain": "crm", "guarded": True}
    assert links(user_groups) == [
        ("user_id", "Uuid()", True, "people.id", "CASCADE"),
        ("group_id", "Uuid()", True, "domainkit_groups.id", "CASCADE"),
    ]
    assert links(user_permissions) == [
        ("user_id", "Uuid()", True, "people.id", "CASCADE"),
        ("permission_id", "Integer()", True, "domainkit_permissions.id", "CASCADE"),
    ]
    assert user_link_tables(people) == (user_groups, user_permissions)


def test_lookup_indexes_follow_the_models_fields() -> None:
    """``lower()`` of the address and the username, and ``(email, id)``.

    A field the table lacks or encrypts gets none; a long table name is cut
    to PostgreSQL's 63 characters with a digest (A28-PERF-02).
    """
    from domainkit.contrib.auth.models import User
    from domainkit.db import EncryptedType
    from domainkit_tests.sample_domains.members.models import Member

    assert lookup_indexes(User, users, lambda name: f'"{name}"') == {
        "ix_domainkit_users_email_lower": 'lower("email")',
        "ix_domainkit_users_username_lower": 'lower("username")',
        "ix_domainkit_users_email_id": '"email", "id"',
    }
    assert lookup_indexes(Member, users) == {  # Logs in by its address: once.
        "ix_domainkit_users_email_lower": "lower(email)",
        "ix_domainkit_users_email_id": "email, id",
    }
    bare = Table("bare", MetaData(), *abstract_base_user_columns())
    assert lookup_indexes(Member, bare) == {}
    sealed = Table(
        "sealed",
        MetaData(),
        Column("email", EncryptedType()),
        *(column for column in abstract_user_columns() if column.name != "email"),
    )
    assert list(lookup_indexes(User, sealed)) == ["ix_sealed_username_lower"]
    long = Table("a" * 60, MetaData(), *abstract_user_columns())
    names = list(lookup_indexes(User, long))
    assert [len(name) for name in names] == [63, 63, 63]
    assert len(set(names)) == 3


ACCOUNTS: Final = "domainkit_tests.sample_domains.accounts"
CUSTOM: Final = {
    "USER_MODEL": f"{ACCOUNTS}.models.Account",
    "USER_TABLE": f"{ACCOUNTS}.tables.accounts",
}


@pytest.fixture(params=["default", "custom"])
def migrated(
    request: pytest.FixtureRequest, database_url: str, tmp_path: Path
) -> Iterator[str]:
    """Auth migrated, addresses identifying users: the default user or a custom one."""
    custom = request.param == "custom"
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, ALPHA, *([ACCOUNTS] if custom else [])],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "IDENTIFIERS": ("email", "username"),
            **(CUSTOM if custom else {}),
        },
    ):
        setup()
        call_command("makemigrations", "-m", "auth", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield "accounts_users" if custom else "domainkit_users"
    registry.clear()


async def index_names(table: str) -> set[str]:
    """The names of ``table``'s indexes, as the database has them."""

    def read(connection: Any) -> set[str]:  # noqa: ANN401 - a sync connection.
        return {str(index["name"]) for index in inspect(connection).get_indexes(table)}

    async with connections.engine().connect() as connection:
        return await connection.run_sync(read)


def test_migrate_makes_the_lookup_indexes_on_postgresql(migrated: str) -> None:
    """Three indexes on PostgreSQL, none on SQLite; revisions leave them alone.

    ``migrate`` makes them on the user table, the project's own too, once:
    a second ``migrate`` keeps them, and ``makemigrations`` finds nothing.
    """
    call_command("migrate", stdout=io.StringIO())
    checked = io.StringIO()
    call_command("makemigrations", "--check", stdout=checked)
    present = run_async(index_names(migrated))

    wanted = {
        f"ix_{migrated}_email_lower",
        f"ix_{migrated}_username_lower",
        f"ix_{migrated}_email_id",
    }
    assert present & wanted == (wanted if ON_POSTGRES else set())
    assert checked.getvalue() == "No changes detected.\n"


def test_migrate_before_any_revision_makes_no_index(
    database_url: str, tmp_path: Path
) -> None:
    """Without the user table yet, ``migrate`` makes no index, and fails not."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, ALPHA],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        call_command("migrate", stdout=io.StringIO())
        tables = run_async(table_names())
    registry.clear()

    assert users.name not in tables


async def table_names() -> set[str]:
    """The default database's tables."""
    async with connections.engine().connect() as connection:
        return set(
            await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        )


@contextmanager
def statements() -> Iterator[list[tuple[str, Any]]]:
    """Each statement the default database runs in the block, with its values."""
    seen: list[tuple[str, Any]] = []
    engine = connections.engine().sync_engine

    def keep(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy's event arguments.
        seen.append((args[2], args[3]))

    event.listen(engine, "before_cursor_execute", keep)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", keep)


async def plan(statement: str, values: Any) -> str:  # noqa: ANN401 - the driver's.
    """PostgreSQL's plan for ``statement``, as it would read a large table."""
    async with connections.engine().connect() as connection:
        await connection.exec_driver_sql("SET enable_seqscan = off")
        rows = await connection.exec_driver_sql("EXPLAIN " + statement, values)
        return "\n".join(str(row[0]) for row in rows)


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}: SQLite has none")
def test_user_lookups_read_their_indexes(migrated: str) -> None:
    """Login, signup, the taken checks, and the admin's order use an index.

    Each statement that compares ``lower()`` of an address or a username is
    planned on its index, as the repository writes it (A28-PERF-02).
    """
    create_user("ada", email="Ada@Example.com")

    async def look_up() -> list[tuple[str, Any]]:
        with statements() as seen:
            async with AuthUnitOfWork() as uow:
                await uow.users.find_by_email("ada@example.com")
                await uow.users.username_taken("ADA")
                user = await uow.users.get_by_natural_key("ada")
                assert isinstance(user, AbstractUser)
                user.email = "ADA2@example.com"
                await uow.commit()
            async with AuthUnitOfWork() as uow:
                await uow.users.filter(Selection(ordering=["email"], limit=51))
        return seen

    seen = run_async(look_up())
    plans = {
        statement: run_async(plan(statement, values))
        for statement, values in seen
        if f"FROM {migrated}" in statement
        and ("lower(" in statement or "ORDER BY" in statement)
    }

    folded = [each for statement, each in plans.items() if "lower(" in statement]
    listed = [each for statement, each in plans.items() if "lower(" not in statement]
    assert len(folded) == 3  # The address, the username, the taken address.
    assert all("_lower" in each for each in folded), folded
    assert any(f"ix_{migrated}_email_id" in each for each in listed), listed
