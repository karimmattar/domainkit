"""``dumpdata``, ``loaddata``, ``flush``, and ``seed``: data in and out."""

import asyncio
import io
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    JSON,
    Column,
    Date,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Time,
    Uuid,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.types import NullType, TypeDecorator

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit import record
from domainkit.contrib.audit.tables import audit_log
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.db import UTCDateTime, connections, metadata
from domainkit.db import fixtures as fixtures_module
from domainkit.db.fixtures import FixtureError, read_fixture, select_tables, to_json
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    SHOP,
    MakePackage,
)
from domainkit_tests.sample_domains.shop.tables import products


class Opaque(TypeDecorator[str]):
    """A type whose Python type nobody knows."""

    impl = String(20)
    cache_ok = True

    @property
    def python_type(self) -> type:
        """Not known."""
        raise NotImplementedError


kinds = Table(
    "framework_test_kinds",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("label", String(20)),
    Column("blob", LargeBinary),
    Column("amount", Numeric(10, 2, asdecimal=True)),
    Column("day", Date),
    Column("hour", Time),
    Column("at", UTCDateTime),
    Column("reference", Uuid),
    Column("extra", JSON),
    Column("opaque", Opaque),
    info={"domain": "kinds"},
    extend_existing=True,
)
"""Every kind of value a fixture carries, in the ``kinds`` test domain."""
kind_notes = Table(
    "framework_test_kind_notes",
    metadata,
    Column("text", String(20)),
    info={"domain": "kinds"},
    extend_existing=True,
)
"""A table without a primary key: rows are only ever added."""
kind_tags = Table(
    "framework_test_kind_tags",
    metadata,
    Column("kind", Integer, primary_key=True),
    Column("tag", String(20), primary_key=True),
    info={"domain": "kinds"},
    extend_existing=True,
)
"""A table that is all key: a stored row stays as it is."""
ROW: dict[str, Any] = {
    "id": 1,
    "label": "first",
    "blob": b"\x00\xffbytes",
    "amount": Decimal("12.50"),
    "day": date(2026, 10, 19),
    "hour": time(13, 30),
    "at": datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
    "reference": uuid.UUID("6f1c7e6a-7d4b-4c55-9f0e-1f2a3b4c5d6e"),
    "extra": {"tags": ["a", "b"], "n": 1},
    "opaque": "as is",
}
SEEDING = """
from sqlalchemy import insert, select

from domainkit.apps import DomainConfig
from domainkit.db import connections
from domainkit_tests.test_data_commands import kinds


class KindsConfig(DomainConfig):
    label = "kinds"

    async def seed(self) -> None:
        async with connections.engine().begin() as connection:
            if await connection.scalar(select(kinds.c.id).where(kinds.c.id == 7)):
                return
            await connection.execute(insert(kinds).values(id=7, label="seeded"))
"""


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


@pytest.fixture
def project(
    database_url: str, tmp_path: Path, make_package: MakePackage
) -> Iterator[str]:
    """Content types, auth, audit, shop, and the ``kinds`` domain, migrated."""
    make_package("kinds", {"apps.py": SEEDING})
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.audit",
            SHOP,
            ALPHA,
            "kinds",
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        AUTH={"PASSWORD_HASHERS": [FAST_HASHER]},
    ):
        setup()
        run("makemigrations", "-m", "init")
        run("migrate")
        asyncio.run(_fill())
        yield database_url
    registry.clear()


async def _fill() -> None:
    """A product, a row of every kind, and an audit row."""
    async with connections.engine().begin() as connection:
        await connection.execute(
            insert(products).values(id=uuid.uuid4(), name="Lamp", version=1)
        )
        await connection.execute(insert(kinds).values(**ROW))
    await record("test.filled", target="kinds")
    await connections.close_all()


async def _scalar(statement: Any) -> Any:  # noqa: ANN401 - any result.
    """Run a query on the default database."""
    try:
        async with connections.engine().connect() as connection:
            return await connection.scalar(statement)
    finally:
        await connections.close_all()


def count(table: Table) -> int:
    """The rows ``table`` holds."""
    return int(asyncio.run(_scalar(select(func.count()).select_from(table))))


def test_dumps_are_json_tables_parents_first(project: str) -> None:
    """Every fixture table, in foreign key order, values as JSON; no state."""
    document = json.loads(run("dumpdata"))

    assert (document["format"], document["version"]) == ("domainkit.fixture", 1)
    names = [entry["table"] for entry in document["tables"]]
    assert names.index("domainkit_content_types") < names.index("domainkit_permissions")
    assert "domainkit_tokens" not in names
    assert not [name for name in names if name.startswith("domainkit_audit")]
    (kinds_entry,) = [
        entry for entry in document["tables"] if entry["table"] == kinds.name
    ]
    assert kinds_entry["rows"] == [
        {
            "id": 1,
            "label": "first",
            "blob": "AP9ieXRlcw==",
            "amount": "12.50",
            "day": "2026-10-19",
            "hour": "13:30:00",
            "at": "2026-10-19T13:30:00+00:00",
            "reference": "6f1c7e6a-7d4b-4c55-9f0e-1f2a3b4c5d6e",
            "extra": {"tags": ["a", "b"], "n": 1},
            "opaque": "as is",
        }
    ]


def test_dumps_pick_tables_or_domains(project: str, tmp_path: Path) -> None:
    """Names are tables or domain labels; an unknown one is refused."""
    path = tmp_path / "shop.json"

    assert run(
        "dumpdata", "shop", "framework_test_kinds", "shop", "--output", str(path)
    ) == (f"Wrote 2 row(s) of 3 table(s) to {path}.\n")
    tables = [entry["table"] for entry in json.loads(path.read_text())["tables"]]
    assert tables == ["framework_test_kinds", "shop_products", "shop_stock_levels"]
    with pytest.raises(CommandError, match="'domainkit_audit_log' names no fixture"):
        run("dumpdata", "domainkit_audit_log")


def test_loading_replaces_rows_and_says_so_first(project: str, tmp_path: Path) -> None:
    """Rows with a stored key replace it; values convert back from JSON."""
    path = tmp_path / "kinds.json"
    path.write_text(run("dumpdata", kinds.name), encoding="utf-8")
    document = json.loads(path.read_text())
    document["tables"][0]["rows"].append({**document["tables"][0]["rows"][0], "id": 2})
    path.write_text(json.dumps(document), encoding="utf-8")

    assert run("loaddata", str(path), "--dry-run") == (
        f"{path}: framework_test_kinds: 2 rows, replacing 1\n"
    )
    assert count(kinds) == 1
    assert run("loaddata", str(path)).endswith(f"Loaded 2 rows from {path}.\n")

    async def second() -> Any:  # noqa: ANN401 - a row.
        try:
            async with connections.engine().connect() as connection:
                return (
                    await connection.execute(select(kinds).where(kinds.c.id == 2))
                ).one()
        finally:
            await connections.close_all()

    assert dict(asyncio.run(second())._mapping) == {**ROW, "id": 2}


def test_loading_is_refused_in_deployments_without_force(
    project: str, tmp_path: Path
) -> None:
    """Staging and production say what would happen, then refuse."""
    path = tmp_path / "shop.json"
    path.write_text(run("dumpdata", "shop"), encoding="utf-8")
    sqlite = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"}}

    with override_settings(DATABASES=sqlite):
        run("migrate")
        with override_settings(ENVIRONMENT="production"):
            output = io.StringIO()
            with pytest.raises(CommandError, match="repeat with --force"):
                call_command("loaddata", str(path), stdout=output)
            assert output.getvalue() == (
                f"{path}: shop_products: 1 row\n{path}: shop_stock_levels: 0 rows\n"
            )
            assert run("loaddata", str(path), force=True).endswith(
                f"Loaded 1 row from {path}.\n"
            )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "Not a fixture"),
        ({"format": "domainkit.fixture", "version": 2}, "version 2 is unknown"),
        ({"format": "domainkit.fixture", "version": 1, "tables": {}}, "is a list"),
        ({"format": "domainkit.fixture", "version": 1, "tables": [1]}, "has a"),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": 1, "rows": []}],
            },
            "holds no table 1",
        ),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": "domainkit_audit_log", "rows": []}],
            },
            "holds state",
        ),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": "framework_test_kinds", "rows": [[]]}],
            },
            "is not an object",
        ),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": "framework_test_kinds", "rows": [{"nope": 1}]}],
            },
            "has no column nope",
        ),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [
                    {
                        "table": "framework_test_kinds",
                        "rows": [{"day": "secret-not-a-date"}],
                    }
                ],
            },
            "framework_test_kinds.day holds a value that is not date",
        ),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [
                    {"table": "framework_test_kinds", "rows": [{"blob": "%%%"}]}
                ],
            },
            "blob holds a value that is not bytes",
        ),
        (
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": "framework_test_kinds", "rows": [{"blob": 5}]}],
            },
            "blob holds a value that is not bytes",
        ),
    ],
)
def test_loading_refuses_what_is_not_a_fixture(
    project: str, tmp_path: Path, document: object, message: str
) -> None:
    """Shapes, versions, tables, columns, and values; values are never quoted."""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CommandError, match=message) as refused:
        run("loaddata", str(path))
    assert "secret" not in str(refused.value)


def test_loading_reports_unreadable_files_and_refused_rows(
    project: str, tmp_path: Path
) -> None:
    """Missing files, broken JSON, and rows the database refuses; nothing loads."""
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    refused = tmp_path / "refused.json"
    refused.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [
                    {"table": "shop_products", "rows": [{"id": str(uuid.uuid4())}]}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError, match="Cannot read"):
        run("loaddata", str(tmp_path / "missing.json"))
    with pytest.raises(CommandError, match="is not a JSON document"):
        run("loaddata", str(broken))
    with pytest.raises(CommandError, match=r"refused the rows \(not null\); nothing"):
        run("loaddata", str(refused))
    assert count(products) == 1


def test_flush_empties_all_but_the_audit_trail(project: str) -> None:
    """It says what goes and what stays, keeps the trail, and reruns the hooks."""
    plan = run("flush", "--dry-run")
    assert "default: shop_products: delete 1 row\n" in plan
    assert "default: domainkit_audit_log: kept\n" in plan
    assert count(products) == 1

    output = run("flush")

    assert output.startswith(plan)
    assert output.endswith(" from 'default'.\n")
    assert (count(products), count(kinds)) == (0, 0)
    assert count(audit_log) == 1
    assert count(content_types) > 0  # The migrate hooks put them back.
    first_id = asyncio.run(_scalar(select(func.min(content_types.c.id))))
    assert first_id == 1  # Sequences start again.


def test_flush_is_refused_in_deployments_without_force(
    project: str, tmp_path: Path
) -> None:
    """Staging and production list what would go, then refuse."""
    sqlite = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"}}
    with override_settings(DATABASES=sqlite):
        run("migrate")
        with override_settings(ENVIRONMENT="staging"):
            with pytest.raises(CommandError, match="repeat with --force"):
                run("flush")
            assert run("flush", force=True).endswith(" rows from 'default'.\n")


def test_flush_keeps_tables_with_triggers_on_postgres(project: str) -> None:
    """On PostgreSQL, a table with triggers of its own is append-only: kept."""

    async def protect() -> str:
        try:
            async with connections.engine().begin() as connection:
                if connection.dialect.name != "postgresql":
                    return connection.dialect.name
                await connection.execute(
                    text(
                        "CREATE FUNCTION framework_test_refuse() RETURNS trigger "
                        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'no'; END $$"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TRIGGER framework_test_refuse BEFORE DELETE ON "
                        "framework_test_kinds FOR EACH ROW EXECUTE FUNCTION "
                        "framework_test_refuse()"
                    )
                )
                return "postgresql"
        finally:
            await connections.close_all()

    if asyncio.run(protect()) != "postgresql":
        pytest.skip("Only PostgreSQL tables have triggers here")
    assert "framework_test_kinds: kept" in run("flush")
    assert count(kinds) == 1


def test_seed_runs_the_domains_hooks(project: str) -> None:
    """In order, those named, each safe to run again."""
    assert run("seed") == "Seeded kinds.\n"
    assert run("seed", "kinds") == "Seeded kinds.\n"
    assert count(kinds) == 2
    assert run("seed", "shop") == "No domain has seed data.\n"
    with pytest.raises(CommandError, match="No installed domain 'nope'"):
        run("seed", "nope")


def test_tables_without_other_columns_or_keys(project: str, tmp_path: Path) -> None:
    """Rows without a key are added; rows that are all key are kept once."""
    path = tmp_path / "plain.json"
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [
                    {"table": kind_notes.name, "rows": [{"text": "a"}]},
                    {"table": kind_tags.name, "rows": [{"kind": 1, "tag": "x"}]},
                ],
            }
        ),
        encoding="utf-8",
    )

    run("loaddata", str(path))
    plan = run("loaddata", str(path))

    assert f"{path}: framework_test_kind_notes: 1 row\n" in plan
    assert f"{path}: framework_test_kind_tags: 1 row, replacing 1\n" in plan
    assert (count(kind_notes), count(kind_tags)) == (2, 1)


def test_flush_skips_tables_not_created_yet(project: str) -> None:
    """A table the models have but the database lacks is left out."""
    ghost = Table(
        "framework_test_kind_ghosts",
        metadata,
        Column("id", Integer, primary_key=True),
        info={"domain": "kinds"},
    )
    try:
        plan = run("flush", "--dry-run")
    finally:
        metadata.remove(ghost)

    assert "framework_test_kind_ghosts" not in plan
    assert "framework_test_kinds: delete 1 row" in plan


def test_fixture_helpers() -> None:
    """NaN is refused, unknown types pass, and other databases hold other tables."""
    with pytest.raises(ValueError, match="Out of range float"):
        to_json({"value": float("nan")}, indent=None)
    with pytest.raises(FixtureError, match="names no fixture table"):
        select_tables("default", ["nope"])
    assert (
        read_fixture(
            {"format": "domainkit.fixture", "version": 1, "tables": []}, "default"
        )
        == []
    )
    unknown = Column("unknown", NullType())
    assert fixtures_module._decoded(unknown, {"kept": True}) == {"kept": True}
    assert fixtures_module._encoded(kinds.c.label, 3) == 3
