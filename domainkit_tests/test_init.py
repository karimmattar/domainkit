"""``init`` and the ``domainkit`` program: a new project that works at once."""

import io
import json
import os
import re
import runpy
import shlex
import sqlite3
import subprocess
import sys
import tomllib
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

import domainkit
from domainkit.management import (
    CommandError,
    call_command,
    execute_from_command_line,
)
from domainkit.management.commands.init import TEMPLATE_DIR, requirement
from domainkit.utils import formatting
from domainkit_tests.test_sec12c_2_image_bakes_env_file import ignored

CHECKED = "System check identified no issues. Domains: messaging, contenttypes, auth.\n"
REDIS_URL = "rediss://:s3cret@cache.internal:6380/0"
"""The shared cache of the production settings: TLS, and a password."""
PROJECT_FILES = {
    ".dockerignore",
    ".env",
    ".github/workflows/ci.yml",
    ".gitignore",
    "Dockerfile",
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
    """Run ``python *args`` in ``cwd`` as a user would: no test settings.

    Nor mutmut's: under its stats run the child would look for mutmut's
    settings in the project, and fail.
    """
    clean = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DOMAINKIT_", "BILLING_API_"))
        and name != "MUTANT_UNDER_TEST"
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
        '  pip install "domainkit[postgres,redis,sqlite,testing]"   # or: uv sync\n'
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


def test_generated_code_never_blocks_the_event_loop(tmp_path: Path) -> None:
    """Ruff's ASYNC rules are on, and generated routes need no thread pool."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    started = run(
        project, "manage.py", "startdomain", "invoices", "--aggregate", "Invoice"
    )
    assert started.returncode == 0, started.stderr
    smaller = run(project, "manage.py", "startdomain", "notes", "--minimal")
    assert smaller.returncode == 0, smaller.stderr

    linted = run(project, "-m", "ruff", "check", "--no-cache", ".")
    checked = run(project, "manage.py", "check")

    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["ruff"]["lint"]["extend-select"] == ["ASYNC"]
    assert linted.returncode == 0, linted.stdout + linted.stderr
    assert checked.stdout == (
        "System check identified no issues. "
        "Domains: messaging, contenttypes, auth, invoices, notes.\n"
    )


@pytest.mark.parametrize("name", ["billingAPI", "ledger.payments-and-settlements_API"])
def test_a_new_project_passes_its_own_ruff_as_written(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ruff where init runs, the project still passes its own ruff.

    init runs before the project's environment exists, so nothing formats
    what it writes: the templates are sorted and formatted as the project's
    configuration wants (``domainkit`` is a third-party package there).
    """
    monkeypatch.setattr(
        formatting,
        "find_spec",
        lambda module: None if module == "ruff" else find_spec(module),
    )
    project = tmp_path / "project"
    init(name, str(project))

    linted = run(project, "-m", "ruff", "check", "--no-cache", ".")
    formatted = run(project, "-m", "ruff", "format", "--check", "--no-cache", ".")

    assert linted.returncode == 0, linted.stdout + linted.stderr
    assert formatted.returncode == 0, formatted.stdout + formatted.stderr
    assert "already formatted" in formatted.stdout


def test_projects_get_the_ruff_the_templates_are_checked_with() -> None:
    """The project's dev group installs ruff, in domainkit's own tested range."""
    template = (TEMPLATE_DIR / "pyproject.toml-tpl").read_text(encoding="utf-8")
    framework = Path(__file__).parents[1] / "pyproject.toml"

    def ruff_in(requirements: list[str]) -> SpecifierSet:
        (found,) = [
            Requirement(each.replace(" (", "").replace(")", ""))
            for each in requirements
            if each.startswith("ruff")
        ]
        return found.specifier

    project_dev = tomllib.loads(template)["dependency-groups"]["dev"]
    own = tomllib.loads(framework.read_text(encoding="utf-8"))
    own_dev = own["dependency-groups"]["dev"]
    assert ruff_in(project_dev) == ruff_in(own_dev)
    assert "domainkit[testing]" in project_dev


def test_production_settings_come_from_the_environment(tmp_path: Path) -> None:
    """Production starts only with its hosts, database, Redis, and proxies set."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    production = {
        "BILLING_API_SETTINGS_MODULE": "config.settings.production",
        "BILLING_API_ALLOWED_HOSTS": "api.example.com",
        "BILLING_API_TRUSTED_PROXIES": "10.0.0.0/8, fd00::/8",
        "BILLING_API_SECRET_KEY": "s3cret-" * 8,
        "BILLING_API_REDIS_URL": REDIS_URL,
    }
    database = {"BILLING_API_DATABASE_URL": "postgresql+asyncpg://app@db/billing"}

    missing = run(project, "manage.py", "check", env=production)
    no_proxies = {**production, **database}
    del no_proxies["BILLING_API_TRUSTED_PROXIES"]
    without_proxies = run(project, "manage.py", "check", env=no_proxies)
    direct = {**no_proxies, "BILLING_API_TRUSTED_PROXIES": ""}
    directly = run(project, "manage.py", "check", env=direct)
    ready = run(project, "manage.py", "check", env={**production, **database})
    no_key = {**production, **database}
    del no_key["BILLING_API_SECRET_KEY"]
    without_key = run(project, "manage.py", "check", env=no_key)
    weak_key = {**no_key, "BILLING_API_SECRET_KEY": "domainkit-insecure-" * 3}
    with_weak_key = run(project, "manage.py", "check", env=weak_key)
    no_redis = {**production, **database}
    del no_redis["BILLING_API_REDIS_URL"]
    without_redis = run(project, "manage.py", "check", env=no_redis)
    quiet = run(
        project / "src",
        "-c",
        "from config.settings.production import LOGGING as L; "
        "print(L['loggers']['httpx2']['level'], L['loggers']['httpcore2']['level'])",
        env={**production, **database},
    )

    assert missing.returncode == 1
    assert "Set the BILLING_API_DATABASE_URL environment variable." in missing.stderr
    assert without_proxies.returncode == 1
    assert "Set the BILLING_API_TRUSTED_PROXIES" in without_proxies.stderr
    # Empty: callers connect directly, and check says what that means.
    assert directly.returncode == 0
    assert "security.W004" in warned(directly.stdout)
    # Docs are off, HSTS is on, and callers, passwords, and codes are counted
    # in Redis; only the pool, left to SQLAlchemy's default size and smaller
    # than the requests a process admits, is left to warn about.
    summary = ready.stdout.splitlines()[0]
    assert summary == CHECKED.replace("issues.", "issues and 2 warnings.").strip()
    assert warned(ready.stdout) == ["deploy.W004", "deploy.W006"]
    assert without_redis.returncode == 1
    assert "Set the BILLING_API_REDIS_URL environment variable." in (
        without_redis.stderr
    )
    assert without_key.returncode == 1
    assert "Set the BILLING_API_SECRET_KEY environment variable." in (
        without_key.stderr
    )
    assert with_weak_key.returncode == 1
    assert "SECRET_KEY holds a development key in production" in with_weak_key.stderr
    assert "domainkit-insecure-" not in with_weak_key.stderr
    # httpx2's own lines name whole URLs, query strings included.
    assert quiet.stdout.split() == ["WARNING", "WARNING"], quiet.stderr


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
        "BILLING_API_SECRET_KEY": "s3cret-" * 8,
        "BILLING_API_API_DOCS": "true",
        "BILLING_API_SECURE_HSTS_SECONDS": "0",
        "BILLING_API_REDIS_URL": REDIS_URL,
    }

    checked = run(project, "manage.py", "check", env=relaxed)

    assert checked.stdout.splitlines()[0] == (
        "System check identified no issues and 4 warnings. "
        "Domains: messaging, contenttypes, auth."
    )
    assert warned(checked.stdout) == [
        "security.W001",
        "security.W002",
        "deploy.W004",
        "deploy.W006",
    ]


def warned(output: str) -> list[str]:
    """The ids of the warnings ``check`` printed."""
    return [
        line.split()[1].rstrip(":")
        for line in output.splitlines()
        if line.startswith("  Warning: ")
    ]


def test_production_samples_access_lines_from_the_environment(
    tmp_path: Path,
) -> None:
    """Every access line unless the rate is set; then that share, checked."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    production = {
        "BILLING_API_SETTINGS_MODULE": "config.settings.production",
        "BILLING_API_ALLOWED_HOSTS": "api.example.com",
        "BILLING_API_DATABASE_URL": "postgresql+asyncpg://app@db/billing",
        "BILLING_API_TRUSTED_PROXIES": "10.0.0.0/8",
        "BILLING_API_SECRET_KEY": "s3cret-" * 8,
        "BILLING_API_REDIS_URL": REDIS_URL,
    }
    show = "from domainkit.conf import settings; print(settings.ACCESS_LOG_SAMPLE_RATE)"

    unset = run(project / "src", "-c", show, env=production)
    sampled = run(
        project / "src",
        "-c",
        show,
        env={**production, "BILLING_API_ACCESS_LOG_SAMPLE_RATE": "0.25"},
    )
    wrong = run(
        project,
        "manage.py",
        "check",
        env={**production, "BILLING_API_ACCESS_LOG_SAMPLE_RATE": "2"},
    )

    assert unset.stdout.strip() == "1.0", unset.stderr
    assert sampled.stdout.strip() == "0.25", sampled.stderr
    assert wrong.returncode == 1
    assert "ACCESS_LOG_SAMPLE_RATE" in wrong.stderr


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (
            "_billing",
            (
                r"^'_billing' is not a valid project name: use letters, digits, "
                r"'\.', '-', and '_', starting and ending with a letter or digit\.$"
            ),
        ),
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
        ("0.1.1", "domainkit[postgres,redis,sqlite]>=0.1.1,<0.2"),
        ("1.4.0", "domainkit[postgres,redis,sqlite]>=1.4.0,<2"),
        ("0.0.0+unknown", "domainkit[postgres,redis,sqlite]"),
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


def test_python_dash_m_domainkit_is_the_domainkit_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m domainkit`` runs the same dispatcher, under its own name."""
    import domainkit.__main__ as main_module

    def run_module(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", [main_module.__file__, *args])
        runpy.run_path(main_module.__file__, run_name="__main__")

    monkeypatch.chdir(tmp_path)
    run_module("init", "billingAPI", "b")
    run_module()
    with pytest.raises(SystemExit) as exited:
        run_module("migrat")

    assert (tmp_path / "b" / "manage.py").is_file()
    out, err = capsys.readouterr()
    assert "Usage: python -m domainkit <command> [options]\n" in out
    assert err == "Error: Unknown command 'migrat'. Did you mean 'migrate'?\n"
    assert exited.value.code == 1


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


ROOT = Path(__file__).parents[1]


def test_the_image_runs_as_a_user_who_cannot_change_the_code(tmp_path: Path) -> None:
    """Dependencies in a build stage; code owned by root; uvicorn on port 8000.

    CI's build job builds the image of a new project with the wheel, and
    runs it: id, a write refused, check, and /health (INFRA-09).
    """
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    dockerfile = (project / "Dockerfile").read_text(encoding="utf-8")
    lines = [line for line in dockerfile.splitlines() if not line.startswith("#")]
    final = lines[max(i for i, line in enumerate(lines) if line.startswith("FROM "))]

    assert "{{" not in dockerfile
    assert final == "FROM python:3.13-slim"
    stage = lines[lines.index(final) :]
    user = stage.index("USER 10001")
    assert [line for line in stage if line.startswith("COPY ")] == [
        "COPY --from=build /opt/venv /opt/venv",
        "COPY . .",
    ]
    assert all(stage.index(line) < user for line in stage if line.startswith("COPY "))
    assert "    BILLING_API_SETTINGS_MODULE=config.settings.production" in stage
    cmd = json.loads(
        " ".join(dockerfile.split("CMD ", 1)[1].split("\\\n")).replace("\n", " ")
    )
    assert cmd[:4] == ["uvicorn", "config.asgi:application", "--app-dir", "src"]
    assert cmd[4:8] == ["--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
    patterns = (project / ".dockerignore").read_text(encoding="utf-8").splitlines()
    copied = [
        "manage.py",
        "pyproject.toml",
        "src/config/settings/production.py",
        "migrations/0001_ab12_create.py",
    ]
    left_out = [
        ".env",
        ".git/config",
        ".venv/bin/python",
        "tests/test_health.py",
        "db.sqlite3",
        "production.env",
        "secrets/jwt.key",
        "src/config/.env.production",
        "src/config/settings/__pycache__/base.cpython-313.pyc",
    ]
    assert [path for path in copied if ignored(patterns, path)] == []
    assert [path for path in left_out if not ignored(patterns, path)] == []
    gitignored = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", "*.env", "secrets/"} <= set(gitignored)


def test_the_projects_workflow_passes_on_a_new_project(tmp_path: Path) -> None:
    """Every step of its test job but the install, as CI runs them, without .env."""
    project = tmp_path / "billing"
    init("billingAPI", str(project))
    assert (
        run(project, "manage.py", "startdomain", "invoices", "--minimal").returncode
        == 0
    )
    made = run(project, "manage.py", "makemigrations", "-m", "first")
    assert made.returncode == 0, made.stderr
    (project / ".env").unlink()  # Not committed: CI never has it.
    workflow = yaml.safe_load(
        (project / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["test"]
    programs = {
        "python": [],
        "ruff": ["-m", "ruff"],
        "pytest": ["-m", "pytest", "-p", "no:cacheprovider"],
    }

    ran = []
    for step in job["steps"]:
        if "run" not in step or step["name"] == "Install":
            continue
        for line in step["run"].splitlines():
            program, *args = shlex.split(line)
            done = run(project, *programs[program], *args, env=job["env"])
            assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
            ran.append(line)

    assert workflow["permissions"] == {"contents": "read"}
    assert ran == [
        "ruff check .",
        "ruff format --check .",
        "python manage.py check",
        "python manage.py migrate",
        'python manage.py makemigrations --check -m "unused"',
        "pytest",
    ]
    install = next(step for step in job["steps"] if step.get("name") == "Install")
    listing = re.search(r'(python -c ".*") > requirements.txt', install["run"])
    assert listing is not None
    listed = run(project, *shlex.split(listing[1])[1:])
    assert listed.stdout.splitlines() == [requirement(domainkit.__version__)]


def test_the_projects_workflow_pins_the_actions_domainkits_ci_does(
    tmp_path: Path,
) -> None:
    """Commit SHAs, as Renovate keeps domainkit's own: one update moves both."""
    init("billingAPI", str(tmp_path / "billing"))
    ours = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    theirs = (tmp_path / "billing" / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    uses = re.findall(r"uses: (\S+@\S+ # \S+)", theirs)

    assert len(uses) == 3
    assert all(re.fullmatch(r"[\w/-]+@[0-9a-f]{40} # v[\d.]+", use) for use in uses)
    assert all(f"uses: {use}" in ours for use in uses)
    assert theirs.count("persist-credentials: false") == 2
