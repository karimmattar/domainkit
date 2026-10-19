"""``dbshell``, ``diffsettings``, and ``shell``: consoles on the project."""

import io
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from domainkit.conf import UNSET, override_settings, use_settings
from domainkit.management import CommandError, call_command
from domainkit.management.commands import shell
from domainkit_tests.conftest import MakePackage, UseAuth

PASSWORD = "s3cret-pass"


def run(*args: str, **options: Any) -> str:  # noqa: ANN401 - command options.
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output, **options)
    return output.getvalue()


class Client:
    """Records how ``dbshell`` would run the database's client."""

    def __init__(self, returncode: int = 0) -> None:
        """Answer every run with ``returncode``."""
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, argv: list[str], *, env: dict[str, str], check: bool) -> Any:  # noqa: ANN401
        """Record the call."""
        self.calls.append((argv, env))
        return SimpleNamespace(returncode=self.returncode)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Client:
    """``psql`` and ``sqlite3`` installed, and run by the recorder."""
    recorder = Client()
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subprocess.run", recorder.run)
    monkeypatch.setenv("PGPASSWORD", "from-the-shell")
    monkeypatch.setenv("PGSERVICE", "elsewhere")
    return recorder


def postgres(**options: object) -> dict[str, object]:
    """``DATABASES`` with a PostgreSQL default."""
    return {
        "default": {
            "URL": f"postgresql+asyncpg://app:{PASSWORD}@db.internal:6543/shop",
            **options,
        }
    }


def test_dbshell_passes_the_password_in_the_environment(client: Client) -> None:
    """``psql`` gets the connection in libpq's variables; argv holds no secret."""
    with override_settings(
        DATABASES=postgres(SSLMODE="verify-full", SSLROOTCERT="/etc/ca.pem")
    ):
        run("dbshell", "--", "-c", "SELECT 1")

    ((argv, env),) = client.calls
    assert argv == ["/usr/bin/psql", "-c", "SELECT 1"]
    assert PASSWORD not in " ".join(argv)
    assert {name: env[name] for name in env if name.startswith("PG")} == {
        "PGHOST": "db.internal",
        "PGPORT": "6543",
        "PGUSER": "app",
        "PGPASSWORD": PASSWORD,
        "PGDATABASE": "shop",
        "PGSSLMODE": "verify-full",
        "PGSSLROOTCERT": "/etc/ca.pem",
    }


def test_dbshell_connects_as_the_owner_on_request(client: Client) -> None:
    """``--owner`` uses ``MIGRATION_URL``; a socket host comes from the query."""
    owner = "postgresql+asyncpg://owner:pw@/shop?host=/var/run/postgresql"
    with override_settings(DATABASES=postgres(MIGRATION_URL=owner)):
        run("dbshell", "--owner")
        run("dbshell", "x")  # Without --: the client's arguments still pass.

    (_, owner_env), (argv, app_env) = client.calls
    assert (owner_env["PGUSER"], owner_env["PGHOST"]) == (
        "owner",
        "/var/run/postgresql",
    )
    assert "PGPORT" not in owner_env
    assert "PGSERVICE" not in owner_env
    assert app_env["PGUSER"] == "app"
    assert argv == ["/usr/bin/psql", "x"]


def test_dbshell_opens_sqlite_files(client: Client, tmp_path: Path) -> None:
    """SQLite's client gets the absolute path; memory databases have none."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        run("dbshell")
    with pytest.raises(CommandError, match="in-memory SQLite database"):
        run("dbshell")

    ((argv, env),) = client.calls
    assert argv == ["/usr/bin/sqlite3", str(tmp_path / "local.db")]
    assert "PGPASSWORD" not in env


def test_dbshell_refuses_production_without_consent(
    client: Client, tmp_path: Path
) -> None:
    """Production needs --allow-production."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    with override_settings(
        DATABASES={"default": {"URL": url}},
        ENVIRONMENT="production",
        ALLOWED_HOSTS=["api.example.com"],
    ):
        with pytest.raises(CommandError, match="--allow-production"):
            run("dbshell")
        run("dbshell", allow_production=True)

    assert len(client.calls) == 1


def test_dbshell_explains_what_it_cannot_do(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown databases and missing clients; the client's failure is ours."""
    with (
        override_settings(DATABASES={"default": {"URL": "mysql+aiomysql://u:p@h/d"}}),
        pytest.raises(CommandError, match="no client for 'mysql'"),
    ):
        run("dbshell")
    with pytest.raises(Exception, match="DATABASES has no 'other' alias"):
        run("dbshell", "--database", "other")
    client.returncode = 3
    with override_settings(DATABASES=postgres()):
        with pytest.raises(CommandError, match="psql exited with 3") as failed:
            run("dbshell")
        assert failed.value.returncode == 3
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(CommandError, match="psql is not installed"):
            run("dbshell")


def test_dbshell_runs_psql(
    database_url: str, capfd: pytest.CaptureFixture[str]
) -> None:
    """On PostgreSQL, with psql installed, the command really connects."""
    if not database_url.startswith("postgresql") or shutil.which("psql") is None:
        pytest.skip("needs PostgreSQL and psql")
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        run("dbshell", "--", "-tAc", "SELECT 41 + 1")

    assert capfd.readouterr().out.strip() == "42"


# diffsettings ---------------------------------------------------------------------


def test_diffsettings_shows_changes_with_secrets_hidden(restore_logging: None) -> None:
    """Changed settings only; URLs, free-form secrets, and domain settings hidden."""
    databases = postgres(CONNECT_ARGS={"password": PASSWORD, "ssl": "prefer"})
    logging = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "remote": {
                "format": f"https://ops:{PASSWORD}@logs.example.com/ %(message)s"
            }
        },
        "root": {"handlers": [], "level": "WARNING"},
    }
    with override_settings(DATABASES=databases, LOGGING=logging, DEBUG=True):
        output = run("diffsettings")

    assert PASSWORD not in output
    lines = output.splitlines()
    assert lines[0] == "# domainkit_tests.settings: settings that differ from defaults"
    assert "DEBUG = True" in lines
    assert "ALPHA = '**********'" in lines
    assert not [
        line for line in lines if line.startswith(("API_DOCS", "SETTINGS_MODULE"))
    ]
    (database,) = [line for line in lines if line.startswith("DATABASES")]
    assert "'URL': '**********'" in database
    assert "'password': '**********'" in database
    assert "'ssl': 'prefer'" in database
    (handlers,) = [line for line in lines if line.startswith("LOGGING")]
    assert "https://ops:**********@logs.example.com/" in handlers
    assert "'handlers': []" in handlers


def test_diffsettings_hides_project_settings_named_like_secrets(
    make_package: MakePackage,
) -> None:
    """A project's own plain-text setting is hidden when its name says secret."""
    make_package(
        "secret_project",
        {
            "settings.py": (
                "from domainkit.conf import Settings\n"
                "class ProjectSettings(Settings):\n"
                "    PAYMENT_API_KEY: str = ''\n"
                "    PAYMENT_REGION: str = 'eu'\n"
                "SETTINGS_CLASS = ProjectSettings\n"
                f"PAYMENT_API_KEY = {PASSWORD!r}\n"
                "PAYMENT_REGION = 'us'\n"
            )
        },
    )
    with use_settings("secret_project.settings"):
        output = run("diffsettings")
        every = run("diffsettings", "--all")

    assert PASSWORD not in output + every
    assert "PAYMENT_API_KEY = '**********'" in output.splitlines()
    assert "PAYMENT_REGION = 'us'" in output.splitlines()


def test_diffsettings_all_marks_defaults() -> None:
    """``--all`` lists every setting, defaults marked ``###``."""
    lines = run("diffsettings", "--all").splitlines()

    assert "### API_DOCS = True" in lines
    assert "ENVIRONMENT = 'test'" in lines


# shell ----------------------------------------------------------------------------


def test_shell_runs_code_with_top_level_await(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-c`` code may await; the project is loaded."""
    run(
        "shell",
        "-c",
        "from sqlalchemy import text\n"
        "async with connections.engine().connect() as c:\n"
        "    print(await c.scalar(text('SELECT 41 + 1')))\n"
        "print(settings.SETTINGS_MODULE, registry.ready)",
    )

    assert capsys.readouterr().out == "42\ndomainkit_tests.settings True\n"


def test_shell_imports_domain_classes(
    use_auth: UseAuth, capsys: pytest.CaptureFixture[str]
) -> None:
    """Models, repositories, and units of work of installed domains, by name."""
    use_auth()
    names = "{'User', 'Group', 'UserRepository', 'AuthUnitOfWork', 'BaseBackend'}"
    probe = f"print(sorted(set(globals()) & {names}))"

    run("shell", "-c", probe)
    run("shell", "--no-imports", "-c", probe)

    assert capsys.readouterr().out.splitlines() == [
        "['AuthUnitOfWork', 'Group', 'User', 'UserRepository']",
        "[]",
    ]


CLASHING = {
    "models.py": (
        "from uuid import UUID\n"
        "from domainkit.ddd import Entity\n"
        "class Box(Entity[UUID]):\n    pass\n"
        "class _Hidden(Entity[UUID]):\n    pass\n"
    ),
    "broken.py": "raise RuntimeError('broken on import')\n",
    "management/commands/never.py": "raise RuntimeError('commands are skipped')\n",
}


@pytest.fixture
def console_input(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Lines typed into the console, then end of input."""
    lines: list[str] = []

    def raw_input(self: object, prompt: str = "") -> str:
        if not lines:
            raise EOFError
        return lines.pop(0)

    monkeypatch.setattr(shell.AsyncConsole, "raw_input", raw_input)
    return lines


def test_the_console_awaits_and_reports(
    make_package: MakePackage,
    console_input: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Lines may await; errors are shown; broken modules and clashes are listed."""
    make_package("boxes_one", CLASHING)
    make_package("boxes_two", {"models.py": CLASHING["models.py"]})
    console_input.extend(
        [
            "box = Box(id=None)",
            "value = await asyncio.sleep(0, result=7)",
            "print(value, type(box).__module__)",
            "1 / 0",
            "print('still here')",
        ]
    )

    with override_settings(INSTALLED_DOMAINS=["boxes_one", "boxes_two"], ALPHA=UNSET):
        run("shell")

    captured = capsys.readouterr()
    assert "7 boxes_one.models\n" in captured.out
    assert "still here" in captured.out
    assert "ZeroDivisionError" in captured.err
    assert "Not imported: boxes_one.broken (RuntimeError)." in captured.err
    assert "Not imported: boxes_two.models.Box (name taken)." in captured.err
    assert "_Hidden" not in captured.err


def test_the_console_exits_on_request(console_input: list[str]) -> None:
    """``exit()`` leaves the console, closing what it opened."""
    console_input.append("raise SystemExit(4)")

    with pytest.raises(SystemExit) as exited:
        run("shell")

    assert exited.value.code == 4
