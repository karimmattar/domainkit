"""Translate ``DATABASES`` options into arguments for each async driver.

TLS uses libpq's mode names for every driver. PostgreSQL connections also get
server-side timeouts, so a slow query or an abandoned transaction cannot hold
a pooled connection forever. ``CONNECT_ARGS`` adds driver arguments but never
replaces these: an override could turn TLS or the timeouts off unseen.
"""

import re
import ssl
from collections.abc import Mapping, Set
from typing import Any, Final

from sqlalchemy.engine import make_url

from domainkit.conf import DatabaseConfig
from domainkit.exceptions import ImproperlyConfigured

TIMEOUTS: Final = "STATEMENT_TIMEOUT, LOCK_TIMEOUT, IDLE_IN_TRANSACTION_TIMEOUT"
SOURCES: Final = {
    "ssl": "SSLMODE",
    "sslmode": "SSLMODE",
    "sslrootcert": "SSLROOTCERT",
    "sslcert": "SSLCERT",
    "sslkey": "SSLKEY",
    "server_settings": TIMEOUTS,
    "options": TIMEOUTS,
    "statement_timeout": "STATEMENT_TIMEOUT",
    "lock_timeout": "LOCK_TIMEOUT",
    "idle_in_transaction_session_timeout": "IDLE_IN_TRANSACTION_TIMEOUT",
}
"""The settings behind each driver argument and server setting derived here."""
_OPTION: Final = re.compile(r"(?:-c\s*|--)([\w.-]+)=")
"""A server setting in libpq ``options``, as PostgreSQL's command line takes it."""


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
    any request should. ``CONNECT_ARGS`` is added to the derived arguments.
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
    return _add(derived, config.CONNECT_ARGS)


def _add(derived: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Add ``extra`` (``CONNECT_ARGS``) to ``derived``, replacing nothing.

    asyncpg's ``server_settings`` and libpq's ``options`` are combined with
    the derived timeouts; any other derived argument may not be set again.
    """
    combined = {**extra, **derived}
    for name in derived.keys() & extra.keys():
        added = extra[name]
        if name == "server_settings" and isinstance(added, Mapping):
            _refuse(derived[name].keys() & {_setting(key) for key in added})
            combined[name] = {**derived[name], **added}
        elif name == "options" and isinstance(added, str):
            _refuse(_option_settings(derived[name]) & _option_settings(added))
            combined[name] = f"{derived[name]} {added}"
        else:
            _refuse({name})
    return combined


def _setting(name: str) -> str:
    """A PostgreSQL setting's name as the server reads it: any case, - as _."""
    return name.lower().replace("-", "_")


def _option_settings(options: str) -> set[str]:
    """The settings libpq ``options`` set: ``-c name=``, ``-cname=``, ``--name=``."""
    return {_setting(name) for name in _OPTION.findall(options)}


def _refuse(names: Set[str]) -> None:
    """Raise if ``CONNECT_ARGS`` sets ``names``, which settings already set."""
    if names:
        sets = ", ".join(f"{name} (from {SOURCES[name]})" for name in sorted(names))
        raise ImproperlyConfigured(
            f"CONNECT_ARGS may not replace driver arguments derived from the "
            f"settings: {sets}. Change those settings instead."
        )


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
