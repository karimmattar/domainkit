"""Driver arguments: TLS contexts and server-side timeouts."""

import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from domainkit.conf import DatabaseConfig, override_settings
from domainkit.db.drivers import (
    DEADLINE_SLACK,
    client_ssl_context,
    command_timeout,
    connect_args,
    statement_name,
    transaction_settings,
)
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application

POSTGRES = "postgresql+asyncpg://app@db/app"
TIMEOUTS_SETTINGS = "STATEMENT_TIMEOUT, LOCK_TIMEOUT, IDLE_IN_TRANSACTION_TIMEOUT"


def config(url: str = POSTGRES, **options: Any) -> DatabaseConfig:  # noqa: ANN401 - settings values.
    """Build a ``DATABASES`` entry."""
    return DatabaseConfig.model_validate({"URL": url, **options})


@pytest.fixture(scope="module")
def client_certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway self-signed certificate and key, made with openssl."""
    openssl = shutil.which("openssl")
    assert openssl, "openssl is needed to create a test certificate"
    folder = tmp_path_factory.mktemp("tls")
    cert, key = folder / "client.crt", folder / "client.key"
    subprocess.run(  # noqa: S603 - fixed arguments.
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=test",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.mark.parametrize(
    ("mode", "verify", "hostname"),
    [
        ("require", ssl.CERT_NONE, False),
        ("verify-ca", ssl.CERT_REQUIRED, False),
        ("verify-full", ssl.CERT_REQUIRED, True),
    ],
)
def test_tls_modes(mode: str, verify: ssl.VerifyMode, hostname: bool) -> None:
    """Each libpq mode verifies exactly what its name says."""
    context = client_ssl_context(config(SSLMODE=mode))

    assert context is not None
    assert (context.verify_mode, context.check_hostname) == (verify, hostname)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_tls_disabled_or_unset() -> None:
    """No context means plaintext (or the driver's own default)."""
    assert client_ssl_context(config()) is None
    assert client_ssl_context(config(SSLMODE="disable")) is None


def test_tls_files(client_certificate: tuple[Path, Path], tmp_path: Path) -> None:
    """CA bundles and client certificates load; bad files are configuration errors."""
    cert, key = client_certificate
    context = client_ssl_context(
        config(SSLMODE="verify-full", SSLROOTCERT=cert, SSLCERT=cert, SSLKEY=key)
    )
    assert context is not None

    with pytest.raises(ImproperlyConfigured) as missing:
        client_ssl_context(config(SSLMODE="verify-ca", SSLROOTCERT=tmp_path / "x"))
    assert str(missing.value) == "Cannot load TLS files (No such file or directory)."
    with pytest.raises(ValueError, match="SSLCERT and SSLKEY"):
        config(SSLCERT=cert)


def test_asyncpg_arguments() -> None:
    """The asyncpg driver gets an SSL context and timeouts in milliseconds."""
    args = connect_args(config(SSLMODE="verify-full", LOCK_TIMEOUT=None))

    assert isinstance(args["ssl"], ssl.SSLContext)
    assert args["server_settings"] == {
        "statement_timeout": "30000",
        "idle_in_transaction_session_timeout": "60000",
    }
    assert (args["timeout"], args["command_timeout"]) == (10.0, 35.0)
    assert connect_args(config(SSLMODE="disable"))["ssl"] is False
    plain = connect_args(
        config(
            STATEMENT_TIMEOUT=None, LOCK_TIMEOUT=None, IDLE_IN_TRANSACTION_TIMEOUT=None
        )
    )
    assert plain == {"timeout": 10.0}


def test_migrations_skip_timeouts() -> None:
    """Migrations keep TLS and the connect timeout, but may run long."""
    args = connect_args(config(SSLMODE="disable"), timeouts=False)

    assert args == {"ssl": False, "timeout": 10.0}


def test_psycopg_arguments(client_certificate: tuple[Path, Path]) -> None:
    """The psycopg driver gets libpq parameters and timeouts as options."""
    cert, key = client_certificate
    args = connect_args(
        config(
            "postgresql+psycopg://app@db/app",
            SSLMODE="verify-full",
            SSLROOTCERT=cert,
            SSLCERT=cert,
            SSLKEY=key,
        )
    )

    assert args["sslmode"] == "verify-full"
    assert args["sslrootcert"] == str(cert)
    assert args["sslcert"] == str(cert)
    assert args["sslkey"] == str(key)
    assert args["options"] == (
        "-c statement_timeout=30000 -c lock_timeout=10000 "
        "-c idle_in_transaction_session_timeout=60000"
    )
    assert args["connect_timeout"] == 10
    assert connect_args(config("postgresql+psycopg://a@b/c"), timeouts=False) == {
        "connect_timeout": 10
    }
    assert connect_args(config("postgresql+psycopg://a@b/c", SSLMODE="require")) == {
        "connect_timeout": 10,
        "sslmode": "require",
        "options": (
            "-c statement_timeout=30000 -c lock_timeout=10000 "
            "-c idle_in_transaction_session_timeout=60000"
        ),
    }


def test_other_drivers() -> None:
    """SQLite takes no TLS, and no timeouts; disabling TLS is no error."""
    assert connect_args(config("sqlite+aiosqlite:///x.db")) == {}
    assert connect_args(config("sqlite+aiosqlite:///x.db", SSLMODE="disable")) == {}
    with pytest.raises(ImproperlyConfigured, match="not supported by the 'aiosqlite'"):
        connect_args(config("sqlite+aiosqlite:///x.db", SSLMODE="require"))
    assert connect_args(config(), "sqlite+aiosqlite:///other.db") == {}


TIMEOUTS = {
    "statement_timeout": "30000",
    "lock_timeout": "10000",
    "idle_in_transaction_session_timeout": "60000",
}
PSYCOPG = "postgresql+psycopg://app@db/app"
IDLE = "idle_in_transaction_session_timeout"


def test_connect_args_add_to_the_derived_ones() -> None:
    """Extra arguments and server settings join TLS and the timeouts."""
    args = connect_args(
        config(
            SSLMODE="verify-full",
            CONNECT_ARGS={
                "max_cached_statement_lifetime": 5,
                "server_settings": {"jit": "off"},
            },
        )
    )
    libpq = connect_args(
        config(PSYCOPG, CONNECT_ARGS={"options": "-c jit=off", "application_name": "a"})
    )
    migrating = connect_args(
        config(CONNECT_ARGS={"server_settings": {"statement_timeout": "0"}}),
        timeouts=False,
    )

    assert isinstance(args["ssl"], ssl.SSLContext)
    assert args["max_cached_statement_lifetime"] == 5
    assert args["server_settings"] == {**TIMEOUTS, "jit": "off"}
    assert libpq == {
        "connect_timeout": 10,
        "options": (
            "-c statement_timeout=30000 -c lock_timeout=10000 "
            "-c idle_in_transaction_session_timeout=60000 -c jit=off"
        ),
        "application_name": "a",
    }
    # Without derived timeouts (migrations), nothing is replaced.
    assert migrating == {"server_settings": {"statement_timeout": "0"}, "timeout": 10}
    # Without SSLMODE nothing is derived for TLS, so CONNECT_ARGS may set it.
    assert connect_args(config(CONNECT_ARGS={"ssl": True}))["ssl"] is True


@pytest.mark.parametrize(
    ("url", "options", "refused"),
    [
        (
            POSTGRES,
            {"SSLMODE": "verify-full", "CONNECT_ARGS": {"ssl": False}},
            "ssl (from SSLMODE)",
        ),
        (
            POSTGRES,
            {
                "CONNECT_ARGS": {
                    "server_settings": {"statement_timeout": "0", "jit": "off"}
                }
            },
            "statement_timeout (from STATEMENT_TIMEOUT)",
        ),
        (
            POSTGRES,
            {"CONNECT_ARGS": {"server_settings": "statement_timeout=0"}},
            f"server_settings (from {TIMEOUTS_SETTINGS})",
        ),
        (
            PSYCOPG,
            {"SSLMODE": "verify-full", "CONNECT_ARGS": {"sslmode": "disable"}},
            "sslmode (from SSLMODE)",
        ),
        (
            PSYCOPG,
            {"CONNECT_ARGS": {"options": f"-c lock_timeout=0 -c {IDLE}=0"}},
            (
                f"{IDLE} (from IDLE_IN_TRANSACTION_TIMEOUT), "
                "lock_timeout (from LOCK_TIMEOUT)"
            ),
        ),
        (
            PSYCOPG,
            {"CONNECT_ARGS": {"options": ["-c", "statement_timeout=0"]}},
            f"options (from {TIMEOUTS_SETTINGS})",
        ),
        # PostgreSQL reads setting names in any case, and - as _.
        (
            POSTGRES,
            {"CONNECT_ARGS": {"server_settings": {"STATEMENT_TIMEOUT": "0"}}},
            "statement_timeout (from STATEMENT_TIMEOUT)",
        ),
        (
            POSTGRES,
            {"CONNECT_ARGS": {"server_settings": {"Lock-Timeout": "0"}}},
            "lock_timeout (from LOCK_TIMEOUT)",
        ),
        (
            PSYCOPG,
            {"CONNECT_ARGS": {"options": "-c STATEMENT_TIMEOUT=0"}},
            "statement_timeout (from STATEMENT_TIMEOUT)",
        ),
        (
            PSYCOPG,
            {"CONNECT_ARGS": {"options": "--lock-timeout=0"}},
            "lock_timeout (from LOCK_TIMEOUT)",
        ),
        (
            PSYCOPG,
            {"CONNECT_ARGS": {"options": f"-c{IDLE.upper()}=0"}},
            f"{IDLE} (from IDLE_IN_TRANSACTION_TIMEOUT)",
        ),
    ],
)
def test_connect_args_never_replace_tls_or_timeouts(
    url: str, options: dict[str, Any], refused: str
) -> None:
    """Turning TLS or a timeout off takes its setting, not CONNECT_ARGS."""
    with pytest.raises(ImproperlyConfigured) as replaced:
        connect_args(config(url, **options))

    assert str(replaced.value) == (
        "CONNECT_ARGS may not replace driver arguments derived from the settings: "
        f"{refused}. Change those settings instead."
    )


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "Infinity"])
def test_timeouts_are_finite(value: object) -> None:
    """An endless timeout is no timeout: it is refused, not overflowed later."""
    with pytest.raises(ValidationError, match="finite number"):
        config(STATEMENT_TIMEOUT=value)


def test_tls_files_cannot_be_replaced(client_certificate: tuple[Path, Path]) -> None:
    """A CA set by SSLROOTCERT stays; one CONNECT_ARGS adds alone is fine."""
    cert, _ = client_certificate
    pinned = config(PSYCOPG, SSLMODE="verify-full", SSLROOTCERT=cert)
    unpinned = config(
        PSYCOPG, SSLMODE="verify-full", CONNECT_ARGS={"sslrootcert": "/ca.pem"}
    )

    with pytest.raises(ImproperlyConfigured, match=r"sslrootcert \(from SSLROOTCERT\)"):
        connect_args(pinned.model_copy(update={"CONNECT_ARGS": {"sslrootcert": "/x"}}))
    assert connect_args(unpinned)["sslrootcert"] == "/ca.pem"


def test_applications_refuse_bad_driver_arguments() -> None:
    """The refusal comes when the application is built, before any query."""
    weakened = {"URL": POSTGRES, "SSLMODE": "verify-full", "CONNECT_ARGS": {"ssl": 0}}
    with (
        override_settings(DATABASES={"default": weakened}),
        pytest.raises(ImproperlyConfigured, match=r"ssl \(from SSLMODE\)"),
    ):
        get_asgi_application()


# Client-side timeouts and PgBouncer ---------------------------------------------


def test_the_driver_waits_a_bounded_time_to_connect_and_per_statement() -> None:
    """CONNECT_TIMEOUT, and COMMAND_TIMEOUT or STATEMENT_TIMEOUT plus a margin."""
    assert connect_args(config(CONNECT_TIMEOUT=2.5))["timeout"] == 2.5
    assert connect_args(config(COMMAND_TIMEOUT=7.0))["command_timeout"] == 7.0
    assert connect_args(config(STATEMENT_TIMEOUT=60.0))["command_timeout"] == 65.0
    assert "command_timeout" not in connect_args(config(STATEMENT_TIMEOUT=None))
    assert command_timeout(config(STATEMENT_TIMEOUT=None, COMMAND_TIMEOUT=3.0)) == 3.0
    assert connect_args(config(PSYCOPG, CONNECT_TIMEOUT=2.5))["connect_timeout"] == 3


def test_timeouts_of_a_millisecond_or_less_never_become_zero() -> None:
    """PostgreSQL reads 0 as no timeout at all."""
    args = connect_args(config(STATEMENT_TIMEOUT=0.0001, LOCK_TIMEOUT=0.0004))

    assert args["server_settings"]["statement_timeout"] == "1"
    assert args["server_settings"]["lock_timeout"] == "1"


@pytest.mark.parametrize("name", ["timeout", "command_timeout"])
def test_connect_args_still_set_the_client_timeouts_deprecated(name: str) -> None:
    """They were driver arguments before they were settings; they warn now."""
    with pytest.warns(DomainkitDeprecationWarning, match=f"CONNECT_ARGS\\['{name}'\\]"):
        args = connect_args(config(CONNECT_ARGS={name: 99}))

    assert args[name] == 99
    with pytest.warns(DomainkitDeprecationWarning, match="CONNECT_TIMEOUT instead"):
        libpq = connect_args(config(PSYCOPG, CONNECT_ARGS={"connect_timeout": 9}))
    assert libpq["connect_timeout"] == 9


def test_pgbouncer_mode_keeps_nothing_in_the_session() -> None:
    """No statement cache, unique statement names, and no startup settings."""
    args = connect_args(config(PGBOUNCER=True, SSLMODE="disable"))
    libpq = connect_args(config(PSYCOPG, PGBOUNCER=True))

    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    name = args["prepared_statement_name_func"]
    assert name is statement_name
    assert name() != name()
    assert name().startswith("__domainkit_")
    assert "server_settings" not in args
    assert args["command_timeout"] == 35.0
    assert libpq == {"connect_timeout": 10, "prepare_threshold": None}
    migrating = connect_args(config(PGBOUNCER=True), timeouts=False)
    assert migrating["statement_cache_size"] == 0
    assert "command_timeout" not in migrating


@pytest.mark.parametrize(
    ("url", "argument"),
    [
        (POSTGRES, "statement_cache_size"),
        (POSTGRES, "prepared_statement_cache_size"),
        (POSTGRES, "prepared_statement_name_func"),
        (PSYCOPG, "prepare_threshold"),
    ],
)
def test_pgbouncer_arguments_cannot_be_replaced(url: str, argument: str) -> None:
    """Turning the statement cache back on would break transaction pooling."""
    with pytest.raises(ImproperlyConfigured, match=f"{argument} \\(from PGBOUNCER\\)"):
        connect_args(config(url, PGBOUNCER=True, CONNECT_ARGS={argument: 100}))


def test_pgbouncer_needs_postgresql() -> None:
    """SQLite has no PgBouncer."""
    with pytest.raises(ValidationError, match="PGBOUNCER needs a postgresql:// URL"):
        config("sqlite+aiosqlite:///x.db", PGBOUNCER=True)
    assert config(PSYCOPG, PGBOUNCER=True).PGBOUNCER


def test_transactions_set_the_time_left_and_pgbouncers_timeouts() -> None:
    """statement_timeout drops to the deadline; under PgBouncer, all are set."""
    plain = config()
    pooled = config(PGBOUNCER=True)

    assert transaction_settings(plain, None) == {}
    assert transaction_settings(plain, 5.0) == {"statement_timeout": "5000"}
    assert transaction_settings(plain, 0.0004) == {"statement_timeout": "1"}
    # Within DEADLINE_SLACK of STATEMENT_TIMEOUT: the server's own will do.
    assert transaction_settings(plain, 30.0 - DEADLINE_SLACK) == {
        "statement_timeout": "29000"
    }
    assert transaction_settings(plain, 29.5) == {}
    # A passed deadline changes nothing: cleanup after it must still run.
    assert transaction_settings(plain, 0.0) == {}
    assert transaction_settings(config(STATEMENT_TIMEOUT=None), 120.0) == {
        "statement_timeout": "120000"
    }
    assert transaction_settings(pooled, None) == TIMEOUTS
    assert transaction_settings(pooled, 29.5) == {
        **TIMEOUTS,
        "statement_timeout": "29500",
    }
    assert transaction_settings(pooled, 0.0) == TIMEOUTS


def test_some_databases_look_like_pgbouncer() -> None:
    """Port 6432, or a statement cache turned off by hand."""
    assert config("postgresql+asyncpg://app@pool:6432/app").looks_pooled()
    assert config(CONNECT_ARGS={"statement_cache_size": 0}).looks_pooled()
    assert config(PSYCOPG, CONNECT_ARGS={"prepare_threshold": None}).looks_pooled()
    assert not config(CONNECT_ARGS={"statement_cache_size": 100}).looks_pooled()
    assert not config().looks_pooled()
    assert not config(
        "postgresql+asyncpg://app@pool:6432/app", PGBOUNCER=True
    ).looks_pooled()
    assert not config("postgresql+asyncpg://app@db:port/app").looks_pooled()
