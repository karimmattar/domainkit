"""Stores in process memory: ``memory://``, and ``dummy://``, which keeps nothing."""

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from domainkit.cache.base import CacheError, Store

_INTEGER: Final = re.compile(rb"-?[0-9]+")


class MemoryStore(Store):
    """Entries in this process, evicted least recently used first.

    Each process has its own entries, so replicas do not share them and a
    deletion in one process does not reach the others. Use Redis for values
    that every replica must see change. Every event loop of the process
    shares the entries, so a lock keeps threads (test clients) apart.
    """

    def __init__(
        self, max_entries: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Keep at most ``max_entries``; ``clock`` reads seconds."""
        self.max_entries = max_entries
        self.clock = clock
        self._entries: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()
        self._lock = threading.Lock()

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """Return live values, marking each as recently used."""
        with self._lock:
            now = self.clock()
            return [self._live(key, now) for key in keys]

    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        """Store every item."""
        with self._lock:
            now = self.clock()
            for key, value in items.items():
                self._put(key, value, self._expiry(ttl, now))

    async def add(self, key: str, value: bytes, ttl: int | None) -> bool:
        """Store ``value`` unless ``key`` holds a live value."""
        with self._lock:
            now = self.clock()
            if self._live(key, now) is not None:
                return False
            self._put(key, value, self._expiry(ttl, now))
            return True

    async def delete_many(self, keys: Sequence[str]) -> int:
        """Delete the keys; count those that were live."""
        with self._lock:
            now = self.clock()
            deleted = 0
            for key in keys:
                deleted += self._live(key, now) is not None
                self._entries.pop(key, None)
            return deleted

    async def incr(self, key: str, delta: int, ttl: int | None) -> int:
        """Add ``delta`` to the integer under ``key``, starting at 0."""
        with self._lock:
            now = self.clock()
            current = self._live(key, now)
            if current is None:
                value, expires = delta, self._expiry(ttl, now)
            elif _INTEGER.fullmatch(current):
                value, expires = int(current) + delta, self._entries[key][1]
            else:
                raise CacheError("The value is not an integer.")
            self._put(key, str(value).encode(), expires)
            return value

    async def touch(self, key: str, ttl: int | None) -> bool:
        """Give a live ``key`` a new expiry."""
        with self._lock:
            now = self.clock()
            value = self._live(key, now)
            if value is None:
                return False
            self._put(key, value, self._expiry(ttl, now))
            return True

    async def clear(self, prefix: str) -> int:
        """Delete the keys starting with ``prefix``."""
        with self._lock:
            doomed = [key for key in self._entries if key.startswith(prefix)]
            for key in doomed:
                del self._entries[key]
            return len(doomed)

    def reset(self) -> None:
        """Forget every entry (tests)."""
        with self._lock:
            self._entries.clear()

    def _live(self, key: str, now: float) -> bytes | None:
        """Return the value of ``key`` unless missing or expired; drop expired ones."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and expires <= now:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def _put(self, key: str, value: bytes, expires: float | None) -> None:
        """Store an entry as the most recently used; evict beyond the limit."""
        self._entries[key] = (value, expires)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    @staticmethod
    def _expiry(ttl: int | None, now: float) -> float | None:
        """Turn a ``ttl`` in milliseconds into a clock reading."""
        return None if ttl is None else now + ttl / 1000


class DummyStore(Store):
    """Keep nothing: every read misses. Turns caching off without code changes."""

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """Miss every key."""
        return [None] * len(keys)

    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        """Store nothing."""

    async def add(self, key: str, value: bytes, ttl: int | None) -> bool:
        """Report the key as added, as it was missing."""
        return True

    async def delete_many(self, keys: Sequence[str]) -> int:
        """Nothing existed."""
        return 0

    async def incr(self, key: str, delta: int, ttl: int | None) -> int:
        """Count from 0 every time."""
        return delta

    async def touch(self, key: str, ttl: int | None) -> bool:
        """Nothing exists."""
        return False

    async def clear(self, prefix: str) -> int:
        """Nothing to delete."""
        return 0
