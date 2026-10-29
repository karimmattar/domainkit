"""A value object of one field nested in another, through ``dumpdata`` and ``loaddata``.

``Code(sku: Sku)``, with ``Sku(value: str)``, is stored as the text: a
``ValueObjectType`` around another. Until 0.13 a dump wrote such a column
as the inner object, ``{"value": "LAMP-1"}``, which ``loaddata`` then
refused ("holds a value that is not str"). The form 0.11 wrote,
``{"sku": {"value": "LAMP-1"}}``, was refused too, and the bare text made
a ``Code`` around a ``str``, which the database refused in turn. A fixture
holds the column as the database does, its innermost value, and loads
every form 0.11 and 0.12 wrote, each object checking what it holds.
"""

import asyncio
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest
from sqlalchemy import delete, insert, select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import connections
from domainkit.db.derive import table_for
from domainkit.ddd import AggregateRoot, ValueObject
from domainkit.ddd.fields import MaxLength
from domainkit.management import CommandError, call_command
from domainkit_tests.conftest import ALPHA, MakePackage


class Sku(ValueObject):
    """A stock-keeping unit: capitals, digits, and dashes."""

    value: Annotated[str, MaxLength(20)]

    def __post_init__(self) -> None:
        """Refuse anything else."""
        if not self.value.replace("-", "").isalnum() or not self.value.isupper():
            raise ValueError("A SKU is capitals, digits, and dashes.")


class Code(ValueObject):
    """A product's code: one SKU."""

    sku: Sku


class Label(ValueObject):
    """A shelf label: one code, three levels deep."""

    code: Code


class Item(AggregateRoot[int]):
    """A product on a shelf."""

    code: Code
    label: Label | None = None


items = table_for(Item, "framework_test_coded_items", domain="coded")
ROW: dict[str, Any] = {
    "id": 1,
    "version": 1,
    "code": Code(Sku("LAMP-1")),
    "label": Label(Code(Sku("SHELF-9"))),
}


def run(*args: str) -> str:
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


async def _execute(*statements: Any) -> list[Any]:  # noqa: ANN401 - any statement.
    """Run statements on the default database, in one transaction; the rows read."""
    try:
        async with connections.engine().begin() as connection:
            results = [await connection.execute(each) for each in statements]
            last = results[-1]
            return [dict(row._mapping) for row in last] if last.returns_rows else []
    finally:
        await connections.close_all()


def stored() -> list[dict[str, Any]]:
    """The rows of the table, by id."""
    return asyncio.run(_execute(select(items).order_by(items.c.id)))


def fixture(path: Path, *rows: dict[str, Any]) -> Path:
    """Write ``rows`` of the table as a fixture at ``path``."""
    document = {
        "format": "domainkit.fixture",
        "version": 1,
        "tables": [{"table": items.name, "rows": list(rows)}],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def project(
    database_url: str, tmp_path: Path, make_package: MakePackage
) -> Iterator[None]:
    """The ``coded`` domain, migrated, with one row."""
    make_package("coded", {})
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "coded"],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        run("makemigrations", "-m", "init")
        run("migrate")
        asyncio.run(_execute(insert(items).values(**ROW)))
        yield
    registry.clear()


@pytest.mark.usefixtures("project")
def test_a_nested_value_object_round_trips(tmp_path: Path) -> None:
    """Dumped as the text the database holds, loaded back as the objects."""
    path = tmp_path / "items.json"
    unlabelled = {**ROW, "id": 2, "label": None}
    asyncio.run(_execute(insert(items).values(**unlabelled)))
    run("dumpdata", items.name, "--output", str(path))
    asyncio.run(_execute(delete(items)))

    dumped = json.loads(path.read_text())["tables"][0]["rows"]
    loaded = run("loaddata", str(path))

    assert dumped == [
        {"id": 1, "version": 1, "code": "LAMP-1", "label": "SHELF-9"},
        {"id": 2, "version": 1, "code": "LAMP-1", "label": None},
    ]
    assert loaded.endswith(f"Loaded 2 rows from {path}.\n")
    assert stored() == [ROW, unlabelled]


@pytest.mark.usefixtures("project")
def test_fixtures_of_earlier_releases_load(tmp_path: Path) -> None:
    """0.12 wrote the inner object; 0.11, every object, as JSON objects.

    Each row names every column, as a dump does.
    """
    path = fixture(
        tmp_path / "earlier.json",
        {"id": 2, "version": 1, "code": {"value": "LAMP-2"}, "label": None},  # 0.12
        {"id": 3, "version": 1, "code": {"sku": {"value": "LAMP-3"}}, "label": None},
        {
            "id": 4,
            "version": 1,
            "code": "LAMP-4",
            "label": {"code": {"sku": {"value": "SHELF-4"}}},  # 0.11
        },
        {"id": 5, "version": 1, "code": "LAMP-5", "label": {"value": "SHELF-5"}},
    )

    run("loaddata", str(path))

    assert [(row["code"], row["label"]) for row in stored()[1:]] == [
        (Code(Sku("LAMP-2")), None),
        (Code(Sku("LAMP-3")), None),
        (Code(Sku("LAMP-4")), Label(Code(Sku("SHELF-4")))),
        (Code(Sku("LAMP-5")), Label(Code(Sku("SHELF-5")))),
    ]


@pytest.mark.usefixtures("project")
def test_the_inner_object_checks_what_it_holds(tmp_path: Path) -> None:
    """A value the innermost object refuses names it, never the value."""
    path = fixture(
        tmp_path / "bad.json", {"id": 6, "version": 1, "code": "lamp 6", "label": None}
    )

    with pytest.raises(CommandError) as refused:
        run("loaddata", str(path))

    assert str(refused.value).endswith(
        "framework_test_coded_items.code holds a value that is not Sku."
    )
    assert "lamp 6" not in str(refused.value)
    assert len(stored()) == 1


@pytest.mark.usefixtures("project")
def test_a_value_of_the_wrong_kind_names_the_stored_type(tmp_path: Path) -> None:
    """A number where the innermost field holds text is refused by the column."""
    path = fixture(
        tmp_path / "number.json", {"id": 7, "version": 1, "code": 5, "label": None}
    )

    with pytest.raises(CommandError) as refused:
        run("loaddata", str(path))

    assert str(refused.value).endswith(
        "framework_test_coded_items.code holds a value that is not str."
    )
