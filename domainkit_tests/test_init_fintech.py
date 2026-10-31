"""``init --profile fintech``: a bank's controls, on and wired, in a new project.

The project it writes passes its own tests, ruff, and mypy --strict as
written, its CI workflow's steps, and ``check --deploy`` with the secrets
its production settings list; the controls themselves are proved by the
tests docs/fintech.md names (FIN-74, ADR 0124).
"""

import io
import os
import re
import shlex
import subprocess
import sys
import tomllib
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.exc import DBAPIError

import domainkit
from domainkit.management import call_command
from domainkit.management.commands.init import (
    EXTRAS,
    PROFILES,
    TEMPLATE_DIR,
    namespace_for,
    requirement,
)
from domainkit_tests.conftest import MakeDatabase, on_server
from domainkit_tests.test_init import PROJECT_FILES, ROOT, warned

STEP_TIMEOUT = 120
"""Seconds one command may run: one that runs two minutes hangs."""
FINTECH_FILES = {
    "src/config/rules.py",
    "src/domains/controls/__init__.py",
    "src/domains/controls/apps.py",
    "src/domains/payments/__init__.py",
    "src/domains/payments/api/__init__.py",
    "src/domains/payments/api/routes.py",
    "src/domains/payments/application/__init__.py",
    "src/domains/payments/application/commands.py",
    "src/domains/payments/application/services.py",
    "src/domains/payments/application/unit_of_work.py",
    "src/domains/payments/apps.py",
    "src/domains/payments/domain/__init__.py",
    "src/domains/payments/domain/model.py",
    "src/domains/payments/infrastructure/__init__.py",
    "src/domains/payments/infrastructure/orm.py",
    "src/domains/payments/infrastructure/unit_of_work.py",
    "tests/conftest.py",
    "tests/people.py",
    "tests/payments/__init__.py",
    "tests/payments/test_cards.py",
    "tests/payments/test_transfers.py",
    "tests/test_controls.py",
}
DOMAINS = (
    "messaging, contenttypes, auth, audit, evidence, idempotency, keys, privacy, "
    "accounting, approvals, limits, controls, payments"
)
SECRETS = {
    "BANK_API_SETTINGS_MODULE": "config.settings.production",
    "BANK_API_ALLOWED_HOSTS": "api.example.com",
    "BANK_API_TRUSTED_PROXIES": "10.0.0.0/8",
    "BANK_API_DATABASE_URL": "postgresql+asyncpg://app@db/bank",
    "BANK_API_REDIS_URL": "rediss://:s3cret@cache.internal:6380/0",
    "BANK_API_SECRET_KEY": "s3cret-" * 8,
    "BANK_API_EVIDENCE_SUBJECT_KEY": "subject-" * 5,
    "BANK_API_KEYS_DATA_KEY_ID": "2026-10",
    "BANK_API_LEDGER_URL": "immudb://writer:pw@immudb:3322/bank",
}
"""What production.py says to set, but the key service and the ledger's files."""
KEY_SERVICES = {
    "aws-kms": {
        "BANK_API_KEYS_PROVIDER": "aws-kms",
        "BANK_API_KEYS_AWS_KMS_KEY_ID": "alias/bank",
        "BANK_API_KEYS_AWS_REGION": "eu-west-1",
        "BANK_API_KEYS_DATA_KEY": "aws-kms:AQIDAHhQ",
    },
    "vault": {
        "BANK_API_KEYS_PROVIDER": "vault",
        "BANK_API_KEYS_VAULT_URL": "https://vault.internal:8200",
        "BANK_API_KEYS_VAULT_KEY": "bank",
        "BANK_API_KEYS_VAULT_TOKEN": "hvs.s3cret",
        "BANK_API_KEYS_DATA_KEY": "vault:v1:AQIDAHhQ",
    },
}


def init(*args: str) -> str:
    """Run ``init`` and return what it printed."""
    output = io.StringIO()
    call_command("init", *args, stdout=output)
    return output.getvalue()


class Project:
    """A new fintech project's own interpreter: its commands, in its directory."""

    def __init__(self, root: Path) -> None:
        """Run commands in ``root``, with this domainkit and none of our settings.

        Nor mutmut's: the project runs its own code, not mutants.
        """
        self.root = root
        self.env = {
            **{
                name: value
                for name, value in os.environ.items()
                if not name.startswith(("DOMAINKIT_", "BANK_API_"))
                and name != "MUTANT_UNDER_TEST"
            },
            "PYTHONPATH": str(Path(domainkit.__file__).parents[1]),
        }

    def start(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        """Start ``python args`` in the project."""
        return subprocess.Popen(  # noqa: S603 - the test interpreter and generated files.
            [sys.executable, *args],
            cwd=self.root,
            env={**self.env, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run ``python args`` in the project, to its end."""
        return self.ended(self.start(*args, env=env))

    @staticmethod
    def ended(process: subprocess.Popen[str]) -> subprocess.CompletedProcess[str]:
        """What ``process`` printed and returned; fail if it ran past its time."""
        try:
            out, err = process.communicate(timeout=STEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            pytest.fail(f"{process.args!r} ran past {STEP_TIMEOUT} s:\n{out}{err}")
        return subprocess.CompletedProcess(process.args, process.returncode, out, err)


@pytest.fixture
def bank(tmp_path: Path) -> Project:
    """A fintech project, with its first migration, as its next steps say."""
    init("bankAPI", str(tmp_path / "bank"), "--profile", "fintech")
    project = Project(tmp_path / "bank")
    made = project.run("manage.py", "makemigrations", "-m", "fintech")
    assert made.returncode == 0, made.stderr
    return project


def test_fintech_writes_the_controls_over_the_project(tmp_path: Path) -> None:
    """The project's files, the profile's added, none left with a placeholder."""
    project = tmp_path / "bank"

    output = init("bankAPI", str(project), "--profile", "fintech")

    files = {
        path.relative_to(project).as_posix()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert files == PROJECT_FILES | FINTECH_FILES
    assert not [
        path
        for path in project.rglob("*")
        if path.is_file() and "{{" in path.read_text(encoding="utf-8")
    ]
    assert output.splitlines()[3:] == [
        (
            '  pip install "domainkit[audit,httpclient,immudb,mfa,postgres,redis,'
            'sqlite,testing]"   # or: uv sync'
        ),
        "  python manage.py check",
        '  python manage.py makemigrations -m "fintech"',
        "  python manage.py migrate",
        "  python manage.py createsuperuser",
        "  # Deploying: the controls and their settings, docs/fintech.md",
        "  pytest",
    ]
    pyproject = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == [
        requirement(domainkit.__version__, EXTRAS["fintech"])
    ]
    assert pyproject["tool"]["mypy"]["strict"] is True
    production = (project / "src/config/settings/production.py").read_text(
        encoding="utf-8"
    )
    assert '"NAMESPACE": "bankapi"' in production
    assert ".env" in {path.name for path in project.iterdir()}


@pytest.mark.parametrize(
    ("name", "namespace"),
    [
        ("bankAPI", "bankapi"),
        ("ledger.payments-and-settlements_API", "ledger-payments-and-settlements_"),
    ],
)
def test_a_projects_namespace_on_the_ledger_follows_its_name(
    name: str, namespace: str
) -> None:
    """Lower-case, dots made dashes, cut to the 32 characters a namespace has."""
    assert namespace_for(name) == namespace
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", namespace)


def test_the_profiles_extras_are_extras_of_domainkit() -> None:
    """Each extra a profile depends on is one domainkit's pyproject.toml offers."""
    own = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    offered = set(own["project"]["optional-dependencies"])

    assert set(EXTRAS["fintech"]) <= offered
    assert set(EXTRAS[None]) < set(EXTRAS["fintech"])
    assert requirement("0.15.0", EXTRAS["fintech"]) == (
        "domainkit[audit,httpclient,immudb,mfa,postgres,redis,sqlite]>=0.15.0,<0.16"
    )
    assert PROFILES["fintech"].parent.parent == TEMPLATE_DIR.parent


@pytest.mark.skipif(
    find_spec("mypy") is None or find_spec("ruff") is None,
    reason="needs mypy and ruff",
)
@pytest.mark.timeout(func_only=True)
def test_a_fintech_project_passes_its_ci_workflow(bank: Project) -> None:
    """Every step of its test job but the install: lint, types, check, tests.

    The steps that read the database run one after another, as CI runs
    them; ruff, mypy, and the tests (on a database of their own) depend on
    nothing else and run beside them, as test_generators runs its checks.
    """
    workflow = yaml.safe_load(
        (bank.root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["test"]
    (bank.root / ".env").unlink()  # Not committed: CI never has it.
    programs = {
        "python": [],
        "ruff": ["-m", "ruff"],
        "mypy": ["-m", "mypy", "--no-incremental"],
        "pytest": ["-m", "pytest", "-q", "-p", "no:cacheprovider"],
    }
    lines = [
        line
        for step in job["steps"]
        if "run" in step and step["name"] != "Install"
        for line in step["run"].splitlines()
    ]
    sequential = [line for line in lines if line.startswith("python ")]
    alongside = [line for line in lines if not line.startswith("python ")]

    started = {
        line: bank.start(*programs[program], *args, env=job["env"])
        for line in alongside
        for program, *args in [shlex.split(line)]
    }
    for line in sequential:
        program, *args = shlex.split(line)
        done = bank.run(*programs[program], *args, env=job["env"])
        assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
    ended = {line: bank.ended(process) for line, process in started.items()}

    for line, done in ended.items():
        assert done.returncode == 0, f"{line}\n{done.stdout}{done.stderr}"
    assert lines == [
        "ruff check .",
        "ruff format --check .",
        "mypy",
        "python manage.py check",
        "python manage.py migrate",
        'python manage.py makemigrations --check -m "unused"',
        "pytest",
    ]
    assert "16 passed" in ended["pytest"].stdout
    assert "--strict" not in ended["mypy"].args  # pyproject.toml's strict = true.


@pytest.fixture
def ledger_files(tmp_path: Path) -> dict[str, str]:
    """The immudb CA and the server's signing key, as files production reads."""
    ca, signing = tmp_path / "immudb-ca.pem", tmp_path / "immudb-signing.pub"
    ca.write_text("ca", encoding="utf-8")
    signing.write_text("key", encoding="utf-8")
    return {
        "BANK_API_LEDGER_TLS_CA": str(ca),
        "BANK_API_LEDGER_SIGNING_KEY": str(signing),
    }


@pytest.mark.parametrize("service", sorted(KEY_SERVICES))
def test_production_passes_check_deploy_once_its_secrets_are_set(
    service: str, tmp_path: Path, ledger_files: dict[str, str]
) -> None:
    """No error and no warning: every control is on, as a deployment asks."""
    init("bankAPI", str(tmp_path / "bank"), "--profile", "fintech")
    project = Project(tmp_path / "bank")

    checked = project.run(
        "manage.py",
        "check",
        "--deploy",
        env={**SECRETS, **ledger_files, **KEY_SERVICES[service]},
    )

    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.splitlines()[0] == (
        f"System check identified no issues. Domains: {DOMAINS}."
    )
    assert warned(checked.stdout) == []


@pytest.mark.parametrize(
    ("left_out", "refusal"),
    [
        ("BANK_API_KEYS_PROVIDER", "Set the BANK_API_KEYS_PROVIDER environment"),
        ("BANK_API_KEYS_DATA_KEY", "Set the BANK_API_KEYS_DATA_KEY environment"),
        ("BANK_API_KEYS_AWS_REGION", "Set the BANK_API_KEYS_AWS_REGION environment"),
        ("BANK_API_EVIDENCE_SUBJECT_KEY", "Set the BANK_API_EVIDENCE_SUBJECT_KEY"),
        ("BANK_API_LEDGER_URL", "Set the BANK_API_LEDGER_URL environment"),
        ("BANK_API_LEDGER_TLS_CA", "Set the BANK_API_LEDGER_TLS_CA environment"),
    ],
)
def test_production_starts_only_with_each_secret(
    left_out: str, refusal: str, tmp_path: Path, ledger_files: dict[str, str]
) -> None:
    """Each documented secret left out stops it, naming the variable."""
    init("bankAPI", str(tmp_path / "bank"), "--profile", "fintech")
    secrets = {**SECRETS, **ledger_files, **KEY_SERVICES["aws-kms"]}
    del secrets[left_out]

    checked = Project(tmp_path / "bank").run("manage.py", "check", env=secrets)

    assert checked.returncode == 1
    assert refusal in checked.stderr


def test_production_refuses_the_development_key_service(
    tmp_path: Path, ledger_files: dict[str, str]
) -> None:
    """The KEK derived from SECRET_KEY is for development: keys.E001 where deployed."""
    init("bankAPI", str(tmp_path / "bank"), "--profile", "fintech")
    local = {
        "BANK_API_KEYS_PROVIDER": "local",
        "BANK_API_KEYS_DATA_KEY": "local:AQIDAHhQ",
    }

    checked = Project(tmp_path / "bank").run(
        "manage.py", "check", "--deploy", env={**SECRETS, **ledger_files, **local}
    )

    assert checked.returncode == 1
    assert "keys.E001" in checked.stderr


ROW = (
    "INSERT INTO domainkit_audit_log (id, occurred_at, actor, action, outcome, "
    "details) VALUES (gen_random_uuid(), now(), 'system', 'test.row', 'success', "
    "'{}')"
)
"""A row of the trail, written as the application writes them."""


@pytest.mark.skipif(
    not os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"),
    reason="set DOMAINKIT_TEST_POSTGRES_URL to a PostgreSQL server",
)
@pytest.mark.timeout(func_only=True)
def test_migrate_makes_the_trail_append_only_on_postgresql(
    tmp_path: Path, make_database: MakeDatabase
) -> None:
    """``domains.controls`` protects the trail after each migrate, afresh.

    ``audit.E001`` to ``E005`` refuse to start where deployed without the
    triggers; the project makes them without editing its revisions.
    """
    init("bankAPI", str(tmp_path / "bank"), "--profile", "fintech")
    bank = Project(tmp_path / "bank")
    url = make_database("bank")
    on_postgres = {
        "BANK_API_SETTINGS_MODULE": "config.settings.local",
        "BANK_API_DATABASE_URL": url,
    }

    made = bank.run("manage.py", "makemigrations", "-m", "fintech", env=on_postgres)
    first = bank.run("manage.py", "migrate", env=on_postgres)
    again = bank.run("manage.py", "migrate", env=on_postgres)

    assert made.returncode == 0, made.stderr
    assert first.returncode == 0, first.stderr
    assert again.returncode == 0, again.stderr
    on_server(url, ROW)
    for statement in (
        "UPDATE domainkit_audit_log SET actor = 'someone else'",
        "DELETE FROM domainkit_audit_log",
        "TRUNCATE domainkit_audit_log",
        "TRUNCATE domainkit_audit_seals",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            on_server(url, statement)


def test_the_fintech_workflow_is_the_projects_with_its_types_checked(
    tmp_path: Path,
) -> None:
    """The same jobs and pinned actions as every project's, and mypy."""
    init("billingAPI", str(tmp_path / "plain"))
    init("billingAPI", str(tmp_path / "bank"), "--profile", "fintech")
    workflow = Path(".github/workflows/ci.yml")
    plain = (tmp_path / "plain" / workflow).read_text(encoding="utf-8")
    fintech = (tmp_path / "bank" / workflow).read_text(encoding="utf-8")
    ours = (ROOT / workflow).read_text(encoding="utf-8")

    expected: dict[str, Any] = yaml.safe_load(plain)["jobs"]
    lint = next(
        step for step in expected["test"]["steps"] if step.get("name") == "Lint"
    )
    lint["run"] += "mypy\n"
    assert yaml.safe_load(fintech)["jobs"] == expected
    uses = re.findall(r"uses: (\S+@\S+ # \S+)", fintech)
    assert len(uses) == 3
    assert all(f"uses: {use}" in ours for use in uses)
