"""Driver arguments: TLS contexts and server-side timeouts."""

import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest

from domainkit.conf import DatabaseConfig
from domainkit.db.drivers import client_ssl_context, connect_args
from domainkit.exceptions import ImproperlyConfigured

POSTGRES = "postgresql+asyncpg://app@db/app"


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

    with pytest.raises(ImproperlyConfigured, match="Cannot load TLS files"):
        client_ssl_context(config(SSLMODE="verify-ca", SSLROOTCERT=tmp_path / "x"))
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


def test_other_drivers_and_overrides() -> None:
    """SQLite takes no TLS; explicit CONNECT_ARGS always win."""
    assert connect_args(config("sqlite+aiosqlite:///x.db")) == {}
    with pytest.raises(ImproperlyConfigured, match="not supported by the 'aiosqlite'"):
        connect_args(config("sqlite+aiosqlite:///x.db", SSLMODE="require"))

    args = connect_args(config(CONNECT_ARGS={"server_settings": {"jit": "off"}}))
    assert args["server_settings"] == {"jit": "off"}
    assert connect_args(config(), "sqlite+aiosqlite:///other.db") == {}
