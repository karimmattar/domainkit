"""``dumpdata``, ``loaddata``, ``flush``, and ``seed``: data in and out."""

import asyncio
import enum
import io
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    JSON,
    Column,
    Date,
    Enum,
    Integer,
    Interval,
    LargeBinary,
    Numeric,
    String,
    Table,
    Time,
    UniqueConstraint,
    Uuid,
    func,
    insert,
    select,
    text,
    update,
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
from domainkit.testing import CapturedQueries
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    SHOP,
    MakePackage,
)
from domainkit_tests.sample_domains.shop.tables import products


class Colour(enum.Enum):
    """A column of choices."""

    RED = "red"
    BLUE = "blue"


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
    Column("colour", Enum(Colour)),
    Column("wait", Interval),
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
    "colour": Colour.RED,
    "wait": timedelta(hours=1, seconds=5),
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


async def _scalars(statement: Any) -> list[Any]:  # noqa: ANN401 - any query.
    """Run a query on the default database; every first column."""
    try:
        async with connections.engine().connect() as connection:
            return list(await connection.scalars(statement))
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
            "at": "2026-10-19T13:30:00Z",
            "reference": "6f1c7e6a-7d4b-4c55-9f0e-1f2a3b4c5d6e",
            "extra": {"tags": ["a", "b"], "n": 1},
            "opaque": "as is",
            "colour": "red",
            "wait": "PT1H5S",
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
            with pytest.raises(
                CommandError, match=r"repeat with --force after taking a backup\.$"
            ):
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
        (
            {"format": "domainkit.fixture", "version": 1, "tables": {}},
            ': A fixture\'s "tables" is a list\\.$',
        ),
        (
            {"format": "domainkit.fixture", "version": 1, "tables": [1]},
            ': Each table of a fixture has a "table" and "rows"\\.$',
        ),
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


def test_dump_options_on_the_command_line(project: str, tmp_path: Path) -> None:
    """``-o``, two spaces by default, ``--indent N``, and text as it is."""
    path = tmp_path / "kinds.json"

    async def rename() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(update(kinds).values(label="Café"))
        await connections.close_all()

    asyncio.run(rename())
    printed = run("dumpdata", kinds.name)

    assert printed.startswith('{\n  "format": "domainkit.fixture",\n')
    assert '"label": "Café"' in printed
    assert run("dumpdata", kinds.name, "-o", str(path)) == (
        f"Wrote 1 row(s) of 1 table(s) to {path}.\n"
    )
    assert path.read_text(encoding="utf-8") == printed
    assert run("dumpdata", kinds.name, "--indent", "4").startswith('{\n    "format": ')


def test_dumps_go_to_new_files_only_their_owner_reads(
    project: str, tmp_path: Path
) -> None:
    """Mode 0600 whatever the umask; an existing path, or a planted link, is refused.

    ``-o`` wrote with the umask's mode (0644, for a fixture of personal data
    and password hashes) and followed a link planted where it would go.
    """
    path = tmp_path / "kinds.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("operator's file\n", encoding="utf-8")
    planted = tmp_path / "planted.json"
    planted.symlink_to(victim)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "nowhere.json")
    umask = os.umask(0o022)
    try:
        written = run("dumpdata", kinds.name, "-o", str(path))
    finally:
        os.umask(umask)

    assert written == f"Wrote 1 row(s) of 1 table(s) to {path}.\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for taken in (path, planted, dangling):
        message = f"^{re.escape(str(taken))} exists; name a new file\\.$"
        with pytest.raises(CommandError, match=message):
            run("dumpdata", kinds.name, "-o", str(taken))
    assert victim.read_text(encoding="utf-8") == "operator's file\n"
    assert json.loads(path.read_text(encoding="utf-8"))["tables"][0]["rows"]
    assert not (tmp_path / "nowhere.json").exists()


def test_loading_updates_stored_rows(project: str, tmp_path: Path) -> None:
    """Rows with a stored key replace the stored values, several at a time."""
    path = tmp_path / "kinds.json"
    rows = [{"id": 1, "label": "replaced"}, {"id": 2, "label": "added"}]
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": kinds.name, "rows": rows}],
            }
        ),
        encoding="utf-8",
    )
    run("loaddata", str(path))

    assert run("loaddata", str(path), "--dry-run") == (
        f"{path}: framework_test_kinds: 2 rows, replacing 2\n"
    )
    labels = asyncio.run(_scalars(select(kinds.c.label).order_by(kinds.c.id)))
    assert labels == ["replaced", "added"]


def test_loading_works_in_chunks(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows are written and counted ``CHUNK`` at a time."""
    monkeypatch.setattr(fixtures_module, "CHUNK", 2)
    path = tmp_path / "notes.json"
    tags = [{"kind": number, "tag": "x"} for number in range(5)]
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": kind_tags.name, "rows": tags}],
            }
        ),
        encoding="utf-8",
    )
    run("loaddata", str(path))

    with CapturedQueries() as queries:
        plan = run("loaddata", str(path))

    assert f"{path}: framework_test_kind_tags: 5 rows, replacing 5\n" in plan
    inserts = [each for each in queries.statements if each.startswith("INSERT")]
    assert len(inserts) == 3
    assert count(kind_tags) == 5


def test_dry_runs_cover_every_fixture(project: str, tmp_path: Path) -> None:
    """Each file is read and described; nothing is written."""
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for path, label in ((first, "one"), (second, "two")):
        path.write_text(
            json.dumps(
                {
                    "format": "domainkit.fixture",
                    "version": 1,
                    "tables": [{"table": kind_notes.name, "rows": [{"text": label}]}],
                }
            ),
            encoding="utf-8",
        )

    plan = run("loaddata", str(first), str(second), "--dry-run")

    assert plan == (
        f"{first}: framework_test_kind_notes: 1 row\n"
        f"{second}: framework_test_kind_notes: 1 row\n"
    )
    assert count(kind_notes) == 0


def test_loading_names_constraints_it_cannot_describe(
    project: str, tmp_path: Path
) -> None:
    """A unique value taken by another row is "a constraint"; nothing loads."""
    domain = asyncio.run(_scalar(select(content_types.c.domain).limit(1)))
    model = asyncio.run(
        _scalar(select(content_types.c.model).where(content_types.c.domain == domain))
    )
    path = tmp_path / "taken.json"
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [
                    {
                        "table": content_types.name,
                        "rows": [{"id": 9999, "domain": domain, "model": model}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CommandError) as refused:
        run("loaddata", str(path))

    assert str(refused.value) == (
        f"{path}: the database refused the rows (a constraint); nothing was loaded."
    )


def test_unreadable_fixtures_say_why(project: str, tmp_path: Path) -> None:
    """The reason, not the exception."""
    missing = tmp_path / "missing.json"

    with pytest.raises(CommandError) as refused:
        run("loaddata", str(missing))

    assert str(refused.value) == f"Cannot read {missing}: No such file or directory."


def test_force_is_a_flag(project: str, tmp_path: Path) -> None:
    """``--force`` on the command line, for ``loaddata`` and ``flush``."""
    path = tmp_path / "shop.json"
    path.write_text(run("dumpdata", "shop"), encoding="utf-8")
    sqlite = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"}}
    with override_settings(DATABASES=sqlite):
        run("migrate")
        with override_settings(ENVIRONMENT="staging"):
            assert run("loaddata", str(path), "--force").endswith(
                f"Loaded 1 row from {path}.\n"
            )
            assert run("flush", "--force").endswith(" from 'default'.\n")


def test_flush_says_how_many_rows_went(project: str) -> None:
    """The total of the plan."""
    plan = run("flush", "--dry-run")
    total = sum(int(match) for match in re.findall(r": delete (\d+) rows?\n", plan))

    assert run("flush").endswith(f"Deleted {total} rows from 'default'.\n")


def test_flush_keeps_tables_marked_to_stay(project: str) -> None:
    """``info={"flush": False}`` keeps a table and its rows."""
    kinds.info["flush"] = False
    try:
        plan = run("flush")
    finally:
        del kinds.info["flush"]

    assert "default: framework_test_kinds: kept\n" in plan
    assert count(kinds) == 1


def test_seeds_and_unknown_domains_are_listed(
    project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every domain seeded, in order; every unknown label."""

    async def seed(self: object) -> None:
        return None

    monkeypatch.setattr(type(registry.get_domain_config("shop")), "seed", seed)

    assert run("seed") == "Seeded shop, kinds.\n"
    with pytest.raises(CommandError) as refused:
        run("seed", "nope", "nada")
    assert str(refused.value) == "No installed domain 'nope', 'nada'."


def test_fixtures_refuse_odd_table_names(project: str, tmp_path: Path) -> None:
    """A table name that is not text is refused like an unknown one."""
    with pytest.raises(FixtureError, match="holds no table"):
        read_fixture(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": ["x"], "rows": []}],
            },
            "default",
        )
    with pytest.raises(FixtureError) as refused:
        read_fixture(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": kinds.name, "rows": [{"a": 1, "b": 2}]}],
            },
            "default",
        )
    assert str(refused.value) == "'framework_test_kinds' has no column a, b."


def test_null_values_load_as_null(project: str, tmp_path: Path) -> None:
    """``null`` is no value, whatever the column's type."""
    path = tmp_path / "nulls.json"
    row: dict[str, Any] = dict.fromkeys(ROW) | {"id": 3}
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": kinds.name, "rows": [row]}],
            }
        ),
        encoding="utf-8",
    )

    run("loaddata", str(path))

    stored = asyncio.run(_scalar(select(kinds.c.day).where(kinds.c.id == 3)))
    assert stored is None


def test_serial_ids_continue_after_a_load(project: str, tmp_path: Path) -> None:
    """New rows get ids after the highest loaded, past tables without serials."""
    path = tmp_path / "ids.json"
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [
                    {"table": kind_notes.name, "rows": [{"text": "first"}]},
                    {"table": kinds.name, "rows": [{"id": 5, "label": "five"}]},
                ],
            }
        ),
        encoding="utf-8",
    )
    run("loaddata", str(path))

    async def add() -> int:
        try:
            async with connections.engine().begin() as connection:
                added = await connection.execute(
                    insert(kinds).values(label="next").returning(kinds.c.id)
                )
                return int(added.scalar_one())
        finally:
            await connections.close_all()

    assert asyncio.run(add()) == 6


def test_tables_that_are_all_key_still_refuse_other_duplicates(
    project: str, tmp_path: Path
) -> None:
    """Only a row stored under the same key is kept; another unique value is not."""
    pairs = Table(
        "framework_test_kind_pairs",
        metadata,
        Column("kind", Integer, primary_key=True),
        Column("tag", String(20), primary_key=True),
        UniqueConstraint("tag"),
        info={"domain": "kinds"},
    )
    path = tmp_path / "pairs.json"
    path.write_text(
        json.dumps(
            {
                "format": "domainkit.fixture",
                "version": 1,
                "tables": [{"table": pairs.name, "rows": [{"kind": 2, "tag": "x"}]}],
            }
        ),
        encoding="utf-8",
    )

    async def create() -> None:
        async with connections.engine().begin() as connection:
            await connection.run_sync(pairs.create)
            await connection.execute(insert(pairs).values(kind=1, tag="x"))
        await connections.close_all()

    try:
        asyncio.run(create())
        with pytest.raises(CommandError, match="the database refused the rows"):
            run("loaddata", str(path))
        stored = asyncio.run(_scalars(select(pairs.c.kind)))
    finally:
        metadata.remove(pairs)

    assert stored == [1]
