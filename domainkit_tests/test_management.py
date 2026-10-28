"""Management commands: discovery, dispatch, and the built-in commands."""

import io
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from domainkit import setup
from domainkit.apps import registry
from domainkit.apps.hooks import on_deployment_warnings
from domainkit.architecture import check_domain
from domainkit.conf import UNSET, override_settings, use_settings
from domainkit.conf.env import project_prefix
from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE, RATE_LIMIT_MIDDLEWARE
from domainkit.db import migrations as migrations_module
from domainkit.db.connections import pool_sizing_warnings
from domainkit.management import (
    BaseCommand,
    CommandError,
    call_command,
    execute_from_command_line,
    get_commands,
)
from domainkit.management.commands import runserver, startdomain
from domainkit.utils import formatting
from domainkit_tests.conftest import MakePackage
from domainkit_tests.test_db import test_metadata

HTTPS_DIRECT = (
    "  Info: security.I001: HTTPS is not checked: the application serves plain "
    "HTTP, and TLS must end in front of it. It takes a request's scheme from the "
    "connection alone (TRUSTED_PROXIES is empty), so every request counts as "
    "plain HTTP, even one a proxy received over HTTPS.\n"
    "    Hint: Redirect plain HTTP to HTTPS at the proxy, and keep "
    "SECURE_HSTS_SECONDS set (docs/security.md, 'The edge').\n"
)
"""What ``check`` says of HTTPS where deployed with no trusted proxy."""
HTTPS_PROXIED = HTTPS_DIRECT.replace(
    "from the connection alone (TRUSTED_PROXIES is empty), so every request "
    "counts as plain HTTP, even one a proxy received over HTTPS.",
    "from the X-Forwarded-Proto header of TRUSTED_PROXIES only, so the proxy "
    "must set it, over a caller's.",
)
"""And behind trusted proxies, which say the scheme."""
BUILTINS = {
    "addcommand",
    "addevent",
    "addquery",
    "broker",
    "cache",
    "check",
    "compilemessages",
    "dbshell",
    "diffsettings",
    "dumpdata",
    "flush",
    "init",
    "loaddata",
    "makemigrations",
    "migrate",
    "openapi",
    "partitions",
    "runserver",
    "seed",
    "shell",
    "showmigrations",
    "squashmigrations",
    "startaggregate",
    "startdomain",
}


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


def test_commands_come_from_framework_and_domains() -> None:
    """Built-ins are always listed; installed domains add theirs."""
    commands = get_commands()

    assert set(commands) >= BUILTINS
    assert (
        commands["greet"] == "domainkit_tests.sample_domains.alpha.management.commands"
    )
    assert "_private" not in commands
    assert "helpers" not in commands


def test_domain_commands_need_loadable_settings(
    monkeypatch: pytest.MonkeyPatch, make_package: MakePackage
) -> None:
    """Without settings only built-ins exist; domains without commands are fine."""
    make_package("plain_domain", {"management/__init__.py": ""})
    with override_settings(INSTALLED_DOMAINS=["plain_domain", "missing_domain_pkg"]):
        assert set(get_commands()) == BUILTINS

    monkeypatch.setenv(f"{project_prefix()}SETTINGS_MODULE", "no.such.settings")
    with use_settings("domainkit_tests.settings"):
        from domainkit.conf import reset_settings

        reset_settings()
        assert set(get_commands()) == BUILTINS


def test_async_domain_command_reads_domain_settings() -> None:
    """Async ``handle`` methods run in their own event loop."""
    assert run("greet", "Ada") == "hi, Ada\n"
    assert run("greet", "Ada", shout=True) == "HI, ADA\n"
    with pytest.raises(TypeError, match="Unknown options for 'greet': loud"):
        run("greet", "Ada", loud=True)


def test_unknown_commands_suggest_close_names() -> None:
    """Typos get a suggestion; nonsense does not."""
    with pytest.raises(CommandError, match="Did you mean 'check'"):
        run("chek")
    with pytest.raises(CommandError, match=r"Unknown command 'zzz'\.$"):
        run("zzz")


def test_command_line_dispatch(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Help lists commands; errors print a message and exit non-zero."""
    for argv in (["manage.py"], ["manage.py", "help"], ["manage.py", "--help"]):
        execute_from_command_line(argv)
        assert "[domainkit_tests.sample_domains.alpha]" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["manage.py", "greet", "Grace"])
    execute_from_command_line()
    assert capsys.readouterr().out == "hi, Grace\n"

    with pytest.raises(SystemExit) as exit_info:
        execute_from_command_line(["manage.py", "nope"])
    assert exit_info.value.code == 1
    assert "Error: Unknown command 'nope'" in capsys.readouterr().err

    registry.clear()
    with override_settings(ALPHA={"GREETING": 5}), pytest.raises(SystemExit):
        execute_from_command_line(["manage.py", "check"])
    assert "Configuration error: Invalid ALPHA settings" in capsys.readouterr().err


def test_base_command_contract() -> None:
    """``handle`` must be implemented; ``CommandError`` carries an exit code."""
    with pytest.raises(NotImplementedError):
        BaseCommand().handle()
    assert CommandError("x", returncode=3).returncode == 3


def test_check_reports_success_and_violations(make_package: MakePackage) -> None:
    """``check`` builds the app and enforces the layer rules."""
    assert run("check") == "System check identified no issues. Domains: alpha.\n"

    make_package("bad_layers", {"domain/model.py": "import sqlalchemy\n"})
    with (
        override_settings(
            INSTALLED_DOMAINS=["bad_layers"], ALPHA=UNSET, ROOT_ROUTER=None
        ),
        pytest.raises(
            CommandError, match=r"bad_layers\.domain\.model imports sqlalchemy"
        ),
    ):
        run("check")


BOUNDED_CACHES = {"default": {"COMPUTE_TIMEOUT": 30}}
"""Caches ``check`` has nothing to say about where deployed."""


def test_check_lists_weak_deployment_choices() -> None:
    """In staging and production, weak choices are warnings, not failures."""
    production = {
        "ENVIRONMENT": "production",
        "SECURE_HSTS_SECONDS": 0,
        "CACHES": BOUNDED_CACHES,
        "SILENCED_CHECKS": ["deploy.W005"],  # The tests' SQLite database.
    }
    with override_settings(**production):
        assert run("check") == (
            "System check identified no issues and 3 warnings (1 silenced). "
            "Domains: alpha.\n"
            + HTTPS_DIRECT
            + "  Warning: security.W001: API_DOCS publishes /docs, /redoc, and "
            "/openapi.json to anyone; set it to False unless the API is public.\n"
            "  Warning: security.W002: SECURE_HSTS_SECONDS is 0, so browsers may "
            "still use plain HTTP; set it (31536000 is a year) once HTTPS works "
            "everywhere.\n"
            "  Warning: security.W003: Requests are not rate limited; set "
            "RATE_LIMIT and list domainkit.http.ratelimit.RateLimitMiddleware in "
            "MIDDLEWARE.\n"
        )
    with_hsts = {**production, "SECURE_HSTS_SECONDS": 1}
    with override_settings(**with_hsts):
        assert run("check").startswith(
            "System check identified no issues and 2 warnings (1 silenced). "
            "Domains: alpha.\n"
        )
    hardened = {
        **with_hsts,
        "RATE_LIMIT": "10/second",
        "MIDDLEWARE": [*DEFAULT_MIDDLEWARE, RATE_LIMIT_MIDDLEWARE],
        "TRUSTED_PROXIES": ["10.0.0.0/8"],
    }
    with override_settings(**hardened):
        assert run("check") == (
            "System check identified no issues and 1 warning (1 silenced). "
            "Domains: alpha.\n"
            + HTTPS_PROXIED
            + "  Warning: security.W001: API_DOCS publishes /docs, /redoc, and "
            "/openapi.json to anyone; set it to False unless the API is public.\n"
        )
    with override_settings(**{**hardened, "CACHES": {"default": {}}}):
        assert run("check").endswith(
            "  Warning: caches.W001: CACHES['default'] has no COMPUTE_TIMEOUT, so "
            "one get_or_set "
            "computation that hangs holds its key until the process restarts; set "
            "CACHES['default']['COMPUTE_TIMEOUT'] (REQUEST_TIMEOUT's value, for "
            "example).\n"
        )


NETWORK = "postgresql+asyncpg://app@db.invalid/app"  # Never connected to.
VERIFIED = {"SSLMODE": "verify-full", "SSLROOTCERT": "/etc/ca.pem"}


def test_check_lists_pools_too_small(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check`` lists what ``pool_sizing_warnings`` finds."""
    monkeypatch.setattr(
        "domainkit.management.commands.check.pool_sizing_warnings",
        lambda: ["The pool is small."],
    )
    assert run("check").endswith("  Warning: deploy.W006: The pool is small.\n")


def test_check_warns_of_pools_smaller_than_the_requests_admitted() -> None:
    """1,000 requests at once against 15 connections a loop: warned (PERF-14)."""
    deployed = {
        "ENVIRONMENT": "staging",
        "API_DOCS": False,
        "CACHES": BOUNDED_CACHES,
        "DATABASES": {
            "default": {"URL": NETWORK, **VERIFIED},
            "big": {"URL": NETWORK, "POOL_SIZE": 50, "MAX_OVERFLOW": 0, **VERIFIED},
            "local": {"URL": "sqlite+aiosqlite:///:memory:"},
        },
    }
    with override_settings(**deployed):
        assert pool_sizing_warnings() == [
            (
                "MAX_CONCURRENT_REQUESTS (1000) admits more requests than the pool of "
                "DATABASES['default'] serves: it holds 15 connections per event loop "
                "(POOL_SIZE + MAX_OVERFLOW), so past about 60 requests at once they "
                "wait for a connection, unseen by the shedding queue, for up to 10s, "
                "then fail with 503. Lower MAX_CONCURRENT_REQUESTS to about 60, or "
                "raise the pool within what the database accepts: each process opens "
                "up to 15 connections per pool (twice that when it reads through "
                "Reads.STATEMENTS), times processes and replicas."
            ),
            (
                "MAX_CONCURRENT_REQUESTS (1000) admits more requests than the pool of "
                "DATABASES['big'] serves: it holds 50 connections per event loop "
                "(POOL_SIZE + MAX_OVERFLOW), so past about 200 requests at once they "
                "wait for a connection, unseen by the shedding queue, for up to 10s, "
                "then fail with 503. Lower MAX_CONCURRENT_REQUESTS to about 200, or "
                "raise the pool within what the database accepts: each process opens "
                "up to 50 connections per pool (twice that when it reads through "
                "Reads.STATEMENTS), times processes and replicas."
            ),
        ]
    with override_settings(**deployed, MAX_CONCURRENT_REQUESTS=200):
        warned = pool_sizing_warnings()
        assert len(warned) == 1
        assert "DATABASES['default']" in warned[0]
    with override_settings(**deployed, MAX_CONCURRENT_REQUESTS=60):
        assert pool_sizing_warnings() == []
    timed = {
        "URL": NETWORK,
        "POOL_SIZE": 2,
        "MAX_OVERFLOW": 1,
        "POOL_TIMEOUT": 1.5,
        **VERIFIED,
    }
    with override_settings(**{**deployed, "DATABASES": {"default": timed}}):
        (warning,) = pool_sizing_warnings()
        assert "it holds 3 connections" in warning
        assert "for up to 1.5s" in warning
    with override_settings(**deployed, MAX_CONCURRENT_REQUESTS=None):
        assert pool_sizing_warnings() == []
    with override_settings(**{**deployed, "ENVIRONMENT": "local"}):
        assert pool_sizing_warnings() == []


def test_check_lists_the_domains_warnings_too() -> None:
    """Each installed domain may add warnings of its own settings."""
    with override_settings(
        ENVIRONMENT="production",
        API_DOCS=False,
        CACHES=BOUNDED_CACHES,
        SILENCED_CHECKS=["deploy.W005"],
    ):
        setup()
        on_deployment_warnings(lambda: ["ALPHA is weak here."], domain="alpha")
        output = run("check")

    assert output.startswith(
        "System check identified no issues and 3 warnings (1 silenced). "
        "Domains: alpha.\n"
    )
    assert output.endswith("  Warning: ALPHA is weak here.\n")


def test_check_takes_one_migration_head_per_database(auth_project: str) -> None:
    """A history with one head is what ``check`` expects; more is a problem."""
    with override_settings(ROOT_ROUTER=None):
        assert run("check").startswith("System check identified no issues")


def test_check_without_domains() -> None:
    """A project with no domains is valid."""
    with override_settings(INSTALLED_DOMAINS=[], ALPHA=UNSET, ROOT_ROUTER=None):
        assert run("check").endswith("Domains: none.\n")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9000", ("127.0.0.1", 9000)),
        ("0.0.0.0:80", ("0.0.0.0", 80)),  # noqa: S104 - parsing only.
        ("[::1]:8080", ("::1", 8080)),
    ],
)
def test_runserver_addresses(value: str, expected: tuple[str, int]) -> None:
    """Ports, host:port, and bracketed IPv6 addresses are accepted."""
    assert runserver.parse_address(value) == expected


@pytest.mark.parametrize("value", ["host:", ":80", "x", "localhost:99999"])
def test_runserver_rejects_bad_addresses(value: str) -> None:
    """Malformed addresses fail before uvicorn starts."""
    with pytest.raises(CommandError, match="not a valid port"):
        runserver.parse_address(value)


def test_runserver_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uvicorn gets the configured application and the framework's logging."""
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs))
    )

    with override_settings(ASGI_APPLICATION="config.asgi:application"):
        run("runserver", "8001", reload=False)

    ((app, options),) = calls
    assert app == "config.asgi:application"
    assert options["port"] == 8001
    assert options["reload"] is False
    assert options["log_config"] is None
    with pytest.raises(CommandError, match="ASGI_APPLICATION"):
        run("runserver")


@pytest.fixture
def migration_project(
    tmp_path: Path, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """An empty database and revision directory for the test table."""
    monkeypatch.setattr(migrations_module, "metadata", test_metadata)
    versions = tmp_path / "migrations"
    with override_settings(
        MIGRATIONS_DIR=versions, DATABASES={"default": {"URL": database_url}}
    ):
        yield versions


def test_reading_needs_no_migrations_directory(migration_project: Path) -> None:
    """Before the first revision, read-only commands work and create nothing."""
    assert run("check").startswith("System check identified no issues.")
    assert run("showmigrations") == "No migrations.\n"
    assert run("migrate") == "Database 'default' is at head.\n"
    assert not migration_project.exists()


def test_migration_commands(migration_project: Path) -> None:
    """makemigrations, migrate, and --check work together."""
    with pytest.raises(
        CommandError,
        match=r"^Model changes have no migration: default: New upgrade operations "
        r"detected: \[\('add_table', Table\('framework_test_notes'",
    ):
        run("makemigrations", "--check")
    assert run("makemigrations", "-m", "notes").startswith("Created ")
    with pytest.raises(CommandError, match=r"Run 'manage\.py migrate' first"):
        run("makemigrations")

    assert run("migrate") == "Database 'default' is at head.\n"
    assert run("makemigrations", "--check") == "No changes detected.\n"
    assert run("makemigrations") == "No changes detected.\n"
    assert run("migrate", "base", "--downgrade") == "Database 'default' is at base.\n"
    with pytest.raises(CommandError, match="Can't locate revision"):
        run("migrate", "nope")


def test_template_context_validates_names() -> None:
    """Names must be valid snake_case packages and CamelCase classes."""
    context = startdomain.template_context("order_lines", "domains", None, None)
    assert context == {
        "domain": "order_lines",
        "Domain": "OrderLines",
        "DOMAIN": "ORDER_LINES",
        "domain_title": "Order Lines",
        "package": "domains.order_lines",
        "Aggregate": "OrderLines",
        "aggregate": "order_lines",
        "model": "orderlines",
        "aggregates": "order_lineses",
        "aggregates_title": "order lineses",
        "table": "order_lines_order_lineses",
    }
    assert (
        startdomain.template_context("shop", "d", "Box", None)["aggregates"] == "boxes"
    )

    for name, aggregate, plural, message in [
        ("Shop", None, None, "'Shop' is not a valid snake_case package name."),
        ("class", None, None, "'class' is not a valid snake_case package name."),
        ("shop", "box", None, "'box' is not a valid CamelCase class name."),
        ("shop", "Box", "box", "'box' is not a valid snake_case plural."),
        ("shop", "Box", "Bad-Plural", "'Bad-Plural' is not a valid snake_case plural."),
    ]:
        with pytest.raises(CommandError) as refused:
            startdomain.template_context(name, "domains", aggregate, plural)
        assert str(refused.value) == message


def test_merges_of_several_targets_are_listed_one_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each target's merge revision gets its own line."""
    monkeypatch.setattr(
        migrations_module, "migration_targets", lambda: ("default", "reports")
    )
    monkeypatch.setattr(
        migrations_module, "make_merge", lambda target: Path(f"/{target}.py")
    )

    assert run("makemigrations", "--merge") == (
        "Created /default.py.\nCreated /reports.py.\n"
    )


def test_startdomain_refuses_bad_targets(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """The parent must be importable and the target must not exist."""
    with pytest.raises(CommandError, match="cannot be imported"):
        run("startdomain", "shop", parent="no_such_parent", tests_dir=tmp_path)
    with pytest.raises(CommandError, match="cannot be imported"):
        run("startdomain", "shop", parent="no_such_parent.child", tests_dir=tmp_path)
    make_package("taken_parent", {"shop/__init__.py": ""})
    with pytest.raises(CommandError, match="already exists"):
        run("startdomain", "shop", parent="taken_parent", tests_dir=tmp_path)


def test_startdomain_generates_a_working_domain(
    make_package: MakePackage, tmp_path: Path
) -> None:
    """The generated domain imports, obeys the layer rules, and passes its tests."""
    make_package("generated", {})
    tests_dir = tmp_path / "generated_tests"

    output = run(
        "startdomain",
        "shop",
        aggregate="OrderLine",
        parent="generated",
        tests_dir=tests_dir,
    )

    package = tmp_path / "generated" / "shop"
    assert output.startswith(f"Created {package} and {tests_dir / 'shop'}.\n")
    assert output.endswith(
        "Next steps:\n"
        "  1. Add 'generated.shop' to INSTALLED_DOMAINS in your settings.\n"
        "  2. Mount its router in your ROOT_ROUTER module:\n"
        "       from generated.shop.api.routes import router as shop_router\n"
        '       router.include_router(shop_router, prefix="/api/v1/shop")\n'
        '  3. python manage.py makemigrations -m "create shop"\n'
        "  4. python manage.py migrate\n"
        "  5. Grant shop.view_orderline (and add_, change_, delete_) to the users "
        "or groups that need them; superusers have them.\n"
        "  6. python manage.py runserver; then, from another shell, log in as a "
        "superuser, typing its password at the prompt, and list the order lines:\n"
        '       read -rsp "Password: " PASSWORD && export PASSWORD\n'
        '       TOKEN=$(python -c "import json, os; '
        "print(json.dumps({'username': '<username>', "
        """'password': os.environ['PASSWORD']}))" """
        "| curl -s http://127.0.0.1:8000/auth/login "
        '-H "Content-Type: application/json" -d @- '
        '| python -c "import json, sys; '
        """print(json.load(sys.stdin)['access_token'])")\n"""
        "       unset PASSWORD\n"
        "       curl http://127.0.0.1:8000/api/v1/shop/order_lines "
        '-H "Authorization: Bearer $TOKEN"\n'
    )
    assert sorted(
        path.relative_to(package).as_posix() for path in package.rglob("*.py")
    ) == [
        "__init__.py",
        "api/__init__.py",
        "api/routes.py",
        "application/__init__.py",
        "application/commands.py",
        "application/handlers.py",
        "application/services.py",
        "application/unit_of_work.py",
        "apps.py",
        "domain/__init__.py",
        "domain/events.py",
        "domain/model.py",
        "infrastructure/__init__.py",
        "infrastructure/orm.py",
        "infrastructure/unit_of_work.py",
    ]
    generated = [*package.rglob("*.py"), *(tests_dir / "shop").rglob("*.py")]
    assert not [path for path in generated if "{{" in path.read_text(encoding="utf-8")]
    assert check_domain("generated.shop") == []

    result = subprocess.run(  # noqa: S603 - the test interpreter and generated files.
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(tests_dir / "shop" / "test_model.py"),
            str(tests_dir / "shop" / "test_services.py"),
        ],
        cwd=tmp_path,
        # The child checks the generated code, not mutmut's mutants: under
        # mutmut's stats run it would look for mutmut's settings in tmp_path.
        env={
            **{k: v for k, v in os.environ.items() if k != "MUTANT_UNDER_TEST"},
            "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_startdomain_creates_test_packages_inside_the_project(
    make_package: MakePackage, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test directories inside the project get ``__init__.py`` files with docstrings."""
    make_package("inner_parent.sub", {})
    monkeypatch.setattr(startdomain, "project_root", lambda: tmp_path)
    monkeypatch.setattr(startdomain, "format_python", lambda paths: False)

    run("startdomain", "shop", parent="inner_parent.sub")
    run("startdomain", "cart", parent="inner_parent.sub")

    tests = tmp_path / "tests"
    for directory in (tests / "inner_parent", tests / "inner_parent" / "sub"):
        init = directory / "__init__.py"
        assert init.read_text(encoding="utf-8") == (
            f'"""Tests of {directory.name}."""\n'
        )
    assert (tests / "inner_parent" / "sub" / "shop").is_dir()
    assert (tests / "__init__.py").is_file()


def test_format_python_needs_ruff(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Formatting is skipped when ruff is not installed, with a warning saying so."""
    monkeypatch.setattr(formatting, "find_spec", lambda name: None)

    with caplog.at_level(logging.WARNING, logger="domainkit.utils.formatting"):
        assert formatting.format_python([Path("x.py")]) is False

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, formatting.UNFORMATTED)
    ]
    assert "ruff check --fix . && ruff format ." in formatting.UNFORMATTED


def test_format_python_formats_nothing_without_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No paths, no ruff: given none, ruff would format the whole directory.

    ``makemigrations`` with no changes passes no paths; the user's own code
    in the working directory stays as it is, and nothing is logged.
    """
    mine = tmp_path / "mine.py"
    mine.write_text("import sys, os\nx = { 'a':1 }\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="domainkit.utils.formatting"):
        assert formatting.format_python([]) is False
        monkeypatch.setattr(formatting, "find_spec", lambda name: None)
        assert formatting.format_python(iter([])) is False

    assert mine.read_text(encoding="utf-8") == "import sys, os\nx = { 'a':1 }\n"
    assert not caplog.records


def test_render_copies_plain_files_and_skips_caches(tmp_path: Path) -> None:
    """Only ``*-tpl`` files are filled; caches are not copied."""
    source = tmp_path / "template"
    (source / "__pycache__").mkdir(parents=True)
    (source / "__pycache__" / "x.pyc").write_text("cache", encoding="utf-8")
    (source / "{{ domain }}.txt").write_text("{{ domain }}", encoding="utf-8")
    (source / "readme.md-tpl").write_text("{{ domain }}", encoding="utf-8")

    created = startdomain.render(source, tmp_path / "out", {"domain": "shop"})

    assert sorted(path.name for path in created) == ["readme.md", "shop.txt"]
    assert (tmp_path / "out" / "shop.txt").read_text(encoding="utf-8") == "{{ domain }}"
    assert (tmp_path / "out" / "readme.md").read_text(encoding="utf-8") == "shop"


def _branch(revision: str, parent: str, message: str) -> None:
    """Write an empty revision on top of ``parent``, as a second branch would."""
    from alembic import command

    command.revision(
        migrations_module.alembic_config(),
        message=message,
        head=parent,
        splice=True,
        rev_id=revision,
    )


def test_branches_must_be_merged(migration_project: Path) -> None:
    """Two branches adding 0002 leave two heads until --merge joins them."""
    assert run("showmigrations") == "No migrations.\n"
    run("makemigrations", "-m", "notes")
    (first,) = migrations_module.heads()
    _branch("0002_aaaa", first, "branch a")
    _branch("0002_bbbb", first, "branch b")

    with pytest.raises(CommandError, match=r"2 heads \(0002_aaaa, 0002_bbbb\)"):
        run("check")
    with pytest.raises(CommandError, match="makemigrations --merge"):
        run("migrate")
    with pytest.raises(CommandError) as unmerged:
        run("makemigrations")
    assert str(unmerged.value) == (
        "The migration history of 'default' has 2 heads; run 'manage.py "
        "makemigrations --merge' first."
    )

    assert run("makemigrations", "--merge").startswith("Created ")
    assert run("makemigrations", "--merge") == "Nothing to merge.\n"
    (merged,) = migrations_module.heads()
    assert merged.startswith("0003_")

    pending = run("showmigrations").splitlines()
    assert [line[:3] for line in pending] == ["[ ]"] * 4
    run("migrate")
    applied = run("showmigrations").splitlines()
    assert [line[:3] for line in applied] == ["[X]"] * 4
    assert "Notes." in applied[0]


def test_downgrades_need_consent_in_deployments(
    migration_project: Path, tmp_path: Path
) -> None:
    """Staging and production refuse a downgrade without --allow-data-loss."""
    # Always SQLite: staging also demands TLS from network databases.
    local = {"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'deployed.db'}"}}
    with override_settings(DATABASES=local):
        run("makemigrations", "-m", "notes")
        run("migrate")

        with override_settings(ENVIRONMENT="staging"):
            with pytest.raises(CommandError, match="--allow-data-loss"):
                run("migrate", "base", downgrade=True)
            assert run("migrate", "base", downgrade=True, allow_data_loss=True) == (
                "Database 'default' is at base.\n"
            )


@pytest.mark.anyio
async def test_migrations_run_from_async_code(migration_project: Path) -> None:
    """Called inside an event loop, migrations run on a separate thread."""
    run("makemigrations", "-m", "notes")

    migrations_module.upgrade()

    assert [revision.applied for revision in migrations_module.status()] == [True]


def test_startdomain_next_steps_leave_out_what_it_did(tmp_path: Path) -> None:
    """Installed and mounted: the steps start at the migration, end at the curl."""
    context = startdomain.template_context("invoices", "domains", "Invoice", None)
    done = ["Added 'domains.invoices' to INSTALLED_DOMAINS in base.py."]

    output = startdomain._next_steps(context, tmp_path / "p", tmp_path / "t", done)

    assert output.splitlines() == [
        f"Created {tmp_path / 'p'} and {tmp_path / 't'}.",
        *done,
        "Next steps:",
        '  1. python manage.py makemigrations -m "create invoices"',
        "  2. python manage.py migrate",
        (
            "  3. Grant invoices.view_invoice (and add_, change_, delete_) to the "
            "users or groups that need them; superusers have them."
        ),
        (
            "  4. python manage.py runserver; then, from another shell, log in as "
            "a superuser, typing its password at the prompt, and list the invoices:"
        ),
        f"       {startdomain.PROMPT_LINE}",
        f"       {startdomain.LOGIN_CURL}",
        f"       {startdomain.UNSET_LINE}",
        (
            "       curl http://127.0.0.1:8000/api/v1/invoices/invoices "
            '-H "Authorization: Bearer $TOKEN"'
        ),
    ]


def test_the_smaller_domain_asks_only_for_the_permissions_it_uses(
    tmp_path: Path,
) -> None:
    """``--minimal`` deletes nothing, so its next steps grant no ``delete_``."""
    context = startdomain.template_context("invoices", "domains", "Invoice", None)

    output = startdomain._next_steps(
        context, tmp_path / "p", tmp_path / "t", ["Installed."], minimal=True
    )

    assert (
        "  3. Grant invoices.view_invoice (and add_, change_) to the users or "
        "groups that need them; superusers have them."
    ) in output.splitlines()
