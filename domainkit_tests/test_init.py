"""``init`` and the ``domainkit`` program: a new project that works at once."""

import io
import os
import sqlite3
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

CHECKED = "System check identified no issues. Domains: messaging, contenttypes, auth.\n"
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

    assert output == (
        f"Created billingAPI in {project}.\n"
        "Next steps:\n"
        f"  cd {project.relative_to(Path.cwd(), walk_up=True)}\n"
        '  pip install "domainkit[postgres,sqlite,testing]"   # or: uv sync\n'
        "  python manage.py check\n"
        '  python manage.py makemigrations -m "auth"\n'
        "  python manage.py migrate\n"
        "  python manage.py createsuperuser\n"
        "  python manage.py startdomain <name>\n"
        "  pytest\n"
    )
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
    assert checked.stdout == CHECKED
    tested = run(project, "-m", "pytest", "-q", "-p", "no:cacheprovider")
    assert tested.returncode == 0, tested.stdout + tested.stderr
    assert "3 passed" in tested.stdout


def test_production_settings_come_from_the_environment(tmp_path: Path) -> None:
    """Production starts only with its hosts, database URL, and proxies set."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    production = {
        "BILLING_API_SETTINGS_MODULE": "config.settings.production",
        "BILLING_API_ALLOWED_HOSTS": "api.example.com",
        "BILLING_API_TRUSTED_PROXIES": "10.0.0.0/8, fd00::/8",
    }
    database = {"BILLING_API_DATABASE_URL": "postgresql+asyncpg://app@db/billing"}

    missing = run(project, "manage.py", "check", env=production)
    no_proxies = {**production, **database}
    del no_proxies["BILLING_API_TRUSTED_PROXIES"]
    without_proxies = run(project, "manage.py", "check", env=no_proxies)
    direct = {**no_proxies, "BILLING_API_TRUSTED_PROXIES": ""}
    directly = run(project, "manage.py", "check", env=direct)
    ready = run(project, "manage.py", "check", env={**production, **database})

    assert missing.returncode == 1
    assert "Set the BILLING_API_DATABASE_URL environment variable." in missing.stderr
    assert without_proxies.returncode == 1
    assert "Set the BILLING_API_TRUSTED_PROXIES" in without_proxies.stderr
    # Empty: callers connect directly, and check says what that means.
    assert directly.returncode == 0
    assert directly.stdout.splitlines()[1].split()[1] == "TRUSTED_PROXIES"
    # Docs are off, HSTS is on, and callers are rate limited: nothing to warn about.
    assert ready.stdout == CHECKED


def test_production_hardening_can_be_relaxed_from_the_environment(
    tmp_path: Path,
) -> None:
    """Docs and HSTS are switched by variables, and check warns when they are."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    relaxed = {
        "BILLING_API_SETTINGS_MODULE": "config.settings.production",
        "BILLING_API_ALLOWED_HOSTS": "api.example.com",
        "BILLING_API_DATABASE_URL": "postgresql+asyncpg://app@db/billing",
        "BILLING_API_TRUSTED_PROXIES": "10.0.0.0/8",
        "BILLING_API_API_DOCS": "true",
        "BILLING_API_SECURE_HSTS_SECONDS": "0",
    }

    checked = run(project, "manage.py", "check", env=relaxed)

    summary, *warnings = checked.stdout.splitlines()
    assert summary == (
        "System check identified no issues and 2 warnings. "
        "Domains: messaging, contenttypes, auth."
    )
    assert [warning.split()[1] for warning in warnings] == [
        "API_DOCS",
        "SECURE_HSTS_SECONDS",
    ]


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


ROUTED_BASE = '''"""Settings shared by every environment."""

from pathlib import Path

from domainkit.conf.env import Env

env = Env()
BASE_DIR = Path(__file__).resolve().parents[3]

INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
    "domainkit.contrib.contenttypes",
    "domainkit.contrib.auth",
    "domains.invoices",
]
ROOT_ROUTER = "config.urls.router"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
]
DATABASE_ROUTING = {"invoices": "billing"}
'''
ROUTED_LOCAL = """from config.settings.base import *
from config.settings.base import BASE_DIR

DATABASES = {
    "default": {"URL": f"sqlite+aiosqlite:///{BASE_DIR / 'db.sqlite3'}"},
    "billing": {"URL": f"sqlite+aiosqlite:///{BASE_DIR / 'billing.sqlite3'}"},
}
"""
ROUTED_TEST = """from config.settings.base import *

ENVIRONMENT = "test"
ALLOWED_HOSTS = ["testserver"]
DATABASES = {
    "default": {"URL": "sqlite+aiosqlite:///:memory:"},
    "billing": {"URL": "sqlite+aiosqlite:///:memory:"},
}
AUTH = {"PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"]}
"""
ROUTES = """
from domains.invoices.api.routes import router as invoices_router

router.include_router(invoices_router, prefix="/api/v1/invoices")
"""


def test_a_generated_domain_can_live_in_its_own_database(tmp_path: Path) -> None:
    """Route a generated domain: its table and its sessions both move there."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    started = run(
        project,
        "manage.py",
        "startdomain",
        "invoices",
        "--aggregate",
        "Invoice",
        "--manual",  # The test installs and mounts it, routed, below.
    )
    assert started.returncode == 0, started.stderr
    settings_dir = project / "src" / "config" / "settings"
    (settings_dir / "base.py").write_text(ROUTED_BASE, encoding="utf-8")
    (settings_dir / "local.py").write_text(ROUTED_LOCAL, encoding="utf-8")
    (settings_dir / "test.py").write_text(ROUTED_TEST, encoding="utf-8")
    with (project / "src" / "config" / "urls.py").open("a", encoding="utf-8") as urls:
        urls.write(ROUTES)

    created = run(project, "manage.py", "makemigrations", "-m", "create invoices")
    migrated = run(project, "manage.py", "migrate")
    tested = run(project, "-m", "pytest", "-q", "-p", "no:cacheprovider")

    assert created.returncode == 0, created.stderr
    assert (project / "migrations" / "billing").is_dir()
    assert migrated.stdout == (
        "Database 'default' is at head.\nDatabase 'billing' is at head.\n"
    )
    billing = sqlite3.connect(project / "billing.sqlite3")
    default = sqlite3.connect(project / "db.sqlite3")
    try:
        billing_tables = {row[0] for row in billing.execute(TABLES)}
        default_tables = {row[0] for row in default.execute(TABLES)}
    finally:
        billing.close()
        default.close()
    assert "invoices_invoices" in billing_tables
    assert "invoices_invoices" not in default_tables
    assert tested.returncode == 0, tested.stdout + tested.stderr


TABLES = "SELECT name FROM sqlite_master WHERE type = 'table'"
