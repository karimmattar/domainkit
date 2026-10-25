"""Caches per ``CACHES`` alias (the cache counterpart of ``connections``).

Redis clients belong to the event loop that opened their connections, so
each running loop gets its own ``Cache`` objects, as with database engines.
Memory stores belong to the process: every loop sees the same entries.
Loops on several threads (free-threaded Python, a loop per thread) look
caches up at once, so the registries change only under a lock.
"""

import asyncio
import threading
from collections.abc import Callable, Mapping
from functools import partial
from typing import Final
from weakref import WeakKeyDictionary

from domainkit.cache.base import Store
from domainkit.cache.core import Cache
from domainkit.cache.memory import DummyStore, MemoryStore
from domainkit.conf import DEFAULT_CACHE_ALIAS, CacheConfig, settings
from domainkit.conf.global_settings import REDIS_SCHEMES
from domainkit.exceptions import ImproperlyConfigured
from domainkit.utils.closing import close_each
from domainkit.utils.readiness import failing_checks

type StoreFactory = Callable[[CacheConfig], Store]


def redis_store(config: CacheConfig) -> Store:
    """Connect to Redis or Valkey; needs ``domainkit[redis]``."""
    try:
        from domainkit.cache.redis import RedisStore
    except ImportError as exc:  # pragma: no cover - only without the [redis] extra.
        raise ImproperlyConfigured(
            "Redis caches need the redis extra: pip install 'domainkit[redis]'."
        ) from exc
    return RedisStore.from_config(config)


PROCESS_STORES: Final[Mapping[str, StoreFactory]] = {
    "memory": lambda config: MemoryStore(config.MAX_ENTRIES),
    "dummy": lambda config: DummyStore(),
}
"""Stores shared by every event loop of the process, by URL scheme."""


class CacheHandler:
    """Create each configured cache on first use, per event loop."""

    def __init__(self, connect: StoreFactory = redis_store) -> None:
        """Use ``connect`` to build Redis stores (tests may replace it)."""
        self._connect = connect
        self._caches: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Cache]] = (
            WeakKeyDictionary()
        )
        self._replaced: WeakKeyDictionary[asyncio.AbstractEventLoop, list[Cache]] = (
            WeakKeyDictionary()
        )
        self._shared: dict[tuple[str, CacheConfig], Store] = {}
        self._lock = threading.Lock()
        """Held to change the registries: loops on other threads use them too."""

    def config(self, alias: str = DEFAULT_CACHE_ALIAS) -> CacheConfig:
        """Return the ``CACHES`` entry of ``alias``."""
        try:
            return settings.CACHES[alias]
        except KeyError:
            raise ImproperlyConfigured(f"CACHES has no {alias!r} alias.") from None

    def __getitem__(self, alias: str) -> Cache:
        """The running loop's cache for ``alias``; new if its settings changed."""
        config = self.config(alias)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                f"caches[{alias!r}] needs a running event loop: Redis connections "
                "belong to one, so look caches up inside async code."
            ) from None
        with self._lock:
            caches = self._caches.setdefault(loop, {})
        current = caches.get(alias)
        if current is not None and current.config == config:
            return current
        if current is not None:  # override_settings changed it; close it later.
            with self._lock:
                replaced = self._replaced.setdefault(loop, [])
            replaced.append(current)
        caches[alias] = Cache(alias, config, self._store(alias, config))
        return caches[alias]

    def _store(self, alias: str, config: CacheConfig) -> Store:
        """A new Redis store, or the process's store for ``alias``.

        Loops on two threads that ask at once get the same store.
        """
        if config.scheme in REDIS_SCHEMES:
            return self._connect(config)
        key = (alias, config)
        with self._lock:
            store = self._shared.get(key)
            if store is None:
                store = self._shared[key] = PROCESS_STORES[config.scheme](config)
        return store

    async def check_all(self, seconds: float) -> list[str]:
        """Check caches with ``READINESS``; return ``cache:<alias>`` for failures."""
        return await failing_checks(
            {
                f"cache:{alias}": partial(self._check, alias)
                for alias, config in settings.CACHES.items()
                if config.READINESS
            },
            seconds,
        )

    async def _check(self, alias: str) -> None:
        """Run one cache's readiness check."""
        await self[alias].check()

    async def close_all(self) -> None:
        """Close the running loop's caches; the next use opens new ones.

        Every cache is closed even if one fails; the first error is raised.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            current = self._caches.pop(loop, {})
            replaced = self._replaced.pop(loop, [])
        await close_each([*current.values(), *replaced], Cache.close)

    def stop_computations(self) -> list[asyncio.Task[bytes]]:
        """Stop the running loop's cache computations, as shutdown does.

        Callers still waiting compute their own value. Returns the tasks,
        which the shutdown's ``tasks`` phase waits for.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            current = list(self._caches.get(loop, {}).values())
            replaced = list(self._replaced.get(loop, []))
        return [
            task for cache in current + replaced for task in cache.stop_computations()
        ]

    def reset(self) -> None:
        """Forget every memory entry of the process (between tests)."""
        with self._lock:
            stores = list(self._shared.values())
        for store in stores:
            if isinstance(store, MemoryStore):
                store.reset()


caches: Final = CacheHandler()
"""The process-wide caches: ``caches["default"]``."""
