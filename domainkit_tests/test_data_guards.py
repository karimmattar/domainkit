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
from typing import Any, cast

import pytest
from sqlalchemy import Column, MetaData, String, Table, Uuid, insert, select

from domainkit.conf import override_settings
from domainkit.contrib.auth.models import PasswordHash
from domainkit.contrib.auth.tables import (
    DEFAULT_USER_TABLES,
    PasswordHashType,
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
from domainkit.db import ValueObjectType, connections
from domainkit.db import fixtures as fixtures_module
from domainkit.db.fixtures import FixtureError
from domainkit.ddd import ValueObject
from domainkit.management import CommandError, call_command
from domainkit.management.commands.dumpdata import Command as DumpCommand
from domainkit.management.commands.flush import Command as FlushCommand
from domainkit.management.commands.loaddata import Command as LoadCommand
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
STORED_HASH = (
    "pbkdf2_sha256$1$storedsaltstoredsalt12$"
    "W+srH9/Im56gLCidrwK9+XBMvaiKOVdOcogLjZpzAEg="
)
"""The test hasher's hash, as it makes them: a fixture may load it (SEC-12a-3)."""
FORGED_HASH = (
    "pbkdf2_sha256$1$forgedsaltforgedsalt12$"
    "2xjnWtDU5PuCaZ1ILz9owjpUU+0R1aR4pzeM/E9/LSc="
)
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

    with pytest.raises(CommandError) as refused:
        run("loaddata", str(path))

    assert str(refused.value) == (
        f"{path} writes guarded tables (domainkit_groups, "  # Parents first.
        "domainkit_users_groups, domainkit_group_permissions, "
        "domainkit_users_permissions): their rows are logins, permissions, and "
        "secrets. Nothing was loaded; repeat with --include-guarded if that is "
        "what you mean."
    )
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


def test_the_commands_say_what_each_option_does() -> None:
    """``--help`` names the files, the alias, and each switch, in plain words."""
    loading = " ".join(
        LoadCommand().create_parser("manage.py", "loaddata").format_help().split()
    )
    flushing = " ".join(
        FlushCommand().create_parser("manage.py", "flush").format_help().split()
    )
    dumping = " ".join(
        DumpCommand().create_parser("manage.py", "dumpdata").format_help().split()
    )

    for said in (
        "FIXTURE [FIXTURE ...]",
        "--database DATABASE the alias to write",
        "--dry-run only say what would be written",
        "--force required in staging and production",
        "--replace replace stored rows with the same primary key (refused without)",
        (
            "--include-guarded write guarded tables: users, permissions, grants, "
            "tokens, ..."
        ),
    ):
        assert said in loading
    for said in (
        "[TABLE_OR_DOMAIN ...]",
        (
            "TABLE_OR_DOMAIN tables or domain labels (default: every table of "
            "the database) "
        ),
        "--database DATABASE the alias to read",
        (
            "-o, --output OUTPUT a new file to write it to (mode 0600); default: "
            "standard output"
        ),
        "--indent INDENT spaces per level (default: 2)",
        (
            "--include-guarded dump guarded tables too: users and their password "
            "hashes, permissions, grants, dynamic settings, ..."
        ),
        "--operator",
    ):
        assert said in dumping
    for said in (
        "--database DATABASE the alias to empty",
        "--dry-run only say what would be deleted",
        "--force required in staging and production",
        (
            "--include-guarded empty guarded tables too: users, permissions, "
            "grants, tokens, ..."
        ),
    ):
        assert said in flushing


def test_dumps_leave_guarded_tables_out(stored_user: str, tmp_path: Path) -> None:
    """Named, a guarded table is refused; with no names, left out and said so.

    At 0.11 ``dumpdata`` wrote users with their password hashes, dynamic
    settings, and grants to anyone who could run it, and no audit row said
    it had (DATA-12).
    """
    printed, said = io.StringIO(), io.StringIO()
    call_command("dumpdata", stdout=printed, stderr=said)
    named = [entry["table"] for entry in json.loads(printed.getvalue())["tables"]]

    assert users.name not in named
    assert not [name for name in named if name.startswith("domainkit_users")]
    assert kinds.name in named
    assert said.getvalue().startswith(
        "Left out guarded tables (domainkit_content_types, "  # Parents first.
    )
    assert said.getvalue().endswith("); --include-guarded dumps them.\n")
    assert users.name in said.getvalue()
    for names in ([users.name], ["auth"], [kinds.name, user_permissions.name]):
        with pytest.raises(CommandError) as refused:
            run("dumpdata", *names, "-o", str(tmp_path / "out.json"))
        assert str(refused.value).endswith(
            "hold logins, password hashes, permissions, and secrets. Nothing was "
            "written; repeat with --include-guarded if that is what you mean."
        )
    with pytest.raises(CommandError) as both:
        run("dumpdata", users.name, user_permissions.name)
    assert str(both.value).startswith(
        f"Guarded tables ({users.name}, {user_permissions.name}) hold logins, "
    )
    assert not (tmp_path / "out.json").exists()
    assert operator_rows() == []


def test_guarded_dumps_are_audited(
    stored_user: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which tables, how many rows, and the SHA-256 of what was written.

    A dump without guarded tables is not recorded: it holds nothing a load
    of it could not write back without the option either.
    """
    monkeypatch.delenv("DOMAINKIT_OPERATOR", raising=False)
    path = tmp_path / "users.json"

    run("dumpdata", kinds.name, "--include-guarded")
    printed = run("dumpdata", users.name, "--include-guarded", "--operator", "alice")
    run("dumpdata", users.name, "--include-guarded", "-o", str(path))
    sqlite = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"}}
    with override_settings(DATABASES=sqlite):
        run("migrate")
        with (
            override_settings(ENVIRONMENT="staging"),
            pytest.raises(
                CommandError,
                match=r"^Name the operator who runs dumpdata --include-guarded in "
                r"staging: give --operator NAME, or set DOMAINKIT_OPERATOR\.$",
            ),
        ):
            run("dumpdata", users.name, "--include-guarded")
        with override_settings(ENVIRONMENT="staging"):
            named = run(
                "dumpdata", users.name, "--include-guarded", "--operator", "ops"
            )

    printing, writing = operator_rows()
    assert printed.startswith('{\n  "format": "domainkit.fixture",\n')  # --indent 2.
    assert json.loads(named)["tables"] == [{"table": users.name, "rows": []}]
    assert (printing["action"], printing["actor"]) == (
        "operator.dumpdata",
        "operator:alice",
    )
    assert printing["details"] == {
        "database": "default",
        "output": "stdout",
        "include_guarded": True,
        "personal_tables": [],  # Recorded only where deployed (SEC-12a-7).
        "rows": 1,
        "tables": {users.name: 1},
        "sha256": hashlib.sha256(printed.encode()).hexdigest(),
        "operator_source": "option",
    }
    assert writing["details"]["output"] == "users.json"
    assert writing["details"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert writing["details"]["operator_source"] == "system"


def test_a_user_round_trips_through_a_fixture(stored_user: str, tmp_path: Path) -> None:
    """Dumped with its hash as the text it is stored as, and loaded back.

    At 0.11 ``dumpdata`` wrote the hash as ``{"value": ...}``, which
    ``loaddata`` refused, even with ``--include-guarded``. Such fixtures
    load now too; a raw password in place of a hash is still refused.
    """
    path = tmp_path / "users.json"
    run("dumpdata", users.name, "--include-guarded", "-o", str(path))
    before = stored_user_row()
    document = json.loads(path.read_text(encoding="utf-8"))
    run("flush", "--include-guarded")

    run("loaddata", str(path), "--include-guarded")

    assert document["tables"][0]["rows"][0]["password"] == STORED_HASH
    assert stored_user_row() == before
    (row,) = document["tables"][0]["rows"]
    older = fixture_file(
        tmp_path / "older.json", (users, [{**row, "password": {"value": STORED_HASH}}])
    )
    run("flush", "--include-guarded")
    run("loaddata", str(older), "--include-guarded")
    assert stored_user_row() == before
    for refused in ("correct-horse-battery", {"value": "hunter2"}, 7):
        raw = fixture_file(
            tmp_path / "raw.json", (users, [{**row, "password": refused}])
        )
        with pytest.raises(CommandError, match=r"domainkit_users\.password holds a"):
            run("loaddata", str(raw), "--include-guarded", "--replace")
    assert stored_user_row() == before


def test_the_trail_counts_what_a_load_replaced_not_what_it_planned(
    stored_user: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row stored after the plan was counted is a replaced row in the record.

    At 0.11 the record held the plan's count, made before the load's
    transaction: here 0 replaced, of a row the load did replace.
    """
    path = fixture_file(tmp_path / "kinds.json", (kinds, [{"id": 5, "label": "x"}]))
    plan = fixtures_module.plan_load

    async def planned_then_stored(*args: Any) -> Any:  # noqa: ANN401 - the plan.
        loads = await plan(*args)
        async with connections.engine().begin() as connection:
            await connection.execute(insert(kinds).values(id=5, label="meanwhile"))
        await connections.close_all()
        return loads

    monkeypatch.setattr(fixtures_module, "plan_load", planned_then_stored)
    printed = run("loaddata", str(path), "--replace", "--operator", "alice")

    assert f"{path}: {kinds.name}: 1 row\n" in printed  # The plan saw none.
    (loaded,) = operator_rows()
    assert loaded["details"]["replaced"] == 1
    assert loaded["details"]["tables"] == {kinds.name: {"rows": 1, "replaced": 1}}


def test_a_load_returns_what_it_wrote_and_holds_its_tables(
    stored_user: str,
) -> None:
    """Each table's rows, those it replaced, and whether it is guarded.

    On PostgreSQL the load holds each table in ``SHARE ROW EXCLUSIVE``
    mode until it commits, so no other writer changes what it counted.
    """
    row = {**stored_user_row(), "first_name": "Mal"}
    tables = [(users, [row]), (kinds, [{"id": 1, "label": "again"}])]
    seen: list[set[str]] = []
    lock = fixtures_module._lock

    async def held(connection: Any, locked: Any) -> None:  # noqa: ANN401
        await lock(connection, locked)
        if connection.dialect.name == "postgresql":
            modes = await connection.exec_driver_sql(
                "SELECT c.relname FROM pg_locks l JOIN pg_class c "
                "ON c.oid = l.relation WHERE l.pid = pg_backend_pid() "
                "AND l.mode = 'ShareRowExclusiveLock'"
            )
            seen.append({name for (name,) in modes})

    async def load() -> list[Any]:
        fixtures_module._lock = held  # type: ignore[assignment]
        try:
            return await fixtures_module.load("default", tables, replace=True)
        finally:
            fixtures_module._lock = lock

    loaded = asyncio.run(load())

    assert loaded == [
        fixtures_module.TableLoad(users.name, 1, 1, guarded=True),
        fixtures_module.TableLoad(kinds.name, 1, 1, guarded=False),
    ]
    assert stored_user_row()["first_name"] == "Mal"
    if seen:  # PostgreSQL.
        assert seen == [{users.name, kinds.name}]
    # A fixture of no tables locks nothing and loads nothing.
    assert asyncio.run(fixtures_module.load("default", [], replace=False)) == []


def test_password_hash_columns_keep_their_length_and_their_nulls() -> None:
    """``PasswordHashType(length=...)`` sizes the column; a null stays null."""
    hashes = PasswordHashType(length=128)
    column: Column[PasswordHash] = Column("hash", hashes)

    assert cast("String", PasswordHashType().impl_instance).length == 255
    assert cast("String", hashes.impl_instance).length == 128
    assert repr(hashes) == "PasswordHashType(length=128)"
    assert fixtures_module._encoded(column, None) is None
    assert fixtures_module._encoded(column, PasswordHash(STORED_HASH)) == STORED_HASH


@pytest.mark.parametrize(
    ("password", "why"),
    [
        ("plaintext$hunter2", "no hasher of AUTH.PASSWORD_HASHERS made it"),
        ("pbkdf2_sha256$x", "no hasher of AUTH.PASSWORD_HASHERS made it"),
        (
            "pbkdf2_sha256$1$short$c2hvcnQ=",
            "AUTH.PASSWORD_HASHERS would make it again, more strongly",
        ),
    ],
    ids=["unknown-algorithm", "malformed", "short-salt"],
)
def test_a_fixture_loads_only_hashes_the_hashers_would_make(
    stored_user: str, tmp_path: Path, password: str, why: str
) -> None:
    """Each refusal says why, never the hash; an unusable password still loads.

    At 0.12's first cut ``PasswordHash`` checked the text's shape only, so
    a password in clear, or the test hasher's in production, loaded
    (SEC-12a-3).
    """
    row = user_row(id=str(uuid.uuid4()), username="eve", password=password)
    path = fixture_file(tmp_path / "users.json", (users, [row]))

    with pytest.raises(CommandError) as refused:
        run("loaddata", str(path), "--include-guarded")

    assert str(refused.value) == (
        f"{path}: domainkit_users.password holds a value that is not "
        f"PasswordHash: {why}"
    )
    assert password not in str(refused.value)
    unusable = user_row(
        id=str(uuid.uuid4()), username="ivy", password=PasswordHash.unusable().value
    )
    run(
        "loaddata",
        str(fixture_file(tmp_path / "ivy.json", (users, [unusable]))),
        "--include-guarded",
    )
    assert count(users) == 2


class Sku(ValueObject):
    """A project's single-field value object, which refuses lower case."""

    value: str

    def __post_init__(self) -> None:
        """Upper case only."""
        if not self.value.isupper():
            raise ValueError("lower case")


def test_a_value_object_column_loads_what_its_class_takes() -> None:
    """By default the class decides, and a refusal does not say why or quote."""
    column: Column[Sku] = Column("sku", ValueObjectType(Sku, String(10)))
    Table("framework_test_skus", MetaData(), column)

    assert fixtures_module._decoded(column, "AB") == Sku("AB")
    with pytest.raises(FixtureError) as refused:
        fixtures_module._decoded(column, "ab")
    assert (
        str(refused.value) == "framework_test_skus.sku holds a value that is not Sku."
    )
