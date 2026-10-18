"""The pytest plugin that projects enable with ``-p domainkit.testing``."""

import os
from pathlib import Path

import pytest

# The pytester fixture unloads the modules first imported while it is in use.
# Alembic keeps global state in its context module, and the routes the inner
# tests serve define tables (the shop's, the idempotency keys): load them
# beforehand, or a second run in the same process defines them again.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes
import domainkit_tests.sample_routes  # noqa: F401
from domainkit.conf import override_settings, settings
from domainkit.conf.env import project_prefix
from domainkit.testing import IN_MEMORY, migrated_database
from domainkit_tests.conftest import MIDDLEWARE


@pytest.fixture
def prefix(monkeypatch: pytest.MonkeyPatch) -> str:
    """Restore the variables the plugin sets once the test ends."""
    prefix = project_prefix()
    for name in ("SETTINGS_MODULE", "ENV_FILE"):
        monkeypatch.delenv(f"{prefix}{name}", raising=False)
    return prefix


def test_projects_get_a_database_and_a_client(
    pytester: pytest.Pytester, tmp_path: Path, prefix: str
) -> None:
    """The fixtures migrate a scratch database and serve the application."""
    (tmp_path / "migrations").mkdir()
    pytester.makepyfile(
        test_project="""
        def test_client(client, database):
            assert database.endswith("test.sqlite3")
            assert client.get("/ok").status_code == 200
        """
    )
    with override_settings(
        MIGRATIONS_DIR=tmp_path / "migrations",
        DATABASES={"default": {"URL": IN_MEMORY}},
    ):
        result = pytester.runpytest_inprocess(
            "-p",
            "domainkit.testing",
            "-p",
            "no:randomly",
            "-p",
            "no:cov",
            "-o",
            "domainkit_settings=project.settings.test",
        )

    result.assert_outcomes(passed=1)
    assert os.environ[f"{prefix}SETTINGS_MODULE"] == "project.settings.test"
    assert os.environ[f"{prefix}ENV_FILE"] == ""


def test_the_ini_option_is_described(pytester: pytest.Pytester) -> None:
    """``pytest --help`` explains ``domainkit_settings``."""
    result = pytester.runpytest_inprocess("-p", "domainkit.testing", "--help")

    result.stdout.fnmatch_lines(
        [
            "  domainkit_settings (string):",
            "* Settings module for the tests (config.settings.test)",
        ]
    )


def test_without_the_ini_option_the_settings_are_left_alone(
    pytester: pytest.Pytester, prefix: str
) -> None:
    """Projects may select their settings some other way."""
    pytester.makepyfile(test_nothing="def test_nothing():\n    pass\n")

    result = pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
    )

    result.assert_outcomes(passed=1)
    assert f"{prefix}SETTINGS_MODULE" not in os.environ


def test_a_named_database_keeps_its_url_and_options(tmp_path: Path) -> None:
    """Only in-memory SQLite is replaced; other options survive."""
    (tmp_path / "migrations").mkdir()
    url = f"sqlite+aiosqlite:///{tmp_path / 'named.sqlite3'}"
    with (
        override_settings(
            MIGRATIONS_DIR=tmp_path / "migrations",
            DATABASES={"default": {"URL": url, "STATEMENT_TIMEOUT": 5}},
        ),
        migrated_database(tmp_path) as used,
    ):
        assert used == url
        assert settings.DATABASES["default"].STATEMENT_TIMEOUT == 5


def test_memory_caches_start_empty_in_every_test(
    pytester: pytest.Pytester, prefix: str
) -> None:
    """The plugin empties memory caches before each test, whatever ran before."""
    pytester.makepyfile(
        test_caches="""
        import asyncio
        from domainkit.cache import caches

        def test_writes():
            async def write():
                await caches["default"].set("k", 1)
            asyncio.run(write())

        def test_reads():
            async def read():
                return await caches["default"].get("k")
            assert asyncio.run(read()) is None
        """
    )

    result = pytester.runpytest_inprocess(
        "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
    )

    result.assert_outcomes(passed=2)


def test_clients_for_users_and_superusers(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """``client_for`` and ``admin_client`` authenticate as new users.

    ``auth_project`` comes first: it loads the database driver and writes the
    migrations before pytester takes its snapshot of the loaded modules.
    """
    pytester.makepyfile(
        test_users="""
        def test_clients(client, client_for, admin_client):
            assert client.get("/private").status_code == 401
            viewer = client_for("shop.view_product")
            assert viewer.get("/products").json()["user"].startswith("user-")
            assert viewer.post("/products").status_code == 403
            other = client_for("shop.view_product")
            assert other.get("/private").json() != viewer.get("/private").json()
            staff = client_for(username="carol", is_staff=True)
            assert staff.get("/staff").status_code == 200
            assert staff.get("/products").status_code == 403
            me = admin_client.get("/auth/me").json()
            assert (me["is_superuser"], me["is_staff"]) == (True, False)
            assert admin_client.get("/admin/auth/users").status_code == 200
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = pytester.runpytest_inprocess(
            "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
        )

    result.assert_outcomes(passed=1)


def test_every_test_starts_from_the_migrated_database(
    auth_project: str, prefix: str, pytester: pytest.Pytester
) -> None:
    """What a test commits is gone in the next; the rows ``migrate`` wrote stay.

    Without the reset, the second ``carol`` would be a taken username.
    """
    pytester.makepyfile(
        test_isolation="""
        from sqlalchemy import func, select, table

        from domainkit.db import connections
        from domainkit.management.base import run_async

        def count(name):
            async def read():
                query = select(func.count()).select_from(table(name))
                async with connections.engine().connect() as connection:
                    return await connection.scalar(query)
            return run_async(read())

        def test_carol(client_for):
            assert client_for("shop.view_product", username="carol")
            assert count("domainkit_users") == 1

        def test_carol_again(client_for):
            assert client_for(username="carol")

        def test_without_a_client(db):
            assert count("domainkit_users") == 0
            assert count("domainkit_tokens") == 0
            assert count("domainkit_permissions") > 0
            assert count("domainkit_content_types") > 0
        """
    )
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        result = pytester.runpytest_inprocess(
            "-p", "domainkit.testing", "-p", "no:randomly", "-p", "no:cov"
        )

    result.assert_outcomes(passed=3)
