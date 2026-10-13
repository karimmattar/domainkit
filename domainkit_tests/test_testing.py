"""The pytest plugin that projects enable with ``-p domainkit.testing``."""

import os
from pathlib import Path

import pytest

# pytester's in-process runs unload the modules first imported inside them;
# Alembic keeps global state in its context module, so load it beforehand.
import domainkit.db.migrations  # noqa: F401
from domainkit.conf import override_settings, settings
from domainkit.conf.env import project_prefix
from domainkit.testing import IN_MEMORY, migrated_database


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
