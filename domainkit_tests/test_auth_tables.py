"""The auth tables: user columns (Django's lengths), indexes, and password hashes."""

import asyncio
import io
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    event,
    inspect,
    make_url,
    text,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.auth import AbstractUser, PasswordHash
from domainkit.contrib.auth.apps import AuthConfig
from domainkit.contrib.auth.exceptions import UsernameTaken
from domainkit.contrib.auth.tables import (
    PasswordHashType,
    abstract_base_user_columns,
    abstract_user_columns,
    listing_field,
    lookup_indexes,
    user_link_tables,
    users,
)
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.contrib.auth.users import get_user_model, get_user_table
from domainkit.db import connections
from domainkit.db.migrations import include_object
from domainkit.ddd.query import Selection
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    POSTGRES_VARIABLE,
    on_server,
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
    """``lower()`` of each text column of the address and the username; the order.

    The admin's order, ``(listed, id)``, gets one unless the column is
    unique. A column the table lacks, encrypts, or keeps as other than
    text gets no ``lower()``; a table without an id, no order; a long table
    name is cut to PostgreSQL's 63 characters with a digest (A28-PERF-02).
    """
    from domainkit.contrib.auth.models import User
    from domainkit.db import EncryptedType, ValueObjectType
    from domainkit_tests.sample_domains.members.models import Member
    from domainkit_tests.sample_domains.sealed.tables import sealed

    assert lookup_indexes(User, users, "email", lambda name: f'"{name}"') == {
        "ix_domainkit_users_email_lower": 'lower("email")',
        "ix_domainkit_users_username_lower": 'lower("username")',
        "ix_domainkit_users_email_id": '"email", "id"',
    }
    assert list(lookup_indexes(User, users, "username")) == [  # Unique already.
        "ix_domainkit_users_email_lower",
        "ix_domainkit_users_username_lower",
    ]
    assert lookup_indexes(Member, users, "email") == {  # Logs in by address: once.
        "ix_domainkit_users_email_lower": "lower(email)",
        "ix_domainkit_users_email_id": "email, id",
    }
    bare = Table("bare", MetaData(), *abstract_base_user_columns())
    assert lookup_indexes(Member, bare, "email") == {}
    hidden = Table(
        "hidden",
        MetaData(),
        Column("email", EncryptedType()),
        *(column for column in abstract_user_columns() if column.name != "email"),
    )
    assert list(lookup_indexes(User, hidden, "email")) == ["ix_hidden_username_lower"]
    assert list(lookup_indexes(User, sealed, "email")) == [  # Bytes: no lower().
        "ix_sealed_users_username_lower",
        "ix_sealed_users_email_id",
    ]
    wrapped = Table(
        "wrapped",
        MetaData(),
        Column("email", ValueObjectType(PasswordHash, String(254))),
        *(c for c in abstract_user_columns() if c.name not in {"email", "id"}),
    )
    assert list(lookup_indexes(User, wrapped, "email")) == [  # Text, wrapped.
        "ix_wrapped_email_lower",
        "ix_wrapped_username_lower",
    ]
    long = Table("a" * 60, MetaData(), *abstract_user_columns())
    names = list(lookup_indexes(User, long, "email"))
    assert [len(name) for name in names] == [63, 63, 63]
    assert len(set(names)) == 3


ACCOUNTS: Final = "domainkit_tests.sample_domains.accounts"
SEALED: Final = "domainkit_tests.sample_domains.sealed"
USERS: Final = {
    "default": ("domainkit_users", None, {}),
    "custom": (
        "accounts_users",
        ACCOUNTS,
        {
            "USER_MODEL": f"{ACCOUNTS}.models.Account",
            "USER_TABLE": f"{ACCOUNTS}.tables.accounts",
        },
    ),
    "sealed": (
        "sealed_users",
        SEALED,
        {
            "USER_MODEL": f"{SEALED}.models.Sealed",
            "USER_TABLE": f"{SEALED}.tables.sealed",
        },
    ),
}
"""Each user table: its name, the domain that declares it, and ``AUTH``'s keys."""


@pytest.fixture(params=["default", "custom", "sealed"])
def migrated(
    request: pytest.FixtureRequest, database_url: str, tmp_path: Path
) -> Iterator[str]:
    """Auth migrated, addresses identifying users, the user table's name.

    The default user, a project's own, or one whose address is bytes.
    """
    table, domain, named = USERS[request.param]
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, ALPHA, *([domain] if domain else [])],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "IDENTIFIERS": ("email", "username"),
            **named,
        },
    ):
        setup()
        call_command("makemigrations", "-m", "auth", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        yield table
    registry.clear()


async def index_names(table: str) -> set[str]:
    """The names of ``table``'s indexes, as the database has them."""

    def read(connection: Any) -> set[str]:  # noqa: ANN401 - a sync connection.
        return {str(index["name"]) for index in inspect(connection).get_indexes(table)}

    async with connections.engine().connect() as connection:
        return await connection.run_sync(read)


def test_migrate_makes_the_lookup_indexes_on_postgresql(migrated: str) -> None:
    """The indexes a table can take on PostgreSQL, none on SQLite.

    ``migrate`` makes them on the user table, the project's own too, once:
    a second ``migrate`` keeps them, and ``makemigrations`` finds nothing.
    An address kept as bytes gets no ``lower()``, and fails nothing.
    """
    call_command("migrate", stdout=io.StringIO())
    checked = io.StringIO()
    call_command("makemigrations", "--check", stdout=checked)
    present = run_async(index_names(migrated))

    folded = f"ix_{migrated}_email_lower"
    wanted = {f"ix_{migrated}_username_lower", f"ix_{migrated}_email_id"}
    if migrated != "sealed_users":  # Its address is bytes.
        wanted.add(folded)
    assert present & (wanted | {folded}) == (wanted if ON_POSTGRES else set())
    assert checked.getvalue() == "No changes detected.\n"


def test_revisions_leave_alone_only_the_lookup_indexes_by_name(migrated: str) -> None:
    """A project's index whose name starts like one of them stays its own."""
    table = SimpleNamespace(name=migrated)

    def left(name: str) -> bool:
        return not include_object(
            SimpleNamespace(table=table), name, "index", True, None
        )

    assert left(f"ix_{migrated}_email_id")
    assert left(f"ix_{migrated}_username_lower")
    assert not left(f"ix_{migrated}_email_idx")
    assert listing_field(get_user_model(), get_user_table()) == "email"


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}: SQLite has none")
@pytest.mark.parametrize("migrated", ["default"], indirect=True)
def test_migrate_waits_for_no_writer_once_the_indexes_are_made(
    migrated: str, database_url: str
) -> None:
    """A ``migrate`` with no index to make takes no lock on the users table.

    ``CREATE INDEX IF NOT EXISTS`` locks the table against writes before it
    finds the index: a ``migrate`` on a deploy waited behind any open write,
    and the writes after queued behind it (A28-PERF-02).
    """
    url = make_url(database_url)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    held, done = threading.Event(), threading.Event()

    def write() -> None:
        async def hold() -> None:
            connection = await asyncpg.connect(dsn)
            async with connection.transaction():
                await connection.execute("UPDATE domainkit_users SET email = email")
                held.set()
                await asyncio.to_thread(done.wait)
            await connection.close()

        asyncio.run(hold())

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert held.wait(SET_UP)
        on_server(
            database_url, f"ALTER DATABASE \"{url.database}\" SET lock_timeout = '2s'"
        )
        call_command("migrate", stdout=io.StringIO())
    finally:
        done.set()
        writer.join()


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}: SQLite has none")
@pytest.mark.parametrize("migrated", ["default"], indirect=True)
def test_migrate_makes_the_lookup_indexes_in_the_user_tables_own_schema(
    migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user table that names its schema gets them there, once.

    They were looked for in the schema the connection works in, which has
    no such table, so none was made (found in review). An invalid one is
    dropped there, and built again (user report D2).
    """
    people = Table("people", MetaData(), *abstract_user_columns(), schema="identity")
    config = registry.get_domain_config("auth")
    assert isinstance(config, AuthConfig)
    monkeypatch.setattr(config, "user_table", people)

    async def made() -> dict[str, tuple[int, bool]]:
        async with connections.engine().begin() as connection:
            await connection.exec_driver_sql("CREATE SCHEMA identity")
            await connection.run_sync(people.create)
        async with connections.engine().connect() as connection:
            outside = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await config.index_lookups(outside, "default")
            await config.index_lookups(outside, "default")  # Finds them.
            await outside.exec_driver_sql(
                INVALIDATE, ("identity.ix_people_email_lower",)
            )
            await config.index_lookups(outside, "default")
        return await index_states("people", "identity")

    states = run_async(made())

    assert set(states) == {
        "ix_people_email_lower",
        "ix_people_username_lower",
        "ix_people_email_id",
    }
    assert all(valid for _, valid in states.values())


WAITING: Final = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
    " AND query LIKE 'CREATE INDEX%' AND wait_event_type = 'Lock'"
)
"""Whether a build of an index waits for a lock, as one waits for a write."""
TOUCH: Final = "UPDATE domainkit_users SET email = email"
"""A write that holds the default user table until its transaction ends."""
INVALIDATE: Final = (
    "UPDATE pg_index SET indisvalid = false WHERE indexrelid = $1::regclass"
)
"""Mark an index invalid, as a concurrent build that failed leaves it."""
SET_UP: Final = 60
"""Seconds the main thread waits for a helper thread to connect and set its
case up: more than the 10 s the helper may poll for a build to wait, so a
slow runner only slows a case."""


STATES: Final = text(
    "SELECT i.relname, i.oid, x.indisvalid FROM pg_index x"
    " JOIN pg_class i ON i.oid = x.indexrelid"
    " JOIN pg_class t ON t.oid = x.indrelid"
    " JOIN pg_namespace n ON n.oid = t.relnamespace"
    " WHERE t.relname = :table AND n.nspname = :schema AND i.relname LIKE 'ix\\_%'"
)
"""Each ``ix_`` index of a table: its name, oid, and validity."""


async def index_states(
    table: str, schema: str = "public"
) -> dict[str, tuple[int, bool]]:
    """Each of the lookup indexes of ``table``: its oid, and whether it is valid."""
    async with connections.engine().connect() as connection:
        rows = await connection.execute(STATES, {"table": table, "schema": schema})
        return {str(name): (int(oid), bool(valid)) for name, oid, valid in rows}


def asyncpg_dsn(database_url: str) -> str:
    """``database_url`` as asyncpg takes it."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


@contextmanager
def built_statements() -> Iterator[list[str]]:
    """Each statement of the block, of any engine, that makes or drops an index."""
    seen: list[str] = []

    def keep(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy's event arguments.
        if " INDEX " in args[2]:
            seen.append(args[2])

    event.listen(Engine, "before_cursor_execute", keep)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", keep)


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}: SQLite has none")
@pytest.mark.parametrize("migrated", ["default"], indirect=True)
def test_migrate_builds_again_an_index_a_failed_build_left_invalid(
    migrated: str, database_url: str
) -> None:
    """An invalid index counts as missing: dropped, and built again.

    A concurrent build that fails, here one cancelled while it waits for a
    write, leaves an invalid index of its name, which 2.9's ``migrate``
    took as made (user report D2). A valid one stays, one built by hand
    under the name too, and a second ``migrate`` builds and drops nothing.
    """
    folded = "ix_domainkit_users_email_lower"
    by_hand = "ix_domainkit_users_username_lower"

    async def fail_a_build() -> None:
        writer = await asyncpg.connect(asyncpg_dsn(database_url))
        builder = await asyncpg.connect(asyncpg_dsn(database_url))
        try:
            await builder.execute(f"DROP INDEX {folded}")
            await builder.execute(f"DROP INDEX {by_hand}")
            await builder.execute(
                f"CREATE INDEX CONCURRENTLY {by_hand} ON {migrated} (lower(username))"
            )
            async with writer.transaction():
                await writer.execute(TOUCH)
                await builder.execute("SET statement_timeout = '200ms'")
                with pytest.raises(asyncpg.QueryCanceledError):
                    await builder.execute(
                        f"CREATE INDEX CONCURRENTLY {folded} ON {migrated} "
                        "(lower(email))"
                    )
        finally:
            await writer.close()
            await builder.close()

    run_async(fail_a_build())
    before = run_async(index_states(migrated))
    with built_statements() as rebuilt:
        call_command("migrate", stdout=io.StringIO())
    after = run_async(index_states(migrated))
    with built_statements() as again:
        call_command("migrate", stdout=io.StringIO())

    assert before[folded][1] is False
    assert after[folded][1] is True
    assert after[folded][0] != before[folded][0]  # Dropped, then built.
    assert {name: after[name] for name in after if name != folded} == {
        name: before[name] for name in before if name != folded
    }
    assert rebuilt == [
        f"DROP INDEX CONCURRENTLY IF EXISTS public.{folded}",
        (
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {folded} "
            f"ON public.{migrated} (lower(email))"
        ),
    ]
    assert again == []
    assert run_async(index_states(migrated)) == after


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}: SQLite has none")
@pytest.mark.parametrize("migrated", ["default"], indirect=True)
def test_migrate_leaves_alone_an_index_whose_build_still_runs(
    migrated: str, database_url: str
) -> None:
    """While another session builds an index on the table, nothing is built.

    That build's index is invalid until it ends. Dropped, or built beside,
    one of the two builds was cancelled, or ``migrate`` failed on a
    deadlock (review). ``migrate`` says so instead, and the build goes on;
    once it ends, ``migrate`` finds the index and builds nothing.
    """
    folded = "ix_domainkit_users_email_lower"
    building, finish, built = threading.Event(), threading.Event(), []

    def build() -> None:
        async def run() -> None:
            writer = await asyncpg.connect(asyncpg_dsn(database_url))
            builder = await asyncpg.connect(asyncpg_dsn(database_url))
            try:
                await builder.execute(f"DROP INDEX {folded}")
                async with writer.transaction():
                    await writer.execute(TOUCH)  # The build waits for it.
                    made = asyncio.create_task(
                        builder.execute(
                            f"CREATE INDEX CONCURRENTLY {folded} ON {migrated} "
                            "(lower(email))"
                        )
                    )
                    for _ in range(200):
                        if await writer.fetchval(WAITING):
                            break
                        await asyncio.sleep(0.05)
                    building.set()
                    await asyncio.to_thread(finish.wait, 30)
                await made
                built.append("built")
            finally:
                await writer.close()
                await builder.close()

        asyncio.run(run())

    builder = threading.Thread(target=build)
    builder.start()
    try:
        assert building.wait(SET_UP)
        during = run_async(index_states(migrated))
        output = io.StringIO()
        with built_statements() as statements:
            call_command("migrate", stdout=output)
    finally:
        finish.set()
        builder.join(30)
    after = run_async(index_states(migrated))
    again = io.StringIO()
    call_command("migrate", stdout=again)

    assert during[folded][1] is False
    assert statements == []
    assert (
        "Database 'default': skipped the user lookups' indexes: an index on "
        f"public.{migrated} is being built; run migrate again once it ends"
    ) in output.getvalue().splitlines()
    assert built == ["built"]
    assert after[folded] == (during[folded][0], True)  # The same index, built.
    assert "skipped" not in again.getvalue()
    assert run_async(index_states(migrated)) == after


@pytest.mark.skipif(not ON_POSTGRES, reason=f"set {POSTGRES_VARIABLE}: SQLite has none")
@pytest.mark.parametrize("migrated", ["default"], indirect=True)
def test_migrate_builds_the_lookup_indexes_while_the_table_takes_writes(
    migrated: str, database_url: str
) -> None:
    """Each is built ``CONCURRENTLY``, once the hooks' transaction committed.

    2.9 built them in that transaction, with ``CREATE INDEX``: the build
    waited behind an open write, and every write after it waited behind
    the build (user report D2). A write now goes through while the build
    waits, and the build ends once the open write does.
    """
    for name in lookup_indexes(get_user_model(), get_user_table(), "email"):
        on_server(database_url, f"DROP INDEX {name}")
    held, wrote = threading.Event(), []

    def write() -> None:
        async def hold() -> None:
            first = await asyncpg.connect(asyncpg_dsn(database_url))
            second = await asyncpg.connect(asyncpg_dsn(database_url))
            try:
                async with first.transaction():
                    await first.execute(TOUCH)
                    held.set()
                    for _ in range(200):
                        if await second.fetchval(WAITING):
                            break
                        await asyncio.sleep(0.05)
                    await second.execute("SET lock_timeout = '2s'")
                    try:
                        await second.execute(TOUCH)
                        wrote.append("written")
                    except asyncpg.LockNotAvailableError:
                        wrote.append("held")
            finally:
                await first.close()
                await second.close()

        asyncio.run(hold())

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert held.wait(SET_UP)
        call_command("migrate", stdout=io.StringIO())
    finally:
        writer.join(30)

    assert wrote == ["written"]
    states = run_async(index_states(migrated))
    assert len(states) == 3
    assert all(valid for _, valid in states.values())


@pytest.mark.usefixtures("database_access")
def test_migrate_builds_no_lookup_index_on_sqlite(tmp_path: Path) -> None:
    """SQLite keeps none: the follow-up runs no statement there."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[CONTENTTYPES, AUTH, ALPHA],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'u.sqlite3'}"}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        call_command("makemigrations", "-m", "auth", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with built_statements() as seen:
            call_command("migrate", stdout=io.StringIO())
        present = run_async(sqlite_indexes())
    registry.clear()

    assert seen == []
    assert not any(name.endswith(("_lower", "_email_id")) for name in present)


async def sqlite_indexes() -> set[str]:
    """The names of the default user table's indexes."""
    async with connections.engine().connect() as connection:
        return await connection.run_sync(
            lambda sync: {
                str(index["name"]) for index in inspect(sync).get_indexes(users.name)
            }
        )


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
@pytest.mark.parametrize("migrated", ["default", "custom"], indirect=True)
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
