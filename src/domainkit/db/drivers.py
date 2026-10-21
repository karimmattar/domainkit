"""Translate ``DATABASES`` options into arguments for each async driver.

TLS uses libpq's mode names for every driver. PostgreSQL connections also get
server-side timeouts, so a slow query or an abandoned transaction cannot hold
a pooled connection forever, and client-side ones: asyncpg waits at most
``CONNECT_TIMEOUT`` to connect and ``COMMAND_TIMEOUT`` for a statement.
``CONNECT_ARGS`` adds driver arguments but never replaces these: an override
could turn TLS or the timeouts off unseen.

Behind PgBouncer's transaction pooling (``PGBOUNCER``) no session keeps
anything: statements are not cached, each prepared statement gets a name of
its own, and the timeouts are set in each transaction (``SET LOCAL``) by
``transaction_settings``, which also lowers ``statement_timeout`` to the
time left before the request's deadline.
"""

import math
import re
import ssl
import warnings
from collections.abc import Mapping, Set
from functools import cache
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from sqlalchemy.engine import make_url

from domainkit.conf import DatabaseConfig
from domainkit.deprecation import DomainkitDeprecationWarning
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
    "statement_cache_size": "PGBOUNCER",
    "prepared_statement_cache_size": "PGBOUNCER",
    "prepared_statement_name_func": "PGBOUNCER",
    "prepare_threshold": "PGBOUNCER",
}
"""The settings behind each driver argument and server setting derived here."""
REPLACED: Final = {
    "timeout": "CONNECT_TIMEOUT",
    "connect_timeout": "CONNECT_TIMEOUT",
    "command_timeout": "COMMAND_TIMEOUT",
}
"""Arguments ``CONNECT_ARGS`` may still set, deprecated, and their settings."""
COMMAND_TIMEOUT_MARGIN: Final = 5.0
"""Seconds asyncpg waits beyond ``STATEMENT_TIMEOUT``, so the server ends a
slow statement first, with an error that keeps the connection usable."""
DEADLINE_SLACK: Final = 1.0
"""Seconds a deadline must undercut ``STATEMENT_TIMEOUT`` by before a
transaction lowers it. Requests in their first second skip that round trip;
the server's timeout then ends their statements at most this late."""
MAX_MILLISECONDS: Final = 2_147_483_647
"""PostgreSQL's longest timeout, in milliseconds (about 24.8 days): a
longer one is refused, and the transaction that sets it fails."""
_OPTION: Final = re.compile(r"(?:-c\s*|--)([\w.-]+)=")
"""A server setting in libpq ``options``, as PostgreSQL's command line takes it."""


def client_ssl_context(config: DatabaseConfig) -> ssl.SSLContext | None:
    """The client TLS context for ``SSLMODE``; None means plaintext.

    ``require`` encrypts without verifying anything, ``verify-ca`` checks the
    certificate chain, and ``verify-full`` also checks the host name. It is
    built once per process for each mode and set of files, since reading
    the system's CA store and the files takes milliseconds:
    ``get_asgi_application`` builds it before the server's loop serves.
    """
    if config.SSLMODE in {None, "disable"}:
        return None
    return _ssl_context(
        config.SSLMODE, config.SSLROOTCERT, config.SSLCERT, config.SSLKEY
    )


@cache
def _ssl_context(
    mode: str, rootcert: Path | None, cert: Path | None, key: Path | None
) -> ssl.SSLContext:
    """Build the context ``client_ssl_context`` describes (once, then cached)."""
    try:
        if mode == "require":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context(cafile=rootcert)
            context.check_hostname = mode == "verify-full"
        if cert is not None:
            context.load_cert_chain(cert, key)
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
    any request should, so only the connect timeout applies to them.
    ``CONNECT_ARGS`` is added to the derived arguments.
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
    the derived timeouts. The connect and command timeouts, which
    ``CONNECT_ARGS`` set before they were settings, still replace theirs,
    with a deprecation warning. Any other derived argument may not be set
    again.
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
        elif name in REPLACED:
            warnings.warn(
                f"CONNECT_ARGS[{name!r}] is deprecated; set {REPLACED[name]} "
                f"instead. It is refused from domainkit 0.9.",
                DomainkitDeprecationWarning,
                stacklevel=3,
            )
            combined[name] = added
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
    """Return PostgreSQL timeout settings in milliseconds.

    Never 0, which PostgreSQL reads as no timeout at all.
    """
    seconds = {
        "statement_timeout": config.STATEMENT_TIMEOUT,
        "lock_timeout": config.LOCK_TIMEOUT,
        "idle_in_transaction_session_timeout": config.IDLE_IN_TRANSACTION_TIMEOUT,
    }
    return {
        name: str(max(round(value * 1000), 1))
        for name, value in seconds.items()
        if value
    }


def _asyncpg(config: DatabaseConfig, *, timeouts: bool) -> dict[str, Any]:
    """Arguments for ``asyncpg.connect``, and SQLAlchemy's statement cache."""
    args: dict[str, Any] = {"timeout": config.CONNECT_TIMEOUT}
    if config.SSLMODE is not None:
        args["ssl"] = client_ssl_context(config) or False
    if timeouts:
        if (command := command_timeout(config)) is not None:
            args["command_timeout"] = command
        if not config.PGBOUNCER and (server_settings := _server_settings(config)):
            args["server_settings"] = server_settings
    if config.PGBOUNCER:
        args["statement_cache_size"] = 0
        args["prepared_statement_cache_size"] = 0
        args["prepared_statement_name_func"] = statement_name
    return args


def _psycopg(config: DatabaseConfig, *, timeouts: bool) -> dict[str, Any]:
    """Arguments for ``psycopg.AsyncConnection.connect`` (libpq parameters)."""
    args: dict[str, Any] = {"connect_timeout": math.ceil(config.CONNECT_TIMEOUT)}
    if config.SSLMODE is not None:
        args["sslmode"] = config.SSLMODE
        files = {
            "sslrootcert": config.SSLROOTCERT,
            "sslcert": config.SSLCERT,
            "sslkey": config.SSLKEY,
        }
        args.update({key: str(path) for key, path in files.items() if path})
    if (
        timeouts
        and not config.PGBOUNCER
        and (server_settings := _server_settings(config))
    ):
        args["options"] = " ".join(
            f"-c {name}={value}" for name, value in server_settings.items()
        )
    if config.PGBOUNCER:
        args["prepare_threshold"] = None
    return args


def command_timeout(config: DatabaseConfig) -> float | None:
    """Seconds asyncpg waits for one statement: ``COMMAND_TIMEOUT``.

    Unset, it is ``STATEMENT_TIMEOUT`` plus ``COMMAND_TIMEOUT_MARGIN``, so
    the server's own timeout ends a slow statement first; None without
    either.
    """
    if config.COMMAND_TIMEOUT is not None:
        return config.COMMAND_TIMEOUT
    if config.STATEMENT_TIMEOUT is None:
        return None
    return config.STATEMENT_TIMEOUT + COMMAND_TIMEOUT_MARGIN


def statement_name() -> str:
    """A prepared statement's name, unique among PgBouncer's other clients."""
    return f"__domainkit_{uuid4().hex}__"


def transaction_settings(
    config: DatabaseConfig, remaining: float | None
) -> dict[str, str]:
    """The settings a PostgreSQL transaction sets for itself, in milliseconds.

    ``statement_timeout`` drops to the seconds ``remaining`` before the
    deadline when they are ``DEADLINE_SLACK`` shorter than
    ``STATEMENT_TIMEOUT``, or more, and at most ``MAX_MILLISECONDS``, the
    longest PostgreSQL takes. Under ``PGBOUNCER`` every timeout is set
    here, as no session keeps it, and the deadline counts to the
    millisecond. A deadline that has passed changes nothing: its work is
    being cancelled already, and the cleanup after it must still run.
    """
    values = _server_settings(config) if config.PGBOUNCER else {}
    if remaining is None or remaining <= 0:
        return values
    statement = config.STATEMENT_TIMEOUT
    slack = 0.0 if config.PGBOUNCER else DEADLINE_SLACK
    if statement is None or remaining <= statement - slack:
        milliseconds = min(remaining * 1000, MAX_MILLISECONDS)
        values["statement_timeout"] = str(math.ceil(milliseconds))
    return values
