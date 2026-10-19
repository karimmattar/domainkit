"""Framework settings and their defaults (Django's ``global_settings``).

Every UPPERCASE name in the selected settings module is validated here. A name
the framework does not declare must belong to an installed domain (see
``DomainConfig.settings_class``) or to the project's own ``SETTINGS_CLASS``,
so a misspelled setting fails at startup instead of being silently ignored.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final, Literal, Self
from urllib.parse import SplitResult, parse_qs, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyNetwork,
    SecretStr,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from domainkit.conf.env import project_root
from domainkit.utils.rates import parse_rate

DEFAULT_DB_ALIAS: Final = "default"
HIDDEN: Final = "**********"
"""What ``repr`` and dumps of the settings show for domain settings."""

Environment = Literal["local", "test", "staging", "production"]
DEPLOYED_ENVIRONMENTS: Final = frozenset({"staging", "production"})
"""Environments held to the deployment checks in ``Settings``."""

RATE_LIMIT_MIDDLEWARE: Final = "domainkit.http.ratelimit.RateLimitMiddleware"
DEBUG_TOOLBAR_MIDDLEWARE: Final = "domainkit.debug.DebugToolbarMiddleware"
"""Development middleware: SQL per request, N+1 warnings, profiles."""
DEFAULT_MIDDLEWARE: Final = (
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.AllowedHostsMiddleware",
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

    STATEMENT_TIMEOUT: float | None = Field(default=30.0, gt=0)
    """Seconds before PostgreSQL cancels a statement; migrations are exempt."""
    LOCK_TIMEOUT: float | None = Field(default=10.0, gt=0)
    """Seconds a statement may wait for a lock."""
    IDLE_IN_TRANSACTION_TIMEOUT: float | None = Field(default=60.0, gt=0)
    """Seconds an open transaction may sit idle before the server ends it."""

    POOL_SIZE: int | None = Field(default=None, ge=1)
    MAX_OVERFLOW: int | None = Field(default=None, ge=0)
    POOL_TIMEOUT: float | None = Field(default=None, gt=0)
    POOL_RECYCLE: int | None = Field(default=None, gt=0)
    POOL_PRE_PING: bool = True
    CONNECT_ARGS: dict[str, Any] = Field(default_factory=dict)
    """Extra driver arguments; they may not replace TLS or the timeouts above."""

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
CACHE_SCHEMES: Final = frozenset({"memory", "dummy", "redis", "rediss"})
"""URL schemes of caches (``CACHES``)."""
REDIS_SCHEMES: Final = frozenset({"redis", "rediss"})
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
    """``memory://`` (this process), ``redis://``/``rediss://`` (Redis, Valkey), ``dummy://``."""
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
    TLS_CA: Path | None = None
    """CA bundle for ``rediss://``; default: the system's."""
    TLS_CERT: Path | None = None
    """Client certificate for ``rediss://`` (with ``TLS_KEY``)."""
    TLS_KEY: Path | None = None
    READINESS: bool = False
    """Fail ``/ready`` while this cache is unreachable (off: the service copes)."""

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Accept cache URLs; Redis needs a host and a key prefix of its own."""
        parts = urlsplit(self.URL.get_secret_value())
        if parts.scheme not in CACHE_SCHEMES:
            raise ValueError(
                "a cache URL starts with memory://, redis://, rediss://, or dummy://"
            )
        _check_port(parts)
        if parts.scheme in REDIS_SCHEMES:
            if not parts.hostname:
                raise ValueError("a Redis URL needs a host: redis://host:6379/0")
            if self.KEY_PREFIX is None:
                raise ValueError(
                    "Redis caches need a KEY_PREFIX, unique among the services "
                    "that share the server"
                )
        tls = (self.TLS_CA, self.TLS_CERT, self.TLS_KEY)
        if parts.scheme != "rediss" and any(tls):
            raise ValueError("TLS_CA, TLS_CERT and TLS_KEY need a rediss:// URL")
        if (self.TLS_CERT is None) != (self.TLS_KEY is None):
            raise ValueError("TLS_CERT and TLS_KEY go together")
        return self

    @property
    def scheme(self) -> str:
        """``memory``, ``dummy``, ``redis``, or ``rediss``."""
        return urlsplit(self.URL.get_secret_value()).scheme

    def is_network(self) -> bool:
        """Report whether this cache is a Redis server on another host."""
        host = urlsplit(self.URL.get_secret_value()).hostname
        return self.scheme in REDIS_SCHEMES and host not in LOCAL_HOSTS

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
        parts = urlsplit(self.URL.get_secret_value())
        return bool(parts.password) or self.TLS_CERT is not None


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


ABSOLUTE_URI: Final = r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$"
"""An absolute URI without whitespace (``https://...``, ``urn:...``)."""


class Settings(BaseModel):
    """Validated, read-only settings; extra names are checked at ``setup()``."""

    model_config = ConfigDict(frozen=True, extra="allow", allow_inf_nan=False)

    SETTINGS_MODULE: str = ""
    """Set by the loader to the module that produced these settings."""
    BASE_DIR: Path = Field(default_factory=project_root)
    ENVIRONMENT: Environment = "local"
    """Staging and production get the deployment checks below."""
    DEBUG: bool = False
    PROJECT_NAME: str = "API"
    VERSION: str = "0.1.0"

    INSTALLED_DOMAINS: tuple[str, ...] = ()
    """Domain packages, started in order and stopped in reverse order."""
    MIDDLEWARE: tuple[str, ...] = DEFAULT_MIDDLEWARE
    """ASGI middleware classes by dotted path; the first one is outermost."""
    ROOT_ROUTER: str | None = None
    """Dotted path to the ``APIRouter`` that mounts every route (``ROOT_URLCONF``)."""
    ASGI_APPLICATION: str | None = None
    """``module:attribute`` that ``runserver`` serves."""

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
    LOGGING: dict[str, Any] = Field(default_factory=_minimal_logging)
    """``logging.config.dictConfig`` mapping applied by ``setup()``."""

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
    READINESS_TIMEOUT: float = Field(default=5.0, gt=0)
    """Seconds each readiness check may take before it counts as failed."""
    READINESS_CACHE_SECONDS: float = Field(default=1.0, ge=0)
    """Reuse a readiness result this long, so probe floods cannot load the database."""
    READINESS_DETAILS: bool = False
    """Name failing components in ``/ready`` responses; they are always logged."""
    ACCESS_LOG_QUIET_PATHS: tuple[str, ...] = ("/health", "/ready")
    """Successful requests to these paths are logged at DEBUG, not INFO."""
    TRUSTED_PROXIES: tuple[IPvAnyNetwork, ...] = ()
    """Load balancers whose ``X-Forwarded-For``/``-Proto`` headers are believed."""
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
        if self.ENVIRONMENT in DEPLOYED_ENVIRONMENTS:
            self._check_deployment()
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
        for label, target in self.DATABASE_ROUTING.items():
            if target in self.SHARDS or (target in sql and target not in sharded):
                continue
            raise ValueError(
                f"DATABASE_ROUTING[{label!r}] = {target!r} must name a shard group "
                "or a SQL alias outside the shard groups"
            )

    def deployment_warnings(self) -> list[str]:
        """Weak but valid choices in staging and production; ``check`` lists them.

        Unlike the deployment checks, they do not stop the application: an
        API may publish its documentation on purpose, for example.
        """
        if self.ENVIRONMENT not in DEPLOYED_ENVIRONMENTS:
            return []
        warnings = []
        if self.API_DOCS:
            warnings.append(
                "API_DOCS publishes /docs, /redoc, and /openapi.json to anyone; "
                "set it to False unless the API is public."
            )
        if not self.SECURE_HSTS_SECONDS:
            warnings.append(
                "SECURE_HSTS_SECONDS is 0, so browsers may still use plain HTTP; "
                "set it (31536000 is a year) once HTTPS works everywhere."
            )
        if self.RATE_LIMIT is None or RATE_LIMIT_MIDDLEWARE not in self.MIDDLEWARE:
            warnings.append(
                f"Requests are not rate limited; set RATE_LIMIT and list "
                f"{RATE_LIMIT_MIDDLEWARE} in MIDDLEWARE."
            )
        elif not self.TRUSTED_PROXIES:
            warnings.append(
                "TRUSTED_PROXIES is empty, so callers are rate limited by the "
                "address they connect from: behind a load balancer or proxy, "
                "they all share its count. List its networks in TRUSTED_PROXIES."
            )
        return warnings

    def _check_deployment(self) -> None:
        """Enforce the security baseline for staging and production."""
        where = f"in {self.ENVIRONMENT}"
        if self.DEBUG:
            raise ValueError(f"DEBUG must be False {where}")
        if DEBUG_TOOLBAR_MIDDLEWARE in self.MIDDLEWARE:
            raise ValueError(
                f"{DEBUG_TOOLBAR_MIDDLEWARE} is for development; remove it from "
                f"MIDDLEWARE {where}"
            )
        if "*" in self.ALLOWED_HOSTS:
            raise ValueError(f"ALLOWED_HOSTS must list real host names {where}")
        if any(network.prefixlen == 0 for network in self.TRUSTED_PROXIES):
            raise ValueError(
                f"TRUSTED_PROXIES must list the proxies' networks, not every "
                f"address {where}: any caller could choose its own address"
            )
        rules: dict[str, Callable[[DatabaseConfig], bool]] = {
            "ECHO logs every statement; disable it": lambda db: db.ECHO,
            "LOG_PARAMETERS exposes bound values; disable it": lambda db: (
                db.LOG_PARAMETERS
            ),
            "SSLMODE must be 'verify-full'": lambda db: (
                is_network_database(db.URL.get_secret_value())
                and db.SSLMODE != "verify-full"
            ),
        }
        for message, violates in rules.items():
            if aliases := sorted(a for a, db in self.DATABASES.items() if violates(db)):
                raise ValueError(f"{message} {where} for {aliases}")
        ledger_rules: dict[str, Callable[[LedgerConfig], bool]] = {
            "memory:// ledgers are for tests; use immudb": lambda ledger: (
                ledger.scheme == "memory"
            ),
            "ledgers need TLS_CA (TLS)": lambda ledger: ledger.TLS_CA is None,
            "ledgers need SERVER_SIGNING_KEY (signed states)": lambda ledger: (
                ledger.SERVER_SIGNING_KEY is None
            ),
        }
        for message, violates_ledger in ledger_rules.items():
            ledgers = self.LEDGERS.items()
            if aliases := sorted(a for a, db in ledgers if violates_ledger(db)):
                raise ValueError(f"{message} {where} for LEDGERS {aliases}")
        cache_rules: dict[str, Callable[[CacheConfig], bool]] = {
            "network caches need rediss:// (TLS)": lambda cache: (
                cache.is_network() and cache.scheme != "rediss"
            ),
            "caches must verify the server certificate": lambda cache: (
                not cache.verifies_tls()
            ),
            "network caches need a password or a client certificate": lambda cache: (
                cache.is_network() and not cache.has_credentials()
            ),
        }
        for message, violates_cache in cache_rules.items():
            caches = self.CACHES.items()
            if aliases := sorted(a for a, cache in caches if violates_cache(cache)):
                raise ValueError(f"{message} {where} for CACHES {aliases}")
        broker_rules: dict[str, Callable[[BrokerConfig], bool]] = {
            "memory:// brokers serve one process; use RabbitMQ": lambda broker: (
                broker.scheme == "memory"
            ),
            "network brokers need amqps:// (TLS)": lambda broker: (
                broker.is_network() and broker.scheme != "amqps"
            ),
            "brokers must verify the server certificate": lambda broker: (
                not broker.verifies_tls()
            ),
            "brokers need a user of their own, not guest": lambda broker: (
                broker.scheme != "memory" and not broker.has_credentials()
            ),
        }
        for message, violates_broker in broker_rules.items():
            brokers = self.BROKERS.items()
            if aliases := sorted(a for a, b in brokers if violates_broker(b)):
                raise ValueError(f"{message} {where} for BROKERS {aliases}")
