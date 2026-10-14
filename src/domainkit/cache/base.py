"""The store port: what a cache backend implements.

A store keeps bytes under full keys, with expiry in milliseconds. ``Cache``
(``domainkit.cache.core``) builds keys, encodes values, and handles failures
on top of it, so a new backend implements only these operations.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence


class CacheError(Exception):
    """Base class of cache errors."""


class CacheUnavailable(CacheError):
    """The cache could not be reached in time, or is skipped after a failure."""


class Store(ABC):
    """Bytes under full keys; ``ttl`` is in milliseconds, None keeps the entry.

    Raise ``CacheUnavailable`` when the backend cannot be reached, and
    ``CacheError`` when it refuses an operation.
    """

    @abstractmethod
    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """Return the value of each key, or None where it is missing or expired."""

    @abstractmethod
    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        """Store every item, replacing existing values."""

    @abstractmethod
    async def add(self, key: str, value: bytes, ttl: int | None) -> bool:
        """Store ``value`` only if ``key`` is missing; report whether it was."""

    @abstractmethod
    async def delete_many(self, keys: Sequence[str]) -> int:
        """Delete the keys; return how many existed."""

    @abstractmethod
    async def incr(self, key: str, delta: int, ttl: int | None) -> int:
        """Add ``delta`` to an integer value and return it.

        A missing key starts at 0 and gets ``ttl``; an existing key keeps its
        expiry. Raise ``CacheError`` if the value is not an integer.
        """

    @abstractmethod
    async def touch(self, key: str, ttl: int | None) -> bool:
        """Give ``key`` a new expiry (None: none); report whether it exists."""

    @abstractmethod
    async def clear(self, prefix: str) -> int:
        """Delete every key starting with ``prefix``; return how many."""

    async def check(self) -> None:  # noqa: B027 - optional; most stores have it.
        """Raise if the backend cannot be reached (readiness checks)."""

    async def close(self) -> None:  # noqa: B027 - optional, for connections.
        """Release connections; the store is not used afterwards."""
