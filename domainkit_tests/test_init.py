"""``init`` and the ``domainkit`` program: a new project that works at once."""

import io
import os
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from domainkit.management import (
    CommandError,
    call_command,
    execute_from_command_line,
)
from domainkit.management.commands.init import requirement

PROJECT_FILES = {
    ".env",
    ".gitignore",
    "manage.py",
    "pyproject.toml",
    "src/config/__init__.py",
    "src/config/asgi.py",
    "src/config/settings/__init__.py",
    "src/config/settings/base.py",
    "src/config/settings/local.py",
    "src/config/settings/production.py",
    "src/config/settings/test.py",
    "src/config/urls.py",
    "src/domains/__init__.py",
    "tests/__init__.py",
    "tests/test_health.py",
}


def init(*args: str) -> str:
    """Run ``init`` and return what it printed."""
    output = io.StringIO()
    call_command("init", *args, stdout=output)
    return output.getvalue()


def run(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``python *args`` in ``cwd`` as a user would: no test settings."""
    clean = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DOMAINKIT_", "BILLING_API_"))
    }
    return subprocess.run(  # noqa: S603 - the test interpreter and generated files.
        [sys.executable, *args],
        cwd=cwd,
        env={**clean, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_init_creates_a_project_that_passes_check_and_its_tests(
    tmp_path: Path,
) -> None:
    """The generated project needs no edits: check passes, and so do its tests."""
    project = tmp_path / "billing"

    output = init("billingAPI", str(project))

    assert output.startswith(f"Created billingAPI in {project}.\n")
    files = {
        path.relative_to(project).as_posix()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert files == PROJECT_FILES
    assert not [
        path
        for path in project.rglob("*")
        if path.is_file() and "{{" in path.read_text(encoding="utf-8")
    ]
    env_file = (project / ".env").read_text(encoding="utf-8")
    assert "BILLING_API_SETTINGS_MODULE=config.settings.local" in env_file
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "billingAPI"' in pyproject

    # From outside the project: manage.py finds its own pyproject.toml and .env.
    checked = run(tmp_path, str(project / "manage.py"), "check")
    assert checked.stdout == "System check identified no issues. Domains: messaging.\n"
    tested = run(project, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    assert tested.returncode == 0, tested.stdout + tested.stderr
    assert "1 passed" in tested.stdout


def test_production_settings_come_from_the_environment(tmp_path: Path) -> None:
    """Production starts only with its hosts and database URL set."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    production = {
        "BILLING_API_SETTINGS_MODULE": "config.settings.production",
        "BILLING_API_ALLOWED_HOSTS": "api.example.com",
    }

    missing = run(project, "manage.py", "check", env=production)
    database = {"BILLING_API_DATABASE_URL": "postgresql+asyncpg://app@db/billing"}
    ready = run(project, "manage.py", "check", env={**production, **database})

    assert missing.returncode == 1
    assert "Set the BILLING_API_DATABASE_URL environment variable." in missing.stderr
    assert ready.stdout == "System check identified no issues. Domains: messaging.\n"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("_billing", "not a valid project name"),
        ("billing-", "not a valid project name"),
        ("billing api", "not a valid project name"),
        ("2fa", "must start with a letter"),
    ],
)
def test_init_refuses_bad_names(name: str, message: str, tmp_path: Path) -> None:
    """A name must be a package name that can prefix environment variables."""
    with pytest.raises(CommandError, match=message):
        init(name, str(tmp_path / "project"))

    assert not (tmp_path / "project").exists()


def test_init_needs_an_empty_or_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is overwritten; an empty directory, or none, is fine."""
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("keep me", encoding="utf-8")
    a_file = tmp_path / "file"
    a_file.write_text("", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    for occupied in (busy, a_file):
        with pytest.raises(CommandError, match="already exists and is not empty"):
            init("billingAPI", str(occupied))
    init("billingAPI", str(empty))
    monkeypatch.chdir(tmp_path)
    output = init("ledgerAPI")

    assert (busy / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (empty / "manage.py").is_file()
    assert (tmp_path / "ledgerAPI" / "manage.py").is_file()
    assert "  cd ledgerAPI\n" in output


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.1", "domainkit[postgres,sqlite]>=0.1.1,<0.2"),
        ("1.4.0", "domainkit[postgres,sqlite]>=1.4.0,<2"),
        ("0.0.0+unknown", "domainkit[postgres,sqlite]"),
    ],
)
def test_projects_depend_on_the_running_release_series(
    version: str, expected: str
) -> None:
    """Up to the next release that may break them (a minor one before 1.0)."""
    assert requirement(version) == expected


def test_the_domainkit_program_runs_init(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The installed ``domainkit`` script is the command-line dispatcher."""
    (script,) = entry_points(group="console_scripts", name="domainkit")

    execute_from_command_line(["domainkit", "init", "billingAPI", str(tmp_path / "b")])
    execute_from_command_line(["domainkit"])

    assert script.load() is execute_from_command_line
    assert (tmp_path / "b" / "manage.py").is_file()
    assert "    init\n" in capsys.readouterr().out
