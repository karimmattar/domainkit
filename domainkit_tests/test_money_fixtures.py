"""Money through ``dumpdata`` and ``loaddata``: the amount as text, and the code."""

import asyncio
import io
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, select

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import connections, table_for
from domainkit.management import CommandError, call_command
from domainkit.money import currency
from domainkit_tests.conftest import ALPHA, MakePackage
from domainkit_tests.test_money_db import Wallet

purses = table_for(Wallet, "framework_test_money_purses_kept", domain="pursed")
ID = UUID(int=1)
ROW: dict[str, Any] = {
    "id": ID,
    "version": 1,
    "name": "kept",
    "balance": Decimal("1.234"),
    "balance_currency": currency("KWD"),
    "limit": None,
    "limit_currency": None,
    "unit": currency("JPY"),
}


def run(*args: str) -> str:
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


async def _execute(*statements: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - any statement.
    """Run statements in one transaction; the rows the last one read."""
    try:
        async with connections.engine().begin() as connection:
            results = [await connection.execute(each) for each in statements]
            last = results[-1]
            return [dict(row._mapping) for row in last] if last.returns_rows else []
    finally:
        await connections.close_all()


def fixture(path: Path, **row: object) -> Path:
    """Write one row of the table, ``ROW``'s with ``row``'s changes, as JSON text."""
    values = {
        **{key: _text(value) for key, value in ROW.items()},
        **row,
    }
    document = {
        "format": "domainkit.fixture",
        "version": 1,
        "tables": [{"table": purses.name, "rows": [values]}],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _text(value: object) -> object:
    """A value of ``ROW`` as a fixture holds it."""
    if value is None or isinstance(value, int):
        return value
    return getattr(value, "code", None) or str(value)


@pytest.fixture
def project(
    database_url: str, tmp_path: Path, make_package: MakePackage
) -> Iterator[None]:
    """The ``pursed`` domain, migrated, with one row."""
    make_package("pursed", {})
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA, "pursed"],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        run("makemigrations", "-m", "init")
        run("migrate")
        asyncio.run(_execute(insert(purses).values(**ROW)))
        yield
    registry.clear()


@pytest.mark.usefixtures("project")
def test_money_round_trips_as_amount_text_and_code(tmp_path: Path) -> None:
    """Dumped as the columns hold it, loaded back exactly."""
    path = tmp_path / "purses.json"
    run("dumpdata", purses.name, "--output", str(path))
    asyncio.run(_execute(delete(purses)))

    (dumped,) = json.loads(path.read_text())["tables"][0]["rows"]
    run("loaddata", str(path))

    assert dumped["balance"] == "1.2340"  # The column's places; zeros past KWD's.
    assert (dumped["balance_currency"], dumped["unit"]) == ("KWD", "JPY")
    assert (dumped["limit"], dumped["limit_currency"]) == (None, None)
    (row,) = asyncio.run(_execute(select(purses)))
    assert row["balance"] == Decimal("1.234")
    assert row["balance_currency"] is currency("KWD")


@pytest.mark.usefixtures("project")
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"balance": "1.2345"}, "hold no amount of money"),
        ({"limit": "5.00"}, "hold no amount of money"),
        ({"balance_currency": "XYZ"}, "not Currency"),
        ({"unit": "usd"}, "not Currency"),
    ],
)
def test_money_a_currency_cannot_hold_is_refused(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    """Too many places, half a pair, an unknown code: nothing is written."""
    asyncio.run(_execute(delete(purses)))

    with pytest.raises(CommandError, match=message) as caught:
        run("loaddata", str(fixture(tmp_path / "bad.json", **changes)))

    assert "1.2345" not in str(caught.value)
    assert asyncio.run(_execute(select(purses))) == []
