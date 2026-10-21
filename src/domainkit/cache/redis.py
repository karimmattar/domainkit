"""Redis and Valkey stores (``redis://``, ``rediss://``), through redis-py.

Each event loop gets its own client (the cache handler sees to it), with a
bounded pool, short timeouts, and one retry: a slow cache is worse than no
cache, and ``Cache`` falls back to computing values when this one fails.
Valkey speaks the same protocol, so the same URLs and client work for it.

A ``rediss`` URL connects over TLS; connections share one TLS context per
process for each set of TLS settings, built on a worker thread at the first
connect, never per connection on the event loop.

A Sentinel group (``redis+sentinel://``) gives the address of the current
primary, and the client follows failovers. A Cluster (``redis+cluster://``)
spreads keys over its nodes by hash slot; every operation here touches one
key per command, or splits its keys by slot, so none is refused as crossing
slots. The rate limiter's script reads and writes one key, so it runs on
the node that holds it.
"""

import asyncio
import re
import threading
from collections.abc import Awaitable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.cluster import ClusterNode, RedisCluster
from redis.asyncio.connection import RedisSSLContext, SSLConnection
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import (
    Sentinel,
    SentinelConnectionPool,
    SentinelManagedSSLConnection,
)
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisClusterException, RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from domainkit.cache.base import CacheError, CacheUnavailable, Store
from domainkit.conf import CacheConfig
from domainkit.utils.redis_urls import CLUSTER_SCHEMES, RedisTarget, parse_redis_url

_CLEAR_BATCH: Final = 500

_INCR: Final = """
if ARGV[2] ~= '' then
  redis.call('SET', KEYS[1], 0, 'NX', 'PX', ARGV[2])
end
return redis.call('INCRBY', KEYS[1], ARGV[1])
"""
"""Start a missing counter at 0 with an expiry, then add (one atomic step)."""

_GCRA: Final = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000000 + tonumber(time[2])
local interval = tonumber(ARGV[1])
local tolerance = tonumber(ARGV[2])
local arrival = now
local stored = redis.call('GET', KEYS[1])
if stored then
  arrival = math.max(tonumber(stored), now)
end
local debt = arrival - now
if debt > tolerance then
  return {0, string.format('%.17g', debt)}
end
local next_arrival = arrival + interval
redis.call('SET', KEYS[1], string.format('%.17g', next_arrival),
  'PX', math.ceil((next_arrival - now) / 1000))
return {1, string.format('%.17g', debt)}
"""
"""GCRA on the server's clock, in microseconds; see ``http.ratelimit``.

It compares the debt (``arrival - now``) with the tolerance, never sums of
large clock readings, and keeps each caller only until its quota is full.
"""


if TYPE_CHECKING:
    _TLSConnection = SSLConnection
else:
    _TLSConnection = object


class _SharedTLS(_TLSConnection):
    """A connection that shares its TLS context and builds it off the loop.

    redis-py makes a context for each connection at its first connect, on
    the event loop, reading the system's CA store and the certificate files
    every time. These connections share one per process for each set of
    TLS settings, and the first connect builds it on a worker thread.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - redis-py's.
        """Make the connection, then swap in the shared context."""
        super().__init__(*args, **kwargs)
        self.ssl_context = _shared(self.ssl_context)

    async def connect(self) -> None:
        """Build the shared context on a thread the first time, then connect."""
        if self.ssl_context.context is None:
            await asyncio.to_thread(self.ssl_context.get)
        await super().connect()  # type: ignore[no-untyped-call]  # redis-py leaves it untyped.


class SharedTLSConnection(_SharedTLS, SSLConnection):
    """A TLS connection to a server or a Cluster node, with the shared context."""


class SharedTLSSentinelConnection(_SharedTLS, SentinelManagedSSLConnection):
    """A TLS connection to the primary a Sentinel names, with the shared context."""


_contexts: dict[tuple[tuple[str, object], ...], RedisSSLContext] = {}
_contexts_lock: Final = threading.Lock()


def _shared(context: RedisSSLContext) -> RedisSSLContext:
    """The process's context with ``context``'s settings; ``context`` if new."""
    names = getattr(type(context), "__slots__", None) or vars(context)
    settings = tuple(
        (name, _frozen(getattr(context, name))) for name in names if name != "context"
    )
    try:
        hash(settings)
    except TypeError:  # A setting redis-py adds one day, of a type we cannot key.
        return context
    with _contexts_lock:
        return _contexts.setdefault(settings, context)


def _frozen(value: object) -> object:
    """``value``, with lists (redis-py's verify flags) as tuples."""
    return tuple(value) if isinstance(value, list) else value


class BlockingSentinelPool(SentinelConnectionPool, BlockingConnectionPool):
    """Sentinel's pool that waits for a free connection, as the server's pool does.

    Sentinel's own pool refuses a command at once when every connection is
    busy, which a burst of requests would turn into a cache outage.
    """


type Client = Redis | RedisCluster


def create_client(config: CacheConfig) -> Redis:
    """Build a client: bounded pool, short timeouts, ``RETRIES`` quick retries.

    A Sentinel URL gives a client of the primary the Sentinels name. For a
    Cluster, use ``create_cluster_client``.
    """
    target = parse_redis_url(config.URL.get_secret_value())
    if target.kind == "cluster":
        raise ValueError("A Cluster needs create_cluster_client().")
    options = {**_options(config), "timeout": config.CONNECT_TIMEOUT}
    tls = _tls_options(config)
    if target.kind == "sentinel":
        return _primary(config, target, options, tls)
    tls.pop("ssl", None)  # The scheme says it.
    pool = BlockingConnectionPool.from_url(
        config.URL.get_secret_value(), **options, **tls
    )
    if pool.connection_class is SSLConnection:
        pool.connection_class = SharedTLSConnection
    return Redis.from_pool(pool)


def create_cluster_client(config: CacheConfig) -> RedisCluster:
    """Build a Cluster client from its seed nodes; it finds the others itself."""
    target = parse_redis_url(config.URL.get_secret_value())
    client = RedisCluster(
        startup_nodes=[ClusterNode(host, port) for host, port in target.hosts],
        username=target.username,
        password=target.password,
        **_options(config),
        **_tls_options(config),
    )
    if config.uses_tls():
        # The nodes it starts from, and those it finds, share one context.
        client.nodes_manager.connection_kwargs["connection_class"] = SharedTLSConnection
        for node in client.nodes_manager.startup_nodes.values():
            node.connection_class = SharedTLSConnection
    return client


def _options(config: CacheConfig) -> dict[str, Any]:
    """The pool size, the timeouts, and the retries every client takes."""
    return {
        "max_connections": config.MAX_CONNECTIONS,
        "socket_timeout": config.SOCKET_TIMEOUT,
        "socket_connect_timeout": config.CONNECT_TIMEOUT,
        "retry": _retry(config),
    }


def _retry(config: CacheConfig) -> Retry:
    """``RETRIES`` quick retries: a slow cache is worse than none."""
    return Retry(ExponentialWithJitterBackoff(cap=0.1, base=0.01), config.RETRIES)


def _tls_options(config: CacheConfig) -> dict[str, Any]:
    """redis-py's TLS arguments: on for ``rediss`` schemes, with the CA and cert."""
    if not config.uses_tls():
        return {}
    options: dict[str, Any] = {"ssl": True}
    if config.TLS_CA is not None:
        options["ssl_ca_certs"] = str(config.TLS_CA)
    if config.TLS_CERT is not None and config.TLS_KEY is not None:
        options["ssl_certfile"] = str(config.TLS_CERT)
        options["ssl_keyfile"] = str(config.TLS_KEY)
    return options


def _primary(
    config: CacheConfig,
    target: RedisTarget,
    options: dict[str, Any],
    tls: dict[str, Any],
) -> Redis:
    """A client of the primary the Sentinels name, following failovers.

    The Sentinels get the same timeouts and TLS, and ``SENTINEL_PASSWORD``.
    """
    sentinel_options: dict[str, Any] = {
        "socket_timeout": config.SOCKET_TIMEOUT,
        "socket_connect_timeout": config.CONNECT_TIMEOUT,
        "retry": _retry(config),
        **tls,
    }
    if config.SENTINEL_PASSWORD is not None:
        sentinel_options["password"] = config.SENTINEL_PASSWORD.get_secret_value()
    sentinel = Sentinel(  # type: ignore[no-untyped-call]  # redis-py leaves it untyped.
        list(target.hosts), sentinel_kwargs=sentinel_options
    )
    if tls:
        options = {**options, "connection_class": SharedTLSSentinelConnection}
    client: Redis = sentinel.master_for(
        target.service or "",
        connection_pool_class=BlockingSentinelPool,
        username=target.username,
        password=target.password,
        db=target.db,
        **options,
        **tls,
    )
    return client


async def _run[T](command: Awaitable[T]) -> T:
    """Await a Redis command, translating its errors to cache errors.

    A Cluster that cannot be reached, or with slots no node serves, raises
    an error outside redis-py's own hierarchy: it counts as unavailable.
    """
    try:
        return await command
    except (
        RedisConnectionError,
        RedisTimeoutError,
        RedisClusterException,
        OSError,
    ) as exc:
        raise CacheUnavailable(f"Redis is unavailable: {exc}") from exc
    except RedisError as exc:
        raise CacheError(f"Redis refused the command: {exc}") from exc


class RedisStore(Store):
    """A store on one Redis or Valkey server, or the primary Sentinel names."""

    def __init__(self, client: Client) -> None:
        """Use ``client``; the store closes it."""
        self.client = client
        self._incr = client.register_script(_INCR)
        self._gcra = client.register_script(_GCRA)

    @classmethod
    def from_config(cls, config: CacheConfig) -> "RedisStore":
        """Connect as ``config`` says (lazily: the first command connects)."""
        if config.scheme in CLUSTER_SCHEMES:
            return ClusterStore(create_cluster_client(config))
        return cls(create_client(config))

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """``MGET`` the keys."""
        values = await _run(self.client.mget(keys))
        return cast("list[bytes | None]", values)  # Not decode_responses: bytes.

    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        """``SET`` each item, in one round trip."""
        if len(items) == 1:
            ((key, value),) = items.items()
            await _run(self.client.set(key, value, px=ttl))
            return
        pipeline = self.client.pipeline(transaction=False)
        for key, value in items.items():
            pipeline.set(key, value, px=ttl)
        await _run(pipeline.execute())

    async def add(self, key: str, value: bytes, ttl: int | None) -> bool:
        """``SET NX``."""
        return bool(await _run(self.client.set(key, value, px=ttl, nx=True)))

    async def delete_many(self, keys: Sequence[str]) -> int:
        """``DEL`` the keys."""
        if not keys:
            return 0
        return int(await _run(self.client.delete(*keys)))

    async def incr(self, key: str, delta: int, ttl: int | None) -> int:
        """``INCRBY``, giving a new counter its expiry in the same step."""
        args = [delta, "" if ttl is None else ttl]
        return int(await _run(self._incr(keys=[key], args=args)))

    async def touch(self, key: str, ttl: int | None) -> bool:
        """``PEXPIRE``, or ``PERSIST`` to keep the entry."""
        if ttl is not None:
            return bool(await _run(self.client.pexpire(key, ttl)))
        pipeline = self.client.pipeline(transaction=False)
        pipeline.exists(key)
        pipeline.persist(key)
        exists, _ = await _run(pipeline.execute())
        return bool(exists)

    async def clear(self, prefix: str) -> int:
        """``SCAN`` for the prefix and ``UNLINK`` in batches; never ``FLUSHDB``."""
        pattern = re.sub(r"([*?\[\]\\])", r"\\\1", prefix) + "*"
        deleted = 0
        batch: list[bytes] = []

        async def scan_and_unlink() -> None:
            nonlocal deleted
            async for key in self.client.scan_iter(match=pattern, count=1000):
                batch.append(key)
                if len(batch) >= _CLEAR_BATCH:
                    deleted += await self.client.unlink(*batch)
                    batch.clear()
            if batch:
                deleted += await self.client.unlink(*batch)

        await _run(scan_and_unlink())
        return deleted

    async def gcra(
        self, key: str, interval: float, tolerance: float
    ) -> tuple[bool, float]:
        """Count one request for ``key``; return (allowed, debt), in microseconds."""
        allowed, debt = await _run(self._gcra(keys=[key], args=[interval, tolerance]))
        return bool(allowed), float(debt)

    async def check(self) -> None:
        """``PING``."""
        await _run(self.client.ping())

    async def close(self) -> None:
        """Close the client and its pool, then the Sentinels' connections."""
        pool = getattr(self.client, "connection_pool", None)
        try:
            await self.client.aclose()
        finally:
            if isinstance(pool, SentinelConnectionPool):
                await pool.sentinel_manager.aclose()


class ClusterStore(RedisStore):
    """A store on a Redis or Valkey Cluster: keys spread over nodes by slot."""

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """``MGET`` the keys of each slot (one ``MGET`` would cross slots)."""
        client = cast("RedisCluster", self.client)
        values = await _run(client.mget_nonatomic(list(keys)))
        return cast("list[bytes | None]", values)
