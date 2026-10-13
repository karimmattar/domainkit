"""Translate ``DATABASES`` options into arguments for each async driver.

TLS uses libpq's mode names for every driver. PostgreSQL connections also get
server-side timeouts, so a slow query or an abandoned transaction cannot hold
a pooled connection forever. Explicit ``CONNECT_ARGS`` win over both.
"""

import ssl
from typing import Any

from sqlalchemy.engine import make_url

from domainkit.conf import DatabaseConfig
from domainkit.exceptions import ImproperlyConfigured


def client_ssl_context(config: DatabaseConfig) -> ssl.SSLContext | None:
    """Build a client TLS context for ``SSLMODE``; None means plaintext.

    ``require`` encrypts without verifying anything, ``verify-ca`` checks the
    certificate chain, and ``verify-full`` also checks the host name.
    """
    if config.SSLMODE in {None, "disable"}:
        return None
    try:
        if config.SSLMODE == "require":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context(cafile=config.SSLROOTCERT)
            context.check_hostname = config.SSLMODE == "verify-full"
        if config.SSLCERT is not None:
            context.load_cert_chain(config.SSLCERT, config.SSLKEY)
    except OSError as exc:
        raise ImproperlyConfigured(
            f"Cannot load TLS files ({exc.strerror or exc})."
        ) from exc
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def connect_args(
    config: DatabaseConfig, url: str | None = None, *, timeouts: bool = True
) -> dict[str, Any]:
    """Return driver arguments for ``url`` (default: the entry's ``URL``).

    Migrations pass ``timeouts=False``: schema changes may run longer than
    any request should.
    """
    driver = make_url(url or config.URL.get_secret_value()).get_driver_name()
    if driver == "asyncpg":
        derived = _asyncpg(config, timeouts=timeouts)
    elif driver == "psycopg":
        derived = _psycopg(config, timeouts=timeouts)
    elif config.SSLMODE not in {None, "disable"}:
        raise ImproperlyConfigured(
            f"SSLMODE is not supported by the {driver!r} driver."
        )
    else:
        derived = {}
    return {**derived, **config.CONNECT_ARGS}


def _server_settings(config: DatabaseConfig) -> dict[str, str]:
    """Return PostgreSQL timeout settings in milliseconds."""
    seconds = {
        "statement_timeout": config.STATEMENT_TIMEOUT,
        "lock_timeout": config.LOCK_TIMEOUT,
        "idle_in_transaction_session_timeout": config.IDLE_IN_TRANSACTION_TIMEOUT,
    }
    return {name: str(round(value * 1000)) for name, value in seconds.items() if value}


def _asyncpg(config: DatabaseConfig, *, timeouts: bool) -> dict[str, Any]:
    """Arguments for ``asyncpg.connect``."""
    args: dict[str, Any] = {}
    if config.SSLMODE is not None:
        args["ssl"] = client_ssl_context(config) or False
    if timeouts and (server_settings := _server_settings(config)):
        args["server_settings"] = server_settings
    return args


def _psycopg(config: DatabaseConfig, *, timeouts: bool) -> dict[str, Any]:
    """Arguments for ``psycopg.AsyncConnection.connect`` (libpq parameters)."""
    args: dict[str, Any] = {}
    if config.SSLMODE is not None:
        args["sslmode"] = config.SSLMODE
        files = {
            "sslrootcert": config.SSLROOTCERT,
            "sslcert": config.SSLCERT,
            "sslkey": config.SSLKEY,
        }
        args.update({key: str(path) for key, path in files.items() if path})
    if timeouts and (server_settings := _server_settings(config)):
        args["options"] = " ".join(
            f"-c {name}={value}" for name, value in server_settings.items()
        )
    return args
