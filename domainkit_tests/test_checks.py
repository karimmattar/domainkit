"""System checks: ids, levels, hints, one report of everything, and silencing."""

import copy
import io
import pickle
from typing import Any, Final

import pytest
from pydantic import ValidationError

from domainkit.checks import (
    MAX_SILENCED_CHECKS,
    CheckFailed,
    CheckMessage,
    Level,
    describe,
    silence,
    validate_silenced,
)
from domainkit.conf import UNSET, Settings, override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit_tests.conftest import MakePackage

SECRET_KEY: Final = "tests-only-6Zq3b1Jw8RkV0yXc5MnHf2LpT9sDg4AeU7oKiWx3N"
DEPLOYED: Final = {
    "ENVIRONMENT": "production",
    "ALLOWED_HOSTS": ["api.example.com"],
    "SECRET_KEY": SECRET_KEY,
}
NETWORK_DATABASE: Final = {
    "URL": "postgresql+asyncpg://app@db.internal/app",
    "SSLMODE": "verify-full",
}


def run(*args: str) -> str:
    """Run a command; what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


def test_a_finding_is_its_message_with_an_id_a_level_and_a_hint() -> None:
    """Code that collects problems as strings keeps working (DSGN-08)."""
    found = CheckMessage("security.W001", "Docs are public.", "Turn them off.")

    assert found == "Docs are public."
    assert (found.id, found.level, found.hint) == (
        "security.W001",
        Level.WARNING,
        "Turn them off.",
    )
    assert CheckMessage("deploy.E002", "x").level is Level.ERROR
    assert CheckMessage("apps.I001", "x").level is Level.INFO
    assert found.message == "Docs are public."
    assert type(found.message) is str
    assert repr(found) == "<CheckMessage security.W001: 'Docs are public.'>"
    pickled = pickle.loads(pickle.dumps(found))  # noqa: S301 - our own bytes.
    for kept in (pickled, copy.deepcopy(found)):
        assert isinstance(kept, CheckMessage)
        assert (kept.id, kept.hint, kept) == (found.id, found.hint, found)


@pytest.mark.parametrize(
    "bad", ["security", "security.X001", "security.W01", "Security.W001", ".W001"]
)
def test_ids_have_a_namespace_a_level_and_a_number(bad: str) -> None:
    """``namespace.E001``: lower case, a dot, E, W, or I, three digits."""
    with pytest.raises(ValueError, match="is not a check id"):
        CheckMessage(bad, "message")


def test_findings_are_described_with_their_id_and_hint() -> None:
    """``check`` prints the id first, and the hint on a line of its own."""
    hinted = CheckMessage("deploy.W004", "No pool size.", "Set POOL_SIZE.")

    assert describe(hinted) == "  deploy.W004: No pool size.\n    Hint: Set POOL_SIZE."
    assert describe(hinted, label="Warning: ") == (
        "  Warning: deploy.W004: No pool size.\n    Hint: Set POOL_SIZE."
    )
    assert describe(CheckMessage("deploy.W001", "No deadline.")) == (
        "  deploy.W001: No deadline."
    )
    assert describe("A plain problem.", label="Warning: ") == (
        "  Warning: A plain problem."
    )
    failed = CheckFailed("Two problems", [hinted, "A plain problem."])
    assert isinstance(failed, ImproperlyConfigured)
    assert str(failed) == (
        "Two problems:\n  deploy.W004: No pool size.\n    Hint: Set POOL_SIZE.\n"
        "  A plain problem."
    )
    assert failed.messages == [hinted, "A plain problem."]


def test_silencing_drops_warnings_never_errors_nor_plain_problems() -> None:
    """Only a warning or a note whose id is listed goes."""
    warning = CheckMessage("security.W001", "Docs are public.")
    note = CheckMessage("deploy.I001", "A note.")
    error = CheckMessage("security.E001", "DEBUG is on.")
    found = [warning, note, error, "A plain problem."]

    assert silence(found, ["security.W001", "deploy.I001", "security.E001"]) == [
        error,
        "A plain problem.",
    ]
    assert silence(found, []) == found


@pytest.mark.parametrize(
    ("silenced", "message"),
    [
        (["security.E001"], "cannot silence the error security.E001"),
        (["API_DOCS"], "'API_DOCS' is not a check id"),
        (["security.W001", "security.W001"], "lists an id twice"),
        (
            [f"security.W{n:03}" for n in range(MAX_SILENCED_CHECKS + 1)],
            "at most 64",
        ),
    ],
)
def test_silenced_checks_are_warnings_listed_once(
    silenced: list[str], message: str
) -> None:
    """Errors stand for unsafe configurations: deny by default keeps them."""
    with pytest.raises(ValueError, match=message):
        validate_silenced(silenced)
    with pytest.raises(ValidationError, match="SILENCED_CHECKS"):
        Settings.model_validate({"SILENCED_CHECKS": silenced})


def test_settings_take_warnings_to_silence() -> None:
    """Up to 64 warnings' ids, none by default."""
    assert Settings().SILENCED_CHECKS == ()
    ids = tuple(f"security.W{n:03}" for n in range(MAX_SILENCED_CHECKS))
    assert ids == Settings.model_validate({"SILENCED_CHECKS": ids}).SILENCED_CHECKS


def test_every_breach_of_the_baseline_is_reported_at_once() -> None:
    """One error names them all, with ids, not one per restart (INFRA-08 e)."""
    breaches = {
        **DEPLOYED,
        "DEBUG": True,
        "ALLOWED_HOSTS": ["*"],
        "SECRET_KEY": "short",
        "DATABASES": {"default": {"URL": "postgresql+asyncpg://app@db/app"}},
        "BROKERS": {"default": {"URL": "memory://"}},
    }

    with pytest.raises(ValidationError) as refused:
        Settings.model_validate(breaches)

    report = str(refused.value)
    for found in (
        "security.E001: DEBUG must be False in production",
        "security.E003: ALLOWED_HOSTS must list real host names in production",
        "security.E007: SSLMODE must be 'verify-full' in production for ['default']",
        (
            "deploy.E002: memory:// brokers serve one process; use RabbitMQ in "
            "production for BROKERS ['default']"
        ),
        "security.E018: SECRET_KEY must be random",
    ):
        assert found in report
    assert "short" not in report  # The key itself is never quoted.
    errors = Settings.model_validate({**breaches, "ENVIRONMENT": "local"})
    assert [error.id for error in errors.deployment_errors()] == [
        "security.E001",
        "security.E003",
        "security.E007",
        "deploy.E002",
        "security.E018",
    ]


def test_the_missing_and_development_keys_have_ids_too() -> None:
    """Fallbacks are held to the rules; a missing key gives a hint."""
    missing = Settings.model_validate({**DEPLOYED, "ENVIRONMENT": "local"})
    missing = missing.model_copy(update={"SECRET_KEY": None})
    [error] = missing.deployment_errors()
    assert (error.id, error.hint.startswith("Make one with")) == ("security.E016", True)
    development = Settings.model_validate(
        {
            **DEPLOYED,
            "ENVIRONMENT": "local",
            "SECRET_KEY_FALLBACKS": ["domainkit-insecure-" + "x" * 60],
        }
    )
    assert [error.id for error in development.deployment_errors()] == ["security.E017"]


@pytest.mark.parametrize(
    ("change", "found"),
    [
        ({"CORS_ALLOWED_ORIGINS": ["*"]}, ["security.W006"]),
        ({"DATABASES": {"default": NETWORK_DATABASE}}, ["deploy.W004"]),
        (
            {"DATABASES": {"default": {**NETWORK_DATABASE, "POOL_SIZE": 10}}},
            [],
        ),
        (
            {"DATABASES": {"default": {"URL": "sqlite+aiosqlite:///app.db"}}},
            ["deploy.W005"],
        ),
    ],
)
def test_deployments_are_warned_of_open_cors_unsized_pools_and_sqlite(
    change: dict[str, Any], found: list[str]
) -> None:
    """The checks INFRA-08 found missing, as warnings where deployed."""
    hardened = {
        **DEPLOYED,
        "API_DOCS": False,
        "SECURE_HSTS_SECONDS": 1,
        "RATE_LIMIT": "10/second",
        "MIDDLEWARE": [
            *Settings().MIDDLEWARE,
            "domainkit.http.ratelimit.RateLimitMiddleware",
        ],
        "TRUSTED_PROXIES": ["10.0.0.0/8"],
    }
    warnings = Settings.model_validate({**hardened, **change}).deployment_warnings()

    assert [getattr(warning, "id", None) for warning in warnings] == found
    local = Settings.model_validate({**hardened, **change, "ENVIRONMENT": "local"})
    assert local.deployment_warnings() == []


def test_check_deploy_is_for_deployed_configurations() -> None:
    """Elsewhere it would pass for the wrong reason: it refuses to run."""
    with pytest.raises(CommandError, match="ENVIRONMENT set to staging or production"):
        run("check", "--deploy")


def test_check_deploy_fails_on_warnings_a_project_has_not_silenced() -> None:
    """A deployment pipeline gates on ``check --deploy`` (INFRA-08 f)."""
    production = {
        "ENVIRONMENT": "production",
        "CACHES": {"default": {"COMPUTE_TIMEOUT": 30}},
    }
    with override_settings(**production):
        assert run("check").startswith(
            "System check identified no issues and 4 warnings."
        )
        with pytest.raises(CommandError) as refused:
            run("check", "--deploy")
    weak = ["security.W001", "security.W002", "security.W003", "deploy.W005"]
    assert str(refused.value).startswith(
        "System check --deploy refuses these warnings:\n  security.W001: API_DOCS"
    )
    assert "    Hint: Use PostgreSQL in staging and production." in str(refused.value)
    with override_settings(**production, SILENCED_CHECKS=weak):
        checked = run("check", "--deploy")
    # The HTTPS statement is information: it never fails --deploy.
    assert checked.splitlines()[:2] == [
        "System check identified no issues (4 silenced). Domains: alpha.",
        (
            "  Info: security.I001: HTTPS is not checked: the application serves "
            "plain HTTP, and TLS must end in front of it. It takes a request's "
            "scheme from the connection alone (TRUSTED_PROXIES is empty), so every "
            "request counts as plain HTTP, even one a proxy received over HTTPS."
        ),
    ]
    assert len(checked.splitlines()) == 3


def test_check_reports_every_problem_at_once_where_deployed(
    make_package: MakePackage,
) -> None:
    """A clash refuses the build there, yet layer rules are reported too.

    On 0.9.2 the build's refusal ended ``check`` before the layer rules
    were checked (INFRA-08 e).
    """
    make_package("bad_layers", {"domain/model.py": "import sqlalchemy\n"})
    with (
        override_settings(
            ENVIRONMENT="production",
            INSTALLED_DOMAINS=["bad_layers"],
            ALPHA=UNSET,
            ROOT_ROUTER="domainkit_tests.test_openapi.twice",
            OPENAPI_OPERATION_IDS="route_name",
            REQUIRE_ROUTE_RULES=False,
        ),
        pytest.raises(CommandError) as refused,
    ):
        run("check")

    lines = str(refused.value).splitlines()
    assert lines[0] == "System check found problems:"
    assert [line.split(":")[0].strip() for line in lines[1:]] == [
        "openapi.E001",
        "architecture.E001",
    ]
    assert "imports sqlalchemy" in lines[2]


@pytest.mark.parametrize(
    ("change", "source"),
    [
        ({}, "from the connection alone (TRUSTED_PROXIES is empty)"),
        (
            {"TRUSTED_PROXIES": ["10.0.0.0/8"], "PROXY_SCHEME_HEADER": None},
            "from the connection alone (PROXY_SCHEME_HEADER is None)",
        ),
        (
            {"TRUSTED_PROXIES": ["10.0.0.0/8"], "PROXY_SCHEME_HEADER": "X-Scheme"},
            (
                "from the X-Scheme header of TRUSTED_PROXIES only, so the proxy "
                "must set it, over a caller's."
            ),
        ),
    ],
)
def test_check_says_where_https_ends_and_whose_word_the_scheme_is(
    change: dict[str, Any], source: str
) -> None:
    """``check`` cannot see the TLS in front, so it says so (INFRA-08)."""
    from domainkit.management.commands.check import https_statement

    with override_settings(ENVIRONMENT="staging", **change):
        statement = https_statement()
        printed = run("check").splitlines()

    assert (statement.id, statement.level) == ("security.I001", Level.INFO)
    assert f"It takes a request's scheme {source}" in statement
    assert statement.hint.startswith("Redirect plain HTTP to HTTPS at the proxy")
    assert printed[1] == f"  Info: security.I001: {statement.message}"
    assert printed[2] == f"    Hint: {statement.hint}"
    with override_settings(**change):  # Locally, nothing is said of it.
        assert "security.I001" not in run("check")
