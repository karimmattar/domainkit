"""Framework settings and their defaults (Django's ``global_settings``).

Every UPPERCASE name in the selected settings module is validated here. A name
the framework does not declare must belong to an installed domain (see
``DomainConfig.settings_class``) or to the project's own ``SETTINGS_CLASS``,
so a misspelled setting fails at startup instead of being silently ignored.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal, NamedTuple, Self
from urllib.parse import SplitResult, parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyNetwork,
    SecretStr,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from domainkit.architecture import LAYERS
from domainkit.checks import (
    MAX_SILENCED_CHECKS,
    CheckFailed,
    CheckMessage,
    validate_silenced,
)
from domainkit.conf.env import project_root
from domainkit.utils.rates import parse_rate
from domainkit.utils.redis_urls import REDIS_SCHEMES as REDIS_SCHEMES
from domainkit.utils.redis_urls import SENTINEL_SCHEMES, parse_redis_url

DEFAULT_DB_ALIAS: Final = "default"
HIDDEN: Final = "**********"
"""What ``repr`` and dumps of the settings show for domain settings."""

Environment = Literal["local", "test", "staging", "production"]
DEPLOYED_ENVIRONMENTS: Final = frozenset({"staging", "production"})
"""Environments held to the deployment checks in ``Settings``."""

SECRET_KEY_MIN_LENGTH: Final = 50
SECRET_KEY_MIN_CHARACTERS: Final = 5
"""Different characters a deployed ``SECRET_KEY`` needs, at the least."""
DEVELOPMENT_KEY_PREFIXES: Final = ("domainkit-insecure-", "django-insecure-")
"""Start keys made for development; staging and production refuse them."""
LANGUAGE_PATTERN: Final = r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$"
"""A language code in settings: lower case, as ``ar`` or ``pt-br``."""

MAX_REAUTH_INTERVAL: Final = 3600.0
"""The longest an open live connection may go between re-checks, in seconds."""

RATE_LIMIT_MIDDLEWARE: Final = "domainkit.http.ratelimit.RateLimitMiddleware"
DEBUG_TOOLBAR_MIDDLEWARE: Final = "domainkit.debug.DebugToolbarMiddleware"
"""Development middleware: SQL per request, N+1 warnings, profiles."""
DEADLINE_MIDDLEWARE: Final = "domainkit.http.deadlines.DeadlineMiddleware"
"""Gives each request its ``REQUEST_TIMEOUT`` deadline."""
CONCURRENCY_MIDDLEWARE: Final = "domainkit.http.shedding.ConcurrencyLimitMiddleware"
"""Sheds requests past ``MAX_CONCURRENT_REQUESTS``."""
DEFAULT_MIDDLEWARE: Final = (
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    DEADLINE_MIDDLEWARE,
    CONCURRENCY_MIDDLEWARE,
    "domainkit.http.middleware.AllowedHostsMiddleware",
    "domainkit.http.websockets.WebSocketLimitsMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
)


def _minimal_logging() -> dict[str, Any]:
    """Leave existing logging untouched when a module does not define LOGGING."""
    return {"version": 1, "disable_existing_loggers": False}


SSLMode = Literal["disable", "require", "verify-ca", "verify-full"]
"""libpq TLS modes, minus ``allow`` and ``prefer`` (they fall back to plaintext)."""


def is_network_database(url: str) -> bool:
    """Report whether ``url`` reaches a server over the network (not SQLite)."""
    return not url.split(":", 1)[0].split("+", 1)[0].startswith("sqlite")


LEDGER_SCHEMES: Final = frozenset({"immudb", "memory"})
"""URL schemes of ledgers (``LEDGERS``); ``DATABASES`` refuses them."""
IMMUDB_PORT: Final = 3322
PGBOUNCER_PORT: Final = 6432
"""The port PgBouncer listens on unless told otherwise."""
PGBOUNCER_CACHE_ARGUMENTS: Final = (
    "statement_cache_size",
    "prepared_statement_cache_size",
    "prepare_threshold",
)
"""Driver arguments that turn prepared statements off, as PgBouncer needed."""
DEFAULT_POOL_SIZE: Final = 5
DEFAULT_MAX_OVERFLOW: Final = 10
"""SQLAlchemy's pool, when ``POOL_SIZE`` and ``MAX_OVERFLOW`` are unset."""
MAX_DATABASE_TIMEOUT: Final = 2_147_483.647
"""Seconds: PostgreSQL's longest timeout, 2147483647 milliseconds (24.8 days)."""
MAX_REQUEST_SECONDS: Final = 3600.0
"""The most seconds a request, its body, an outgoing call, or a circuit's
cooldown may be given: an hour. Longer work belongs in a task."""
MAX_BACKOFF_SECONDS: Final = 600.0
"""The longest wait before an outgoing call's retry: ten minutes."""
MAX_RESPONSE_SIZE: Final = 1_073_741_824
"""The largest answer an outgoing call may be allowed to read: 1 GiB."""
MAX_REQUESTS_AT_ONCE: Final = 100_000
"""The most requests an event loop may admit, or queue, at once."""
MAX_LOOKUP_THREADS: Final = 256
"""The most threads the URL guard may look host names up on (each holds a stack)."""


class DatabaseConfig(BaseModel):
    """One ``DATABASES`` entry: an SQLAlchemy async URL plus connection options."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    URL: SecretStr
    """``dialect+driver://user:password@host/name``; hidden in reprs and errors."""
    MIGRATION_URL: SecretStr | None = None
    """Schema-owner credentials used by ``migrate``; defaults to ``URL``."""
    ECHO: bool = False
    """Log every statement (development only)."""
    LOG_PARAMETERS: bool = False
    """Show bound values in SQL logs and error messages; they may hold secrets."""

    SSLMODE: SSLMode | None = None
    """TLS mode; None keeps the driver default. Deployments need ``verify-full``."""
    SSLROOTCERT: Path | None = None
    """CA bundle that signed the server certificate; None uses the system CAs."""
    SSLCERT: Path | None = None
    """Client certificate for mutual TLS; requires ``SSLKEY``."""
    SSLKEY: Path | None = None

    STATEMENT_TIMEOUT: float | None = Field(default=30.0, gt=0, le=MAX_DATABASE_TIMEOUT)
    """Seconds before PostgreSQL cancels a statement; migrations are exempt."""
    LOCK_TIMEOUT: float | None = Field(default=10.0, gt=0, le=MAX_DATABASE_TIMEOUT)
    """Seconds a statement may wait for a lock."""
    IDLE_IN_TRANSACTION_TIMEOUT: float | None = Field(
        default=60.0, gt=0, le=MAX_DATABASE_TIMEOUT
    )
    """Seconds an open transaction may sit idle before the server ends it."""
    CONNECT_TIMEOUT: float = Field(default=10.0, gt=0, le=MAX_DATABASE_TIMEOUT)
    """Seconds to open a connection (asyncpg's ``timeout``, libpq's)."""
    COMMAND_TIMEOUT: float | None = Field(default=None, gt=0, le=MAX_DATABASE_TIMEOUT)
    """Seconds asyncpg waits for one statement; None: ``STATEMENT_TIMEOUT`` plus 5."""
    PGBOUNCER: bool = False
    """Connect through PgBouncer's transaction pooling (no session state kept)."""

    POOL_SIZE: int | None = Field(default=None, ge=1)
    MAX_OVERFLOW: int | None = Field(default=None, ge=0)
    POOL_TIMEOUT: float | None = Field(default=None, gt=0)
    POOL_RECYCLE: int | None = Field(default=None, gt=0)
    POOL_PRE_PING: bool = True
    CONNECT_ARGS: dict[str, Any] = Field(default_factory=dict)
    """Extra driver arguments; they may not replace TLS or the timeouts above."""
    REPLICAS: tuple[str, ...] = ()
    """Aliases of read replicas of this database; read-only work goes there."""
    TEST_NAME: str | None = None
    """The PostgreSQL database the tests create (``test_<name>`` when None)."""

    @model_validator(mode="after")
    def check_client_certificate(self) -> Self:
        """Keep the client certificate and key paired."""
        if (self.SSLCERT is None) != (self.SSLKEY is None):
            raise ValueError("SSLCERT and SSLKEY must be set together")
        return self

    @model_validator(mode="after")
    def check_not_a_ledger(self) -> Self:
        """Point ledger URLs to ``LEDGERS``, where they belong."""
        scheme = urlsplit(self.URL.get_secret_value()).scheme.split("+", 1)[0]
        if scheme in LEDGER_SCHEMES:
            raise ValueError(f"{scheme}:// is a ledger; configure it in LEDGERS")
        return self

    @model_validator(mode="after")
    def check_pgbouncer(self) -> Self:
        """PgBouncer pools PostgreSQL connections only."""
        scheme = urlsplit(self.URL.get_secret_value()).scheme.split("+", 1)[0]
        if self.PGBOUNCER and scheme != "postgresql":
            raise ValueError("PGBOUNCER needs a postgresql:// URL")
        return self

    def looks_pooled(self) -> bool:
        """Whether the entry seems to go through PgBouncer without ``PGBOUNCER``.

        PgBouncer listens on 6432 by default, and a statement cache turned
        off in ``CONNECT_ARGS`` is the usual way around it by hand.
        """
        if self.PGBOUNCER:
            return False
        try:
            port = urlsplit(self.URL.get_secret_value()).port
        except ValueError:
            port = None
        args = self.CONNECT_ARGS
        uncached = any(
            name in args and not args[name] for name in PGBOUNCER_CACHE_ARGUMENTS
        )
        return port == PGBOUNCER_PORT or uncached


def _check_port(parts: SplitResult) -> None:
    """Refuse a URL whose port is not a number, without quoting it.

    A ``/`` in the password ends the host early, so the port Python reads is
    part of the password; its own error would quote it.
    """
    try:
        parts.port  # noqa: B018 - reading it parses it.
    except ValueError:
        raise ValueError(
            "the URL's port is not a number; percent-encode / ? # @ and : in "
            "the user name and password"
        ) from None


class LedgerConfig(BaseModel):
    """One ``LEDGERS`` entry: write once, read many, verified on the client.

    Ledgers hold evidence (see ``domainkit.contrib.evidence``). They are never
    migrated and never take part in a unit of work.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    URL: SecretStr
    """``immudb://user:password@host:3322/database``, or ``memory://`` for tests."""
    TLS_CA: Path | None = None
    """CA certificate that signed the server's; setting it enables TLS."""
    SERVER_SIGNING_KEY: Path | None = None
    """Public key the server signs its states with (immudb ``--signingKey``)."""
    TIMEOUT: float = Field(default=10.0, gt=0)
    """Seconds each ledger call may take."""

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Accept immudb URLs with a host, or the in-memory ledger."""
        parts = urlsplit(self.URL.get_secret_value())
        if parts.scheme not in LEDGER_SCHEMES:
            raise ValueError("a ledger URL starts with immudb:// or memory://")
        _check_port(parts)
        if parts.scheme == "immudb" and not parts.hostname:
            raise ValueError("an immudb URL needs a host: immudb://user:pw@host/db")
        return self

    @property
    def scheme(self) -> str:
        """``immudb`` or ``memory``."""
        return urlsplit(self.URL.get_secret_value()).scheme


class ShardConfig(BaseModel):
    """One ``SHARDS`` group: SQL databases that share a schema, split by key.

    A shard key hashes to one of ``BUCKETS`` buckets, and each bucket belongs
    to one database. Moving buckets between databases is how a group grows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DATABASES: tuple[str, ...] = Field(min_length=1)
    """The group's SQL aliases; ``makemigrations`` compares against the first."""
    BUCKETS: int = Field(default=4096, ge=1, le=1_048_576)
    """Number of hash buckets. Never change it once the group holds data."""
    RANGES: dict[str, tuple[tuple[int, int], ...]] = Field(default_factory=dict)
    """Inclusive bucket ranges per alias; default: equal ranges in list order."""
    PINNED: dict[str, str] = Field(default_factory=dict)
    """Shard keys (as text) always routed to an alias, such as a large tenant."""

    @model_validator(mode="after")
    def check_layout(self) -> Self:
        """Every bucket has exactly one owner, and pins stay inside the group."""
        if len(set(self.DATABASES)) != len(self.DATABASES):
            raise ValueError("a shard group lists an alias twice")
        if len(self.DATABASES) > self.BUCKETS:
            raise ValueError("BUCKETS must be at least the number of DATABASES")
        if outside := sorted(set(self.PINNED.values()) - set(self.DATABASES)):
            raise ValueError(f"PINNED names aliases outside the group: {outside}")
        if not self.RANGES:
            return self
        if set(self.RANGES) != set(self.DATABASES):
            raise ValueError("RANGES must give ranges for exactly the group's aliases")
        owned = sorted(
            (low, high) for ranges in self.RANGES.values() for low, high in ranges
        )
        expected = 0
        for low, high in owned:
            if low != expected or high < low:
                raise ValueError(
                    f"RANGES must cover buckets 0-{self.BUCKETS - 1} once each; "
                    f"bucket {expected} is missing or overlapped"
                )
            expected = high + 1
        if expected != self.BUCKETS:
            raise ValueError(
                f"RANGES must cover buckets 0-{self.BUCKETS - 1} once each; "
                f"they end at {expected - 1}"
            )
        return self

    def ranges(self) -> dict[str, tuple[tuple[int, int], ...]]:
        """Bucket ranges per alias: ``RANGES``, or equal contiguous ranges."""
        if self.RANGES:
            return dict(self.RANGES)
        count, buckets = len(self.DATABASES), self.BUCKETS
        return {
            alias: ((index * buckets // count, (index + 1) * buckets // count - 1),)
            for index, alias in enumerate(self.DATABASES)
        }


DEFAULT_CACHE_ALIAS: Final = "default"
CACHE_SCHEMES: Final = frozenset({"memory", "dummy"}) | REDIS_SCHEMES
"""URL schemes of caches (``CACHES``): Redis's include Sentinel and Cluster."""
LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
_UNVERIFIED_TLS: Final = {
    "ssl_cert_reqs": frozenset({"none", "optional"}),
    "ssl_check_hostname": frozenset({"false", "0", "no", "off", "n", "f"}),
}
"""redis-py URL options that weaken certificate checks, with the weak values."""


class CacheConfig(BaseModel):
    """One ``CACHES`` entry: where cached values live, and for how long.

    Values are JSON. A cache is an optimization: when Redis is unreachable,
    ``get_or_set`` and ``@cached`` compute values instead (see ADR 0012).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    URL: SecretStr = SecretStr("memory://")
    """``memory://`` (this process), ``redis://``/``rediss://`` (Redis, Valkey), ``dummy://``.

    ``redis+sentinel://`` and ``redis+cluster://`` (``rediss`` for TLS) name a
    Sentinel group or a Cluster by several addresses.
    """
    TTL: float | None = Field(default=300.0, gt=0)
    """Seconds an entry lives unless a call says otherwise; None keeps entries."""
    KEY_PREFIX: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    """Namespace of every key (default: the alias). Redis caches must set one."""
    VERSION: int = Field(default=1, ge=1)
    """Part of every key: raise it to drop every entry, as after changing a type."""
    MAX_ENTRIES: int = Field(default=10_000, ge=1)
    """``memory://``: the least recently used entries are evicted beyond it."""
    SOCKET_TIMEOUT: float = Field(default=1.0, gt=0)
    """Seconds one Redis command may take."""
    CONNECT_TIMEOUT: float = Field(default=1.0, gt=0)
    """Seconds to connect, or to wait for a free connection from the pool."""
    RETRIES: int = Field(default=1, ge=0)
    """Retries of a command after a connection error or timeout."""
    MAX_CONNECTIONS: int = Field(default=50, ge=1)
    """Redis connections per process and event loop."""
    FAILURE_COOLDOWN: float = Field(default=5.0, ge=0)
    """Seconds the cache is skipped after it fails, so requests do not each wait."""
    EARLY_RECOMPUTE: float = Field(default=1.0, ge=0)
    """How early ``get_or_set`` refreshes hot entries (XFetch's beta); 0 disables."""
    COMPUTE_TIMEOUT: float | None = Field(default=None, gt=0, le=MAX_REQUEST_SECONDS)
    """Seconds a shared ``get_or_set`` computation may run (at most an hour).

    It goes on after its callers stop waiting, so a slow factory still fills
    the cache; this bounds it. None (the default, as in 0.9.0) leaves it
    unbounded; set it, to ``REQUEST_TIMEOUT``'s value for example. 0.10
    plans a bounded default.
    """
    TLS_CA: Path | None = None
    """CA bundle for ``rediss://``; default: the system's."""
    TLS_CERT: Path | None = None
    """Client certificate for ``rediss://`` (with ``TLS_KEY``)."""
    TLS_KEY: Path | None = None
    READINESS: bool = False
    """Fail ``/ready`` while this cache is unreachable (off: the service copes)."""
    SENTINEL_PASSWORD: SecretStr | None = None
    """``redis+sentinel://``: the Sentinels' own password, if they ask for one."""

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Accept cache URLs; Redis needs a host and a key prefix of its own."""
        scheme = self.scheme
        if scheme not in CACHE_SCHEMES:
            raise ValueError(
                "a cache URL starts with memory://, redis://, rediss://, "
                "redis+sentinel://, redis+cluster://, or dummy://"
            )
        if scheme in REDIS_SCHEMES:
            parse_redis_url(self.URL.get_secret_value())
            if self.KEY_PREFIX is None:
                raise ValueError(
                    "Redis caches need a KEY_PREFIX, unique among the services "
                    "that share the server"
                )
        else:
            _check_port(urlsplit(self.URL.get_secret_value()))
        tls = (self.TLS_CA, self.TLS_CERT, self.TLS_KEY)
        if not scheme.startswith("rediss") and any(tls):
            raise ValueError("TLS_CA, TLS_CERT and TLS_KEY need a rediss:// URL")
        if (self.TLS_CERT is None) != (self.TLS_KEY is None):
            raise ValueError("TLS_CERT and TLS_KEY go together")
        if self.SENTINEL_PASSWORD is not None and scheme not in SENTINEL_SCHEMES:
            raise ValueError("SENTINEL_PASSWORD needs a redis+sentinel:// URL")
        return self

    @property
    def scheme(self) -> str:
        """``memory``, ``dummy``, ``redis``, ``rediss``, or a Sentinel or Cluster's."""
        return self.URL.get_secret_value().partition("://")[0].lower()

    def is_network(self) -> bool:
        """Report whether this cache is a Redis server on another host."""
        if self.scheme not in REDIS_SCHEMES:
            return False
        hosts = parse_redis_url(self.URL.get_secret_value()).hosts
        return any(host not in LOCAL_HOSTS for host, _ in hosts)

    def uses_tls(self) -> bool:
        """Report whether connections to this Redis use TLS (a ``rediss`` scheme)."""
        return self.scheme.startswith("rediss")

    def verifies_tls(self) -> bool:
        """Report whether the URL leaves redis-py's certificate checks on."""
        options = parse_qs(urlsplit(self.URL.get_secret_value()).query)
        return not any(
            value.lower() in weak
            for name, weak in _UNVERIFIED_TLS.items()
            for value in options.get(name, ())
        )

    def has_credentials(self) -> bool:
        """Report whether the cache authenticates: a password or a certificate."""
        if self.TLS_CERT is not None:
            return True
        if self.scheme not in REDIS_SCHEMES:
            return False
        return bool(parse_redis_url(self.URL.get_secret_value()).password)


BROKER_SCHEMES: Final = frozenset({"amqp", "amqps", "memory"})
"""URL schemes of message brokers (``BROKERS``)."""
NAME_PATTERN: Final = r"^[A-Za-z0-9_.-]{1,64}$"
"""Queue names and broker prefixes: safe in AMQP names and routing keys."""


class BrokerConfig(BaseModel):
    """One ``BROKERS`` entry: a message broker the worker takes its work from.

    The outbox stays the record of every message; the broker only hands due
    messages to workers, so a broker that loses messages delays them but
    never drops them (see ADR 0014).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    URL: SecretStr
    """``amqps://user:password@host:5671/vhost`` (RabbitMQ), or ``memory://``."""
    PREFIX: str | None = Field(default=None, pattern=NAME_PATTERN)
    """Starts every exchange and queue name; RabbitMQ brokers must set one."""
    TLS_CA: Path | None = None
    """CA bundle for ``amqps://``; default: the system's."""
    TLS_CERT: Path | None = None
    """Client certificate for ``amqps://`` (with ``TLS_KEY``)."""
    TLS_KEY: Path | None = None
    CONNECT_TIMEOUT: float = Field(default=10.0, gt=0)
    """Seconds to connect before the attempt counts as failed."""
    PUBLISH_TIMEOUT: float = Field(default=10.0, gt=0)
    """Seconds to wait for the broker to confirm a published message."""

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Accept AMQP URLs with a host and a prefix, or the in-memory broker."""
        parts = urlsplit(self.URL.get_secret_value())
        if parts.scheme not in BROKER_SCHEMES:
            raise ValueError("a broker URL starts with amqp://, amqps://, or memory://")
        _check_port(parts)
        if parts.scheme != "memory":
            if not parts.hostname:
                raise ValueError("an AMQP URL needs a host: amqps://user:pw@host/vhost")
            if self.PREFIX is None:
                raise ValueError(
                    "RabbitMQ brokers need a PREFIX, unique among the services "
                    "that share the virtual host"
                )
        tls = (self.TLS_CA, self.TLS_CERT, self.TLS_KEY)
        if parts.scheme != "amqps" and any(tls):
            raise ValueError("TLS_CA, TLS_CERT and TLS_KEY need an amqps:// URL")
        if (self.TLS_CERT is None) != (self.TLS_KEY is None):
            raise ValueError("TLS_CERT and TLS_KEY go together")
        return self

    @property
    def scheme(self) -> str:
        """``amqp``, ``amqps``, or ``memory``."""
        return urlsplit(self.URL.get_secret_value()).scheme

    def is_network(self) -> bool:
        """Report whether the broker is a server on another host."""
        host = urlsplit(self.URL.get_secret_value()).hostname
        return self.scheme != "memory" and host not in LOCAL_HOSTS

    def verifies_tls(self) -> bool:
        """Report whether certificate checks stay on (no ``no_verify_ssl``)."""
        options = parse_qs(urlsplit(self.URL.get_secret_value()).query)
        return all(value == "0" for value in options.get("no_verify_ssl", ()))

    def has_credentials(self) -> bool:
        """Report whether the broker logs in as a real user, not RabbitMQ's guest."""
        parts = urlsplit(self.URL.get_secret_value())
        user = parts.username or ("" if self.TLS_CERT is None else "certificate")
        return user not in {"", "guest"}


MEMORY_STORAGE: Final = "domainkit.storage.MemoryStorage"


class StorageConfig(BaseModel):
    """One ``STORAGES`` entry: a ``Storage`` class and its arguments (Django's)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    BACKEND: str = "domainkit.storage.FileSystemStorage"
    """Dotted path to a ``domainkit.storage.Storage`` class."""
    OPTIONS: dict[str, Any] = Field(default_factory=dict)
    """Keyword arguments of the class, such as ``{"location": "/srv/files"}``."""


class DebugToolbarConfig(BaseModel):
    """``DEBUG_TOOLBAR``: what ``DebugToolbarMiddleware`` adds while ``DEBUG``."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    REPEATED_QUERIES: int = Field(default=5, ge=2)
    """A statement run this often in one request is logged as a likely N+1."""
    HEADERS: bool = False
    """Add ``X-Debug-Queries``, ``-Query-Time``, and ``-Repeated-Queries``."""
    PROFILE_DIR: Path | None = None
    """Where requests sent with ``X-Debug-Profile: 1`` leave a cProfile file;
    None turns profiling off."""


class HttpClientConfig(BaseModel):
    """``HTTP_CLIENT``: what ``domainkit.http.client`` clients do by default.

    Each ``HttpClient`` may change the timeout and the retries for itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    TIMEOUT: float = Field(default=10.0, gt=0, le=MAX_REQUEST_SECONDS)
    """Seconds one call may take, its retries included, and never past the deadline."""
    CONNECT_TIMEOUT: float = Field(default=5.0, gt=0)
    """Seconds to open a connection."""
    RETRIES: int = Field(default=2, ge=0, le=10)
    """Retries of a call that failed in a way worth trying again."""
    BACKOFF: float = Field(default=0.1, ge=0)
    """Seconds before the first retry, doubling for each next one, jittered."""
    MAX_BACKOFF: float = Field(default=2.0, ge=0, le=MAX_BACKOFF_SECONDS)
    """The longest wait before a retry, ``Retry-After`` included."""
    BREAKER_FAILURES: int = Field(default=5, ge=1)
    """Failures in a row that open a host's circuit: its calls then fail at once."""
    BREAKER_COOLDOWN: float = Field(default=30.0, gt=0, le=MAX_REQUEST_SECONDS)
    """Seconds a host's circuit stays open before one call may try it again."""
    MAX_CONNECTIONS: int = Field(default=100, ge=1)
    """Connections per client and event loop."""
    MAX_KEEPALIVE_CONNECTIONS: int = Field(default=20, ge=0)
    """Idle connections each client keeps open for the next calls."""
    MAX_RESPONSE_BYTES: int = Field(default=10_485_760, ge=1, le=MAX_RESPONSE_SIZE)
    """Largest response body read, decompressed; a larger one fails the call."""
    USER_AGENT: str = Field(default="domainkit", pattern=r"^[\x20-\x7e]{1,256}$")
    """The ``User-Agent`` of every call."""
    CA_BUNDLE: Path | None = None
    """CA certificates that sign the servers' certificates; default: the system's."""


MODULE_NAME: Final = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,15}$"
"""A dotted module name, as ``myproject.kernel`` (16 parts at most)."""
LAYER_NAME: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
"""A layer package's name, as ``ports``."""
MAX_ARCHITECTURE_ENTRIES: Final = 64
"""The most names one ``ARCHITECTURE`` list may hold."""
MAX_EXTRA_LAYERS: Final = 8
"""The most layers ``ARCHITECTURE.EXTRA_LAYERS`` may add."""
MAX_LAYERS: Final = MAX_EXTRA_LAYERS + 4
"""The most layers there can be: the four built in, and the extra ones."""

type _ModuleName = Annotated[str, Field(pattern=MODULE_NAME, max_length=256)]
type _LayerName = Annotated[str, Field(pattern=LAYER_NAME)]
type _Layers = Annotated[tuple[_LayerName, ...], Field(max_length=MAX_LAYERS)]
type _Modules = Annotated[
    tuple[_ModuleName, ...], Field(max_length=MAX_ARCHITECTURE_ENTRIES)
]


class ArchitectureConfig(BaseModel):
    """``ARCHITECTURE``: what the project adds to the layer rules ``check`` enforces.

    Each entry extends ``domainkit.architecture``'s defaults; none removes a
    rule. Empty, the rules are the framework's own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    DOMAIN_ALLOWED: _Modules = ()
    """More packages a domain layer may import, such as a shared kernel."""
    FORBIDDEN_LIBRARIES: dict[_LayerName, _Modules] = Field(
        default_factory=dict, max_length=MAX_LAYERS
    )
    """More libraries a layer must not import: ``{"application": ("httpx",)}``."""
    EXTRA_LAYERS: tuple[_LayerName, ...] = Field(
        default=(), max_length=MAX_EXTRA_LAYERS
    )
    """Layer packages of the project's own, such as ``ports``; rules name them below."""
    FORBIDDEN_LAYERS: dict[_LayerName, _Layers] = Field(
        default_factory=dict, max_length=MAX_LAYERS
    )
    """More layers of the same domain a layer must not import, by layer."""
    PUBLIC_SURFACE: _Modules = ()
    """More parts of a domain other domains may import, as ``domain.values``."""

    @model_validator(mode="after")
    def check_layers(self) -> Self:
        """Every layer named is known, and the domain layer keeps its allowlist."""
        builtin = LAYERS
        if clash := sorted(set(self.EXTRA_LAYERS) & set(builtin)):
            raise ValueError(f"EXTRA_LAYERS repeats a built-in layer: {clash}")
        if len(set(self.EXTRA_LAYERS)) != len(self.EXTRA_LAYERS):
            raise ValueError("EXTRA_LAYERS names a layer twice")
        layers = {*builtin, *self.EXTRA_LAYERS}
        for name, table in (
            ("FORBIDDEN_LIBRARIES", self.FORBIDDEN_LIBRARIES),
            ("FORBIDDEN_LAYERS", self.FORBIDDEN_LAYERS),
        ):
            if unknown := sorted(set(table) - layers):
                raise ValueError(
                    f"{name} names layers that are not in the layer rules: "
                    f"{unknown}; add them to EXTRA_LAYERS"
                )
            if "domain" in table:
                raise ValueError(
                    f"{name} cannot name the domain layer: it may import only "
                    "what the rules allow; add packages to DOMAIN_ALLOWED"
                )
        for layer, forbidden in self.FORBIDDEN_LAYERS.items():
            if unknown := sorted(set(forbidden) - layers):
                raise ValueError(
                    f"FORBIDDEN_LAYERS[{layer!r}] names layers that are not in "
                    f"the layer rules: {unknown}; add them to EXTRA_LAYERS"
                )
            if layer in forbidden:
                raise ValueError(
                    f"FORBIDDEN_LAYERS[{layer!r}] forbids the layer itself"
                )
        if outside := sorted(
            part for part in self.PUBLIC_SURFACE if part.partition(".")[0] not in layers
        ):
            raise ValueError(
                f"PUBLIC_SURFACE names parts outside the layers: {outside}; "
                "start each with a layer, as domain.values"
            )
        return self


ABSOLUTE_URI: Final = r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$"
"""An absolute URI without whitespace (``https://...``, ``urn:...``)."""
HEADER_NAME: Final = r"^[A-Za-z0-9-]{1,64}$"
"""A header name: letters, digits, and hyphens."""


class _StoreRule(NamedTuple):
    """One deployment rule over the entries of ``DATABASES`` or another store."""

    id: str
    message: str
    setting: str
    """``LEDGERS``, ``CACHES``, ``BROKERS``; empty for ``DATABASES``, unnamed."""
    breaks: Callable[[Any], bool]


class Settings(BaseModel):
    """Validated, read-only settings; extra names are checked at ``setup()``."""

    model_config = ConfigDict(frozen=True, extra="allow", allow_inf_nan=False)

    SETTINGS_MODULE: str = ""
    """Set by the loader to the module that produced these settings."""
    BASE_DIR: Path = Field(default_factory=project_root)
    ENVIRONMENT: Environment = "local"
    """Staging and production get the deployment checks below."""
    SILENCED_CHECKS: tuple[str, ...] = Field(default=(), max_length=MAX_SILENCED_CHECKS)
    """Ids of warnings ``check`` stops reporting, as ``security.W001``.

    Errors cannot be silenced (``domainkit.checks``)."""
    DEBUG: bool = False
    SLOW_CALLBACK_DURATION: float | None = Field(default=0.1, gt=0)
    """With ``DEBUG``: log loop steps this slow (asyncio debug mode); None: off."""
    PROJECT_NAME: str = "API"
    VERSION: str = "0.1.0"

    SECRET_KEY: SecretStr | None = Field(default=None, min_length=1)
    """Signs what the service hands out (``domainkit.signing``); hidden in reprs."""
    SECRET_KEY_FALLBACKS: tuple[SecretStr, ...] = ()
    """Earlier keys: what they signed still verifies, while ``SECRET_KEY`` signs."""

    INSTALLED_DOMAINS: tuple[str, ...] = ()
    """Domain packages, started in order and stopped in reverse order."""
    ARCHITECTURE: ArchitectureConfig = Field(default_factory=ArchitectureConfig)
    """What the project adds to the layer rules ``check`` enforces (none)."""
    MIDDLEWARE: tuple[str, ...] = DEFAULT_MIDDLEWARE
    """ASGI middleware classes by dotted path; the first one is outermost."""
    ROOT_ROUTER: str | None = None
    """Dotted path to the ``APIRouter`` that mounts every route (``ROOT_URLCONF``)."""
    FLATTEN_ROUTERS: bool = True
    """Serve the routes of included routers from the top (ADR 0060); False nests."""
    REQUIRE_ROUTE_RULES: bool = True
    """Every route must say who may call it (``Require``, or ``Public``); staging
    and production refuse to start otherwise (``domainkit.http.access``)."""
    ASGI_APPLICATION: str | None = None
    """``module:attribute`` that ``runserver`` serves."""
    EVENT_LOOP: Literal["auto", "asyncio", "uvloop"] = "auto"
    """Commands' and ``runserver``'s loop: uvloop when installed (``auto``), or not."""

    DATABASES: dict[str, DatabaseConfig] = Field(default_factory=dict)
    """SQL databases by alias; if any are set, ``default`` is required."""
    LEDGERS: dict[str, LedgerConfig] = Field(default_factory=dict)
    """Ledgers by alias: write once, read many (``immudb://``, ``memory://``)."""
    SHARDS: dict[str, ShardConfig] = Field(default_factory=dict)
    """Shard groups by name; route a domain to one in ``DATABASE_ROUTING``."""
    DATABASE_ROUTING: dict[str, str] = Field(default_factory=dict)
    """Domain label to target: a SQL alias or a shard group; others use default."""
    MIGRATIONS_DIR: Path = Field(default_factory=lambda: project_root() / "migrations")
    """Revisions of ``default``; any other target uses ``MIGRATIONS_DIR/<target>``."""
    CACHES: dict[str, CacheConfig] = Field(
        default_factory=lambda: {DEFAULT_CACHE_ALIAS: CacheConfig()}
    )
    """Caches by alias; ``default`` is required (``memory://`` unless set)."""
    BROKERS: dict[str, BrokerConfig] = Field(default_factory=dict)
    """Message brokers by alias; ``MESSAGING.BROKER`` names the worker's."""
    STORAGES: dict[str, StorageConfig] = Field(default_factory=dict)
    """File storages by alias (``domainkit.storage``); none unless set."""
    LOGGING: dict[str, Any] = Field(default_factory=_minimal_logging)
    """``logging.config.dictConfig`` mapping applied by ``setup()``."""
    EVENT_HANDLER_TIMEOUT: float = Field(default=10.0, gt=0, le=3600)
    """Seconds an in-process event handler may run before it is cut off."""
    SHUTDOWN_TIMEOUT: float = Field(default=15.0, gt=0, le=300)
    """Seconds a shutdown may take in all, split across its phases.

    Domain hooks, work in flight, background tasks, then connections (see
    ``domainkit.lifecycle``). With the server's own graceful shutdown, keep
    it under the orchestrator's grace period (30 s in Kubernetes).
    """

    ALLOWED_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")
    """Accepted ``Host`` header values; ``*.example.com`` matches subdomains."""
    API_DOCS: bool = True
    """Serve ``/docs``, ``/redoc``, and ``/openapi.json``."""
    OPENAPI_OPERATION_IDS: Literal["fastapi", "route_name"] = "fastapi"
    """Operation ids: FastAPI's (name, path, and method), or the route's name."""
    ERROR_FORMAT: Literal["domainkit", "problem+json"] = "domainkit"
    """Error bodies: ``{"detail", "code", ...}``, or RFC 9457 problem details."""
    ERROR_TYPE_BASE_URL: str | None = Field(default=None, pattern=ABSOLUTE_URI)
    """Problem details: each code's ``type`` is this URL plus the code."""
    DEBUG_TOOLBAR: DebugToolbarConfig = Field(default_factory=DebugToolbarConfig)
    """Options of ``DebugToolbarMiddleware`` (development only)."""
    MAX_REQUEST_BODY_SIZE: int = Field(default=1_048_576, ge=1)
    """Largest accepted request body in bytes; larger requests get 413."""
    REQUEST_BODY_TIMEOUT: float = Field(default=10.0, gt=0, le=MAX_REQUEST_SECONDS)
    """Seconds a request may spend waiting for its body; longer gets 408."""
    REQUEST_TIMEOUT: float | None = Field(default=30.0, gt=0, le=MAX_REQUEST_SECONDS)
    """Seconds an HTTP request may take until its response starts; None: no limit."""
    MAX_CONCURRENT_REQUESTS: int | None = Field(
        default=1000, ge=1, le=MAX_REQUESTS_AT_ONCE
    )
    """HTTP requests each event loop works on at once; None: no cap."""
    MAX_QUEUED_REQUESTS: int = Field(default=100, ge=0, le=MAX_REQUESTS_AT_ONCE)
    """Requests past the cap that may wait for a slot; more are refused at once."""
    REQUEST_QUEUE_TIMEOUT: float = Field(default=1.0, ge=0, le=60)
    """Seconds a request past the cap waits for a slot before it is refused."""
    CONCURRENCY_EXEMPT_PATHS: tuple[str, ...] = ("/health", "/ready", "/metrics")
    """Exact paths never counted against the cap, so probes are never refused."""
    HTTP_CLIENT: HttpClientConfig = Field(default_factory=HttpClientConfig)
    """Defaults of outgoing calls through ``domainkit.http.client``."""
    GUARD_LOOKUP_THREADS: int = Field(default=16, ge=1, le=MAX_LOOKUP_THREADS)
    """Threads that look host names up for guarded calls and webhooks, per process."""
    READINESS_TIMEOUT: float = Field(default=5.0, gt=0, le=60)
    """Seconds each readiness check may take before it counts as failed."""
    READINESS_CACHE_SECONDS: float = Field(default=1.0, ge=0)
    """Reuse a readiness result this long, so probe floods cannot load the database."""
    READINESS_DETAILS: bool = False
    """Name failing components in ``/ready`` responses; they are always logged."""
    ACCESS_LOG_QUIET_PATHS: tuple[str, ...] = ("/health", "/ready")
    """Successful requests to these paths are logged at DEBUG, not INFO."""
    ACCESS_LOG_SAMPLE_RATE: float = Field(default=1.0, ge=0.0, le=1.0)
    """Share of access lines written; from status 400 up, and slow ones: all of them."""
    ACCESS_LOG_SLOW_DURATION: float = Field(default=1.0, gt=0, le=MAX_REQUEST_SECONDS)
    """Seconds after which a request's access line is written whatever the rate."""
    TRUSTED_PROXIES: tuple[IPvAnyNetwork | Literal["unix"], ...] = ()
    """Load balancers whose ``X-Forwarded-For``/``-Proto`` headers are believed.

    ``"unix"`` names the peer of a unix socket (``uvicorn --uds``), which
    has no address: the proxy that connects to it."""
    PROXY_SCHEME_HEADER: str | None = Field(
        default="X-Forwarded-Proto", pattern=HEADER_NAME
    )
    """The header in which trusted proxies say the client's scheme; None: none does."""
    RATE_LIMIT: str | None = None
    """Requests per caller, e.g. ``"300/minute"``; None disables the limit."""
    RATE_LIMIT_EXEMPT_PATHS: tuple[str, ...] = ("/health", "/ready", "/metrics")
    """Exact paths never counted, so probes and scrapers are not refused."""
    RATE_LIMIT_BACKEND: str = "domainkit.http.ratelimit.MemoryBackend"
    """Dotted path to a ``Backend`` class; the default counts per process."""
    RATE_LIMIT_CACHE: str = DEFAULT_CACHE_ALIAS
    """The Redis cache ``RedisBackend`` keeps counts in, shared by every replica."""
    RATE_LIMIT_IPV6_PREFIX: int = Field(default=64, ge=1, le=128)
    """IPv6 clients are counted per network of this prefix length (one customer's)."""
    METRICS_TOKEN: SecretStr | None = None
    """Bearer token for ``/metrics``; without it staging and production refuse."""

    LIVE_MAX_CONNECTIONS: int = Field(default=1000, ge=1, le=1_000_000)
    """Open WebSockets and event streams per process; more are refused (503)."""
    LIVE_MAX_CONNECTIONS_PER_CALLER: int = Field(default=10, ge=1, le=1_000_000)
    """Open WebSockets and event streams per user, or per address if anonymous."""
    LIVE_SEND_QUEUE: int = Field(default=64, ge=1, le=10_000)
    """Messages or events a live connection may have waiting to be sent."""
    LIVE_SEND_TIMEOUT: float = Field(default=10.0, gt=0)
    """Seconds a message may take to reach the client before it counts as too slow."""
    LIVE_REAUTH_INTERVAL: float | None = Field(
        default=60.0, gt=0, le=MAX_REAUTH_INTERVAL
    )
    """Seconds between re-checks of an open connection's credentials and rules."""
    WEBSOCKET_IDLE_TIMEOUT: float = Field(default=300.0, gt=0)
    """Seconds without a message either way before a WebSocket is closed (1001)."""
    WEBSOCKET_MAX_LIFETIME: float = Field(default=3600.0, gt=0)
    """Seconds a WebSocket may stay open before it is closed (1001)."""
    WEBSOCKET_MESSAGE_RATE: str | None = "20/second"
    """Messages a WebSocket client may send, such as ``"20/second"``; None: any."""
    SSE_KEEPALIVE: float = Field(default=15.0, gt=0)
    """Seconds of silence before an event stream sends a keep-alive comment."""
    SSE_MAX_LIFETIME: float = Field(default=3600.0, gt=0)
    """Seconds an event stream may stay open; the client then reconnects."""
    ERROR_REPORTERS: tuple[str, ...] = ()
    """Callables ``(exc, context) -> None`` by dotted path, e.g. a Sentry adapter."""

    SECURE_HSTS_SECONDS: int = Field(default=0, ge=0)
    """``Strict-Transport-Security`` max-age; 0 omits the header."""
    SECURE_HSTS_INCLUDE_SUBDOMAINS: bool = False
    SECURE_CONTENT_TYPE_NOSNIFF: bool = True
    SECURE_REFERRER_POLICY: str | None = "no-referrer"
    X_FRAME_OPTIONS: Literal["DENY", "SAMEORIGIN"] = "DENY"

    CORS_ALLOWED_ORIGINS: tuple[str, ...] = ()
    CORS_ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")
    CORS_ALLOWED_HEADERS: tuple[str, ...] = (
        "Authorization",
        "Content-Type",
        "X-Request-ID",
    )
    CORS_ALLOW_CREDENTIALS: bool = False

    LANGUAGE_CODE: str = Field(default="en", pattern=LANGUAGE_PATTERN)
    """The language of responses when the caller asks for none of ``LANGUAGES``."""
    LANGUAGES: tuple[tuple[str, str], ...] = (("en", "English"), ("ar", "Arabic"))
    """``(code, name)`` of each language responses may be in (``LocaleMiddleware``)."""
    LOCALE_PATHS: tuple[Path, ...] = ()
    """Directories of ``<locale>/LC_MESSAGES/messages.mo``, ahead of the domains'."""
    TIME_ZONE: str = "UTC"
    """The zone dates are shown in (``localtime``); they are stored in UTC."""

    @field_validator("SILENCED_CHECKS")
    @classmethod
    def check_silenced(cls, ids: tuple[str, ...]) -> tuple[str, ...]:
        """Warnings' ids, each once: an error cannot be silenced."""
        return validate_silenced(ids)

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """Show declared settings; domain settings, which may hold keys, hidden."""
        domain = self.model_extra or {}
        for name, value in super().__repr_args__():
            yield name, HIDDEN if name in domain else value

    @model_serializer(mode="wrap")
    def _hide_domain_settings(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Dump domain settings hidden: they are checked, and typed, elsewhere."""
        dumped: dict[str, Any] = handler(self)
        return {
            name: HIDDEN if name in (self.model_extra or {}) else value
            for name, value in dumped.items()
        }

    @model_validator(mode="after")
    def check_consistency(self) -> Self:
        """Reject combinations that are unsafe or would fail later at runtime."""
        self._check_databases()
        if DEFAULT_CACHE_ALIAS not in self.CACHES:
            raise ValueError("CACHES must define the 'default' alias")
        if len(set(self.INSTALLED_DOMAINS)) != len(self.INSTALLED_DOMAINS):
            raise ValueError("INSTALLED_DOMAINS contains duplicates")
        if self.CORS_ALLOW_CREDENTIALS and "*" in self.CORS_ALLOWED_ORIGINS:
            raise ValueError("CORS_ALLOW_CREDENTIALS cannot be used with origin '*'")
        if self.RATE_LIMIT is not None:
            parse_rate(self.RATE_LIMIT)
        self._check_live_connections()
        self._check_i18n()
        if self.ENVIRONMENT in DEPLOYED_ENVIRONMENTS and (
            errors := self.deployment_errors()
        ):
            raise ValueError(str(CheckFailed("The deployment checks failed", errors)))
        return self

    def _check_databases(self) -> None:
        """Validate aliases, shard groups, and routing targets together."""
        sql = self.DATABASES
        if sql and DEFAULT_DB_ALIAS not in sql:
            raise ValueError(f"DATABASES must define the {DEFAULT_DB_ALIAS!r} alias")
        sharded: dict[str, str] = {}
        for group, shards in self.SHARDS.items():
            if group in self.DATABASES:
                raise ValueError(f"shard group {group!r} has the name of an alias")
            for alias in shards.DATABASES:
                if alias not in sql or alias == DEFAULT_DB_ALIAS:
                    raise ValueError(
                        f"shard group {group!r}: {alias!r} must be a SQL alias "
                        f"other than {DEFAULT_DB_ALIAS!r}"
                    )
                if alias in sharded:
                    raise ValueError(
                        f"{alias!r} is in shard groups {sharded[alias]!r} and {group!r}"
                    )
                sharded[alias] = group
        replicas = self._check_replicas(sharded)
        for label, target in self.DATABASE_ROUTING.items():
            if target in self.SHARDS or (
                target in sql and target not in sharded and target not in replicas
            ):
                continue
            raise ValueError(
                f"DATABASE_ROUTING[{label!r}] = {target!r} must name a shard group "
                "or a SQL alias outside the shard groups that is not a replica"
            )

    def _check_live_connections(self) -> None:
        """The message rate reads, and a caller's cap fits in the process's."""
        if self.WEBSOCKET_MESSAGE_RATE is not None:
            parse_rate(self.WEBSOCKET_MESSAGE_RATE)
        if self.LIVE_MAX_CONNECTIONS_PER_CALLER > self.LIVE_MAX_CONNECTIONS:
            raise ValueError(
                "LIVE_MAX_CONNECTIONS_PER_CALLER cannot exceed LIVE_MAX_CONNECTIONS"
            )

    def _check_i18n(self) -> None:
        """Languages are codes like ``pt-br``, listed once; the zone exists."""
        codes = [code for code, _ in self.LANGUAGES]
        if not codes:
            raise ValueError("LANGUAGES must list at least one language")
        if bad := [code for code in codes if not re.fullmatch(LANGUAGE_PATTERN, code)]:
            raise ValueError(
                f"LANGUAGES codes are lower case, as 'ar' or 'pt-br', not {bad}"
            )
        if len(set(codes)) != len(codes):
            raise ValueError("LANGUAGES lists a language twice")
        if self.LANGUAGE_CODE not in codes:
            raise ValueError(
                f"LANGUAGE_CODE {self.LANGUAGE_CODE!r} is not in LANGUAGES"
            )
        try:
            ZoneInfo(self.TIME_ZONE)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError(
                f"TIME_ZONE {self.TIME_ZONE!r} is not a time zone, such as "
                "'Africa/Cairo' or 'UTC' (install tzdata if the system has none)"
            ) from None

    def _check_replicas(self, sharded: Mapping[str, str]) -> dict[str, str]:
        """Each replica is another alias, of one primary, and nothing more.

        A replica is not ``default``, in no shard group, and has no replicas
        of its own, so nothing ever writes to or migrates it.
        """
        replicas: dict[str, str] = {}
        for primary, config in self.DATABASES.items():
            for alias in config.REPLICAS:
                if alias not in self.DATABASES or alias in {primary, DEFAULT_DB_ALIAS}:
                    raise ValueError(
                        f"DATABASES[{primary!r}].REPLICAS: {alias!r} must be "
                        f"another SQL alias in DATABASES, not {DEFAULT_DB_ALIAS!r}"
                    )
                if alias in replicas or alias in sharded:
                    raise ValueError(
                        f"{alias!r} is a replica of {primary!r}, so it cannot also "
                        "be another's replica or in a shard group"
                    )
                if self.DATABASES[alias].REPLICAS:
                    raise ValueError(f"replica {alias!r} cannot have REPLICAS")
                replicas[alias] = primary
        return replicas

    def deployment_warnings(self) -> list[str]:
        """Weak but valid choices in staging and production; ``check`` lists them.

        Unlike the deployment checks, they do not stop the application: an
        API may publish its documentation on purpose, for example. Each is
        a ``CheckMessage`` with an id that ``SILENCED_CHECKS`` may list.
        """
        if self.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        warnings: list[str] = []
        if self.API_DOCS:
            warnings.append(
                CheckMessage(
                    "security.W001",
                    "API_DOCS publishes /docs, /redoc, and /openapi.json to "
                    "anyone; set it to False unless the API is public.",
                )
            )
        if not self.SECURE_HSTS_SECONDS:
            warnings.append(
                CheckMessage(
                    "security.W002",
                    "SECURE_HSTS_SECONDS is 0, so browsers may still use plain "
                    "HTTP; set it (31536000 is a year) once HTTPS works everywhere.",
                )
            )
        if self.RATE_LIMIT is None or RATE_LIMIT_MIDDLEWARE not in self.MIDDLEWARE:
            warnings.append(
                CheckMessage(
                    "security.W003",
                    f"Requests are not rate limited; set RATE_LIMIT and list "
                    f"{RATE_LIMIT_MIDDLEWARE} in MIDDLEWARE.",
                )
            )
        elif not self.TRUSTED_PROXIES:
            warnings.append(
                CheckMessage(
                    "security.W004",
                    "TRUSTED_PROXIES is empty, so callers are rate limited by the "
                    "address they connect from: behind a load balancer or proxy, "
                    "they all share its count. List its networks in TRUSTED_PROXIES.",
                )
            )
        if self.LIVE_REAUTH_INTERVAL is None:
            warnings.append(
                CheckMessage(
                    "security.W005",
                    "LIVE_REAUTH_INTERVAL is None, so an open WebSocket or event "
                    "stream keeps a revoked token or a removed permission until it "
                    "closes; set it (60 is a minute).",
                )
            )
        if "*" in self.CORS_ALLOWED_ORIGINS:
            warnings.append(
                CheckMessage(
                    "security.W006",
                    "CORS_ALLOWED_ORIGINS allows '*': any web site may call the "
                    "API from its visitors' browsers and read the answers.",
                    "List the origins of the sites that call the API, as "
                    "'https://app.example.com'.",
                )
            )
        warnings.extend(self._load_warnings())
        warnings.extend(self._database_warnings())
        return warnings

    def _load_warnings(self) -> list[str]:
        """Requests without a deadline or a cap, and PgBouncer left unsaid."""
        warnings: list[str] = []
        if self.REQUEST_TIMEOUT is None or DEADLINE_MIDDLEWARE not in self.MIDDLEWARE:
            warnings.append(
                CheckMessage(
                    "deploy.W001",
                    f"Requests have no deadline, so a slow one holds its resources "
                    f"for as long as it takes; set REQUEST_TIMEOUT and list "
                    f"{DEADLINE_MIDDLEWARE} in MIDDLEWARE.",
                )
            )
        if (
            self.MAX_CONCURRENT_REQUESTS is None
            or CONCURRENCY_MIDDLEWARE not in self.MIDDLEWARE
        ):
            warnings.append(
                CheckMessage(
                    "deploy.W002",
                    f"Requests are not shed under load, so they queue without end; "
                    f"set MAX_CONCURRENT_REQUESTS and list {CONCURRENCY_MIDDLEWARE} "
                    f"in MIDDLEWARE.",
                )
            )
        if pooled := sorted(a for a, db in self.DATABASES.items() if db.looks_pooled()):
            warnings.append(
                CheckMessage(
                    "deploy.W003",
                    f"DATABASES {pooled} seem to go through PgBouncer (port "
                    f"{PGBOUNCER_PORT}, or a statement cache turned off in "
                    f"CONNECT_ARGS); set PGBOUNCER = True, so timeouts hold per "
                    f"transaction and prepared statements do not clash.",
                )
            )
        return warnings

    def _database_warnings(self) -> list[str]:
        """Pools sized by default, and SQLite, where deployed (INFRA-08)."""
        warnings: list[str] = []
        network = {
            alias: db
            for alias, db in self.DATABASES.items()
            if is_network_database(db.URL.get_secret_value())
        }
        if unsized := sorted(a for a, db in network.items() if db.POOL_SIZE is None):
            warnings.append(
                CheckMessage(
                    "deploy.W004",
                    f"DATABASES {unsized} have no POOL_SIZE, so each process "
                    f"opens up to {DEFAULT_POOL_SIZE + DEFAULT_MAX_OVERFLOW} "
                    f"connections (SQLAlchemy's {DEFAULT_POOL_SIZE} plus "
                    f"{DEFAULT_MAX_OVERFLOW} overflow), whatever the server's "
                    "max_connections allows.",
                    "Set POOL_SIZE and MAX_OVERFLOW so that processes x replicas "
                    "x (POOL_SIZE + MAX_OVERFLOW) stays below max_connections.",
                )
            )
        if local := sorted(set(self.DATABASES) - set(network)):
            warnings.append(
                CheckMessage(
                    "deploy.W005",
                    f"DATABASES {local} are SQLite files: each process and host "
                    "sees its own, and writes wait for one another.",
                    "Use PostgreSQL in staging and production.",
                )
            )
        return warnings

    def deployment_errors(self) -> list[CheckMessage]:
        """Every breach of the security baseline for staging and production.

        Staging and production refuse to load settings with any; all of
        them are reported at once. Errors are never silenced. The messages
        never quote a key.
        """
        where = f"in {self.ENVIRONMENT}"
        errors: list[CheckMessage] = []
        if self.DEBUG:
            errors.append(
                CheckMessage(
                    "security.E001",
                    f"DEBUG must be False {where}",
                    "DEBUG shows tracebacks and settings to callers.",
                )
            )
        if DEBUG_TOOLBAR_MIDDLEWARE in self.MIDDLEWARE:
            errors.append(
                CheckMessage(
                    "security.E002",
                    f"{DEBUG_TOOLBAR_MIDDLEWARE} is for development; remove it "
                    f"from MIDDLEWARE {where}",
                )
            )
        if "*" in self.ALLOWED_HOSTS:
            errors.append(
                CheckMessage(
                    "security.E003",
                    f"ALLOWED_HOSTS must list real host names {where}",
                    "List the names clients reach the service by, as "
                    "'api.example.com'.",
                )
            )
        if any(
            not isinstance(network, str) and network.prefixlen == 0
            for network in self.TRUSTED_PROXIES
        ):
            errors.append(
                CheckMessage(
                    "security.E004",
                    f"TRUSTED_PROXIES must list the proxies' networks, not every "
                    f"address {where}: any caller could choose its own address",
                )
            )
        errors.extend(self._store_errors(where))
        errors.extend(self._secret_key_errors(where))
        return errors

    def _store_errors(self, where: str) -> Iterator[CheckMessage]:
        """Databases, ledgers, caches, brokers, and storages that cannot hold."""
        rules: tuple[_StoreRule, ...] = (
            _StoreRule(
                "security.E005",
                "ECHO logs every statement; disable it",
                "",
                lambda db: db.ECHO,
            ),
            _StoreRule(
                "security.E006",
                "LOG_PARAMETERS exposes bound values; disable it",
                "",
                lambda db: db.LOG_PARAMETERS,
            ),
            _StoreRule(
                "security.E007",
                "SSLMODE must be 'verify-full'",
                "",
                lambda db: (
                    is_network_database(db.URL.get_secret_value())
                    and db.SSLMODE != "verify-full"
                ),
            ),
            _StoreRule(
                "deploy.E001",
                "memory:// ledgers are for tests; use immudb",
                "LEDGERS",
                lambda ledger: ledger.scheme == "memory",
            ),
            _StoreRule(
                "security.E008",
                "ledgers need TLS_CA (TLS)",
                "LEDGERS",
                lambda ledger: ledger.TLS_CA is None,
            ),
            _StoreRule(
                "security.E009",
                "ledgers need SERVER_SIGNING_KEY (signed states)",
                "LEDGERS",
                lambda ledger: ledger.SERVER_SIGNING_KEY is None,
            ),
            _StoreRule(
                "security.E010",
                "network caches need rediss:// (TLS)",
                "CACHES",
                lambda cache: cache.is_network() and not cache.uses_tls(),
            ),
            _StoreRule(
                "security.E011",
                "caches must verify the server certificate",
                "CACHES",
                lambda cache: not cache.verifies_tls(),
            ),
            _StoreRule(
                "security.E012",
                "network caches need a password or a client certificate",
                "CACHES",
                lambda cache: cache.is_network() and not cache.has_credentials(),
            ),
            _StoreRule(
                "deploy.E002",
                "memory:// brokers serve one process; use RabbitMQ",
                "BROKERS",
                lambda broker: broker.scheme == "memory",
            ),
            _StoreRule(
                "security.E013",
                "network brokers need amqps:// (TLS)",
                "BROKERS",
                lambda broker: broker.is_network() and broker.scheme != "amqps",
            ),
            _StoreRule(
                "security.E014",
                "brokers must verify the server certificate",
                "BROKERS",
                lambda broker: not broker.verifies_tls(),
            ),
            _StoreRule(
                "security.E015",
                "brokers need a user of their own, not guest",
                "BROKERS",
                lambda broker: (
                    broker.scheme != "memory" and not broker.has_credentials()
                ),
            ),
        )
        stores: dict[str, Mapping[str, Any]] = {
            "": self.DATABASES,
            "LEDGERS": self.LEDGERS,
            "CACHES": self.CACHES,
            "BROKERS": self.BROKERS,
        }
        for rule in rules:
            configs = stores[rule.setting]
            if aliases := sorted(
                a for a, config in configs.items() if rule.breaks(config)
            ):
                named = f"{rule.setting} {aliases}" if rule.setting else str(aliases)
                yield CheckMessage(rule.id, f"{rule.message} {where} for {named}")
        if aliases := sorted(
            alias
            for alias, storage in self.STORAGES.items()
            if storage.BACKEND == MEMORY_STORAGE
        ):
            yield CheckMessage(
                "deploy.E003",
                f"memory storages keep files in one process, until it ends; use "
                f"FileSystemStorage or a storage of your own {where} for "
                f"STORAGES {aliases}",
            )

    def _secret_key_errors(self, where: str) -> Iterator[CheckMessage]:
        """A missing, short, simple, or development ``SECRET_KEY``.

        Fallbacks are held to the same rules: they still verify signatures.
        The errors never quote a key.
        """
        if self.SECRET_KEY is None:
            yield CheckMessage(
                "security.E016",
                f"SECRET_KEY is required {where}: it signs reset links, cookies, "
                "and other values the service hands out",
                "Make one with: python -c 'import secrets; "
                "print(secrets.token_urlsafe(64))'.",
            )
            return
        keys = [("SECRET_KEY", self.SECRET_KEY)]
        keys += [("SECRET_KEY_FALLBACKS", key) for key in self.SECRET_KEY_FALLBACKS]
        for name, key in keys:
            value = key.get_secret_value()
            if value.startswith(DEVELOPMENT_KEY_PREFIXES):
                yield CheckMessage(
                    "security.E017", f"{name} holds a development key {where}"
                )
            elif (
                len(value) < SECRET_KEY_MIN_LENGTH
                or len(set(value)) < SECRET_KEY_MIN_CHARACTERS
            ):
                yield CheckMessage(
                    "security.E018",
                    f"{name} must be random, with at least "
                    f"{SECRET_KEY_MIN_LENGTH} characters, {where}",
                )
