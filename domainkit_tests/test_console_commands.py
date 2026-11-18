"""``dbshell``, ``diffsettings``, and ``shell``: consoles on the project."""

import asyncio
import importlib
import io
import json
import pkgutil
import shutil
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from pydantic import BaseModel, SecretBytes, SecretStr

import domainkit
from domainkit.conf import UNSET, override_settings, settings, use_settings
from domainkit.management import CommandError, call_command
from domainkit.management.commands import diffsettings, shell
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
        self.checks: list[bool] = []

    def run(self, argv: list[str], *, env: dict[str, str], check: bool) -> Any:  # noqa: ANN401
        """Record the call."""
        self.calls.append((argv, env))
        self.checks.append(check)
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
    assert "ALPHA = {'GREETING': 'hi'}" in lines
    assert not [
        line
        for line in lines
        if line.startswith(("X_FRAME_OPTIONS", "SETTINGS_MODULE"))
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


KEYED = {
    "apps.py": """
        from typing import Any

        from pydantic import BaseModel, SecretStr

        from domainkit.apps import DomainConfig


        class KeyedSettings(BaseModel):
            ADDRESS: str
            TOKEN: SecretStr
            REGION: str = "eu"
            OPTIONS: dict[str, Any] = {}


        class KeyedConfig(DomainConfig):
            settings_class = KeyedSettings
    """
}


def test_diffsettings_shows_a_domains_settings_with_its_secrets_hidden(
    make_package: MakePackage,
) -> None:
    """Validated, with defaults; secret values and secret-named entries hidden.

    The domain's name looks like a secret's (``API_KEYS``), but its
    settings are typed, so they are shown, scrubbed, not hidden whole.
    """
    make_package("api_keys", KEYED)
    keyed = {
        "ADDRESS": f"https://vault:{PASSWORD}@vault.internal",
        "TOKEN": PASSWORD,
        "OPTIONS": {"api_key": PASSWORD, "pool": 4},
    }
    with override_settings(INSTALLED_DOMAINS=["api_keys"], ALPHA=UNSET, API_KEYS=keyed):
        output = run("diffsettings")

    assert PASSWORD not in output
    assert (
        "API_KEYS = {'ADDRESS': 'https://vault:**********@vault.internal', "
        "'TOKEN': '**********', 'REGION': 'eu', "
        "'OPTIONS': {'api_key': '**********', 'pool': 4}}"
    ) in output.splitlines()


def test_diffsettings_hides_a_providers_options_whatever_their_names() -> None:
    """Unifonic's ``APP_SID`` is a secret, in a mapping no type describes."""
    notifications = {
        "SMS_BACKEND": "domainkit.contrib.notifications.sms.UnifonicSmsBackend",
        "SMS_OPTIONS": {"APP_SID": PASSWORD, "SENDER_ID": "Acme"},
    }
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit.contrib.notifications",
        ],
        ALPHA=UNSET,
        NOTIFICATIONS=notifications,
    ):
        output = run("diffsettings") + run("diffsettings", "--all")

    assert PASSWORD not in output
    (line,) = {line for line in output.splitlines() if line.startswith("NOTIFICA")}
    assert "'SMS_OPTIONS': {'APP_SID': '**********', 'SENDER_ID': '**********'}" in line


TYPED = {
    "apps.py": """
        from collections.abc import Mapping
        from typing import Annotated, Any, Literal

        from pydantic import BaseModel, ConfigDict, Field, SecretStr

        from domainkit.apps import DomainConfig


        class Thing:
            def __repr__(self) -> str:
                return "Thing()"


        class Provider(BaseModel):
            NAME: str
            KEY: SecretStr | None = None


        class TypedSettings(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)

            URLS: tuple[Annotated[str, Field(min_length=1)], ...] = ()
            MODE: Literal["fast", "safe"] = "safe"
            SALT: bytes = b""
            PROVIDER: Provider | None = None
            PROVIDERS: dict[str, Provider] = {}
            LIMITS: dict[str, int] = {}
            NAMES: Mapping[str, str] | None = None
            EXTRA: object = None
            VALUES: list[dict[str, Any]] = []
            THING: Thing | None = None


        class TypedConfig(DomainConfig):
            settings_class = TypedSettings
    """
}
PROBES = {
    "redis_empty_user": f"redis://:{PASSWORD}@cache:6379/0",
    "rediss_user": f"rediss://default:{PASSWORD}@cache:6380/0",
    "redis_query": f"redis://cache:6379/0?password={PASSWORD}",
    "at_in_password": f"postgresql://app:ab@{PASSWORD}@db/shop",
    "encoded": f"postgresql+asyncpg://app:{PASSWORD}%40x@db/shop",
    "libpq_keywords": f"host=db user=app password={PASSWORD} dbname=shop",
    "libpq_quoted": f"host=db password='{PASSWORD} x' dbname=shop",
    "amqp": f"amqp://guest:{PASSWORD}@rabbit:5672/",
    "upper_scheme": f"POSTGRESQL://app:{PASSWORD}@db/shop",
    "key_as_user": f"https://{PASSWORD}@o1.ingest.example.com/1",
    "user_with_slash": f"redis://us/er:{PASSWORD}@cache",
    "token_in_query": f"https://api.example.com/v1?access_token={PASSWORD}&x=1",
    "libpq_spaced": f"host = db user = app password = {PASSWORD} dbname = shop",
    "json_key": json.dumps({"type": "service_account", "private_key": PASSWORD}),
    "header": f"Authorization: Bearer {PASSWORD}",
    "header_basic": f"Proxy-Authorization: Basic {PASSWORD}==",
    "quoted_key": f"{{'client_secret': '{PASSWORD} x'}}",
}
"""Secrets in text, which the patterns find even in a field typed ``str``."""
UNTYPED = {
    **PROBES,
    "path_token": f"https://hooks.example.com/services/T0/B0/{PASSWORD}",
    "json": json.dumps({"type": "service_account", "private_key": PASSWORD}),
    "pem": f"-----BEGIN PRIVATE KEY-----\n{PASSWORD}\n-----END PRIVATE KEY-----",
    "space": f"postgresql://app:{PASSWORD} x@db/shop",
    "nested": {"inner": {"client": PASSWORD, "list": [{"Neutral": PASSWORD}]}},
}
"""What only failing closed hides: text in a value that no type describes."""


@pytest.mark.parametrize("name", sorted(UNTYPED))
def test_diffsettings_hides_the_text_of_untyped_values(
    make_package: MakePackage, name: str
) -> None:
    """In a mapping, ``Any``, or ``object``: text hidden, keys and numbers shown."""
    make_package("typed_probe", TYPED)
    settings = {
        "NAMES": {"first": "plain text"},
        "EXTRA": UNTYPED[name],
        "VALUES": [{"value": UNTYPED[name], "count": 3, "on": True, "none": None}],
    }
    with override_settings(
        INSTALLED_DOMAINS=["typed_probe"], ALPHA=UNSET, TYPED_PROBE=settings
    ):
        output = run("diffsettings") + run("diffsettings", "--all")

    assert PASSWORD not in output
    assert "'NAMES': {'first': '**********'}, 'EXTRA': " in output
    assert "'count': 3, 'on': True, 'none': None}]" in output


@pytest.mark.parametrize("name", sorted(PROBES))
def test_diffsettings_hides_passwords_in_typed_text(
    make_package: MakePackage, name: str
) -> None:
    """A URL's user and password, and ``password=``, in a field typed ``str``."""
    make_package("typed_probe", TYPED)
    with override_settings(
        INSTALLED_DOMAINS=["typed_probe"],
        ALPHA=UNSET,
        TYPED_PROBE={"URLS": [PROBES[name]]},
    ):
        output = run("diffsettings")

    assert PASSWORD not in output
    assert "**********" in output


def test_diffsettings_hides_the_value_of_a_secret_pair_and_keeps_the_rest(
    make_package: MakePackage,
) -> None:
    """``password = x``, ``"private_key": "x"``, ``Authorization: Bearer x``.

    A name and its value, with ``=`` or ``:`` between, spaced or not, the
    name quoted or not; a header's scheme is hidden with its token. What
    follows the value stays (audit A28-DX-10).
    """
    make_package("typed_probe", TYPED)
    urls = [
        f"host = db password = {PASSWORD} dbname = shop port = 5432",
        json.dumps({"type": "service_account", "private_key": PASSWORD}),
        f"Authorization: Bearer {PASSWORD}",
        f"X-Request-Id: 7 Proxy-Authorization: Basic {PASSWORD}== X-Other: 1",
        f"password: {PASSWORD} next: 1",
        "token: Bearer, region: eu",  # A scheme with no token: the scheme hides.
    ]
    with override_settings(
        INSTALLED_DOMAINS=["typed_probe"], ALPHA=UNSET, TYPED_PROBE={"URLS": urls}
    ):
        lines = run("diffsettings").splitlines()

    shown = [
        "host = db password = ********** dbname = shop port = 5432",
        '{"type": "service_account", "private_key": **********}',
        "Authorization: **********",
        "X-Request-Id: 7 Proxy-Authorization: ********** X-Other: 1",
        "password: ********** next: 1",
        "token: ********** region: eu",
    ]
    (line,) = [line for line in lines if line.startswith("TYPED_PROBE")]
    assert line.startswith(f"TYPED_PROBE = {{'URLS': {shown!r}, 'MODE': ")


LONG_TEXTS = ["key.", "a.", "pin-", ".token", "key: a ", "a://", "Bearer key: "]
"""Texts that made the patterns read each name or address again from within."""


@pytest.mark.parametrize("unit", LONG_TEXTS)
def test_diffsettings_reads_a_long_text_in_a_time_its_length_bounds(unit: str) -> None:
    """100 KB of text is scrubbed well under a second, 2 KB in 20 ms.

    Each name and each address is read from its start once: the pattern
    that read a name again from each position within it took about a
    second for 2 KB of ``key.``, and over 100 s for 10 KB; the one for a
    URL's password read the rest of a word again at each ``a://``. The
    bound grows with the length, so a text read again and again fails at
    2 KB already, at once, and no machine is too slow for it: both take
    about 1% of it (audit A28-DX-10).
    """
    for size in (2_000, 100_000):
        text = unit * (size // len(unit))
        started = time.perf_counter()
        diffsettings._scrub(text)
        assert time.perf_counter() - started < size / 100_000, size


PLAIN = {
    "apps.py": """
        from collections import deque
        from dataclasses import dataclass, field
        from typing import Any, NamedTuple

        from pydantic import BaseModel

        from domainkit.apps import DomainConfig


        @dataclass
        class Provider:
            NAME: str
            OPTIONS: dict[str, Any] = field(default_factory=dict)
            EXTRA: Any = None


        class Pair(NamedTuple):
            NAME: str
            OPTIONS: dict[str, Any]


        class PlainSettings(BaseModel):
            PROVIDER: Provider | None = None
            PAIR: Pair | None = None
            PROVIDERS: list[Provider] = []
            WAITING: deque[Provider] = deque()


        class PlainConfig(DomainConfig):
            settings_class = PlainSettings
    """
}


def test_diffsettings_hides_the_text_of_values_that_are_not_models(
    make_package: MakePackage,
) -> None:
    """A dataclass, and a mapping in a ``NamedTuple``, fail closed as mappings do.

    No model's types say what their fields hold: their keys, numbers and
    booleans show, their text is hidden, as in a field typed ``Any``. So
    for a container the walk does not know, a ``deque`` (audit A28-DX-10).
    """
    make_package("plain_probe", PLAIN)
    provider = {
        "NAME": "sms",
        "OPTIONS": {"client": PASSWORD, "pool": 4, "on": True},
        "EXTRA": [PASSWORD, 2],
    }
    settings = {
        "PROVIDER": provider,
        "PAIR": ["second", {"client": PASSWORD, "pool": 5}],
        "PROVIDERS": [provider],
        "WAITING": [provider],
    }
    with override_settings(
        INSTALLED_DOMAINS=["plain_probe"], ALPHA=UNSET, PLAIN_PROBE=settings
    ):
        output = run("diffsettings") + run("diffsettings", "--all")

    assert PASSWORD not in output
    hidden = (
        "{'NAME': '**********', 'OPTIONS': {'client': '**********', 'pool': 4, "
        "'on': True}, 'EXTRA': ['**********', 2]}"
    )
    (line,) = {line for line in output.splitlines() if line.startswith("PLAIN")}
    assert line == (
        f"PLAIN_PROBE = {{'PROVIDER': {hidden}, 'PAIR': ['second', "
        f"{{'client': '**********', 'pool': 5}}], 'PROVIDERS': [{hidden}], "
        f"'WAITING': [{hidden}]}}"
    )


def test_diffsettings_shows_typed_values_and_keeps_a_urls_user(
    make_package: MakePackage,
) -> None:
    """Models by their fields, numbers in mappings, bytes hidden, never a crash."""
    make_package("typed_probe", TYPED)
    thing = importlib.import_module("typed_probe.apps").Thing()
    settings = {
        "URLS": ["https://api.example.com/v1", f"postgresql://app:p@{PASSWORD}@db"],
        "MODE": "fast",
        "SALT": b"\xff\x00",  # Not UTF-8: JSON could not hold it.
        "PROVIDER": {"NAME": "first", "KEY": PASSWORD},
        "PROVIDERS": {"second": {"NAME": "second"}},
        "LIMITS": {"daily": 5},
        "THING": thing,  # No JSON type: shown as its repr.
    }
    with override_settings(
        INSTALLED_DOMAINS=["typed_probe"], ALPHA=UNSET, TYPED_PROBE=settings
    ):
        lines = run("diffsettings").splitlines()

    (line,) = [line for line in lines if line.startswith("TYPED_PROBE")]
    assert line == (
        "TYPED_PROBE = {'URLS': ['https://api.example.com/v1', "
        "'postgresql://app:**********@db'], 'MODE': 'fast', 'SALT': '**********', "
        "'PROVIDER': {'NAME': 'first', 'KEY': '**********'}, "
        "'PROVIDERS': {'second': {'NAME': 'second', 'KEY': '**********'}}, "
        "'LIMITS': {'daily': 5}, 'NAMES': None, 'EXTRA': None, 'VALUES': [], "
        "'THING': 'Thing()'}"
    )


RUN_ON_IMPORT = {"domainkit.contrib.audit.tasks", "domainkit.db.migration_env.env"}
"""Modules that act when imported: one needs AUDIT settings, one is Alembic's env."""


def _models() -> Iterator[type[BaseModel]]:
    """Every pydantic model that domainkit defines, its modules imported."""
    for info in pkgutil.walk_packages(domainkit.__path__, "domainkit."):
        if info.name in RUN_ON_IMPORT:
            continue
        module = importlib.import_module(info.name)
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
            ):
                yield value


def _secret(annotation: object) -> bool:
    """Whether the annotation holds a ``SecretStr`` or ``SecretBytes``."""
    if isinstance(annotation, type) and issubclass(annotation, SecretStr | SecretBytes):
        return True
    return any(_secret(argument) for argument in get_args(annotation))


def test_every_secret_field_has_a_name_diffsettings_hides() -> None:
    """A provider's options are a mapping until the provider reads them.

    ``diffsettings`` sees them by name only, and a project's own free-form
    settings too, so every ``SecretStr`` field of domainkit's models has a
    name the pattern hides; a connection URL's password is hidden by the
    URL pattern.
    """
    missed = {
        f"{model.__module__}.{model.__qualname__}.{name}"
        for model in _models()
        for name, field in model.model_fields.items()
        if _secret(field.annotation)
        and not diffsettings.SECRET_KEY.search(name)
        and not name.endswith("URL")
    }

    assert missed == set()


def test_diffsettings_leaves_a_domains_default_settings_out() -> None:
    """Settings a domain reads but the module leaves unset are its defaults."""
    with override_settings(ALPHA=UNSET):
        changed = run("diffsettings").splitlines()
        every = run("diffsettings", "--all").splitlines()

    assert not [line for line in changed if line.startswith("ALPHA")]
    assert "### ALPHA = {'GREETING': 'hello'}" in every


def test_diffsettings_all_marks_defaults() -> None:
    """``--all`` lists every setting, defaults marked ``###``."""
    lines = run("diffsettings", "--all").splitlines()

    assert "### X_FRAME_OPTIONS = 'DENY'" in lines
    assert "API_DOCS = True" in lines  # The tests' settings turn it on.
    assert "ENVIRONMENT = 'test'" in lines


# shell ----------------------------------------------------------------------------


@pytest.mark.usefixtures("database_access")
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
    assert "never" not in captured.err  # Commands are never imported.
    assert captured.err.startswith(
        f"{settings.PROJECT_NAME} shell (domainkit_tests.settings); use await at "
        "the top level.\nAvailable: asyncio, settings, registry, connections, Box.\n"
        "Not imported: "
    )
    assert captured.err.endswith("ZeroDivisionError: division by zero\n\n")


def test_the_console_exits_on_request(console_input: list[str]) -> None:
    """``exit()`` leaves the console, closing what it opened."""
    console_input.append("raise SystemExit(4)")

    with pytest.raises(SystemExit) as exited:
        run("shell")

    assert exited.value.code == 4


def test_dbshell_flags_and_messages(client: Client, tmp_path: Path) -> None:
    """``--allow-production`` on the command line; the client's status is ours."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    with override_settings(
        DATABASES={"default": {"URL": url}},
        ENVIRONMENT="production",
        ALLOWED_HOSTS=["api.example.com"],
    ):
        with pytest.raises(CommandError) as refused:
            run("dbshell")
        run("dbshell", "--allow-production")
    with (
        override_settings(DATABASES={"default": {"URL": "sqlite+aiosqlite://"}}),
        pytest.raises(CommandError) as memory,
    ):
        run("dbshell")

    assert str(refused.value) == (
        "dbshell gives full access to the production database; repeat with "
        "--allow-production if that is meant."
    )
    assert str(memory.value) == "An in-memory SQLite database has no file to open."
    assert client.checks == [False]


def test_diffsettings_leaves_unchanged_settings_out() -> None:
    """Defaults of every type compare equal; lists keep their items."""
    middleware = ["domainkit.http.middleware.RequestContextMiddleware"]
    with override_settings(MIDDLEWARE=middleware):
        lines = run("diffsettings").splitlines()

    assert not [line for line in lines if line.startswith("ACCESS_LOG_QUIET_PATHS")]
    assert f"MIDDLEWARE = {middleware!r}" in lines


def test_the_console_waits_for_the_rest_of_a_block() -> None:
    """An unfinished block asks for more lines, as Python's console does."""
    loop = asyncio.new_event_loop()
    try:
        console = shell.AsyncConsole({}, loop)
        assert console.push("if True:") is True
        assert console.push("    value = await asyncio.sleep(0, result=1)") is True
        console.locals["asyncio"] = asyncio
        assert console.push("") is False
    finally:
        loop.close()

    assert console.locals["value"] == 1


def test_command_code_names_itself_in_tracebacks() -> None:
    """Errors in ``-c`` code point at ``<command>``."""
    with pytest.raises(ZeroDivisionError) as failed:
        run("shell", "-c", "1 / 0")

    frames = traceback.extract_tb(failed.value.__traceback__)
    assert frames[-1].filename == "<command>"


def test_dbshell_passes_every_host(client: Client) -> None:
    """Several ``host`` values become libpq's list; none leaves PGHOST unset."""
    hosts = "postgresql+asyncpg://app@/shop?host=/run/a&host=/run/b"
    with override_settings(DATABASES={"default": {"URL": hosts}}):
        run("dbshell")
    with override_settings(
        DATABASES={"default": {"URL": "postgresql+asyncpg:///shop"}}
    ):
        run("dbshell")

    (_, several), (_, none) = client.calls
    assert several["PGHOST"] == "/run/a,/run/b"
    assert "PGHOST" not in none


@pytest.mark.parametrize(
    ("query", "host", "port"),
    [
        ("host=/run/postgresql&port=55432", "/run/postgresql", "55432"),
        ("host=db1:5433&host=db2:5434", "db1,db2", "5433,5434"),
        ("host=db1:5433&host=/run/b", "db1,/run/b", "5433,"),
    ],
)
def test_dbshell_passes_ports_given_in_the_query(
    client: Client, query: str, host: str, port: str
) -> None:
    """A ``port`` value, or ``host:port`` pairs, reach libpq as asyncpg reads them."""
    url = f"postgresql+asyncpg://app@/shop?{query}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        run("dbshell")

    [(_, environment)] = client.calls
    assert (environment["PGHOST"], environment["PGPORT"]) == (host, port)


def test_diffsettings_names_the_classes_logging_holds(restore_logging: None) -> None:
    """``build_logging_config`` puts classes in ``LOGGING``: shown by their path.

    Every project ``init`` makes has them, and ``diffsettings`` failed on
    them (``Unable to serialize unknown type``).
    """
    from domainkit.log import ContextFilter, build_logging_config

    logging = build_logging_config(log_format="text", level="INFO")
    with override_settings(LOGGING=logging):
        output = run("diffsettings")

    (line,) = [line for line in output.splitlines() if line.startswith("LOGGING")]
    assert f"'{ContextFilter.__module__}.ContextFilter'" in line
