"""The suite's own project fixtures: a copy of a template migrated once per run.

``auth_project``, ``api`` and ``identity`` used to generate and apply their
revisions in every test; now each shape is migrated once per run (and per
pytest-xdist worker) and every test gets a copy of it. These tests check
that a copy is what ``migrate`` left, whatever an earlier test wrote,
sequences included, and that the templates go when the run ends.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    API,
    AUTH_PROJECT,
    IDENTITY,
    POSTGRES_VARIABLE,
    TEMPLATE_PREFIX,
    Api,
    MakeDatabase,
    Templates,
    on_server,
)

NEXT_TESTS = """
import pytest
from sqlalchemy import text

from domainkit.db import connections
from domainkit.management.base import run_async

FIXTURES = ["auth_project", "api", "identity"]
written = {}


async def write() -> tuple[int, int]:
    async with connections.engine().begin() as connection:
        made = await connection.scalar(
            text(
                "INSERT INTO domainkit_content_types (domain, model) "
                "VALUES ('probe', 'probe') RETURNING id"
            )
        )
        users = await connection.scalar(text("SELECT count(*) FROM domainkit_users"))
    await connections.close_all()
    return made, users


@pytest.mark.parametrize("fixture", FIXTURES)
def test_first(fixture, request):
    request.getfixturevalue(fixture)
    written[fixture] = run_async(write())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_next(fixture, request):
    request.getfixturevalue(fixture)
    assert run_async(write()) == written[fixture]
"""


def test_what_a_test_writes_is_gone_in_the_next(
    identity: Api, pytester: pytest.Pytester
) -> None:
    """The next test of a shape sees neither the rows nor the ids one wrote.

    Both tests insert a content type and read its id: the second gets the
    same id, so the row the first committed is gone and no sequence moved
    on. Restoring rows into a shared database would fail this on
    PostgreSQL, where sequences are not put back. ``identity`` comes first:
    it imports every module the shapes install before pytester takes its
    snapshot of the loaded modules, so none is imported twice.
    """
    pytester.makeconftest("from domainkit_tests.conftest import *  # noqa: F403\n")
    pytester.makepyfile(test_next=NEXT_TESTS)

    result = pytester.runpytest_inprocess(
        "-p", "no:randomly", "-p", "no:cov", "-p", "no:cacheprovider"
    )

    result.assert_outcomes(passed=6)


def test_copies_start_from_the_template_and_leave_it_alone(
    templates: Templates, make_database: MakeDatabase, tmp_path: Path
) -> None:
    """A copy holds the migrated tables; writing to it never reaches the next."""
    first = templates.copy(API, make_database, tmp_path / "first.sqlite3")
    second = templates.copy(API, make_database, tmp_path / "second.sqlite3")
    assert first != second

    async def tables(url: str, *, write: bool = False) -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                if write:
                    await connection.execute(text("DROP TABLE domainkit_users_groups"))
                return set(
                    await connection.run_sync(
                        lambda sync: inspect(sync).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    migrated = run_async(tables(first, write=True))
    assert {"domainkit_users", "domainkit_audit_log", "alembic_version"} <= migrated
    assert "domainkit_users_groups" not in migrated
    assert "domainkit_users_groups" in run_async(tables(second))
    assert len(list(templates.get(API).migrations.glob("*.py"))) == 1
    assert templates.get(API) is templates.get(API)


@pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE),
    reason="set DOMAINKIT_TEST_POSTGRES_URL to a PostgreSQL server",
)
def test_postgresql_templates_are_dropped_when_the_run_ends(tmp_path: Path) -> None:
    """``close`` drops every template it made, and only those."""
    server = os.environ[POSTGRES_VARIABLE]
    made = Templates(tmp_path, server)
    names = [made.get(shape).database for shape in (AUTH_PROJECT, IDENTITY)]
    assert all(name and name.startswith(TEMPLATE_PREFIX) for name in names)

    made.close()

    async def existing() -> set[str]:
        engine = create_async_engine(server)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(text("SELECT datname FROM pg_database"))
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    assert not set(names) & run_async(existing())
    on_server(server, "SELECT 1")  # The server is still there for the next test.
