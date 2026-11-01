"""DATA-13: a revision ``makemigrations`` writes on PostgreSQL fails on SQLite.

``domainkit.db.migrations._configure_and_run`` renders revisions in batch
mode only when the database at hand is SQLite. Written against
PostgreSQL (``DATABASE_URL`` pointed at the compose service, as a team
that develops on what it deploys does), a revision that changes a column
or adds a nullable money field holds ``op.alter_column`` and
``op.create_check_constraint``, which SQLite cannot run: the project's
test suite, on the SQLite test settings ``startproject`` writes, fails
to migrate. Revisions written on SQLite apply on PostgreSQL (batch mode
is plain ``ALTER`` there), so rendering in batch mode everywhere makes
them portable both ways.
"""

import io
import os
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table

from domainkit.conf import override_settings
from domainkit.db import MoneyColumns
from domainkit.db import migrations as migrations_module
from domainkit.db.orm import NAMING_CONVENTION
from domainkit.management import call_command
from domainkit_tests.conftest import POSTGRES_VARIABLE, MakeDatabase

NAME = "framework_test_portable_shelves"


@pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
)
def test_revisions_written_on_postgresql_apply_on_sqlite(
    tmp_path: Path, make_database: MakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column made longer and a nullable money field added migrate SQLite too."""
    schema = MetaData(naming_convention=NAMING_CONVENTION)
    first = Table(
        NAME,
        schema,
        Column("id", Integer, primary_key=True),
        Column("name", String(20), nullable=False),
    )
    monkeypatch.setattr(migrations_module, "metadata", schema)
    versions = tmp_path / "migrations"
    with override_settings(
        MIGRATIONS_DIR=versions,
        DATABASES={"default": {"URL": make_database("written_on")}},
    ):
        call_command("makemigrations", "-m", "shelves", stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        schema.remove(first)
        Table(
            NAME,
            schema,
            Column("id", Integer, primary_key=True),
            Column("name", String(40), nullable=False),
            *MoneyColumns("price", nullable=True),
        )
        call_command("makemigrations", "-m", "longer names", stdout=io.StringIO())

    sqlite = f"sqlite+aiosqlite:///{tmp_path / 'tests.sqlite3'}"
    with override_settings(
        MIGRATIONS_DIR=versions, DATABASES={"default": {"URL": sqlite}}
    ):
        call_command("migrate", stdout=io.StringIO())  # As the test plugin does.
