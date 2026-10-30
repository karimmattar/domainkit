"""SEC-14a-3: a money field added to a table later gets no both-or-neither check.

``MoneyColumns`` gives a pair that may be missing a check that both are
missing or neither is (ADR 0115), and ``makemigrations`` writes it when it
creates the table. Alembic's autogenerate compares no check constraints,
so a nullable money field added to an aggregate whose table exists (the
upgrade docs/money.md describes, "one column and a data migration") gets
its two columns and no check: the database then takes an amount without
its currency, which loads as ``Money(amount, None)`` and fails every read
of the row, and a currency without its amount, which loads as no money.
"""

import io
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NewType
from uuid import UUID

import pytest
from sqlalchemy import MetaData

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.db import migrations as migrations_module
from domainkit.db import table_for
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.ddd import AggregateRoot
from domainkit.management import call_command
from domainkit.money import Money
from domainkit_tests.conftest import ALPHA

PurseId = NewType("PurseId", UUID)
TABLE = "framework_test_sec14a_purses"


class Before(AggregateRoot[PurseId]):
    """A purse with a balance only."""

    balance: Money


class After(AggregateRoot[PurseId]):
    """The same purse, given a limit that may be missing."""

    balance: Money
    limit: Money | None = None


@pytest.mark.usefixtures("database_access")
def test_an_added_money_field_keeps_amount_and_currency_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the second migration, an amount without a currency is refused."""
    database = tmp_path / "purses.sqlite3"
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[ALPHA],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{database}"}},
        MIGRATIONS_DIR=tmp_path / "migrations",
    ):
        setup()
        for model, message in ((Before, "purses"), (After, "limits")):
            schema = MetaData(naming_convention=NAMING_CONVENTION)
            table_for(model, TABLE, metadata=schema)
            monkeypatch.setattr(migrations_module, "metadata", schema)
            call_command("makemigrations", "-m", message, stdout=io.StringIO())
            call_command("migrate", stdout=io.StringIO())
    registry.clear()
    (added,) = (tmp_path / "migrations").glob("*limits*.py")

    assert "limit_money" in added.read_text()
    with (
        closing(sqlite3.connect(database)) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "INSERT INTO framework_test_sec14a_purses (id, version, balance, "
            "balance_currency, "
            '"limit", limit_currency) VALUES (?, 0, 0, ?, 1000000, NULL)',
            ("0" * 32, "USD"),
        )
