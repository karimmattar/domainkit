"""Settings: the environment reader, the schema, and the lazy proxy."""

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from domainkit.conf import (
    UNSET,
    Settings,
    configure,
    load_settings,
    override_settings,
    reset_settings,
    settings,
    unwrap_settings,
    use_settings,
)
from domainkit.conf.env import (
    ENV_PREFIX_VARIABLE,
    PROJECT_ROOT_VARIABLE,
    Env,
    prefix_for,
    project_prefix,
    project_root,
)
from domainkit.conf.global_settings import DEFAULT_MIDDLEWARE, RATE_LIMIT_MIDDLEWARE
from domainkit.conf.lazy import LazySettings
from domainkit.contrib.auth.settings import AuthSettings
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.messaging.settings import MessagingSettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakePackage
from domainkit_tests.settings import SECRET_KEY


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """A ``.env`` file with one value of each kind."""
    path = tmp_path / ".env"
    path.write_text(
        "APP_TEXT=from-file\nAPP_NUMBER=7\nAPP_SHARED=file\nAPP_SPACED=${HOME}\n",
        encoding="utf-8",
    )
    return path


def test_env_prefers_process_environment(
    env_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process environment wins over the file; ``${...}`` stays literal."""
    monkeypatch.setenv("APP_SHARED", "process")
    env = Env(prefix="APP_", env_file=env_file)

    assert env.str("TEXT") == "from-file"
    assert env.str("SHARED") == "process"
    assert env.str("SPACED") == "${HOME}"
    assert env.int("NUMBER") == 7


def test_env_conversions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each reader converts its type and falls back to the default when unset."""
    for name, value in {
        "F": "1.5",
        "YES": "On",
        "NO": "off",
        "L": "a, ,b",
        "P": "data",
    }.items():
        monkeypatch.setenv(f"T_{name}", value)
    env = Env(prefix="T_", env_file=tmp_path / "missing")

    assert env.float("F") == 1.5
    assert env.bool("YES") is True
    assert env.bool("NO") is False
    assert env.list("L") == ["a", "b"]
    assert env.path("P") == project_root() / "data"
    assert env.str("UNSET", None) is None
    assert env.int("UNSET", 3) == 3
    assert env.float("UNSET", 0.5) == 0.5
    assert env.bool("UNSET", False) is False
    assert env.list("UNSET", []) == []
    assert env.path("UNSET", None) is None


@pytest.mark.parametrize(
    ("method", "value", "expected"),
    [
        ("int", "s3cr3t-x", "an integer"),
        ("float", "s3cr3t-y", "a finite number"),
        ("float", "inf", "a finite number"),
        ("float", "-Infinity", "a finite number"),
        ("float", "nan", "a finite number"),
        ("bool", "s3cr3t-z", "a boolean (true/false)"),
    ],
)
def test_env_invalid_values_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, method: str, value: str, expected: str
) -> None:
    """Conversion errors name the variable and what was expected, never the value.

    The value may be a secret from a ``_FILE``; no chained error quotes it.
    """
    monkeypatch.setenv("T_BAD", value)

    with pytest.raises(ImproperlyConfigured) as caught:
        getattr(Env(prefix="T_", env_file=Path("/nonexistent")), method)("BAD")

    assert str(caught.value) == f"T_BAD must be {expected}."
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


@pytest.mark.parametrize("method", ["str", "int", "float", "bool", "list", "path"])
def test_env_required_variable(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """A missing variable without a default names the variable to set."""
    monkeypatch.delenv("T_REQUIRED", raising=False)

    with pytest.raises(ImproperlyConfigured) as missing:
        getattr(Env(prefix="T_", env_file=Path("/nonexistent")), method)("REQUIRED")
    assert str(missing.value) == "Set the T_REQUIRED environment variable."


def test_env_secret_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``<NAME>_FILE`` supplies the value; setting both, or a bad path, fails."""
    secret = tmp_path / "secret"
    secret.write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setenv("T_TOKEN_FILE", str(secret))
    env = Env(prefix="T_", env_file=tmp_path / "missing")

    assert env.str("TOKEN") == "s3cret"

    monkeypatch.setenv("T_TOKEN", "plain")
    with pytest.raises(ImproperlyConfigured, match="not both"):
        env.str("TOKEN")

    monkeypatch.delenv("T_TOKEN")
    monkeypatch.setenv("T_TOKEN_FILE", str(tmp_path / "absent"))
    with pytest.raises(ImproperlyConfigured, match="cannot read"):
        env.str("TOKEN")


def test_env_file_selection(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, project: Path
) -> None:
    """``<PREFIX>ENV_FILE`` picks the file, empty skips it, unset uses ``.env``."""
    monkeypatch.setenv("APP_ENV_FILE", str(env_file))
    assert Env(prefix="APP_").str("TEXT") == "from-file"

    monkeypatch.setenv("APP_ENV_FILE", "")
    assert Env(prefix="APP_").str("TEXT", None) is None

    monkeypatch.delenv("APP_ENV_FILE")
    assert env_file.parent == project
    assert Env(prefix="APP_").str("TEXT") == "from-file"

    env_file.unlink()
    assert Env(prefix="APP_").file_values == {}


@pytest.fixture
def pyproject(project: Path) -> Path:
    """The project's ``pyproject.toml`` (not created yet)."""
    return project / "pyproject.toml"


def test_project_root_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable wins; else the nearest pyproject.toml upwards; else the cwd."""
    nested = tmp_path / "repo" / "src" / "pkg"
    nested.mkdir(parents=True)
    (tmp_path / "repo" / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv(PROJECT_ROOT_VARIABLE, raising=False)
    try:
        monkeypatch.chdir(nested)
        project_root.cache_clear()
        assert project_root() == (tmp_path / "repo").resolve()

        monkeypatch.chdir(tmp_path)
        project_root.cache_clear()
        assert project_root() == tmp_path.resolve()

        monkeypatch.setenv(PROJECT_ROOT_VARIABLE, str(nested))
        project_root.cache_clear()
        assert project_root() == nested.resolve()
    finally:
        project_root.cache_clear()


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("billingAPI", "BILLING_API_"),
        ("order-hub", "ORDER_HUB_"),
        ("billing.service", "BILLING_SERVICE_"),
        ("--", ""),
    ],
)
def test_prefix_from_project_names(name: str, prefix: str) -> None:
    """Names in any style become UPPER_SNAKE prefixes."""
    assert prefix_for(name) == prefix


@pytest.mark.parametrize(
    ("toml", "prefix"),
    [
        ('[project]\nname = "order-desk"\n', "ORDER_DESK_"),
        ('[project]\nname = "x"\n[tool.domainkit]\nenv-prefix = "od"\n', "OD_"),
        ('[tool.domainkit]\nenv-prefix = "OD_"\n', "OD_"),
        ('[tool.domainkit]\nenv-prefix = ""\n', ""),
    ],
)
def test_project_prefix_sources(pyproject: Path, toml: str, prefix: str) -> None:
    """An explicit ``env-prefix`` wins over the project name; empty means none."""
    pyproject.write_text(toml, encoding="utf-8")

    assert project_prefix() == prefix
    assert Env().prefix == prefix


def test_project_prefix_without_pyproject(
    pyproject: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without pyproject.toml, ``DOMAINKIT_ENV_PREFIX`` is required."""
    with pytest.raises(ImproperlyConfigured, match="Cannot derive"):
        project_prefix()

    monkeypatch.setenv(ENV_PREFIX_VARIABLE, "shop")
    project_prefix.cache_clear()
    assert project_prefix() == "SHOP_"


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[tool.domainkit]\nenv-prefix = "1st"\n', "must start with a letter"),
        ("not = [toml", "not valid TOML"),
    ],
)
def test_invalid_prefix_configuration(pyproject: Path, toml: str, message: str) -> None:
    """Prefixes must be usable as variable names; broken TOML is reported."""
    pyproject.write_text(toml, encoding="utf-8")

    with pytest.raises(ImproperlyConfigured, match=message):
        project_prefix()


def test_load_settings_collects_uppercase_names(make_package: MakePackage) -> None:
    """Only public UPPERCASE names are settings; extras are kept for domains."""
    make_package(
        "conf_ok",
        {
            "settings.py": """
                DEBUG = True
                lowercase = "ignored"
                _HIDDEN = "ignored"
                CUSTOM = {"KEY": 1}
            """
        },
    )

    loaded = load_settings("conf_ok.settings")

    assert loaded.DEBUG is True
    assert loaded.SETTINGS_MODULE == "conf_ok.settings"
    assert loaded.model_extra == {"CUSTOM": {"KEY": 1}}


def test_load_settings_errors(make_package: MakePackage) -> None:
    """Missing modules and invalid values are explained without leaking values."""
    make_package(
        "conf_bad",
        {
            "invalid.py": """
                DATABASES = {"default": {"URL": "postgresql://u:hunter2@db/x"},
                             "other": {"URL": "x", "POOL_SIZE": 0}}
                MAX_REQUEST_BODY_SIZE = 0
            """,
            "broken.py": "import conf_bad_dependency_that_is_missing\n",
        },
    )

    with pytest.raises(ImproperlyConfigured, match="does not exist"):
        load_settings("conf_bad.nope")
    with pytest.raises(ModuleNotFoundError):
        load_settings("conf_bad.broken")
    with pytest.raises(ImproperlyConfigured) as caught:
        load_settings("conf_bad.invalid")
    message = str(caught.value)
    assert "DATABASES.other.POOL_SIZE" in message
    assert "MAX_REQUEST_BODY_SIZE" in message
    assert "hunter2" not in message
    assert caught.value.__cause__ is None


def test_settings_class_extends_the_schema(make_package: MakePackage) -> None:
    """``SETTINGS_CLASS`` may be a class or a dotted path to one."""
    make_package(
        "conf_schema",
        {
            "schema.py": """
                from domainkit.conf import Settings

                class ProjectSettings(Settings):
                    SENTRY_DSN: str | None = None
            """,
            "by_path.py": """
                SETTINGS_CLASS = "conf_schema.schema.ProjectSettings"
                SENTRY_DSN = "https://sentry.example"
            """,
            "by_class.py": """
                from conf_schema.schema import ProjectSettings as SETTINGS_CLASS
            """,
            "missing.py": 'SETTINGS_CLASS = "conf_schema.schema.Nope"\n',
            "wrong.py": "SETTINGS_CLASS = dict\n",
        },
    )

    by_path = load_settings("conf_schema.by_path")
    assert type(by_path).__name__ == "ProjectSettings"
    assert by_path.model_extra == {}
    assert type(load_settings("conf_schema.by_class")).__name__ == "ProjectSettings"
    with pytest.raises(ImproperlyConfigured, match="has no attribute 'Nope'"):
        load_settings("conf_schema.missing")
    with pytest.raises(ImproperlyConfigured, match="must be a subclass"):
        load_settings("conf_schema.wrong")


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"DATABASES": {"other": {"URL": "sqlite://"}}}, "'default' alias"),
        ({"INSTALLED_DOMAINS": ["a", "a"]}, "duplicates"),
        (
            {"CORS_ALLOW_CREDENTIALS": True, "CORS_ALLOWED_ORIGINS": ["*"]},
            "CORS_ALLOW_CREDENTIALS",
        ),
        ({"ENVIRONMENT": "production", "DEBUG": True}, "DEBUG must be False"),
        ({"ENVIRONMENT": "staging", "ALLOWED_HOSTS": ["*"]}, "real host names"),
        (
            {
                "ENVIRONMENT": "production",
                "DATABASES": {"default": {"URL": "sqlite://", "ECHO": True}},
            },
            "ECHO",
        ),
        (
            {
                "ENVIRONMENT": "production",
                "DATABASES": {"default": {"URL": "sqlite://", "LOG_PARAMETERS": True}},
            },
            "LOG_PARAMETERS",
        ),
        (
            {
                "ENVIRONMENT": "staging",
                "DATABASES": {"default": {"URL": "postgresql+asyncpg://db/app"}},
            },
            "SSLMODE must be 'verify-full' in staging for \\['default'\\]",
        ),
        (
            {"ENVIRONMENT": "production", "TRUSTED_PROXIES": ["10.0.0.0/8", "::/0"]},
            "TRUSTED_PROXIES .* not every address in production: any caller",
        ),
        (
            {"ENVIRONMENT": "staging", "TRUSTED_PROXIES": ["0.0.0.0/0"]},
            "proxies' networks, not every address in staging",
        ),
    ],
)
def test_schema_rejects_unsafe_combinations(
    values: dict[str, object], message: str
) -> None:
    """Consistency and deployment checks run at validation time."""
    with pytest.raises(ValueError, match=message):
        Settings.model_validate(values)


def test_production_baseline_passes() -> None:
    """Safe production settings validate."""
    production = Settings.model_validate(
        {
            "ENVIRONMENT": "production",
            "ALLOWED_HOSTS": ["api.example.com"],
            "SECRET_KEY": SECRET_KEY,
            "DATABASES": {
                "default": {
                    "URL": "postgresql+asyncpg://db/app",
                    "SSLMODE": "verify-full",
                },
                "cache": {"URL": "sqlite+aiosqlite:///cache.db"},
            },
        }
    )

    assert production.DEBUG is False


HARDENED = {
    "ENVIRONMENT": "production",
    "ALLOWED_HOSTS": ["api.example.com"],
    "SECRET_KEY": SECRET_KEY,
    "API_DOCS": False,
    "SECURE_HSTS_SECONDS": 1,
    "RATE_LIMIT": "300/minute",
    "MIDDLEWARE": [*DEFAULT_MIDDLEWARE, RATE_LIMIT_MIDDLEWARE],
    "TRUSTED_PROXIES": ["10.0.0.0/8"],
}
DOCS_WARNING = (
    "API_DOCS publishes /docs, /redoc, and /openapi.json to anyone; "
    "set it to False unless the API is public."
)
HSTS_WARNING = (
    "SECURE_HSTS_SECONDS is 0, so browsers may still use plain HTTP; "
    "set it (31536000 is a year) once HTTPS works everywhere."
)
RATE_WARNING = (
    "Requests are not rate limited; set RATE_LIMIT and list "
    "domainkit.http.ratelimit.RateLimitMiddleware in MIDDLEWARE."
)
PROXY_WARNING = (
    "TRUSTED_PROXIES is empty, so callers are rate limited by the address they "
    "connect from: behind a load balancer or proxy, they all share its count. "
    "List its networks in TRUSTED_PROXIES."
)


@pytest.mark.parametrize(
    ("change", "warnings"),
    [
        ({}, []),
        ({"ENVIRONMENT": "staging"}, []),
        ({"API_DOCS": True}, [DOCS_WARNING]),
        ({"SECURE_HSTS_SECONDS": 0}, [HSTS_WARNING]),
        ({"RATE_LIMIT": None}, [RATE_WARNING]),
        ({"MIDDLEWARE": DEFAULT_MIDDLEWARE}, [RATE_WARNING]),
        ({"TRUSTED_PROXIES": []}, [PROXY_WARNING]),
        ({"RATE_LIMIT": None, "TRUSTED_PROXIES": []}, [RATE_WARNING]),
        (
            {
                "ENVIRONMENT": "staging",
                "API_DOCS": True,
                "SECURE_HSTS_SECONDS": 0,
                "MIDDLEWARE": DEFAULT_MIDDLEWARE,
            },
            [DOCS_WARNING, HSTS_WARNING, RATE_WARNING],
        ),
    ],
)
def test_deployments_are_warned_about_weak_choices(
    change: dict[str, object], warnings: list[str]
) -> None:
    """Public docs, no HSTS, and no rate limit are valid but named in deployments."""
    values = {**HARDENED, **change}

    assert Settings.model_validate(values).deployment_warnings() == warnings


DEADLINE_WARNING = (
    "Requests have no deadline, so a slow one holds its resources for as long "
    "as it takes; set REQUEST_TIMEOUT and list "
    "domainkit.http.deadlines.DeadlineMiddleware in MIDDLEWARE."
)
SHEDDING_WARNING = (
    "Requests are not shed under load, so they queue without end; set "
    "MAX_CONCURRENT_REQUESTS and list "
    "domainkit.http.shedding.ConcurrencyLimitMiddleware in MIDDLEWARE."
)
POOLED_WARNING = (
    "DATABASES ['default'] seem to go through PgBouncer (port 6432, or a "
    "statement cache turned off in CONNECT_ARGS); set PGBOUNCER = True, so "
    "timeouts hold per transaction and prepared statements do not clash."
)
POOLED = "postgresql+asyncpg://app@pool.internal:6432/app"


@pytest.mark.parametrize(
    ("change", "warnings"),
    [
        ({"REQUEST_TIMEOUT": None}, [DEADLINE_WARNING]),
        ({"MAX_CONCURRENT_REQUESTS": None}, [SHEDDING_WARNING]),
        (
            {
                "MIDDLEWARE": [
                    name
                    for name in [*DEFAULT_MIDDLEWARE, RATE_LIMIT_MIDDLEWARE]
                    if "deadlines" not in name and "shedding" not in name
                ]
            },
            [DEADLINE_WARNING, SHEDDING_WARNING],
        ),
        (
            {"DATABASES": {"default": {"URL": POOLED, "SSLMODE": "verify-full"}}},
            [POOLED_WARNING],
        ),
        (
            {
                "DATABASES": {
                    "default": {
                        "URL": POOLED,
                        "SSLMODE": "verify-full",
                        "PGBOUNCER": True,
                    }
                }
            },
            [],
        ),
    ],
)
def test_deployments_are_warned_without_deadlines_shedding_or_pgbouncer(
    change: dict[str, object], warnings: list[str]
) -> None:
    """No deadline, no shedding, or PgBouncer used unsaid, are named in deployments."""
    values = {**HARDENED, **change}

    assert Settings.model_validate(values).deployment_warnings() == warnings


@pytest.mark.parametrize("environment", ["local", "test"])
def test_only_deployments_are_warned(environment: str) -> None:
    """Development keeps its docs and plain HTTP without being warned."""
    values = {"ENVIRONMENT": environment, "API_DOCS": True, "SECURE_HSTS_SECONDS": 0}

    assert Settings.model_validate(values).deployment_warnings() == []


def test_lazy_settings_are_read_only() -> None:
    """Public names cannot be assigned; private ones are internal state."""
    with pytest.raises(AttributeError, match="read-only"):
        settings.DEBUG = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        _ = settings._missing  # type: ignore[attr-defined]
    assert settings.SETTINGS_MODULE == "domainkit_tests.settings"
    assert isinstance(unwrap_settings(), Settings)


def test_override_and_use_restore_previous_settings() -> None:
    """Overrides are validated and undone, even when nested."""
    with override_settings(DEBUG=True):
        assert settings.DEBUG is True
        with override_settings(API_DOCS=False):
            assert (settings.DEBUG, settings.API_DOCS) == (True, False)
        assert settings.API_DOCS is True
    assert settings.DEBUG is False

    with (
        pytest.raises(ImproperlyConfigured, match="MAX_REQUEST_BODY_SIZE"),
        override_settings(MAX_REQUEST_BODY_SIZE=0),
    ):
        pass

    with override_settings(DEBUG=True, ALPHA=UNSET, ABSENT=UNSET):
        assert settings.DEBUG is True
        assert unwrap_settings().model_extra == {}
    with override_settings(DEBUG=UNSET):
        assert settings.DEBUG is False
    assert repr(UNSET) == "UNSET"

    with use_settings("domainkit_tests.other_settings"):
        assert settings.SETTINGS_MODULE == "domainkit_tests.other_settings"
    assert settings.SETTINGS_MODULE == "domainkit_tests.settings"


def test_configure_reset_and_module_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``configure`` loads now; ``reset`` reloads from ``<PREFIX>SETTINGS_MODULE``."""
    lazy = LazySettings(Env(env_file=tmp_path / "missing"))
    variable = f"{project_prefix()}SETTINGS_MODULE"
    monkeypatch.delenv(variable, raising=False)

    assert lazy.configured is False
    with pytest.raises(ImproperlyConfigured, match=variable):
        _ = lazy.DEBUG

    monkeypatch.setenv(variable, "domainkit_tests.settings")
    assert lazy.ALLOWED_HOSTS == ("testserver",)
    lazy.configure("domainkit_tests.other_settings")
    assert lazy.SETTINGS_MODULE == "domainkit_tests.other_settings"
    lazy.reset()
    assert lazy.configured is False


def test_global_configure_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module-level helpers act on the shared settings object."""
    monkeypatch.setenv(
        f"{project_prefix()}SETTINGS_MODULE", "domainkit_tests.other_settings"
    )
    with use_settings("domainkit_tests.settings"):
        configure("domainkit_tests.settings")
        assert settings.SETTINGS_MODULE == "domainkit_tests.settings"
        reset_settings()
        assert unwrap_settings().SETTINGS_MODULE == "domainkit_tests.other_settings"


def test_domain_settings_are_hidden_in_reprs_and_dumps() -> None:
    """Domain settings may hold keys; only their own validated models show them."""
    key = "k" * 40  # Built, so secret scanners do not take it for a real key.
    loaded = Settings.model_validate(
        {"DEBUG": True, "AUTH": {"JWT": {"SIGNING_KEY": key}}}
    )

    for shown in (
        repr(loaded),
        str(loaded),
        str(loaded.model_dump()),
        loaded.model_dump_json(),
    ):
        assert key not in shown
        assert "**********" in shown
    assert "DEBUG=True" in repr(loaded)
    assert loaded.model_dump()["DEBUG"] is True
    assert dict(loaded)["AUTH"] == {"JWT": {"SIGNING_KEY": key}}


@pytest.mark.parametrize(
    ("setting", "entry"),
    [
        (
            "CACHES",
            {"URL": "redis://cache:pa/ss-s3cr3t@redis:6379/0", "KEY_PREFIX": "a"},
        ),
        ("LEDGERS", {"URL": "immudb://ledger:pa/ss-s3cr3t@immudb:3322/defaultdb"}),
        ("BROKERS", {"URL": "amqps://broker:pa/ss-s3cr3t@rabbit/vhost", "PREFIX": "a"}),
    ],
)
def test_urls_with_unreadable_ports_are_refused_quietly(
    setting: str, entry: dict[str, str]
) -> None:
    """A / in the password ends the host early; the error quotes none of it."""
    with (
        pytest.raises(ImproperlyConfigured) as refused,
        override_settings(**{setting: {"default": entry}}),
    ):
        pass  # pragma: no cover - refused on entry.

    message = str(refused.value)
    assert message.endswith(
        "Value error, the URL's port is not a number; percent-encode / ? # @ and : "
        "in the user name and password"
    )
    assert "pa" not in message.replace("password", "")
    assert "s3cr3t" not in message


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_settings_numbers_are_finite(value: float) -> None:
    """Infinity and NaN are no setting's value, in any settings model."""
    with (
        pytest.raises(ImproperlyConfigured, match="finite number"),
        override_settings(READINESS_TIMEOUT=value),
    ):
        pass  # pragma: no cover - refused on entry.


@pytest.mark.parametrize(
    ("schema", "field"),
    [
        (MessagingSettings, "SHUTDOWN_TIMEOUT"),
        (IdempotencySettings, "LEASE_SECONDS"),
    ],
)
def test_domain_settings_numbers_are_finite(
    schema: type[BaseModel], field: str
) -> None:
    """Domain settings refuse infinity too: an endless shutdown never ends."""
    with pytest.raises(ValidationError, match="finite number"):
        schema.model_validate({field: float("inf")})


def refused_above(
    schema: type[BaseModel], field: str, highest: float, **others: object
) -> None:
    """``schema`` takes ``highest`` for ``field``, and refuses anything above it."""
    taken = schema.model_validate({**others, field: highest})
    assert getattr(taken, field) == highest
    with pytest.raises(
        ValidationError, match=f"{field}\n  Input should be less than or equal to"
    ):
        schema.model_validate({**others, field: highest + 1})


@pytest.mark.parametrize(
    ("field", "highest"),
    [
        ("TASK_TIMEOUT", 24 * 3600),
        ("VISIBILITY_TIMEOUT", 2 * 24 * 3600),
        ("POLL_INTERVAL", 3600),
        ("BATCH_SIZE", 10_000),
        ("CONCURRENCY", 65_535),  # RabbitMQ's prefetch is a 16-bit count.
        ("SHUTDOWN_TIMEOUT", 3600),
        ("REPUBLISH_AFTER", 7 * 24 * 3600),
        ("RETENTION_SECONDS", 365 * 24 * 3600),
        ("CLEANUP_INTERVAL", 24 * 3600),
    ],
)
def test_messaging_settings_have_upper_bounds(field: str, highest: int) -> None:
    """Generous, but bounded: a typo cannot make a wait or a batch endless."""
    refused_above(MessagingSettings, field, highest, VISIBILITY_TIMEOUT=2 * 24 * 3600)


@pytest.mark.parametrize(
    ("field", "highest"),
    [("TTL_SECONDS", 30 * 24 * 3600), ("LEASE_SECONDS", 24 * 3600)],
)
def test_idempotency_settings_have_upper_bounds(field: str, highest: int) -> None:
    """Keys live at most 30 days, and a lease lasts at most a day."""
    refused_above(IdempotencySettings, field, highest)


@pytest.mark.parametrize(
    ("field", "highest"), [("HASHING_THREADS", 1024), ("HASHING_QUEUE", 10_000)]
)
def test_auth_hashing_settings_have_upper_bounds(field: str, highest: int) -> None:
    """The hashing pool's threads and queue are bounded."""
    refused_above(AuthSettings, field, highest)


def test_readiness_timeout_has_an_upper_bound() -> None:
    """A readiness check waits a minute at most."""
    refused_above(Settings, "READINESS_TIMEOUT", 60)
