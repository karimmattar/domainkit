"""The plugin refuses the test databases to a test that did not request them."""

import asyncio
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

# Loaded before pytester takes its snapshot of the loaded modules (see
# test_testing.py): the routes define tables, Alembic keeps global state.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes  # noqa: F401
from domainkit.conf import override_settings
from domainkit.db import DatabaseAccessRefused, connections
from domainkit.db.connections import create_owner_engine
from domainkit.testing import IN_MEMORY, DatabaseBlocker
from domainkit.testing.databases import REFUSAL
from domainkit_tests.conftest import MIDDLEWARE
from domainkit_tests.test_testing import prefix, run_plugin

__all__ = ["prefix"]

COUNT_USERS = """
from sqlalchemy import func, select, table

from domainkit.db import connections
from domainkit.management.base import run_async


def users():
    async def read():
        query = select(func.count()).select_from(table("domainkit_users"))
        async with connections.engine().connect() as connection:
            return await connection.scalar(query)

    return run_async(read())
"""


@pytest.fixture
def auth_settings(auth_project: str) -> object:
    """The inner session's project: auth, on a database the plugin makes."""
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        yield


def test_a_test_without_db_is_refused_the_database(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """Until 0.13 it read what the test before it left: here, carol."""
    pytester.makepyfile(
        test_refused=COUNT_USERS
        + """

def test_carol(client_for):
    client_for(username="carol")
    assert users() == 1


def test_without_db():
    assert users() == 1


def test_with_db(db):
    assert users() == 0
"""
    )

    result = run_plugin(pytester)

    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(
        [
            (
                "*DatabaseAccessRefused: This test reached the database without "
                "requesting it*Request the db fixture*"
            ),
            "FAILED test_refused.py::test_without_db - *",
        ]
    )


def test_the_first_test_never_reaches_the_configured_database(
    prefix: str, pytester: pytest.Pytester, tmp_path: Path
) -> None:
    """Before any test made the test databases, the settings name the real ones.

    Until 0.13 a first test without ``db`` wrote to the database the test
    settings name; now nothing is written there.
    """
    configured = tmp_path / "configured.sqlite3"
    pytester.makepyfile(
        test_first="""
        from sqlalchemy import text

        from domainkit.db import connections
        from domainkit.management.base import run_async

        def test_writes():
            async def write():
                async with connections.autocommit() as connection:
                    await connection.execute(text("CREATE TABLE written (id INT)"))

            run_async(write())
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{configured}"}}
    ):
        result = run_plugin(pytester)

    result.assert_outcomes(failed=1)
    assert not configured.exists()


def test_the_mark_allows_a_test_what_0_12_allowed(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """``allow_database``: the test databases, as the last test left them."""
    pytester.makepyfile(
        test_marked=COUNT_USERS
        + """
import pytest


def test_carol(client_for):
    client_for(username="carol")


@pytest.mark.allow_database
def test_marked():
    assert users() == 1


@pytest.mark.allow_database
def test_marked_with_db(db):
    assert users() == 0
"""
    )

    run_plugin(pytester).assert_outcomes(passed=3)


def test_a_marked_first_test_reaches_the_test_databases(
    prefix: str, pytester: pytest.Pytester, tmp_path: Path
) -> None:
    """The mark makes the test databases first: the settings' own stay untouched.

    An item without fixtures (a plugin's own kind) is let through as marked.
    """
    (tmp_path / "migrations").mkdir()
    pytester.makeconftest(
        """
        import pytest

        class Plain(pytest.Item):
            def runtest(self):
                pass

        def pytest_collect_file(parent, file_path):
            if file_path.name == "plain.txt":
                return PlainFile.from_parent(parent, path=file_path)

        class PlainFile(pytest.File):
            def collect(self):
                item = Plain.from_parent(self, name="plain")
                item.add_marker(pytest.mark.allow_database)
                yield item
        """
    )
    pytester.makefile(".txt", plain="")
    pytester.makepyfile(
        test_first="""
        import pytest
        from sqlalchemy import text

        from domainkit.conf import settings
        from domainkit.db import connections
        from domainkit.management.base import run_async

        def url():
            return settings.DATABASES["default"].URL.get_secret_value()

        @pytest.fixture
        def early():
            assert url().endswith("test.sqlite3")

        @pytest.mark.parametrize("unmarked", [True])
        def test_unmarked_first(unmarked):
            assert url() == "sqlite+aiosqlite:///:memory:"

        @pytest.mark.allow_database
        def test_writes(early):
            async def write():
                async with connections.autocommit() as connection:
                    await connection.execute(text("CREATE TABLE written (id INT)"))

            run_async(write())
            url = settings.DATABASES["default"].URL.get_secret_value()
            assert url.endswith("test.sqlite3")
        """
    )
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations",
        DATABASES={"default": {"URL": IN_MEMORY}},
    ):
        result = run_plugin(pytester, "-p", "no:doctest", "plain.txt", ".")

    result.assert_outcomes(passed=3)


def test_the_ini_option_allows_every_test(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """``domainkit_allow_database = true`` keeps 0.12's behaviour for the run."""
    pytester.makepyfile(
        test_allowed=COUNT_USERS
        + """

def test_carol(client_for):
    client_for(username="carol")


def test_unmarked():
    assert users() == 1
"""
    )

    result = run_plugin(pytester, "-o", "domainkit_allow_database=true")

    result.assert_outcomes(passed=2)
    refused = run_plugin(pytester, "-o", "domainkit_allow_database=false")
    refused.assert_outcomes(passed=1, failed=1)


def test_the_ini_option_is_described(pytester: pytest.Pytester) -> None:
    """``pytest --help`` explains ``domainkit_allow_database``."""
    result = pytester.runpytest_inprocess("-p", "domainkit.testing", "--help")

    described = " ".join(result.stdout.str().split())  # However it wraps.
    assert (
        "domainkit_allow_database (bool): Let every test reach the databases "
        "without requesting db, as before 0.13 (false)"
    ) in described


def test_a_wider_fixture_unblocks_while_it_works(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """``database_blocker.unblock()``: a module's rows, made once, read by its tests.

    The refusal is back once the block ends: a test that did not request
    ``db`` is still refused.
    """
    pytester.makepyfile(
        test_wider=COUNT_USERS
        + """
import pytest

from domainkit.contrib.auth.testing import create_user
from domainkit.management.base import run_async


@pytest.fixture(scope="module")
def carol(database, database_blocker):
    with database_blocker.unblock():
        run_async(create_user(username="carol"))
    assert database_blocker.blocked


@pytest.fixture
def no_reset(database_blocker):
    with database_blocker.unblock():
        yield


def test_sees_carol(carol, no_reset):
    assert users() == 1


def test_refused_after(carol):
    with pytest.raises(Exception, match="without requesting it"):
        users()
"""
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_the_refusal_reaches_the_application_s_thread(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """A client the test made itself, without ``db``, is refused in its thread too."""
    pytester.makepyfile(
        test_thread="""
        import pytest

        from domainkit.db import DatabaseAccessRefused
        from domainkit.http import get_asgi_application
        from domainkit.testing import TestClient

        def test_own_client():
            with TestClient(get_asgi_application()) as client:
                with pytest.raises(DatabaseAccessRefused):
                    client.authenticated("shop.view_product")
        """
    )

    run_plugin(pytester).assert_outcomes(passed=1)


def test_keepdb_and_a_worker_s_databases_are_made_unblocked(
    auth_settings: None,
    prefix: str,
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--keepdb`` and an xdist worker make, migrate, and put back as before.

    Each worker is a process of its own, with its own refusal.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    pytester.makepyfile(
        test_worker=COUNT_USERS
        + """

def test_one(client_for):
    client_for(username="carol")


def test_two(db):
    assert users() == 0
"""
    )

    run_plugin(pytester, "--keepdb").assert_outcomes(passed=2)
    run_plugin(pytester, "--keepdb").assert_outcomes(passed=2)


@pytest.mark.usefixtures("database_access")
def test_the_plugin_puts_back_what_it_found(
    prefix: str, pytester: pytest.Pytester
) -> None:
    """Once the inner run ends, the databases are as open, or refused, as before."""
    pytester.makepyfile(test_nothing="def test_nothing():\n    pass\n")

    run_plugin(pytester).assert_outcomes(passed=1)
    assert connections.refusal is None
    with DatabaseBlocker("outer").block():
        run_plugin(pytester).assert_outcomes(passed=1)
        assert connections.refusal == "outer"
        run_plugin(pytester, "-o", "domainkit_allow_database=true").assert_outcomes(
            passed=1
        )
        assert connections.refusal == "outer"


@pytest.mark.anyio
async def test_refusing_stops_every_engine_and_session(database_url: str) -> None:
    """Sessions, engines, ``autocommit``, owner engines; ``close_all`` still works."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        await connections.check()
        connections.refuse("not now")
        try:
            for reach in (
                connections.engine,
                connections.session,
                lambda: create_owner_engine(connections.config()),
            ):
                with pytest.raises(DatabaseAccessRefused, match=r"^not now$"):
                    reach()
            with pytest.raises(DatabaseAccessRefused):
                async with connections.autocommit():
                    pass
            assert connections.pool_stats()["default"]["checked_out"] == 0
            await connections.close_all()
        finally:
            connections.allow()
        async with connections.autocommit() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
        await connections.close_all()


def test_refusing_is_seen_by_every_thread(database_url: str) -> None:
    """The refusal is the process's: a loop on another thread is refused too."""
    seen: list[BaseException] = []

    def reach() -> None:
        try:
            asyncio.run(connections.check())
        except DatabaseAccessRefused as refused:
            seen.append(refused)

    blocker = DatabaseBlocker("elsewhere")
    with (
        override_settings(DATABASES={"default": {"URL": database_url}}),
        blocker.block(),
    ):
        thread = threading.Thread(target=reach)
        thread.start()
        thread.join()
    assert [str(refused) for refused in seen] == ["elsewhere"]


@pytest.mark.usefixtures("database_access")
def test_blocks_nest_and_put_back_what_they_found() -> None:
    """An unblock inside a block, and a block inside it, each restore on exit."""
    blocker = DatabaseBlocker()
    assert not blocker.blocked
    with blocker.block():
        assert connections.refusal == REFUSAL
        with blocker.unblock():
            assert not blocker.blocked
            with DatabaseBlocker("inner").block():
                assert connections.refusal == "inner"
            assert not blocker.blocked
        assert blocker.blocked
    assert connections.refusal is None
