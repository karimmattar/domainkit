"""A server default ``makemigrations`` writes on one engine applies on the other.

A project may write its revisions on SQLite and deploy them on PostgreSQL:
each contrib table's server defaults, rendered as ``makemigrations`` renders
them on SQLite, must give the value the model's own default gives, on the
database at hand (PostgreSQL with ``DOMAINKIT_TEST_POSTGRES_URL``). Written
as ``false()``, a boolean's default was rendered ``sa.text('0')``, which
PostgreSQL refuses for a boolean column.
"""

import ast
import importlib
import uuid
from typing import Any

import pytest
from alembic.autogenerate import render
from alembic.autogenerate.api import AutogenContext
from alembic.migration import MigrationContext
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    TextClause,
    insert,
    select,
    text,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.db import metadata
from domainkit.management.base import run_async
from domainkit_tests.conftest import MakeDatabase

APPS = (
    "accounting",
    "approvals",
    "audit",
    "auth",
    "contenttypes",
    "flags",
    "idempotency",
    "keys",
    "limits",
    "messaging",
    "objectpermissions",
    "webhooks",
)
"""Every contrib app with tables of its own."""
for app in APPS:
    importlib.import_module(f"domainkit.contrib.{app}.tables")
DEFAULTED = sorted(
    (
        column
        for table in metadata.tables.values()
        if table.name.startswith("domainkit_")
        for column in table.columns
        if column.server_default is not None
    ),
    key=str,
)


def rendered_on_sqlite(column: Column[Any]) -> str | TextClause:
    """The default as a revision written on SQLite holds it: text or SQL."""
    context = AutogenContext(
        MigrationContext.configure(dialect=sqlite.dialect(), opts={}),
        opts={
            "sqlalchemy_module_prefix": "sa.",
            "alembic_module_prefix": "op.",
            "user_module_prefix": None,
        },
    )
    source = render._render_server_default(column.server_default, context)
    assert isinstance(source, str)
    if source.startswith("sa.text("):
        return text(ast.literal_eval(source.removeprefix("sa.text(")[:-1]))
    value = ast.literal_eval(source)
    assert isinstance(value, str)
    return value


def read_default(url: str, column: Column[Any], default: str | TextClause) -> object:
    """A new row's value of ``column``, in a table where it defaults to ``default``."""
    table = Table(
        f"portable_{uuid.uuid4().hex[:12]}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column(column.name, column.type, nullable=False, server_default=default),
    )

    async def run() -> object:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(table.create)
                await connection.execute(insert(table).values(id=1))
                return await connection.scalar(select(table.c[column.name]))
        finally:
            await engine.dispose()

    return run_async(run())


def test_the_contrib_tables_have_server_defaults_to_check() -> None:
    """The booleans of auth, audit, and idempotency, and messaging's queue."""
    assert [str(column) for column in DEFAULTED] == [
        "domainkit_audit_seals.late",
        "domainkit_idempotency_keys.pinned",
        "domainkit_idempotency_keys.tombstone",
        "domainkit_messages.priority",
        "domainkit_messages.queue",
        "domainkit_users.email_verified",
    ]


@pytest.mark.parametrize("column", DEFAULTED, ids=str)
def test_a_default_written_on_sqlite_applies_where_deployed(
    column: Column[Any], make_database: MakeDatabase
) -> None:
    """The same value as the model's own default, on this database."""
    url = make_database("defaults")
    assert column.server_default is not None

    expected = read_default(url, column, column.server_default.arg)  # type: ignore[attr-defined]
    written = read_default(url, column, rendered_on_sqlite(column))

    assert written == expected
    assert expected is not None
