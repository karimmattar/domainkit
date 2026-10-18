"""Caches without a server: settings, the Cache API, stampedes, failures, helpers.

Redis itself is exercised in ``test_redis.py``; here, ``memory://`` and
test doubles stand in for it, and a closed port plays a Redis that is down.
"""

import asyncio
import io
import logging
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic import BaseModel, ValidationError

from domainkit.broker import brokers
from domainkit.cache import (
    Cache,
    CacheError,
    CacheHandler,
    CacheUnavailable,
    DummyStore,
    MemoryStore,
    Store,
    cached,
    caches,
    invalidate_on,
)
from domainkit.cache.core import split
from domainkit.cache.redis import RedisStore, create_client
from domainkit.conf import CacheConfig, Settings, override_settings
from domainkit.db.connections import connections
from domainkit.ddd import DomainEvent, EventBus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.ratelimit import RedisBackend
from domainkit.ledger import ledgers
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async

pytestmark = pytest.mark.anyio

CLOSED: Final = "redis://127.0.0.1:9/0"
"""A Redis URL nothing listens on (port 9, discard): a cache that is down."""
DOWN: Final = {"URL": CLOSED, "KEY_PREFIX": "down", "CONNECT_TIMEOUT": 0.5}


class Invoice(BaseModel):
    """A cacheable read model."""

    id: uuid.UUID
    total: int


@dataclass(frozen=True)
class Line:
    """A cacheable dataclass."""

    sku: str
    quantity: int


class Clock:
    """A clock tests move by hand; starts at an odd reading."""

    def __init__(self, now: float = 1_000_003.25) -> None:
        """Start at ``now``."""
        self.now = now

    def __call__(self) -> float:
        """Read the clock."""
        return self.now


class FlakyStore(MemoryStore):
    """A memory store that fails on demand and counts calls."""

    def __init__(self) -> None:
        """Work until told otherwise."""
        super().__init__(100)
        self.error: CacheError | None = None
        self.write_error: CacheError | None = None
        self.calls = 0

    def _fail(self, *, write: bool = False) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if write and self.write_error is not None:
            raise self.write_error

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """Fail, or read."""
        self._fail()
        return await super().get_many(keys)

    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        """Fail, or write."""
        self._fail(write=True)
        await super().set_many(items, ttl)


class Stuck:
    """A factory whose first call waits until cancelled; later calls return 7."""

    def __init__(self, on_cancel: Callable[[], object] = lambda: None) -> None:
        """Call ``on_cancel`` when the first call is cancelled."""
        self.on_cancel = on_cancel
        self.calls = 0
        self.started = asyncio.Event()

    async def __call__(self) -> int:
        """Wait forever the first time; return 7 after."""
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.on_cancel()
                raise
        return 7


def make_cache(
    store: Store | None = None,
    *,
    alias: str = "default",
    clock: Callable[[], float] | None = None,
    uniform: Callable[[], float] = lambda: 0.5,
    **options: object,
) -> Cache:
    """A cache on ``store`` (a new memory store by default)."""
    config = CacheConfig.model_validate(options)
    return Cache(
        alias,
        config,
        store or MemoryStore(config.MAX_ENTRIES),
        clock=clock or Clock(),
        uniform=uniform,
    )


def sample(name: str, labels: dict[str, str]) -> float:
    """Read a Prometheus sample (0 if it was never recorded)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


# Settings ---------------------------------------------------------------------


def test_default_cache_is_in_memory() -> None:
    """Without CACHES, ``default`` is a memory cache with five-minute entries."""
    config = Settings().CACHES["default"]

    assert (config.scheme, config.TTL, config.VERSION) == ("memory", 300.0, 1)
    with pytest.raises(ValidationError, match="CACHES must define the 'default'"):
        Settings.model_validate({"CACHES": {"other": {}}})


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"URL": "memcached://host"}, "a cache URL starts with"),
        ({"URL": "redis:///0", "KEY_PREFIX": "x"}, "a Redis URL needs a host"),
        ({"URL": "redis://cache:6379/0"}, "Redis caches need a KEY_PREFIX"),
        ({"KEY_PREFIX": "has space"}, "String should match pattern"),
        (
            {"URL": "redis://h/0", "KEY_PREFIX": "x", "TLS_CA": "ca.pem"},
            "need a rediss",
        ),
        (
            {"URL": "rediss://h/0", "KEY_PREFIX": "x", "TLS_CERT": "c.pem"},
            "TLS_CERT and TLS_KEY go together",
        ),
        ({"TTL": 0}, "greater than 0"),
    ],
)
def test_cache_settings_are_checked(options: dict[str, object], message: str) -> None:
    """Unusable cache settings fail at startup."""
    with pytest.raises(ValidationError, match=message):
        CacheConfig.model_validate(options)


def test_cache_url_properties() -> None:
    """Network, TLS verification, and credentials are read from the URL."""
    local = CacheConfig(URL="redis://localhost:6379/0", KEY_PREFIX="x")
    remote = CacheConfig(URL="rediss://:pw@cache.internal/0", KEY_PREFIX="x")
    lax = CacheConfig(
        URL="rediss://cache.internal/0?ssl_cert_reqs=none", KEY_PREFIX="x"
    )
    unchecked = CacheConfig(
        URL="rediss://cache.internal/0?ssl_check_hostname=False", KEY_PREFIX="x"
    )
    certified = CacheConfig(
        URL="rediss://cache.internal/0",
        KEY_PREFIX="x",
        TLS_CERT=Path("c.pem"),
        TLS_KEY=Path("k.pem"),
    )

    assert (local.is_network(), remote.is_network(), CacheConfig().is_network()) == (
        False,
        True,
        False,
    )
    assert (remote.verifies_tls(), lax.verifies_tls(), unchecked.verifies_tls()) == (
        True,
        False,
        False,
    )
    assert (remote.has_credentials(), lax.has_credentials()) == (True, False)
    assert certified.has_credentials()


@pytest.mark.parametrize(
    ("cache", "message"),
    [
        ({"URL": "redis://:pw@cache.internal/0"}, "network caches need rediss://"),
        (
            {"URL": "rediss://:pw@cache.internal/0?ssl_cert_reqs=none"},
            "caches must verify the server certificate",
        ),
        ({"URL": "rediss://cache.internal/0"}, "need a password or a client cert"),
    ],
)
def test_deployed_caches_need_tls_and_credentials(
    cache: dict[str, object], message: str
) -> None:
    """Staging and production refuse plaintext, unverified, or anonymous Redis."""
    base = {"ENVIRONMENT": "production", "ALLOWED_HOSTS": ["api.example.com"]}
    caches_setting = {"default": {}, "shared": {**cache, "KEY_PREFIX": "billing"}}

    with pytest.raises(ValidationError, match=f"{message}.*CACHES \\['shared'\\]"):
        Settings.model_validate({**base, "CACHES": caches_setting})

    fine = {"URL": "rediss://:pw@cache.internal/0", "KEY_PREFIX": "billing"}
    local = {"URL": "redis://localhost:6379/0", "KEY_PREFIX": "billing"}
    settings = Settings.model_validate(
        {**base, "CACHES": {"default": {}, "shared": fine, "local": local}}
    )
    assert set(settings.CACHES) == {"default", "shared", "local"}


# The Cache API on a memory store ----------------------------------------------


async def test_values_round_trip_as_json_or_as_types() -> None:
    """Untyped reads give JSON; typed reads validate models, dataclasses, UUIDs."""
    cache = make_cache()
    invoice = Invoice(id=uuid.uuid4(), total=12)
    await cache.set("invoice", invoice)
    await cache.set("lines", [Line("a", 2)])
    await cache.set("id", invoice.id)

    assert await cache.get("invoice") == {"id": str(invoice.id), "total": 12}
    assert await cache.get("invoice", Invoice) == invoice
    assert await cache.get("lines", list[Line]) == [Line("a", 2)]
    assert await cache.get("id", uuid.UUID) == invoice.id
    assert await cache.get("missing") is None
    assert await cache.get("missing", Invoice) is None


async def test_values_must_be_json(caplog: pytest.LogCaptureFixture) -> None:
    """Unserializable values are refused; unfit entries read as misses."""
    cache = make_cache()
    with pytest.raises(TypeError, match="object values cannot be cached"):
        await cache.set("x", object())

    await cache.set("invoice", {"id": "not a uuid", "total": 1})
    with caplog.at_level(logging.WARNING, "domainkit.cache.core"):
        assert await cache.get("invoice", Invoice) is None
    assert "not a valid Invoice (ValidationError); treating it as a miss" in (
        caplog.text
    )
    assert "not a uuid" not in caplog.text  # Cached data never reaches logs.


async def test_many_keys_at_once() -> None:
    """get_many leaves misses out; set_many and delete_many cover several keys."""
    cache = make_cache()
    await cache.set_many({"a": 1, "b": 2})
    await cache.set_many({})

    assert await cache.get_many(["a", "b", "c"]) == {"a": 1, "b": 2}
    assert await cache.get_many(["a"], int) == {"a": 1}
    assert await cache.delete_many(["a", "c"]) == 1
    assert await cache.delete_many([]) == 0
    assert (await cache.delete("b"), await cache.delete("b")) == (True, False)


async def test_add_incr_touch_and_expiry() -> None:
    """Adding keeps existing values; counters count; entries expire on time."""
    clock = Clock()
    store = MemoryStore(100, clock=clock)
    cache = make_cache(store, clock=clock, TTL=10)

    assert await cache.add("k", "first")
    assert not await cache.add("k", "second")
    assert await cache.get("k") == "first"
    assert [await cache.incr("n"), await cache.incr("n", 4)] == [1, 5]
    await cache.set("text", "five")
    with pytest.raises(CacheError, match="not an integer"):
        await cache.incr("text")

    clock.now += 5
    assert await cache.touch("k", 20)
    assert not await cache.touch("missing")
    assert await cache.incr("n") == 6  # Keeps its original expiry.
    clock.now += 6
    assert await cache.get("n") is None
    assert await cache.get("k") == "first"
    assert await cache.touch("k", None)
    clock.now += 1000
    assert await cache.get("k") == "first"
    assert await cache.get("text") is None


@pytest.mark.parametrize("ttl", [0, -1, math.nan, timedelta(0)])
async def test_ttls_are_positive(ttl: float | timedelta) -> None:
    """Zero, negative, and NaN TTLs are mistakes, not 'expire now'."""
    with pytest.raises(ValueError, match="Cache TTLs are positive"):
        await make_cache().set("k", 1, ttl)


async def test_ttls_round_up_to_milliseconds() -> None:
    """Fractions of a millisecond still keep the entry; timedeltas work."""
    clock = Clock()
    cache = make_cache(MemoryStore(10, clock=clock), clock=clock)
    await cache.set("tiny", 1, 0.0001)
    await cache.set("minute", 1, timedelta(minutes=1))

    clock.now += 0.0009
    assert await cache.get("tiny") == 1
    clock.now += 59.5
    assert (await cache.get("tiny"), await cache.get("minute")) == (None, 1)


async def test_keys_are_namespaced_and_versioned() -> None:
    """Prefix and version keep caches apart; clear stays in its namespace."""
    store = MemoryStore(100)
    billing = make_cache(store, KEY_PREFIX="billing")
    billing_v2 = make_cache(store, KEY_PREFIX="billing", VERSION=2)
    shipping = make_cache(store, KEY_PREFIX="shipping")
    await billing.set("k", 1)
    await shipping.set("k", 2)

    assert billing.make_key("k") == "billing:1:k"
    assert await billing_v2.get("k") is None
    await billing_v2.set("k", 3)
    assert await billing.clear() == 2  # Both versions.
    assert await shipping.get("k") == 2
    assert make_cache(alias="sessions").make_key("k") == "sessions:1:k"
    for bad in ("", "x" * 1025):
        with pytest.raises(ValueError, match="Cache keys have 1 to 1024"):
            await billing.get(bad)


async def test_memory_store_evicts_least_recently_used() -> None:
    """Reads refresh recency; the oldest entry goes first."""
    cache = make_cache(MAX_ENTRIES=2)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.get("a")
    await cache.set("c", 3)

    assert await cache.get_many(["a", "b", "c"]) == {"a": 1, "c": 3}


async def test_dummy_store_keeps_nothing() -> None:
    """dummy:// turns caching off while code keeps calling the cache."""
    cache = make_cache(DummyStore())
    calls = []

    async def compute() -> int:
        calls.append(1)
        return 7

    await cache.set("k", 1)
    assert (await cache.get("k"), await cache.add("k", 1)) == (None, True)
    assert (await cache.incr("n"), await cache.incr("n")) == (1, 1)
    assert (await cache.touch("k"), await cache.delete("k"), await cache.clear()) == (
        False,
        False,
        0,
    )
    assert [await cache.get_or_set("k", compute) for _ in range(2)] == [7, 7]
    assert len(calls) == 2


# get_or_set: stampedes, early recomputation, failures ---------------------------


async def test_get_or_set_computes_once_then_hits() -> None:
    """A miss computes and stores; later calls read it, typed like a hit."""
    cache = make_cache()
    calls = []

    async def compute() -> Invoice:
        calls.append(1)
        return Invoice(id=uuid.UUID(int=1), total=3)

    first = await cache.get_or_set("invoice", compute, Invoice)
    second = await cache.get_or_set("invoice", compute, Invoice)
    untyped = await cache.get_or_set("invoice", compute)

    assert first == second == Invoice(id=uuid.UUID(int=1), total=3)
    assert untyped == {"id": str(uuid.UUID(int=1)), "total": 3}
    assert len(calls) == 1
    assert await make_cache().get_or_set("fresh", compute) == untyped  # Via JSON.


async def test_concurrent_misses_share_one_computation() -> None:
    """A stampede on one key runs the factory once; errors reach every caller."""
    cache = make_cache()
    release = asyncio.Event()
    calls = []

    async def slow() -> int:
        calls.append(1)
        await release.wait()
        return 42

    waiting = [asyncio.create_task(cache.get_or_set("k", slow)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*waiting) == [42] * 5
    assert len(calls) == 1

    async def broken() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        raise RuntimeError("database down")

    results = await asyncio.gather(
        *(cache.get_or_set("b", broken) for _ in range(3)), return_exceptions=True
    )
    assert all(isinstance(result, RuntimeError) for result in results)
    assert len(calls) == 2
    with pytest.raises(RuntimeError):  # Not cached: the next caller tries again.
        await cache.get_or_set("b", broken)


async def test_a_cancelled_caller_does_not_cancel_the_computation() -> None:
    """Others waiting on the same key still get the value."""
    cache = make_cache()
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "done"

    first = asyncio.create_task(cache.get_or_set("k", slow))
    second = asyncio.create_task(cache.get_or_set("k", slow))
    await asyncio.sleep(0)
    first.cancel()
    release.set()

    assert await second == "done"
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await cache.get("k") == "done"


async def test_every_caller_gets_its_own_copy() -> None:
    """Concurrent callers share a computation, not objects, and pick their type."""
    cache = make_cache()
    release = asyncio.Event()

    async def lines() -> list[Line]:
        await release.wait()
        return [Line("a", 1)]

    typed = asyncio.create_task(cache.get_or_set("k", lines, list[Line]))
    plain = asyncio.create_task(cache.get_or_set("k", lines))
    other = asyncio.create_task(cache.get_or_set("k", lines, list[Line]))
    await asyncio.sleep(0)
    release.set()
    first, second, third = await typed, await plain, await other

    assert first == third == [Line("a", 1)]
    assert first is not third
    assert second == [{"sku": "a", "quantity": 1}]


async def test_a_failed_early_refresh_serves_the_cached_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entry is still valid, so a failing refresh does not fail the caller."""
    clock = Clock()
    cache = make_cache(clock=clock, uniform=lambda: 0.999999)
    expires = clock.now + 1
    await cache.store.set_many({cache.make_key("k"): b"~%r,5.0\n1" % expires}, None)

    async def broken() -> int:
        raise RuntimeError("database down")

    with caplog.at_level(logging.WARNING, "domainkit.cache.core"):
        assert await cache.get_or_set("k", broken, int) == 1
    assert caplog.messages == [
        "Cache 'default' could not refresh an entry early; serving it until it expires"
    ]
    cause = caplog.records[0].exc_info  # The cause is in the log.
    assert cause
    assert cause[0] is RuntimeError


def test_pending_computations_end_with_their_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A computation nobody waits for any more is cancelled with its loop."""
    cache = make_cache()

    async def abandon() -> None:
        async def never() -> int:
            await asyncio.Event().wait()
            return 1

        caller = asyncio.create_task(cache.get_or_set("k", never))
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

    with caplog.at_level(logging.ERROR, "asyncio"):
        asyncio.run(abandon())

    assert cache._computing == {}
    assert caplog.records == []  # Its cancellation is not an unhandled error.


async def test_hot_entries_are_recomputed_early() -> None:
    """XFetch: an entry close to expiry is refreshed, earlier for costly ones."""
    clock = Clock()
    draws = iter([0.5, 0.99999, 0.5])
    cache = make_cache(clock=clock, uniform=lambda: next(draws))
    full = cache.make_key("k")
    expires = clock.now + 10
    await cache.store.set_many({full: b"~%r,%r\n1" % (expires, 2.0)}, None)

    async def fresh() -> int:
        return 2

    # 2s to compute, 10s left: -2 * ln(0.5) = 1.4s ahead, too early to care.
    assert await cache.get_or_set("k", fresh) == 1
    # An unlucky draw looks 23s ahead: recompute now.
    assert await cache.get_or_set("k", fresh) == 2
    payload, stored_expiry, delta = split(
        (await cache.store.get_many([full]))[0] or b""
    )
    assert (payload, stored_expiry) == (b"2", clock.now + 300)
    assert 0 < delta < 1


async def test_early_recomputation_can_be_turned_off() -> None:
    """EARLY_RECOMPUTE=0, or entries without a TTL, are kept until they expire."""
    clock = Clock()
    cache = make_cache(clock=clock, uniform=lambda: 0.999999, EARLY_RECOMPUTE=0)
    await cache.store.set_many({cache.make_key("k"): b"~%r,5.0\n1" % clock.now}, None)

    async def fresh() -> int:
        return 2

    assert await cache.get_or_set("k", fresh) == 1
    await cache.get_or_set("forever", fresh, ttl=None)
    assert (await cache.store.get_many([cache.make_key("forever")])) == [b"2"]


async def test_unfit_entries_are_recomputed() -> None:
    """An entry that no longer validates is replaced, not returned."""
    cache = make_cache()
    await cache.set("invoice", {"total": "lots"})

    async def compute() -> Invoice:
        return Invoice(id=uuid.UUID(int=2), total=1)

    assert (await cache.get_or_set("invoice", compute, Invoice)).total == 1
    assert await cache.get("invoice", Invoice) == Invoice(id=uuid.UUID(int=2), total=1)


async def test_a_failing_cache_is_skipped_for_a_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """get_or_set computes instead; plain calls raise; the store rests a while."""
    monotonic = Clock(50.0)
    store = FlakyStore()
    cache = Cache(
        "default", CacheConfig(FAILURE_COOLDOWN=5), store, monotonic=monotonic
    )
    store.error = CacheUnavailable("connection refused")

    async def compute() -> list[int]:
        return [1]

    with caplog.at_level(logging.WARNING, "domainkit.cache.core"):
        assert await cache.get_or_set("k", compute) == [1]
        with pytest.raises(CacheUnavailable, match="skipped after a failure"):
            await cache.get("k")
        assert await cache.get_or_set("k", compute) == [1]
    assert store.calls == 1
    assert caplog.messages == [
        "Cache 'default' is unavailable; skipping it for 5.0s: connection refused"
    ]

    store.error = None
    monotonic.now += 5
    assert await cache.get_or_set("k", compute) == [1]
    assert await cache.get("k") == [1]


@pytest.mark.parametrize(
    "error",
    [CacheUnavailable("connection refused"), CacheError("NOPERM")],
    ids=["unavailable", "refused"],
)
async def test_an_outage_still_shares_one_computation(error: CacheError) -> None:
    """While the cache fails, concurrent misses wait for one factory call."""
    store = FlakyStore()
    store.error = error
    cache = make_cache(store)
    release = asyncio.Event()
    calls = []

    async def slow() -> list[Line]:
        calls.append(1)
        await release.wait()
        return [Line("a", 1)]

    waiting = [
        asyncio.create_task(cache.get_or_set("k", slow, list[Line])) for _ in range(5)
    ]
    await asyncio.sleep(0)
    release.set()
    first, *others = await asyncio.gather(*waiting)

    assert first == [Line("a", 1)]
    assert all(other == first and other is not first for other in others)
    assert len(calls) == 1
    # Each refused read reaches the store; an unavailable one starts the
    # cooldown, which skips the rest. The value is not written either way.
    assert store.calls == (1 if isinstance(error, CacheUnavailable) else 5)


async def test_refused_operations_are_worked_around(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused read or write in get_or_set is logged; the value still returns."""
    store = FlakyStore()
    cache = make_cache(store)
    errors = sample("cache_errors_total", {"cache": "default"})

    async def compute() -> str:
        return "value"

    store.write_error = CacheError("OOM command not allowed")
    with caplog.at_level(logging.WARNING, "domainkit.cache.core"):
        assert await cache.get_or_set("k", compute) == "value"
        store.write_error, store.error = None, CacheError("NOPERM")
        assert await cache.get_or_set("k", compute) == "value"
    assert caplog.messages == [
        "Cache 'default' refused a write; computing instead: OOM command not allowed",
        "Cache 'default' refused a read; computing instead: NOPERM",
    ]
    assert sample("cache_errors_total", {"cache": "default"}) == errors + 2


async def test_lookups_are_counted() -> None:
    """Hits, misses, unfit entries, and early refreshes are metrics."""
    labels = {"cache": "metered", "result": "hit"}
    before = {
        result: sample("cache_lookups_total", {**labels, "result": result})
        for result in ("hit", "miss", "invalid", "early")
    }
    clock = Clock()
    cache = make_cache(alias="metered", clock=clock, uniform=lambda: 0.999999)
    await cache.set("k", 1)
    await cache.set("bad", "x")
    await cache.store.set_many({cache.make_key("hot"): b"~%r,1.0\n1" % clock.now}, None)

    async def compute() -> int:
        return 1

    await cache.get("k")
    await cache.get("nothing")
    await cache.get("bad", int)
    await cache.get_or_set("hot", compute)

    after = {
        result: sample("cache_lookups_total", {**labels, "result": result})
        for result in before
    }
    assert {result: after[result] - before[result] for result in before} == {
        "hit": 1,
        "miss": 1,
        "invalid": 1,
        "early": 1,
    }


# Exact behaviour: boundaries, types, logs, metrics --------------------------------


async def test_every_write_applies_its_ttl() -> None:
    """Entries expire exactly at their TTL, however they were written."""
    clock = Clock()
    start = clock.now
    cache = make_cache(MemoryStore(100, clock=clock), clock=clock)
    calls: list[int] = []

    async def compute() -> int:
        calls.append(1)
        return len(calls)

    keys = ["added", "touched", "many", "computed"]
    assert await cache.add("added", 1, 10)
    await cache.set("touched", 1, 100)
    assert await cache.touch("touched", 10)
    await cache.set_many({"many": 1}, 10)
    assert await cache.get_or_set("computed", compute, int, ttl=10) == 1

    clock.now = start + 9.995
    assert set(await cache.get_many(keys)) == set(keys)
    clock.now = start + 10
    assert await cache.get_many(keys) == {}
    assert await cache.get_or_set("computed", compute, int, ttl=10) == 2

    await cache.set("tiny", 1, 0.0001)  # Rounded up to 1 ms, not more.
    clock.now += 0.0009
    assert await cache.get("tiny") == 1
    clock.now += 0.0002
    assert await cache.get("tiny") is None
    assert await cache.add("x" * 1024, 1)  # The longest key.


async def test_typed_reads_on_every_path() -> None:
    """get_many, fall-back computations, and early refreshes return the type."""
    invoice = Invoice(id=uuid.UUID(int=8), total=4)
    cache = make_cache()
    await cache.set("invoice", invoice)
    assert await cache.get_many(["invoice"], Invoice) == {"invoice": invoice}

    async def load() -> Invoice:
        return invoice

    flaky = FlakyStore()
    flaky.error = CacheUnavailable("down")
    fallen = await make_cache(flaky).get_or_set("invoice", load, Invoice)
    assert isinstance(fallen, Invoice)

    clock = Clock()
    hot = make_cache(clock=clock, uniform=lambda: 0.999999)
    stale = b'~%r,5.0\n"%s"' % (clock.now + 1, str(uuid.UUID(int=1)).encode())
    await hot.store.set_many({hot.make_key("id"): stale}, None)

    async def new_id() -> uuid.UUID:
        return uuid.UUID(int=2)

    assert await hot.get("id", uuid.UUID) == uuid.UUID(int=1)  # get never refreshes.
    assert await hot.get_or_set("id", new_id, uuid.UUID) == uuid.UUID(int=2)


async def test_early_recomputation_scales_with_its_setting() -> None:
    """EARLY_RECOMPUTE multiplies how far ahead an entry looks (XFetch's beta)."""
    clock = Clock()

    async def fresh() -> int:
        return 2

    entry = {"k": b"~%r,2.0\n1" % (clock.now + 2.5)}
    results = []
    for beta in (1, 2):
        cache = make_cache(clock=clock, uniform=lambda: 0.5, EARLY_RECOMPUTE=beta)
        await cache.store.set_many({cache.make_key("k"): entry["k"]}, None)
        results.append(await cache.get_or_set("k", fresh))

    # -2 * ln(0.5) = 1.39s ahead: not yet; with beta 2, 2.77s ahead: refresh.
    assert results == [1, 2]


async def test_unfit_entries_are_logged_without_their_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log names the cache and the type, never the cached content."""
    cache = make_cache(alias="reports")
    await cache.store.set_many({cache.make_key("broken"): b"{secret"}, None)
    await cache.set("invoice", {"id": "secret", "total": 1})

    with caplog.at_level(logging.WARNING, "domainkit.cache.core"):
        assert await cache.get("broken") is None
        assert await cache.get("invoice", Invoice) is None

    assert caplog.messages == [
        (
            "Cache 'reports' holds an entry that is not a valid JSON value "
            "(ValueError); treating it as a miss"
        ),
        (
            "Cache 'reports' holds an entry that is not a valid Invoice "
            "(ValidationError); treating it as a miss"
        ),
    ]
    assert "secret" not in caplog.text


async def test_outages_are_counted_per_cache() -> None:
    """Each failed store call counts once, under its cache's name."""
    before = sample("cache_errors_total", {"cache": "flaky"})
    store = FlakyStore()
    store.error = CacheUnavailable("down")
    cache = make_cache(store, alias="flaky", FAILURE_COOLDOWN=0)

    for _ in range(2):
        with pytest.raises(CacheUnavailable):
            await cache.get("k")

    assert sample("cache_errors_total", {"cache": "flaky"}) == before + 2
    assert cache.alias == "flaky"


def test_memory_caches_do_not_share_entries() -> None:
    """Two memory caches with the same settings still keep their own entries."""
    handler = CacheHandler()

    async def use() -> tuple[object, object]:
        await handler["one"].set("k", "one")
        await handler["two"].set("k", "two")
        return await handler["one"].get("k"), handler["two"].alias

    with override_settings(CACHES={"default": {}, "one": {}, "two": {}}):
        assert asyncio.run(use()) == ("one", "two")


async def test_cached_keys_are_stable() -> None:
    """Keys do not change between versions, call styles, or keyword orders."""

    @cached(name="billing.summary", ttl=60)
    async def summary(invoice_id: uuid.UUID, currency: str = "EUR") -> Invoice:
        return Invoice(id=invoice_id, total=len(currency) * 10)

    @cached(name="billing.report")
    async def report(**filters: str) -> dict[str, str]:
        return filters

    invoice_id = uuid.UUID(int=7)
    assert summary.key(invoice_id) == "billing.summary:d285eadc3155161d1ecb8ec5343e73b1"
    assert report.key(a="1", b="2") == report.key(b="2", a="1")

    assert (await summary(invoice_id=invoice_id, currency="USDT")).total == 40
    (raw,) = await caches["default"].store.get_many(
        [caches["default"].make_key(summary.key(invoice_id, "USDT"))]
    )
    _, expires, _ = split(raw or b"")
    assert expires is not None
    assert 59 < expires - time.time() <= 60
    assert await summary.invalidate(invoice_id=invoice_id, currency="USDT")
    assert await report(b="2", a="1") == {"a": "1", "b": "2"}


# The handler, the decorator, invalidation ---------------------------------------


class RecordingStore(DummyStore):
    """A dummy store that remembers being closed."""

    def __init__(self, closed: list[str], name: str) -> None:
        """Report closing as ``name``."""
        self.closed = closed
        self.name = name

    async def close(self) -> None:
        """Record the close."""
        self.closed.append(self.name)


def test_handler_gives_each_loop_its_caches() -> None:
    """Redis stores are per loop and closed with it; memory is per process."""
    closed: list[str] = []
    handler = CacheHandler(
        connect=lambda config: RecordingStore(closed, config.KEY_PREFIX or "")
    )
    shared = {"URL": "redis://cache:6379/0", "KEY_PREFIX": "one"}

    async def use() -> tuple[Cache, Cache]:
        memory, redis = handler["default"], handler["shared"]
        assert handler["default"] is memory
        await memory.set("k", "kept")
        with override_settings(
            CACHES={"default": {}, "shared": {**shared, "KEY_PREFIX": "two"}}
        ):
            assert handler["shared"] is not redis
        await handler.close_all()
        return memory, redis

    with override_settings(CACHES={"default": {}, "shared": shared}):
        first = asyncio.run(use())
        second = asyncio.run(use())

        async def read() -> object:
            return await handler["default"].get("k")

        assert asyncio.run(read()) == "kept"
        with override_settings(CACHES={"default": {}, "off": {"URL": "dummy://"}}):

            async def dummy() -> object:
                return await handler["off"].get("k")

            assert asyncio.run(dummy()) is None
        handler.reset()
        assert asyncio.run(read()) is None

    assert first[0] is not second[0]
    assert first[0].store is second[0].store
    assert closed == ["two", "one", "two", "one"]  # Current, then replaced.
    with pytest.raises(ImproperlyConfigured, match="CACHES has no 'nope' alias"):
        handler.config("nope")
    with pytest.raises(RuntimeError, match="needs a running event loop"):
        handler["default"]


async def test_closing_a_cache_stops_its_computations_first() -> None:
    """close() cancels what is still computing, then closes the store.

    Callers still waiting compute their own value, as when the cache fails.
    """
    events: list[str] = []
    cache = make_cache(RecordingStore(events, "store"))
    factory = Stuck(on_cancel=lambda: events.append("computation"))
    waiting = asyncio.create_task(cache.get_or_set("k", factory, int))
    await factory.started.wait()

    async with asyncio.timeout(5):
        await cache.close()

    assert events == ["computation", "store"]
    assert cache._computing == {}
    await asyncio.wait([waiting], timeout=5)
    assert waiting.result() == 7
    assert factory.calls == 2


async def test_a_caller_cancelled_while_its_cache_closes_stays_cancelled() -> None:
    """Only callers that still want the value compute it after close()."""
    cache = make_cache(DummyStore())
    waiting: asyncio.Task[int]
    factory = Stuck(on_cancel=lambda: waiting.cancel())
    waiting = asyncio.create_task(cache.get_or_set("k", factory, int))
    await factory.started.wait()

    async with asyncio.timeout(5):
        await cache.close()

    await asyncio.wait([waiting], timeout=5)
    assert waiting.cancelled()
    assert factory.calls == 1


def test_caches_close_before_what_their_computations_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At shutdown, cache computations stop before databases and brokers close."""
    closed: list[str] = []
    handlers = {
        "caches": caches,
        "connections": connections,
        "ledgers": ledgers,
        "brokers": brokers,
    }
    for name, handler in handlers.items():

        async def close_all(name: str = name) -> None:
            closed.append(name)

        monkeypatch.setattr(handler, "close_all", close_all)

    with TestClient(get_asgi_application()):
        pass
    assert closed == ["caches", "connections", "ledgers", "brokers"]

    closed.clear()
    run_async(asyncio.sleep(0))  # Management commands.
    assert closed == ["caches", "connections", "brokers"]


def test_readiness_checks_only_caches_that_ask_for_it() -> None:
    """A cache marked READINESS fails /ready while it is down."""
    down = {**DOWN, "READINESS": True}
    with override_settings(
        CACHES={"default": {}, "down": down, "ignored": {**DOWN, "KEY_PREFIX": "i"}},
        READINESS_DETAILS=True,
    ):
        response = TestClient(get_asgi_application()).get("/ready")

    assert response.status_code == 503
    assert response.json()["failing"] == ["cache:down"]


async def test_cached_functions_hash_their_arguments() -> None:
    """Equal calls share an entry; results come back as the return type."""
    calls = []

    @cached(ttl=60)
    async def summary(invoice_id: uuid.UUID, currency: str = "EUR") -> Invoice:
        calls.append(invoice_id)
        return Invoice(id=invoice_id, total=len(currency))

    invoice_id = uuid.UUID(int=7)
    assert summary.key(invoice_id) == summary.key(invoice_id=invoice_id, currency="EUR")
    assert summary.key(invoice_id).startswith(f"{summary.name}:")
    assert summary.key(invoice_id) != summary.key(invoice_id, "USD")
    assert summary.__name__ == "summary"

    first = await summary(invoice_id)
    again = await summary(invoice_id=invoice_id)
    assert first == again == Invoice(id=invoice_id, total=3)
    assert len(calls) == 1
    assert await summary.invalidate(invoice_id)
    assert not await summary.invalidate(invoice_id)
    await summary(invoice_id)
    assert len(calls) == 2


async def test_cached_functions_are_checked() -> None:
    """Sync functions and unserializable arguments are refused; names are stable."""
    with pytest.raises(TypeError, match="@cached decorates async functions"):
        cached()(lambda: 1)  # type: ignore[arg-type,return-value]

    @cached(name="reports.total", cache="default")
    async def total(item: object) -> int:
        return 1

    with pytest.raises(TypeError, match=r"Arguments of reports\.total must be JSON"):
        total.key(object())
    assert total.key(1).startswith("reports.total:")

    @cached()
    async def untyped(n: int):  # type: ignore[no-untyped-def]  # noqa: ANN202
        return {"n": n}

    assert await untyped(1) == {"n": 1}


class InvoicePaid(DomainEvent):
    """An event that changes cached data."""

    invoice_id: uuid.UUID


async def test_events_invalidate_entries() -> None:
    """invalidate_on deletes the named entries when the event is published."""
    bus = EventBus()
    failures: list[BaseException] = []
    bus.error_hook = lambda exc, event: failures.append(exc)
    invalidate_on(InvoicePaid, lambda event: [f"invoice:{event.invoice_id}"], bus=bus)
    cache = caches["default"]
    event = InvoicePaid(invoice_id=uuid.UUID(int=3))
    await cache.set(f"invoice:{event.invoice_id}", 1)

    await bus.publish(event)

    assert await cache.get(f"invoice:{event.invoice_id}") is None
    assert bus.handlers_for(InvoicePaid)[0].__qualname__ == "invalidate_on(InvoicePaid)"
    with override_settings(CACHES={"default": DOWN}):
        await bus.publish(event)
    assert isinstance(failures[0], CacheUnavailable)


# Redis that is down, and the Redis client's options --------------------------------


async def test_a_redis_cache_that_is_down_fails_open() -> None:
    """Connection refused: get_or_set computes, plain calls raise CacheUnavailable."""
    with override_settings(CACHES={"default": DOWN}):
        cache = caches["default"]

        async def compute() -> int:
            return 5

        try:
            assert await cache.get_or_set("k", compute) == 5
            with pytest.raises(CacheUnavailable):
                await cache.check()
        finally:
            await caches.close_all()


def test_redis_clients_use_short_timeouts_and_tls_options(tmp_path: Path) -> None:
    """The pool is bounded; TLS files reach the connection; nothing connects yet."""
    config = CacheConfig(
        URL="rediss://user:pw@cache.internal:6380/2",
        KEY_PREFIX="billing",
        TLS_CA=tmp_path / "ca.pem",
        TLS_CERT=tmp_path / "client.pem",
        TLS_KEY=tmp_path / "client.key",
        MAX_CONNECTIONS=7,
        RETRIES=0,
    )
    pool = create_client(config).connection_pool
    options = pool.connection_kwargs

    assert pool.max_connections == 7
    assert (options["host"], options["port"], options["db"]) == (
        "cache.internal",
        6380,
        2,
    )
    assert (options["socket_timeout"], options["socket_connect_timeout"]) == (1.0, 1.0)
    assert options["ssl_ca_certs"] == str(tmp_path / "ca.pem")
    assert (options["ssl_certfile"], options["ssl_keyfile"]) == (
        str(tmp_path / "client.pem"),
        str(tmp_path / "client.key"),
    )
    assert options["retry"].get_retries() == 0
    assert isinstance(RedisStore.from_config(config).client.connection_pool, type(pool))


# Rate limits in Redis, the command, the testing plugin ------------------------------


def test_redis_rate_limits_need_a_redis_cache() -> None:
    """RedisBackend refuses memory caches at startup."""
    with pytest.raises(ImproperlyConfigured, match="needs a redis:// or rediss://"):
        RedisBackend()
    with override_settings(
        CACHES={"default": {}, "limits": DOWN}, RATE_LIMIT_CACHE="limits"
    ):
        assert RedisBackend().cache == "limits"


async def test_redis_rate_limits_follow_their_cache() -> None:
    """A limiter whose cache stops being Redis says so instead of guessing."""
    with override_settings(CACHES={"default": DOWN}):
        backend = RedisBackend()
    with pytest.raises(ImproperlyConfigured, match=r"CACHES\['default'\] is not Redis"):
        await backend.hit("ip:203.0.113.9", 10, 60)


async def test_redis_rate_limits_fail_open() -> None:
    """Requests pass while Redis is down; limits are a safeguard, not a gate."""
    with override_settings(CACHES={"default": DOWN}):
        try:
            decision = await RedisBackend().hit("ip:203.0.113.9", 10, 60)
        finally:
            await caches.close_all()

    assert (decision.allowed, decision.remaining, decision.retry_after) == (True, 9, 0)


def run(*args: str) -> str:
    """Run a command and return what it printed."""
    output = io.StringIO()
    call_command(*args, stdout=output)
    return output.getvalue()


def test_cache_command_checks_and_clears() -> None:
    """The command pings every cache; clearing needs names or --all."""

    async def fill() -> None:
        await caches["default"].set("k", 1)

    assert run("cache", "check") == "default: ok\n"
    asyncio.run(fill())
    assert run("cache", "clear", "default") == "default: cleared 1 key(s)\n"
    assert run("cache", "clear", "--all") == "default: cleared 0 key(s)\n"
    with pytest.raises(CommandError, match="Name the caches to clear"):
        call_command("cache", "clear")
    with pytest.raises(CommandError, match="CACHES has no 'nope'"):
        call_command("cache", "check", "nope")
    with (
        override_settings(CACHES={"default": {}, "down": DOWN}),
        pytest.raises(CommandError, match="Unavailable: down"),
    ):
        call_command("cache", "check")
