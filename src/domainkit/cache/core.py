"""``Cache``: keys, JSON values, expiry, and failure handling over a ``Store``.

Values are stored as JSON (``pydantic_core``), never pickled, so whoever can
write to the cache cannot run code in the service. Reads take a ``type`` to
turn JSON back into models, dataclasses, UUIDs, or dates; an entry that no
longer fits its type counts as a miss.

A cache is an optimization. When its store fails, the cache is skipped for
``FAILURE_COOLDOWN`` seconds, and ``get_or_set`` (and ``@cached``) compute
values instead of failing, still once per key for concurrent callers. The
plain operations raise ``CacheUnavailable``, so callers that need the cache
can tell.
"""

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import timedelta
from enum import Enum
from functools import cache, partial
from typing import Any, Final, Literal, overload

from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError, from_json, to_json

from domainkit import deadlines
from domainkit.cache.base import CacheError, CacheUnavailable, Store
from domainkit.conf import CacheConfig
from domainkit.deadlines import Deadline, current_deadline
from domainkit.tasks import spawn

try:
    from domainkit.cache.metrics import observe, observe_error
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe(cache: str, result: str) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""

    def observe_error(cache: str) -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


logger = logging.getLogger(__name__)

MAX_KEY_LENGTH: Final = 1024
"""Longest key accepted; hash long parts of keys (``@cached`` does)."""
COMPUTE_TASK: Final = "domainkit.cache.compute"
"""The name of ``get_or_set`` computations' tasks."""
_EARLY: Final = b"~"
"""Starts the header of ``get_or_set`` entries; JSON never starts with it."""


class _Default(Enum):
    TTL = "default"


DEFAULT_TTL: Final = _Default.TTL
"""Use the cache's ``TTL`` setting."""

type TTL = float | timedelta | Literal[_Default.TTL] | None
"""How long an entry lives: seconds or a ``timedelta``; None keeps it."""

type TypeForm = Any
"""A type or type expression ``TypeAdapter`` accepts, like ``list[Invoice]``."""

_MISS: Final = object()


@cache
def adapter(type_: TypeForm) -> TypeAdapter[Any]:
    """Build (once) the validator that turns JSON into ``type_``."""
    return TypeAdapter(type_)


def encode(value: object) -> bytes:
    """Serialize ``value`` to JSON; refuse what JSON cannot hold."""
    try:
        return to_json(value)
    except PydanticSerializationError as exc:
        raise TypeError(
            f"{type(value).__name__} values cannot be cached: {exc}. Cache "
            "JSON-friendly values (models, dataclasses, dicts, lists, UUIDs)."
        ) from None


def decode(payload: bytes, type_: TypeForm) -> Any:  # noqa: ANN401 - as type_.
    """Parse JSON, validated as ``type_`` unless it is None."""
    if type_ is None:
        return from_json(payload)
    return adapter(type_).validate_json(payload)


def split(raw: bytes) -> tuple[bytes, float | None, float]:
    """Return an entry's JSON, and the expiry and cost ``get_or_set`` stored."""
    if not raw.startswith(_EARLY):
        return raw, None, 0.0
    header, _, payload = raw.partition(b"\n")
    expires, _, delta = header[1:].partition(b",")
    return payload, float(expires), float(delta)


class _Computation:
    """A shared ``get_or_set`` computation of one key, and how it ended."""

    __slots__ = ("abandoned", "borrows", "deadline", "owner_failed", "stopped", "task")

    task: asyncio.Task[bytes]

    def __init__(self, *, borrows: bool, deadline: Deadline | None) -> None:
        """Compute for callers; with ``borrows``, bound to its owner's ``deadline``."""
        self.borrows = borrows
        """Whether the factory uses what its owner holds: the computation is
        then the owner's, and ends with it."""
        self.deadline = deadline
        """The owner's deadline, which a computation that borrows works under."""
        self.abandoned = False
        """Its owner was cancelled, so it was stopped: callers still waiting
        share another one."""
        self.stopped = False
        """Shutdown or ``close()`` stopped it: callers still waiting compute
        their own value, without the store."""
        self.owner_failed = False
        """It failed for its owner's sake (the owner's deadline): only the
        owner gets the error; callers still waiting share another one."""


class Cache:
    """One configured cache; get it from ``domainkit.cache.caches``."""

    def __init__(
        self,
        alias: str,
        config: CacheConfig,
        store: Store,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        uniform: Callable[[], float] = random.random,
    ) -> None:
        """Serve ``alias`` from ``store``; the clocks and ``uniform`` are for tests."""
        self.alias = alias
        self.config = config
        self.store = store
        self.namespace = f"{config.KEY_PREFIX or alias}:"
        """Every key of this cache starts with it (``clear`` deletes them)."""
        self._prefix = f"{self.namespace}{config.VERSION}:"
        self._clock = clock
        self._monotonic = monotonic
        self._uniform = uniform
        self._skip_until = -math.inf
        self._computing: dict[str, _Computation] = {}

    def make_key(self, key: str) -> str:
        """Return the full key stored for ``key``: namespace, version, key."""
        if not key or len(key) > MAX_KEY_LENGTH:
            raise ValueError(
                f"Cache keys have 1 to {MAX_KEY_LENGTH} characters; hash long ones."
            )
        return self._prefix + key

    @overload
    async def get(self, key: str) -> Any: ...  # noqa: ANN401 - JSON values.

    @overload
    async def get[T](self, key: str, type: type[T]) -> T | None: ...

    async def get(self, key: str, type: TypeForm = None) -> Any:
        """Return the value of ``key`` as ``type`` (JSON if None), or None on a miss."""
        (raw,) = await self.call(self.store.get_many, [self.make_key(key)])
        value, _ = self._load(raw, type)
        return None if value is _MISS else value

    @overload
    async def get_many(self, keys: Iterable[str]) -> dict[str, Any]: ...

    @overload
    async def get_many[T](self, keys: Iterable[str], type: type[T]) -> dict[str, T]: ...

    async def get_many(
        self, keys: Iterable[str], type: TypeForm = None
    ) -> dict[str, Any]:
        """Return the values found, by key; misses are left out."""
        keys = list(keys)
        raws = await self.call(self.store.get_many, [self.make_key(k) for k in keys])
        found = {
            key: self._load(raw, type)[0] for key, raw in zip(keys, raws, strict=True)
        }
        return {key: value for key, value in found.items() if value is not _MISS}

    async def set(self, key: str, value: object, ttl: TTL = DEFAULT_TTL) -> None:
        """Store ``value`` under ``key`` for ``ttl``."""
        entry = {self.make_key(key): encode(value)}
        await self.call(self.store.set_many, entry, self._milliseconds(ttl))

    async def set_many(
        self, items: Mapping[str, object], ttl: TTL = DEFAULT_TTL
    ) -> None:
        """Store every item for ``ttl``."""
        entries = {self.make_key(key): encode(value) for key, value in items.items()}
        if entries:
            await self.call(self.store.set_many, entries, self._milliseconds(ttl))

    async def add(self, key: str, value: object, ttl: TTL = DEFAULT_TTL) -> bool:
        """Store ``value`` only if ``key`` is missing; report whether it was."""
        full, milliseconds = self.make_key(key), self._milliseconds(ttl)
        return await self.call(self.store.add, full, encode(value), milliseconds)

    async def delete(self, key: str) -> bool:
        """Delete ``key``; report whether it existed."""
        return await self.delete_many([key]) > 0

    async def delete_many(self, keys: Iterable[str]) -> int:
        """Delete the keys; return how many existed."""
        full = [self.make_key(key) for key in keys]
        return await self.call(self.store.delete_many, full) if full else 0

    async def incr(self, key: str, delta: int = 1, ttl: TTL = DEFAULT_TTL) -> int:
        """Add ``delta`` to the integer under ``key`` and return the total.

        A missing key starts at 0 and expires after ``ttl``; later increments
        keep that expiry, so ``incr`` counts in fixed windows.
        """
        full, milliseconds = self.make_key(key), self._milliseconds(ttl)
        return await self.call(self.store.incr, full, delta, milliseconds)

    async def touch(self, key: str, ttl: TTL = DEFAULT_TTL) -> bool:
        """Give ``key`` a new expiry; report whether it exists."""
        full, milliseconds = self.make_key(key), self._milliseconds(ttl)
        return await self.call(self.store.touch, full, milliseconds)

    async def clear(self) -> int:
        """Delete every key of this cache, all versions; return how many.

        Only keys in this cache's namespace go, so other services sharing
        the server keep theirs. Rate-limit counters kept in this cache are
        reset too.
        """
        return await self.call(self.store.clear, self.namespace)

    @overload
    async def get_or_set[T](
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        type: type[T],
        ttl: TTL = DEFAULT_TTL,
        *,
        borrows: bool = False,
    ) -> T: ...

    @overload
    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[object]],
        type: None = None,
        ttl: TTL = DEFAULT_TTL,
        *,
        borrows: bool = False,
    ) -> Any: ...  # noqa: ANN401 - JSON values.

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        type: TypeForm = None,
        ttl: TTL = DEFAULT_TTL,
        *,
        borrows: bool = False,
    ) -> Any:
        """Return the cached value of ``key``, or compute, store, and return it.

        Concurrent misses in this process share one ``factory`` call, and hot
        entries are recomputed shortly before they expire, by one caller
        while the others still get the old value (XFetch); if that early
        refresh fails, the cached value is served until it expires. If the
        cache fails, ``factory`` is called, still once for concurrent callers,
        and the value is returned anyway. Every caller gets its own copy,
        decoded from JSON, so hits and misses look alike and no two requests
        share a mutable object.

        The computation serves every caller, so by default it belongs to
        none: it runs on when they stop waiting, within ``COMPUTE_TIMEOUT``,
        and stores its value, and the factory must use nothing a caller
        holds. Pass ``borrows=True`` for a factory that uses what its caller
        holds and releases (the session of the caller's unit of work): the
        computation is then bound to the caller that starts it, under that
        caller's deadline, and ends with it (ADR 0071).
        """
        full, milliseconds = self.make_key(key), self._milliseconds(ttl)
        compute = partial(
            self._compute_once, full, factory, milliseconds, borrows=borrows
        )
        try:
            (raw,) = await self.call(self.store.get_many, [full])
        except CacheError as exc:
            self._worked_around("read", exc)
            return decode(await compute(write=False), type)
        value, refresh = self._load(raw, type, early=True)
        if value is _MISS:
            return decode(await compute(), type)
        if not refresh:
            return value
        try:
            payload = await compute()
        except Exception:
            logger.warning(
                "Cache %r could not refresh an entry early; serving it until it "
                "expires",
                self.alias,
                exc_info=True,
            )
            return value
        return decode(payload, type)

    async def check(self) -> None:
        """Raise if the store cannot be reached, cooldown or not."""
        await self.store.check()

    async def close(self) -> None:
        """Stop the computations still running, then close the store.

        Callers still waiting for a stopped computation compute their own
        value, as when the cache fails.
        """
        await asyncio.gather(*self.stop_computations(), return_exceptions=True)
        await self.store.close()

    def stop_computations(self) -> list[asyncio.Task[bytes]]:
        """Cancel the computations still running, as stopped; return their tasks.

        For shutdown and ``close()``: callers still waiting compute their
        own value. The caller awaits the tasks.
        """
        stopping = []
        for computation in self._computing.values():
            computation.stopped = True
            computation.task.cancel()
            stopping.append(computation.task)
        return stopping

    async def call[**P, R](
        self, operation: Callable[P, Awaitable[R]], *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run a store operation, skipping the store while it cools down.

        Store-specific features (the Redis rate limiter) use this to get the
        same failure handling as the cache's own operations.
        """
        if self._monotonic() < self._skip_until:
            raise CacheUnavailable(f"Cache {self.alias!r} is skipped after a failure.")
        try:
            return await operation(*args, **kwargs)
        except CacheUnavailable as exc:
            observe_error(self.alias)
            cooldown = self.config.FAILURE_COOLDOWN
            self._skip_until = self._monotonic() + cooldown
            logger.warning(
                "Cache %r is unavailable; skipping it for %.1fs: %s",
                self.alias,
                cooldown,
                exc,
            )
            raise

    def _worked_around(self, operation: str, exc: CacheError) -> None:
        """Note a failed operation that ``get_or_set`` did without."""
        if not isinstance(exc, CacheUnavailable):  # call() logged those.
            observe_error(self.alias)
            logger.warning(
                "Cache %r refused a %s; computing instead: %s",
                self.alias,
                operation,
                exc,
            )

    def _milliseconds(self, ttl: TTL) -> int | None:
        """Turn ``ttl`` into whole milliseconds (at least 1), or None."""
        if ttl is DEFAULT_TTL:
            ttl = self.config.TTL
        if ttl is None:
            return None
        seconds = ttl.total_seconds() if isinstance(ttl, timedelta) else ttl
        if not seconds > 0:
            raise ValueError("Cache TTLs are positive; None keeps entries.")
        return max(1, math.ceil(seconds * 1000))

    def _load(
        self, raw: bytes | None, type_: TypeForm, *, early: bool = False
    ) -> tuple[Any, bool]:
        """Decode an entry as ``type_`` (``_MISS`` if missing or unfit).

        Also report whether XFetch says to refresh it now (only if ``early``).
        """
        if raw is None:
            observe(self.alias, "miss")
            return _MISS, False
        try:
            payload, expires, delta = split(raw)
            value = decode(payload, type_)
        except (ValueError, ValidationError) as exc:
            observe(self.alias, "invalid")
            # The error's text would quote the cached data: name its type only.
            logger.warning(
                "Cache %r holds an entry that is not a valid %s (%s); treating "
                "it as a miss",
                self.alias,
                getattr(type_, "__name__", type_ or "JSON value"),
                type(exc).__name__,
            )
            return _MISS, False
        if early and self._due_early(expires, delta):
            observe(self.alias, "early")
            return value, True
        observe(self.alias, "hit")
        return value, False

    def _due_early(self, expires: float | None, delta: float) -> bool:
        """XFetch: recompute with a probability that rises as expiry nears."""
        beta = self.config.EARLY_RECOMPUTE
        if expires is None or beta == 0 or delta <= 0:
            return False
        gap = -delta * beta * math.log(1.0 - self._uniform())
        return self._clock() + gap >= expires

    async def _compute_once(
        self,
        full: str,
        factory: Callable[[], Awaitable[Any]],
        milliseconds: int | None,
        *,
        write: bool = True,
        borrows: bool = False,
    ) -> bytes:
        """Share one computation of ``full`` among concurrent callers; its JSON.

        The caller that starts the computation decides whether the result is
        written to the store. It is a background task of its own, in a clean
        context (``domainkit.tasks.spawn``), which shutdown stops.

        By default the computation is detached: it runs the factory within
        the cache's ``COMPUTE_TIMEOUT`` and goes on when its callers stop
        waiting, so a slow factory still fills the cache. With ``borrows``,
        it is its owner's: it runs under the owner's deadline, and when the
        owner is cancelled, it is cancelled and the owner waits for it to
        stop. A failure that is the owner's (its deadline passed) reaches
        only the owner. Callers still waiting then share a new computation.
        """
        while True:
            computation = self._computing.get(full)
            owner = computation is None
            if computation is None:
                computation = self._start(
                    full, factory, milliseconds, write=write, borrows=borrows
                )
            task = computation.task
            try:
                # Waiting does not cancel the computation, which goes on for the
                # other callers (asyncio.shield would, on 3.14, log its failure).
                await asyncio.wait([task])
            except asyncio.CancelledError:
                if owner and computation.borrows:
                    await self._abandon(full, computation)
                raise
            if task.cancelled():
                if computation.abandoned:
                    continue  # Its owner left: share another one (or start it).
                # Shutdown or close() stopped it: compute without the store.
                return encode(await factory())
            if computation.owner_failed and not owner:
                continue  # The owner's deadline failed it, not this caller's.
            return task.result()

    def _start(
        self,
        full: str,
        factory: Callable[[], Awaitable[Any]],
        milliseconds: int | None,
        *,
        write: bool,
        borrows: bool,
    ) -> _Computation:
        """Start computing ``full``; one that borrows, under the caller's deadline."""
        deadline = current_deadline() if borrows else None
        computation = _Computation(borrows=borrows, deadline=deadline)
        computation.task = spawn(
            self._compute(computation, full, factory, milliseconds, write=write),
            name=COMPUTE_TASK,
            log_errors=False,
            deadline=deadline,
        )
        self._computing[full] = computation
        computation.task.add_done_callback(partial(self._forget, full, computation))
        return computation

    async def _abandon(self, full: str, computation: _Computation) -> None:
        """Stop the computation of an owner that was cancelled, and wait for it.

        Its factory may be using the owner's resources, which the owner
        releases once its cancellation goes on. One that shutdown or
        ``close()`` stopped is only waited for. A second cancellation of the
        owner stops the wait.
        """
        task = computation.task
        if task.done():
            return
        if not computation.stopped:
            computation.abandoned = True
            self._discard(full, computation)
            task.cancel()
        await asyncio.wait([task])

    def _discard(self, full: str, computation: _Computation) -> None:
        """Stop sharing ``computation``: callers that come next start another."""
        if self._computing.get(full) is computation:
            del self._computing[full]

    def _forget(
        self, full: str, computation: _Computation, task: asyncio.Task[bytes]
    ) -> None:
        """Drop a finished computation; its error reaches whoever awaited it."""
        self._discard(full, computation)
        if not task.cancelled():  # Stopped, abandoned, or its loop closed.
            task.exception()

    async def _compute(
        self,
        computation: _Computation,
        full: str,
        factory: Callable[[], Awaitable[Any]],
        milliseconds: int | None,
        *,
        write: bool,
    ) -> bytes:
        """Call ``factory``, store its JSON with its expiry and cost, return it.

        A detached computation is bounded by ``COMPUTE_TIMEOUT``; one that
        borrows, by its owner's deadline, and a failure once that has passed
        is marked as the owner's. Without ``write``, the cache just failed a
        read, so nothing is stored.
        """
        started = time.perf_counter()
        try:
            if computation.borrows:
                payload = encode(await factory())
            else:
                async with deadlines.timeout(self.config.COMPUTE_TIMEOUT):
                    payload = encode(await factory())
        except Exception:
            # Only once the owner's deadline has passed is a failure the
            # owner's: the factory's own timeout is every caller's.
            deadline = computation.deadline
            computation.owner_failed = deadline is not None and deadline.expired()
            raise
        if not write:
            return payload
        delta = time.perf_counter() - started
        entry = payload
        if milliseconds is not None:
            expires = self._clock() + milliseconds / 1000
            entry = b"%s%r,%r\n%s" % (_EARLY, expires, delta, payload)
        try:
            await self.call(self.store.set_many, {full: entry}, milliseconds)
        except CacheError as exc:
            self._worked_around("write", exc)
        return payload
