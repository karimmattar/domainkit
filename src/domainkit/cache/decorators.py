"""``@cached`` for async functions, and ``invalidate_on`` for domain events."""

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from functools import cached_property, update_wrapper
from typing import Any, get_type_hints

from pydantic_core import PydanticSerializationError, to_jsonable_python

from domainkit.cache.core import DEFAULT_TTL, TTL
from domainkit.cache.handler import caches
from domainkit.conf import DEFAULT_CACHE_ALIAS
from domainkit.ddd import DomainEvent, EventBus, event_bus


class CachedFunction[**P, R]:
    """An async function whose results are cached by its arguments."""

    __name__: str
    __qualname__: str

    def __init__(
        self,
        function: Callable[P, Awaitable[R]],
        *,
        cache: str,
        ttl: TTL,
        name: str | None,
    ) -> None:
        """Wrap ``function``; its entries are named ``<name>:<argument hash>``."""
        update_wrapper(self, function)
        self.function = function
        self.cache = cache
        self.ttl = ttl
        self.name = name or f"{function.__module__}.{function.__qualname__}"
        self._signature = inspect.signature(function)

    @cached_property
    def return_type(self) -> Any:  # noqa: ANN401 - any type form.
        """The annotated return type: cached values are validated as it."""
        return get_type_hints(self.function).get("return", Any)

    def key(self, *args: P.args, **kwargs: P.kwargs) -> str:
        """The key of the entry for these arguments (defaults applied)."""
        bound = self._signature.bind(*args, **kwargs)
        bound.apply_defaults()
        try:
            arguments = to_jsonable_python(bound.arguments)
        except PydanticSerializationError as exc:
            raise TypeError(
                f"Arguments of {self.name} must be JSON-friendly to be cached: {exc}"
            ) from None
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        return f"{self.name}:{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"

    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Return the cached result, or call the function and cache it."""
        result: R = await caches[self.cache].get_or_set(
            self.key(*args, **kwargs),
            lambda: self.function(*args, **kwargs),
            self.return_type,
            self.ttl,
        )
        return result

    async def invalidate(self, *args: P.args, **kwargs: P.kwargs) -> bool:
        """Delete the entry for these arguments; report whether it existed."""
        return await caches[self.cache].delete(self.key(*args, **kwargs))


def cached[**P, R](
    *,
    ttl: TTL = DEFAULT_TTL,
    cache: str = DEFAULT_CACHE_ALIAS,
    name: str | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], CachedFunction[P, R]]:
    """Cache an async function's results, keyed by its arguments.

    For example::

        @cached(ttl=60)
        async def invoice_summary(invoice_id: UUID) -> InvoiceSummary: ...

        await invoice_summary(invoice_id)             # cached
        await invoice_summary.invalidate(invoice_id)  # forgotten

    Arguments must be JSON-friendly (they are hashed into the key), and the
    return annotation types the cached value. Results are cached per
    argument values, so decorate functions, not methods. ``name`` (default:
    the function's import path) is part of every key: set it to keep
    entries when the function moves.
    """

    def decorate(function: Callable[P, Awaitable[R]]) -> CachedFunction[P, R]:
        if not inspect.iscoroutinefunction(function):
            raise TypeError("@cached decorates async functions.")
        return CachedFunction(function, cache=cache, ttl=ttl, name=name)

    return decorate


def invalidate_on[E: DomainEvent](
    event_type: type[E],
    keys: Callable[[E], Iterable[str]],
    *,
    cache: str = DEFAULT_CACHE_ALIAS,
    bus: EventBus = event_bus,
) -> None:
    """Delete the entries ``keys(event)`` names after each ``event_type`` event.

    Call it in ``DomainConfig.ready()``. Units of work publish events after
    they commit, so entries are deleted once the change is visible::

        invalidate_on(InvoicePaid, lambda e: [invoice_summary.key(e.invoice_id)])

    A deletion that fails (the cache is down) is logged and reported by the
    event bus; the entry then lives until its TTL ends.
    """

    async def invalidate(event: E) -> None:
        await caches[cache].delete_many(keys(event))

    invalidate.__qualname__ = f"invalidate_on({event_type.__qualname__})"
    bus.subscribe(event_type, invalidate)
