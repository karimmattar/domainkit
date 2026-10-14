"""Redis and Valkey stores (``redis://``, ``rediss://``), through redis-py.

Each event loop gets its own client (the cache handler sees to it), with a
bounded pool, short timeouts, and one retry: a slow cache is worse than no
cache, and ``Cache`` falls back to computing values when this one fails.
Valkey speaks the same protocol, so the same URLs and client work for it.
"""

import re
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any, Final, cast

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from domainkit.cache.base import CacheError, CacheUnavailable, Store
from domainkit.conf import CacheConfig

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


def create_client(config: CacheConfig) -> Redis:
    """Build a client: bounded pool, short timeouts, ``RETRIES`` quick retries."""
    options: dict[str, Any] = {
        "max_connections": config.MAX_CONNECTIONS,
        "timeout": config.CONNECT_TIMEOUT,
        "socket_timeout": config.SOCKET_TIMEOUT,
        "socket_connect_timeout": config.CONNECT_TIMEOUT,
        "retry": Retry(
            ExponentialWithJitterBackoff(cap=0.1, base=0.01), config.RETRIES
        ),
    }
    if config.TLS_CA is not None:
        options["ssl_ca_certs"] = str(config.TLS_CA)
    if config.TLS_CERT is not None and config.TLS_KEY is not None:
        options["ssl_certfile"] = str(config.TLS_CERT)
        options["ssl_keyfile"] = str(config.TLS_KEY)
    pool = BlockingConnectionPool.from_url(config.URL.get_secret_value(), **options)
    return Redis.from_pool(pool)


async def _run[T](command: Awaitable[T]) -> T:
    """Await a Redis command, translating its errors to cache errors."""
    try:
        return await command
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
        raise CacheUnavailable(f"Redis is unavailable: {exc}") from exc
    except RedisError as exc:
        raise CacheError(f"Redis refused the command: {exc}") from exc


class RedisStore(Store):
    """A store on one Redis or Valkey server (or a managed endpoint)."""

    def __init__(self, client: Redis) -> None:
        """Use ``client``; the store closes it."""
        self.client = client
        self._incr = client.register_script(_INCR)
        self._gcra = client.register_script(_GCRA)

    @classmethod
    def from_config(cls, config: CacheConfig) -> "RedisStore":
        """Connect as ``config`` says (lazily: the first command connects)."""
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
        """Close the client and its pool."""
        await self.client.aclose()
