"""``loaddata`` and ``flush`` against credentials, grants, and the trail (DATA-12).

Until 0.11 a fixture replaced any stored row with the same primary key,
a user's included (password hash, ``is_superuser``, permissions), behind
the aggregates and their events; ``flush`` emptied the auth tables (users,
tokens, second factors); and the audit row of either named neither the
tables nor the fixture's content.
"""

import asyncio
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, MetaData, Table, Uuid, insert, select

from domainkit.conf import override_settings
from domainkit.contrib.auth.models import PasswordHash
from domainkit.contrib.auth.tables import (
    DEFAULT_USER_TABLES,
    group_permissions,
    groups,
    mfa_devices,
    permissions,
    recovery_codes,
    tokens,
    user_link_tables,
    users,
)
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import connections
from domainkit.management import CommandError, call_command
from domainkit.management.operators import Operator, require_named
from domainkit_tests.sample_domains.shop.tables import products
from domainkit_tests.test_data_commands import (
    count,
    kinds,
    operator_rows,
    project,  # noqa: F401 - fixture.
    run,
)

USER_ID: uuid.UUID = uuid.UUID("0b6f3c1e-8f0a-4d7b-9c55-2b1d3e4f5a6b")
STORED_HASH = "pbkdf2_sha256$1$stored$c3RvcmVkLWhhc2g="
FORGED_HASH = "pbkdf2_sha256$1$forged$Zm9yZ2VkLWhhc2g="
user_groups, user_permissions = DEFAULT_USER_TABLES[1:]


def user_row(**changes: object) -> dict[str, Any]:
    """The stored user's row as a fixture holds it, with ``changes``."""
    return {
        "id": str(USER_ID),
        "password": STORED_HASH,
        "last_login": None,
        "is_active": True,
        "is_superuser": False,
        "version": 1,
        "username": "mallory",
        "email": "mallory@example.com",
        "email_verified": False,
        "first_name": "",
        "last_name": "",
        "is_staff": False,
        "date_joined": "2026-10-01T00:00:00Z",
        **changes,
    }


async def _store_user() -> None:
    """A user, a token, an authenticator, and a recovery code of theirs."""
    now = datetime.now(UTC)
    try:
        async with connections.engine().begin() as connection:
            row = user_row(id=USER_ID, password=PasswordHash(STORED_HASH))
            row["date_joined"] = now
            await connection.execute(insert(users).values(**row))
            await connection.execute(
                insert(tokens).values(
                    id="a" * 64,
                    user_id=USER_ID,
                    kind="access",
                    name="",
                    created_at=now,
                )
            )
            await connection.execute(
                insert(mfa_devices).values(
                    id=uuid.uuid4(), user_id=USER_ID, secret=b"s", created_at=now
                )
            )
            await connection.execute(
                insert(recovery_codes).values(id="b" * 64, user_id=USER_ID)
            )
    finally:
        await connections.close_all()


@pytest.fixture
def stored_user(project: str) -> str:  # noqa: F811 - the fixture.
    """The project of ``test_data_commands``, with a user and their credentials."""
    asyncio.run(_store_user())
    return project


def fixture_file(path: Path, *tables: tuple[Table, list[dict[str, Any]]]) -> Path:
    """Write a fixture of ``tables`` to ``path``."""
    document = {
        "format": "domainkit.fixture",
        "version": 1,
        "tables": [{"table": table.name, "rows": rows} for table, rows in tables],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def stored_user_row() -> dict[str, Any]:
    """The user's row as stored now."""

    async def read() -> dict[str, Any]:
        try:
            async with connections.engine().connect() as connection:
                found = await connection.execute(
                    select(users).where(users.c.id == USER_ID)
                )
                return dict(found.one()._mapping)
        finally:
            await connections.close_all()

    return asyncio.run(read())


def test_a_fixture_cannot_replace_a_users_row(stored_user: str, tmp_path: Path) -> None:
    """Nor make them a superuser with a password hash the fixture chose.

    At 0.10 nothing but an accident of the password column's type stopped
    it: the command died with a ``StatementError`` traceback while writing.
    """
    path = fixture_file(
        tmp_path / "users.json",
        (
            users,
            [user_row(password=FORGED_HASH, is_superuser=True, is_staff=True)],
        ),
    )

    with pytest.raises(CommandError, match="guarded tables"):
        run("loaddata", str(path))

    stored = stored_user_row()
    assert stored["password"] == PasswordHash(STORED_HASH)
    assert (stored["is_superuser"], stored["is_staff"]) == (False, False)


def test_a_fixture_cannot_grant_permissions(stored_user: str, tmp_path: Path) -> None:
    """New rows are grants too: refused, not only replacements.

    At 0.10 the fixture gave the stored user every permission, directly and
    through a new group, and no ``auth`` event or audit row named the grant.
    """
    granted = [
        {"user_id": str(USER_ID), "permission_id": permission}
        for permission in asyncio.run(_permissions())
    ]
    group = str(uuid.uuid4())
    path = fixture_file(
        tmp_path / "grants.json",
        (groups, [{"id": group, "name": "root", "version": 1}]),
        (
            group_permissions,
            [
                {"group_id": group, "permission_id": row["permission_id"]}
                for row in granted
            ],
        ),
        (user_groups, [{"user_id": str(USER_ID), "group_id": group}]),
        (user_permissions, granted),
    )

    with pytest.raises(CommandError, match="guarded tables"):
        run("loaddata", str(path))

    assert [count(table) for table in (groups, user_groups, user_permissions)] == [
        0,
        0,
        0,
    ]


async def _permissions() -> list[int]:
    """The ids of the permissions ``migrate`` wrote."""
    try:
        async with connections.engine().connect() as connection:
            return list(await connection.scalars(select(permissions.c.id)))
    finally:
        await connections.close_all()


def test_flush_keeps_users_and_their_credentials(stored_user: str) -> None:
    """At 0.10 it emptied users, tokens, authenticators, and recovery codes."""
    output = run("flush")

    assert "default: domainkit_users: kept (guarded)\n" in output
    assert count(products) == 0
    assert [count(table) for table in (users, tokens, mfa_devices, recovery_codes)] == [
        1,
        1,
        1,
        1,
    ]


def test_the_trail_names_the_tables_and_the_fixture(
    stored_user: str, tmp_path: Path
) -> None:
    """Which tables, how many rows of each, and the fixture's SHA-256.

    At 0.10 the row held the totals and the file's name only: nothing said
    what had been replaced, nor which content the file had then.
    """
    path = fixture_file(
        tmp_path / "kinds.json", (kinds, [{"id": 1, "label": "replaced"}])
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    run("loaddata", str(path), "--replace", "--operator", "alice")
    run("flush", "--operator", "alice")

    loaded, flushed = operator_rows()
    assert loaded["details"]["sha256"] == digest
    assert loaded["details"]["tables"] == {kinds.name: {"rows": 1, "replaced": 1}}
    assert flushed["details"]["tables"][kinds.name] == 1  # Deleted.
    assert set(flushed["details"]["kept"]) >= {users.name, tokens.name}


@pytest.mark.parametrize("command", ["loaddata", "flush"])
def test_deployments_need_a_named_operator(
    stored_user: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """In staging and production the login name is not enough (ADR 0070).

    At 0.10 ``--force`` was enough: the row named whoever the process ran as.
    """
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)
    path = fixture_file(tmp_path / "kinds.json", (kinds, [{"id": 9}]))
    arguments = [str(path)] if command == "loaddata" else []
    sqlite = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"}}

    with override_settings(DATABASES=sqlite):
        run("migrate")
        run("loaddata", str(fixture_file(tmp_path / "one.json", (kinds, [{"id": 1}]))))
        with (
            override_settings(ENVIRONMENT="staging"),
            pytest.raises(CommandError) as refused,
        ):
            call_command(command, *arguments, "--force", stdout=io.StringIO())

        assert str(refused.value) == (
            f"Name the operator who runs {command} in staging: give --operator "
            "NAME, or set DOMAINKIT_OPERATOR."
        )
        assert count(kinds) == 1
        with override_settings(ENVIRONMENT="staging"):
            monkeypatch.setenv("DOMAINKIT_OPERATOR", "ops")
            call_command(command, *arguments, "--force", stdout=io.StringIO())
        assert count(kinds) == (2 if command == "loaddata" else 0)


def test_nobody_is_no_named_operator(tmp_path: Path) -> None:
    """A command run without ``execute`` has no operator: refused where deployed."""
    sqlite = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"}}
    with override_settings(DATABASES=sqlite):
        require_named(Operator("ops", "option"), "flush")
        require_named(None, "flush")
        with (
            override_settings(ENVIRONMENT="production"),
            pytest.raises(CommandError, match=r"^Name the operator who runs flush"),
        ):
            require_named(None, "flush")


def test_groups_and_content_types_are_guarded(stored_user: str) -> None:
    """Permissions hang from content types; groups carry permissions."""
    for table in (groups, group_permissions, permissions, content_types, users):
        assert table.info.get("guarded") is True, table.name


def test_flush_keeps_what_guarded_tables_reference(stored_user: str) -> None:
    """Deleting a parent would cascade into a guarded table: it is kept too.

    Permissions reference content types (``ON DELETE CASCADE``): a content
    type that was not guarded itself still stays, with every permission.
    """
    before = asyncio.run(_permissions())
    del content_types.info["guarded"]
    try:
        output = run("flush", "--dry-run")
        run("flush")
    finally:
        content_types.info["guarded"] = True

    assert "default: domainkit_content_types: kept (guarded)\n" in output
    assert asyncio.run(_permissions()) == before


def test_guarded_tables_on_purpose(stored_user: str, tmp_path: Path) -> None:
    """``--include-guarded`` loads grants and empties credentials, recorded so."""
    permission, *_ = asyncio.run(_permissions())
    path = fixture_file(
        tmp_path / "grant.json",
        (user_permissions, [{"user_id": str(USER_ID), "permission_id": permission}]),
    )

    plan = run("loaddata", str(path), "--dry-run")
    run("loaddata", str(path), "--include-guarded", "--operator", "alice")
    granted = count(user_permissions)
    output = run("flush", "--include-guarded", "--operator", "alice")

    assert plan == f"{path}: {user_permissions.name}: 1 row (guarded)\n"
    assert granted == 1
    assert "kept (guarded)" not in output
    assert [
        count(table) for table in (users, tokens, mfa_devices, user_permissions)
    ] == [
        0,
        0,
        0,
        0,
    ]
    loaded, flushed = operator_rows()
    assert loaded["details"]["include_guarded"] is True
    assert loaded["details"]["tables"] == {
        user_permissions.name: {"rows": 1, "replaced": 0}
    }
    assert flushed["details"]["include_guarded"] is True
    assert flushed["details"]["tables"][users.name] == 1


def test_a_custom_user_table_and_its_links_are_guarded() -> None:
    """Auth marks the table ``AUTH.USER_TABLE`` names when it links it."""
    other = MetaData()
    accounts = Table(
        "accounts_users",
        other,
        Column("id", Uuid, primary_key=True),
        info={"domain": "accounts"},
    )

    links = user_link_tables(accounts)

    assert [table.info.get("guarded") for table in (accounts, *links)] == [True] * 3
