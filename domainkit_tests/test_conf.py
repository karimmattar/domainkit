"""Settings: the environment reader, the schema, and the lazy proxy."""

from pathlib import Path

import pytest

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
from domainkit.conf.lazy import LazySettings
from domainkit.exceptions import ImproperlyConfigured
from domainkit_tests.conftest import MakePackage


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
        ("int", "x", "an integer"),
        ("float", "y", "a number"),
        ("bool", "z", "a boolean"),
    ],
)
def test_env_invalid_values_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, method: str, value: str, expected: str
) -> None:
    """Conversion errors say which variable is wrong and what was expected."""
    monkeypatch.setenv("T_BAD", value)

    with pytest.raises(ImproperlyConfigured, match=f"T_BAD must be {expected}"):
        getattr(Env(prefix="T_", env_file=Path("/nonexistent")), method)("BAD")


def test_env_required_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing variable without a default names the variable to set."""
    monkeypatch.delenv("T_REQUIRED", raising=False)

    with pytest.raises(ImproperlyConfigured, match="Set the T_REQUIRED"):
        Env(prefix="T_", env_file=Path("/nonexistent")).str("REQUIRED")


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
