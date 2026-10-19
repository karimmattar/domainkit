"""Driver arguments: TLS contexts and server-side timeouts."""

import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from domainkit.conf import DatabaseConfig, override_settings
from domainkit.db.drivers import client_ssl_context, connect_args
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
    assert connect_args(config(SSLMODE="disable"))["ssl"] is False
    plain = connect_args(
        config(
            STATEMENT_TIMEOUT=None, LOCK_TIMEOUT=None, IDLE_IN_TRANSACTION_TIMEOUT=None
        )
    )
    assert plain == {}


def test_migrations_skip_timeouts() -> None:
    """Migrations keep TLS but may run longer than requests."""
    args = connect_args(config(SSLMODE="disable"), timeouts=False)

    assert args == {"ssl": False}


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
    assert connect_args(config("postgresql+psycopg://a@b/c"), timeouts=False) == {}
    assert connect_args(config("postgresql+psycopg://a@b/c", SSLMODE="require")) == {
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
            CONNECT_ARGS={"command_timeout": 5, "server_settings": {"jit": "off"}},
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
    assert args["command_timeout"] == 5
    assert args["server_settings"] == {**TIMEOUTS, "jit": "off"}
    assert libpq == {
        "options": (
            "-c statement_timeout=30000 -c lock_timeout=10000 "
            "-c idle_in_transaction_session_timeout=60000 -c jit=off"
        ),
        "application_name": "a",
    }
    # Without derived timeouts (migrations), nothing is replaced.
    assert migrating == {"server_settings": {"statement_timeout": "0"}}
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
