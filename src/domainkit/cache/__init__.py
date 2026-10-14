"""Caches: in process memory, or in Redis or Valkey shared by every replica.

Configure them in ``CACHES`` (``default`` is a memory cache unless set),
then use them anywhere in the application layer::

    from domainkit.cache import cached, caches

    summary = await caches["default"].get_or_set(
        f"invoice:{invoice_id}", lambda: load_summary(invoice_id), InvoiceSummary
    )

    @cached(ttl=60)
    async def invoice_summary(invoice_id: UUID) -> InvoiceSummary: ...

Values are JSON; a failing cache is skipped, not fatal. See ADR 0012 and
``docs/cache.md``.
"""

from domainkit.cache.base import CacheError, CacheUnavailable, Store
from domainkit.cache.core import DEFAULT_TTL, TTL, Cache
from domainkit.cache.decorators import CachedFunction, cached, invalidate_on
from domainkit.cache.handler import CacheHandler, caches
from domainkit.cache.memory import DummyStore, MemoryStore

__all__ = [
    "DEFAULT_TTL",
    "TTL",
    "Cache",
    "CacheError",
    "CacheHandler",
    "CacheUnavailable",
    "CachedFunction",
    "DummyStore",
    "MemoryStore",
    "Store",
    "cached",
    "caches",
    "invalidate_on",
]
