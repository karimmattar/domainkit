"""State shared by event loops on several threads stays consistent.

Free-threaded Python (3.14t), and servers that run a loop per thread, share
one rate limiter, one middleware, one hashing pool, one application's live
connection slots, the event bus, an HTTP client's circuit breaker, the
guard's lookup threads and its owners' shares, a throttled log's counts,
the remembered client addresses, the handles of the request metrics, the
principal cache, and the grants principals share between threads, an
application's throttle backend and attempt counter, the idempotency
middleware's sweep schedule, and the registries that keep each loop's
tasks, publishers, brokers, and caches. Each test
drives them from several threads at once and checks what must always
hold, and that the state is only touched with the lock held (where there
is one).
"""

import asyncio
import ipaddress
import itertools
import logging
import os
import random
import socket
import subprocess
import sys
import sysconfig
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, ClassVar
from weakref import WeakKeyDictionary

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import State
from starlette.requests import HTTPConnection, Request
from starlette.types import Message, Receive, Scope, Send

from domainkit import clock as clock_module
from domainkit import setup, tasks
from domainkit.apps import registry
from domainkit.broker import Broker, BrokerHandler, MemoryBroker
from domainkit.cache import CacheHandler, Store
from domainkit.conf import (
    CacheConfig,
    DatabaseConfig,
    override_settings,
    unwrap_settings,
)
from domainkit.contrib.audit import personal
from domainkit.contrib.auth import AllowAny, principals, throttling
from domainkit.contrib.auth import middleware as middleware_module
from domainkit.contrib.auth.hashers import HashingPool
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.contrib.auth.models import Group, User
from domainkit.contrib.auth.principals import ALL, Entry, PrincipalCache
from domainkit.contrib.contenttypes import InstalledModel, content_type_key
from domainkit.contrib.contenttypes import registry as content_registry
from domainkit.contrib.idempotency import statements as idempotency_statements
from domainkit.contrib.idempotency.middleware import PURGE_BATCH, Sweeps
from domainkit.contrib.messaging import MessagingSettings, dispatch
from domainkit.db import Reads, connections
from domainkit.db.connections import (
    _begin_snapshot,
    _drop_detail,
    _read_alone,
    _read_alone_no_params,
    _require_read_only,
    _reset_session,
    _take_open,
    create_engine,
    create_read_engine,
)
from domainkit.ddd import DomainEvent, EventBus
from domainkit.http import guard as guard_module
from domainkit.http import live, metrics, ratelimit
from domainkit.http.client import BREAKER_HOSTS, CircuitBreaker
from domainkit.http.guard import GuardResolver, lookups_for
from domainkit.http.live import Slots
from domainkit.http.live_events import LiveEvents
from domainkit.http.metrics import REQUESTS, MetricsMiddleware
from domainkit.http.middleware import ProxyHeadersMiddleware, _address
from domainkit.http.ratelimit import MemoryBackend, client_group
from domainkit.http.shedding import QUEUE_FULL, ShedReport
from domainkit.http.websockets import WebSocketLimitsMiddleware
from domainkit.log import JsonFormatter, ThrottledLog, flush_throttled
from domainkit.testing import CapturedQueries
from domainkit_tests.conftest import UseAuth
from domainkit_tests.sample_domains.alpha.apps import AlphaSettings

THREADS = 8
LOOPS = 25
"""Short-lived event loops each thread runs, one after another."""


def on_threads[T](work: Callable[[int], T], count: int = THREADS) -> list[T]:
    """Run ``work(i)`` on ``count`` threads, started together; their results."""
    ready = threading.Barrier(count)
    results: dict[int, T] = {}

    def run(index: int) -> None:
        ready.wait(10)
        results[index] = work(index)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    return [results[index] for index in range(count)]


def test_the_memory_rate_limiter_lets_exactly_the_limit_through() -> None:
    """Callers on many threads, each with its own loop, share one exact count."""
    backend: MemoryBackend

    def clock() -> float:
        assert backend._lock.locked()  # The state is read under the lock.
        return 1000.0  # Frozen: nothing is paid back during the test.

    backend = MemoryBackend(clock=clock)

    async def hits() -> int:
        allowed = 0
        for _ in range(50):
            decision = await backend.hit("ip:203.0.113.5", 100, 3600)
            allowed += decision.allowed
        return allowed

    allowed = on_threads(lambda _: asyncio.run(hits()))

    assert sum(allowed) == 100


def test_the_memory_rate_limiter_gives_back_exactly_what_it_counted() -> None:
    """Hits and refunds from many threads at once leave an exact count."""
    backend: MemoryBackend

    def clock() -> float:
        assert backend._lock.locked()  # The state is read and written under it.
        return 1000.0

    backend = MemoryBackend(clock=clock)

    async def hit_and_give_back() -> int:
        allowed = 0
        for _ in range(50):
            decision = await backend.hit("ip:203.0.113.5", 100, 3600)
            if decision.allowed:
                allowed += 1
                await backend.refund("ip:203.0.113.5", 100, 3600)
        return allowed

    allowed = on_threads(lambda _: asyncio.run(hit_and_give_back()))

    assert sum(allowed) == 50 * len(allowed)  # Each given back: none refused.
    assert "ip:203.0.113.5" not in backend._callers


def test_refused_credential_holds_survive_threads(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holding and expiring addresses from many threads never loses or breaks one."""
    use_auth(settings={"RATE_LIMIT": "10/minute"})
    middleware = AuthenticationMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
    now = [100.0]

    def clock() -> float:
        assert middleware._holding.locked()  # Holds are read under the lock.
        return now[0]

    monkeypatch.setattr(middleware_module, "monotonic", clock)

    def hold_then_expire(index: int) -> tuple[object, object]:
        address = f"ip:203.0.113.{index}"
        middleware._hold(address, 30)
        middleware._hold("ip:198.51.100.1", -1)  # Expired at once, by every thread.
        expired = middleware._held("ip:198.51.100.1")
        return middleware._held(address), expired

    results = on_threads(hold_then_expire)

    assert all(held is not None and expired is None for held, expired in results)
    assert set(middleware.held) == {f"ip:203.0.113.{i}" for i in range(THREADS)}


class CheckedPool(HashingPool):
    """A pool whose count may only change with its lock held."""

    @property
    def pending(self) -> int:
        """Calls running or waiting."""
        return self._pending

    @pending.setter
    def pending(self, value: int) -> None:
        """Change the count, which needs the lock once the pool exists."""
        if hasattr(self, "_lock"):
            assert self._lock.locked()
        self._pending = value


def test_holds_are_skipped_only_while_none_is_held(use_auth: UseAuth) -> None:
    """Requests read whether anything is held without the lock, never wrongly.

    Threads hold and let go of addresses while others ask, as each request
    does, whether to look its address up: an address held for good is
    found by every thread once it is held.
    """
    use_auth(settings={"RATE_LIMIT": "10/minute"})
    middleware = AuthenticationMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
    middleware._hold("ip:192.0.2.1", 3600)

    def ask(index: int) -> int:
        found = 0
        for round_ in range(500):
            middleware._hold(f"ip:203.0.113.{index}", -1 if round_ % 2 else 30)
            held = middleware._held("ip:192.0.2.1") if middleware.held else None
            found += held is not None
        return found

    assert on_threads(ask) == [500] * THREADS


class _Guarded(OrderedDict[Any, Any]):
    """An ``OrderedDict`` that is only touched under ``lock``."""

    def __init__(self, lock: Any) -> None:  # noqa: ANN401 - a lock.
        """Watch ``lock``."""
        super().__init__()
        self.lock = lock

    def _check(self) -> None:
        assert self.lock.locked()

    def __getitem__(self, key: Any) -> Any:  # noqa: ANN401
        self._check()
        return super().__getitem__(key)

    def __setitem__(self, key: Any, value: Any) -> None:  # noqa: ANN401
        self._check()
        super().__setitem__(key, value)

    def get(self, key: Any, default: Any = None) -> Any:  # noqa: ANN401
        self._check()
        return super().get(key, default)

    def pop(self, key: Any, *default: Any) -> Any:  # noqa: ANN401
        self._check()
        return super().pop(key, *default)

    def move_to_end(self, key: Any, last: bool = True) -> None:  # noqa: ANN401
        self._check()
        super().move_to_end(key, last)

    def popitem(self, last: bool = True) -> tuple[Any, Any]:
        self._check()
        return super().popitem(last)


def test_the_principal_cache_stays_whole_on_every_thread() -> None:
    """Keeps, hits, invalidations, and evictions at once: bounded and consistent.

    Above all, nothing read before an invalidation of what it rests on is
    kept after it, whatever the interleaving.
    """
    cache = PrincipalCache(recent=32)
    cache._entries = _Guarded(cache._lock)
    cache._invalidated = _Guarded(cache._lock)

    def hammer(index: int) -> int:
        chosen = random.Random(index)  # noqa: S311 - the order of the calls only.
        kept = 0
        for round_ in range(1_500):
            user = f"user:{chosen.randrange(12)}"
            started = cache.begin()
            entry = Entry(
                ("token", f"{index}-{round_ % 40}"),
                None,
                targets=(user, f"token:{index}-{round_ % 40}"),
                started=started,
                keep=True,
                deadline=10.0,
            )
            step = chosen.random()
            if step < 0.5:
                kept += cache.put(entry, limit=64) is not None
            elif step < 0.8:
                found = cache.get(entry.key, now=chosen.choice((1.0, 20.0)))
                if found is not None:
                    cache.claim_touch(found, clock_module.now())
            elif step < 0.97:
                cache.invalidate([user])
            elif step < 0.99:
                cache.discard(entry)
            else:
                cache.invalidate([ALL])
        return kept

    assert sum(on_threads(hammer)) > 0

    assert len(cache) <= 64
    with cache._lock:
        indexed = {key for keys in cache._by_target.values() for key in keys}
        assert indexed == set(cache._entries)
        for key, entry in cache._entries.items():
            assert entry.key == key
            assert entry.started >= cache._floor
            for target in (ALL, *entry.targets):
                assert cache._invalidated.get(target, -1) <= entry.started
                assert target == ALL or key in cache._by_target[target]


def test_shared_grants_stay_equal_and_bounded_on_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shared`` takes no lock: each value comes back equal, the table bounded.

    Threads that intern one value at once may each keep their copy, and may
    restart interning together: a copy more, never another value.
    """
    monkeypatch.setattr(principals, "INTERNED_MAX", 64)
    monkeypatch.setattr(principals, "_INTERNED", {})

    def intern(index: int) -> int:
        chosen = random.Random(index)  # noqa: S311 - the values only.
        wrong = 0
        for _ in range(5_000):
            number = chosen.randrange(200)
            value = (number, f"grant {number}")  # A new object each time.
            wrong += principals.shared(value) != value
        return wrong

    assert on_threads(intern) == [0] * THREADS
    assert len(principals._INTERNED) < 64 + THREADS


def test_the_hashing_pool_counts_calls_from_every_thread() -> None:
    """Loops on several threads share one count, changed under its lock."""
    pool = CheckedPool(threads=2, queue=1_000)

    async def calls(index: int) -> list[int]:
        return [await pool.run(lambda n: n * 2, index) for _ in range(20)]

    results = on_threads(lambda index: asyncio.run(calls(index)))

    assert results == [[index * 2] * 20 for index in range(THREADS)]
    assert pool.pending == 0


class CheckedRegistry[K, V](WeakKeyDictionary[K, V]):
    """A registry of loops that may be walked or changed only with ``lock`` held.

    Reading one loop's entry needs no lock.
    """

    def __init__(self, lock: threading.Lock) -> None:
        """Check against ``lock``."""
        super().__init__()
        self.lock = lock

    def __iter__(self) -> Iterator[K]:
        """Walk the loops, with the lock held."""
        assert self.lock.locked()
        return super().__iter__()

    def __setitem__(self, key: K, value: V) -> None:
        """Add a loop, with the lock held."""
        assert self.lock.locked()
        super().__setitem__(key, value)

    def __delitem__(self, key: K) -> None:
        """Drop a loop, with the lock held."""
        assert self.lock.locked()
        super().__delitem__(key)

    def setdefault(self, key: K, default: Any = None) -> Any:  # noqa: ANN401
        """Add a loop unless there, with the lock held."""
        assert self.lock.locked()
        return super().setdefault(key, default)

    def pop(self, key: K, *default: Any) -> Any:  # noqa: ANN401
        """Drop a loop, with the lock held."""
        assert self.lock.locked()
        return super().pop(key, *default)


def test_loops_on_threads_own_their_tasks_and_let_closed_ones_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each loop drops the closed loops it finds while others add their own."""
    owned = CheckedRegistry[asyncio.AbstractEventLoop, set[asyncio.Task[Any]]](
        tasks._owned_lock
    )
    monkeypatch.setattr(tasks, "_owned", owned)

    async def spawn_one() -> bool:
        task = tasks.spawn(asyncio.sleep(0), name="test.threads")
        await task
        return not tasks.owned()  # Let go once done.

    results = on_threads(lambda _: [asyncio.run(spawn_one()) for _ in range(LOOPS)])

    assert results == [[True] * LOOPS] * THREADS
    asyncio.run(spawn_one())  # Drops every loop closed so far but its own.
    with tasks._owned_lock:
        assert len(list(owned)) <= 1


def test_loops_on_threads_keep_their_own_publishers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other loop closes without its publishers; the next new loop drops them."""
    publishers = CheckedRegistry[Any, Any](dispatch._publishers_lock)
    monkeypatch.setattr(dispatch, "_publishers", publishers)
    config = MessagingSettings(BROKER="work")

    async def publish(close: bool) -> bool:
        publisher = dispatch.commit_publisher(config)
        same = dispatch.commit_publisher(config) is publisher
        if close:
            await dispatch.close_publishers(within=1)
        return same

    def loops(index: int) -> list[bool]:
        return [asyncio.run(publish(n % 2 == 0)) for n in range(LOOPS)]

    assert on_threads(loops) == [[True] * LOOPS] * THREADS
    asyncio.run(publish(close=True))  # Drops every closed loop's publishers.
    with dispatch._publishers_lock:
        assert list(publishers) == []


def test_loops_on_threads_keep_their_own_brokers() -> None:
    """One handler serves loops on many threads; closed loops' brokers are dropped."""
    handler = BrokerHandler()
    handler._brokers = CheckedRegistry(handler._lock)
    handler._replaced = CheckedRegistry(handler._lock)

    async def use(close: bool) -> bool:
        broker = handler["memory"]
        same = handler["memory"] is broker
        if close:
            await handler.close_all()
        return same

    def loops(index: int) -> list[bool]:
        return [asyncio.run(use(n % 2 == 0)) for n in range(LOOPS)]

    with override_settings(BROKERS={"memory": {"URL": "memory://"}}):
        assert on_threads(loops) == [[True] * LOOPS] * THREADS
        asyncio.run(use(close=True))

    with handler._lock:
        assert list(handler._brokers) == []


def test_brokers_replaced_by_new_settings_are_kept_under_the_lock() -> None:
    """They close with their loop, or are dropped once it closed without that."""
    closed: list[Broker] = []

    class Recorded(MemoryBroker):
        async def close(self) -> None:
            closed.append(self)

    handler = BrokerHandler(connect=lambda config: Recorded())
    handler._brokers = CheckedRegistry(handler._lock)
    handler._replaced = CheckedRegistry(handler._lock)
    rabbit = {"URL": "amqp://app:pw@localhost:5672/app", "PREFIX": "app"}

    async def replace(close: bool) -> list[Broker]:
        first = handler["rabbit"]
        with override_settings(BROKERS={"rabbit": {**rabbit, "PREFIX": "new"}}):
            second = handler["rabbit"]  # The first waits, under the lock, to close.
        if close:
            await handler.close_all()
        return [second, first]

    with override_settings(BROKERS={"rabbit": rabbit}):
        left = asyncio.new_event_loop()
        left.run_until_complete(replace(close=False))
        left.close()
        assert left in handler._replaced  # Still referenced here.
        replaced = asyncio.run(replace(close=True))

    assert closed == replaced  # Not the brokers of the loop closed before.
    assert left not in handler._replaced


class CheckedSlots(Slots):
    """Live connection slots counted only with their lock held, within the cap."""

    checking = False
    """Whether the slots are made, so a change needs the lock."""

    def __init__(self, total: int, per_caller: int) -> None:
        """Slots under ``total`` and ``per_caller``, checked from now on."""
        super().__init__(total, per_caller)
        self.checking = True

    @property
    def open(self) -> int:
        """Connections open now."""
        return self._open

    @open.setter
    def open(self, value: int) -> None:
        """Change the count, which needs the lock once the slots exist."""
        if self.checking:
            assert self._lock.locked()
        assert 0 <= value <= self.total
        self._open = value


def test_live_connection_slots_are_counted_from_every_thread() -> None:
    """Loops on several threads open and close connections; none leaks or fails.

    Each thread opens and closes WebSockets through the middleware, as one
    of two callers, under caps that more threads than they allow can meet.
    """
    application = SimpleNamespace(state=State())
    application.state.live_slots = slots = CheckedSlots(total=6, per_caller=3)
    middleware = WebSocketLimitsMiddleware(refuse_at_once)

    async def connections(index: int) -> None:
        for number in range(200):
            scope: Scope = {
                "type": "websocket",
                "app": application,
                "client": (f"203.0.113.{(index + number) % 2}", 4000),
                "headers": [],
                "path": "/live",
            }
            await middleware(scope, connect, close)

    on_threads(lambda index: asyncio.run(connections(index)))

    assert (slots.open, slots.callers) == (0, {})


async def refuse_at_once(scope: Scope, receive: Receive, send: Send) -> None:
    """A WebSocket route that ends without accepting."""


async def connect() -> Message:
    """A client's handshake."""
    return {"type": "websocket.connect"}


async def close(message: Message) -> None:
    """Take what the middleware sends a refused client."""


class CheckedState(State):
    """An application's state, whose slots are only looked up under the lock."""

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - any state.
        """Read ``name``; the slots only while they cannot be made elsewhere."""
        if name == "live_slots":
            assert live._making_slots.locked()
        return super().__getattr__(name)


def test_an_application_gets_one_set_of_slots_from_every_thread() -> None:
    """The first connections, on several loops at once, share the slots made."""
    scope = {"app": SimpleNamespace(state=CheckedState())}

    found = on_threads(lambda _: live.slots_for(scope))

    assert all(slots is found[0] for slots in found)
    assert scope["app"].state._state == {"live_slots": found[0]}


class CheckedThrottleState(State):
    """An application's state, whose throttle backend is set only under the lock."""

    def __setattr__(self, name: str, value: Any) -> None:  # noqa: ANN401 - any state.
        """Set ``name``; the throttle backend only with its lock held."""
        if name == "throttle_backend":
            assert ratelimit._app_backend_lock.locked()
        super().__setattr__(name, value)


class RacedState:
    """An application's state where another thread makes the backend first.

    The first look finds none; by the time the lock is held, it is there.
    """

    def __init__(self, made: MemoryBackend) -> None:
        """Have ``made`` appear after the first look."""
        self.looks = 0
        self.made = made

    @property
    def throttle_backend(self) -> MemoryBackend | None:
        """None the first time; the other thread's backend after."""
        self.looks += 1
        return None if self.looks == 1 else self.made


def test_a_throttle_backend_made_meanwhile_is_the_one_used() -> None:
    """A loop that lost the race to make it uses the winner's, not a second."""
    made = MemoryBackend()
    connection = HTTPConnection(
        {"type": "http", "app": SimpleNamespace(state=RacedState(made))}
    )

    assert ratelimit._app_backend(connection) is made


class CheckedLoginState(State):
    """An application's state, whose attempt counter is set only under the lock."""

    def __setattr__(self, name: str, value: Any) -> None:  # noqa: ANN401 - any state.
        """Set ``name``; the attempt counter only with its lock held."""
        if name == "login_limiter":
            assert throttling._making_limiter.locked()
        super().__setattr__(name, value)


class RacedLoginState:
    """An application's state where another thread makes the counter first."""

    def __init__(self, made: MemoryBackend) -> None:
        """Have ``made`` appear after the first look."""
        self.looks = 0
        self.made = made

    @property
    def login_limiter(self) -> MemoryBackend | None:
        """None the first time; the other thread's counter after."""
        self.looks += 1
        return None if self.looks == 1 else self.made


def test_first_login_attempts_on_every_thread_share_one_counter() -> None:
    """Two attempt counters for one application would each allow the limit."""
    CountedBackend.made.clear()
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=CheckedLoginState())}
    )

    with override_settings(RATE_LIMIT_BACKEND=f"{__name__}.CountedBackend"):
        found = on_threads(lambda _: throttling.limiter(request))

    assert all(made is found[0] for made in found)
    assert CountedBackend.made == [found[0]]
    made = MemoryBackend()
    raced = Request(
        {"type": "http", "app": SimpleNamespace(state=RacedLoginState(made))}
    )
    assert throttling.limiter(raced) is made  # The winner's, not a second one.


class CountedBackend(MemoryBackend):
    """A memory backend that counts how many were made."""

    made: ClassVar[list["CountedBackend"]] = []

    def __init__(self) -> None:
        """Count this one."""
        super().__init__()
        CountedBackend.made.append(self)


def test_an_application_gets_one_throttle_backend_from_every_thread() -> None:
    """The first throttled requests, on several loops at once, share one count.

    Two memory backends for one application would each let the limit through.
    """
    CountedBackend.made.clear()
    app = SimpleNamespace(state=CheckedThrottleState())
    connection = HTTPConnection({"type": "http", "app": app})
    backend = f"{__name__}.CountedBackend"

    with override_settings(RATE_LIMIT_BACKEND=backend):
        found = on_threads(lambda _: ratelimit._app_backend(connection))

    assert all(made is found[0] for made in found)
    assert CountedBackend.made == [found[0]]


class CheckedStores(dict[tuple[str, CacheConfig], Store]):
    """The process's memory stores, added only with the handler's lock held."""

    def __init__(self, lock: threading.Lock) -> None:
        """Check against ``lock``."""
        super().__init__()
        self.lock = lock

    def __setitem__(self, key: tuple[str, CacheConfig], value: Store) -> None:
        """Add a store, with the lock held."""
        assert self.lock.locked()
        super().__setitem__(key, value)


def test_loops_on_threads_share_one_memory_store_per_cache() -> None:
    """Loops looking a memory cache up at once all see the same entries.

    A second store for one alias would never see the others' writes, their
    invalidations, or ``caches.reset()``.
    """
    handler = CacheHandler()
    handler._caches = CheckedRegistry(handler._lock)
    handler._replaced = CheckedRegistry(handler._lock)
    handler._shared = CheckedStores(handler._lock)

    async def use(index: int, close: bool) -> Store:
        cache = handler["default"]
        await cache.set(f"from-{index}", index)
        if close:
            await handler.close_all()
        return cache.store

    def loops(index: int) -> list[Store]:
        return [asyncio.run(use(index, n % 2 == 0)) for n in range(LOOPS)]

    with override_settings(CACHES={"default": {"URL": "memory://"}}):
        stores = {id(store) for found in on_threads(loops) for store in found}

        async def read() -> list[int | None]:
            cache = handler["default"]
            found = [await cache.get(f"from-{n}", int) for n in range(THREADS)]
            with override_settings(CACHES={"default": {"URL": "dummy://"}}):
                handler["default"]  # Replaced: kept, under the lock, to close.
            await handler.close_all()
            return found

        assert asyncio.run(read()) == list(range(THREADS))
    assert len(stores) == 1
    handler.reset()


class CheckedList(list[Any]):
    """One event type's subscriptions, changed only with the bus's lock held."""

    def __init__(self, bus: EventBus) -> None:
        """No subscriptions yet, of ``bus``."""
        super().__init__()
        self.bus = bus

    def append(self, item: Any) -> None:  # noqa: ANN401
        """Add ``item``, under the bus's lock."""
        assert self.bus._lock.locked()
        super().append(item)

    def __setitem__(self, index: Any, value: Any) -> None:  # noqa: ANN401
        """Replace ``index``, under the bus's lock."""
        assert self.bus._lock.locked()
        super().__setitem__(index, value)


class CheckedSubscriptions(dict[type[DomainEvent], list[Any]]):
    """A bus's subscriptions, changed or listed only with the bus's lock held.

    Publishing reads one type's subscriptions without it.
    """

    def __init__(self, bus: EventBus) -> None:
        """No subscriptions yet, of ``bus``."""
        super().__init__()
        self.bus = bus

    def setdefault(self, key: type[DomainEvent], default: Any = None) -> Any:  # noqa: ANN401
        """Find or start the subscriptions of ``key``, under the bus's lock."""
        assert self.bus._lock.locked()
        return super().setdefault(key, CheckedList(self.bus))

    def items(self) -> Any:  # noqa: ANN401
        """Every type's subscriptions, under the bus's lock."""
        assert self.bus._lock.locked()
        return super().items()

    def clear(self) -> None:
        """Forget every subscription, under the bus's lock."""
        assert self.bus._lock.locked()
        super().clear()


def numbered_handler(number: int) -> Callable[[DomainEvent], Awaitable[None]]:
    """A handler of its own, named after ``number``."""

    async def handle(event: DomainEvent) -> None:
        """Do nothing."""

    handle.__qualname__ = f"handler_{number}"
    return handle


def test_event_bus_subscriptions_survive_threads() -> None:
    """Threads that subscribe the same handlers at once subscribe each once.

    Each then unsubscribes every other one, and lists the durable ones:
    those stay gone, whichever thread subscribed last.
    """
    bus = EventBus()
    bus._handlers = CheckedSubscriptions(bus)
    handlers = [numbered_handler(number) for number in range(50)]

    def churn(index: int) -> list[Any]:
        for handler in handlers:
            bus.subscribe(DomainEvent, handler)
        for handler in handlers[1::2]:
            bus.unsubscribe(DomainEvent, handler)
        return bus.durable_subscriptions()

    durable = on_threads(churn)

    assert bus.handlers_for(DomainEvent) == handlers[::2]
    assert durable == [[]] * THREADS
    bus.clear()
    assert bus.handlers_for(DomainEvent) == []


def test_live_events_hook_the_bus_once_from_every_thread() -> None:
    """Loops on several threads that subscribe at once hook ``publish`` once.

    Hooked twice, every committed event would be sent to each client twice.
    """
    bus = EventBus()
    bus._handlers = CheckedSubscriptions(bus)
    hub = LiveEvents(bus)

    async def subscribe() -> None:
        scope = {"type": "http", "headers": [], "user": SimpleNamespace(id=1)}
        async with hub.subscribe(HTTPConnection(scope), DomainEvent, rule=AllowAny()):
            pass

    on_threads(lambda _: asyncio.run(subscribe()))

    assert bus.handlers_for(DomainEvent) == [hub.publish]
    assert hub._subscribers == set()


def test_a_circuit_lets_one_trial_through_whichever_thread_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the cooldown, threads asking at once let exactly one call try."""
    breakers: list[CircuitBreaker] = []

    def monotonic() -> float:
        assert breakers[-1]._lock.locked()  # Circuits are read under the lock.
        return 1000.0

    def trial(_: int) -> bool:
        return breakers[-1].admits("api.example.com", 0.0)

    monkeypatch.setattr(clock_module, "monotonic", monotonic)
    for _ in range(50):
        breakers.append(CircuitBreaker())
        breakers[-1].failed("api.example.com", 1)
        assert sum(on_threads(trial)) == 1


def test_the_circuit_breaker_survives_every_thread_at_once() -> None:
    """Failures, successes, and trials on many hosts: no error, still bounded."""
    breaker = CircuitBreaker()

    def hammer(seed: int) -> int:
        chosen = random.Random(seed)  # noqa: S311 - the order of the calls only.
        for _ in range(2_000):
            host = f"h{chosen.randrange(BREAKER_HOSTS * 2)}"
            step = chosen.random()
            if step < 0.6:
                breaker.failed(host, 3)
            elif step < 0.8:
                breaker.succeeded(host)
            elif step < 0.95:
                breaker.admits(host, 0.0)
            else:
                breaker.abandoned(host)
        return 0

    on_threads(hammer)

    assert len(breaker._circuits) <= BREAKER_HOSTS


def test_the_shed_report_counts_every_refusal_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count is exact; the clock is read under the lock."""
    report = ShedReport(10)
    report._quiet_until = float("inf")  # Never warn: only count.

    def monotonic() -> float:
        assert report._lock.locked()
        return 1000.0

    monkeypatch.setattr(clock_module, "monotonic", monotonic)

    def shed(_: int) -> None:
        for _ in range(5_000):
            report.shed(QUEUE_FULL)

    on_threads(shed)

    assert report.count == THREADS * 5_000


def test_the_guards_lookups_are_counted_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loops on several threads share the lookup threads; none is lost or leaked."""

    def getaddrinfo(host: str, port: int, **options: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    resolver = GuardResolver(threads=3, per_host=2)

    async def lookups(index: int) -> int:
        found = 0
        for attempt in range(40):
            try:
                await resolver.lookup(f"h{(index + attempt) % 5}.test", 443)
            except socket.gaierror:
                continue  # Too many under way: refused at once.
            found += 1
        return found

    found = on_threads(lambda index: asyncio.run(lookups(index)))
    asyncio.run(resolver.close())

    assert sum(found) > 0
    assert (resolver.running, resolver._running) == (0, {})


def test_the_guards_owners_are_counted_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each owner's share holds across threads; no count is lost or leaked."""

    def getaddrinfo(host: str, port: int, **options: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    resolver = GuardResolver(threads=8, per_host=8)

    async def lookups(index: int) -> int:
        found = 0
        with lookups_for(f"owner{index % 3}"):
            for attempt in range(40):
                try:
                    await resolver.lookup(f"h{(index + attempt) % 5}.test", 443)
                except socket.gaierror:
                    continue  # Past a bound: refused at once.
                found += 1
        return found

    found = on_threads(lambda index: asyncio.run(lookups(index)))
    asyncio.run(resolver.close())

    assert sum(found) > 0
    assert (resolver.running, resolver._running, resolver._owners) == (0, {}, {})


def test_the_guards_slow_answers_are_kept_and_let_go_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answers kept, given, expired, and let go at once: each host gets its own.

    The cache is read and written under the resolver's lock only, and stays
    within ``SLOW_ANSWERS``.
    """
    resolver: GuardResolver
    ticks = itertools.count()
    tick = threading.Lock()

    def monotonic() -> float:
        assert resolver._lock.locked()
        with tick:
            return next(ticks) / 10  # A kept answer runs out after 100 reads.

    def getaddrinfo(host: str, port: int, **options: object) -> list[object]:
        number = int(host.removeprefix("h").removesuffix(".test"))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{number}", port))
        ]

    monkeypatch.setattr(clock_module, "monotonic", monotonic)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(guard_module, "SLOW_ANSWERS", 3)
    resolver = GuardResolver(threads=8, per_host=8, hung_after=0.0, back_off=10.0)

    async def lookups(index: int) -> bool:
        right = True
        for attempt in range(60):
            number = (index + attempt) % 7
            try:
                found = await resolver.lookup(f"h{number}.test", 443)
            except socket.gaierror:
                continue  # Past a bound: refused at once.
            right &= found == [ipaddress.ip_address(f"192.0.2.{number}")]
        return right

    right = on_threads(lambda index: asyncio.run(lookups(index)))
    asyncio.run(resolver.close())

    assert right == [True] * THREADS
    assert len(resolver._answered) <= 3
    assert (resolver.running, resolver._running) == (0, {})


def test_a_throttled_log_counts_every_repeat_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One line per key is written; every other is counted, under the lock."""
    throttled = ThrottledLog(logging.getLogger("domainkit_tests.threads"))

    def monotonic() -> float:
        assert throttled._lock.locked()
        return 1000.0  # Frozen: nothing is let through again.

    monkeypatch.setattr(clock_module, "monotonic", monotonic)

    def repeat(index: int) -> None:
        for _ in range(2_000):
            throttled.warning("event", f"key{index % 2}", "A line")

    on_threads(repeat)

    assert sorted(throttled._seen) == [("event", "key0"), ("event", "key1")]
    assert sum(seen.held for seen in throttled._seen.values()) == THREADS * 2_000 - 2


class _Counting(logging.Handler):
    """How many events the lines a ``ThrottledLog`` wrote stand for."""

    def __init__(self) -> None:
        super().__init__()
        self.events = 0
        self._count = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """A line is one event, plus the repeats it reports held back."""
        args = record.args
        assert isinstance(args, tuple)
        events = 1
        if record.msg.startswith("%d more"):
            events = int(str(args[0]))
        elif record.msg.startswith("%s (and"):
            events += int(str(args[1]))
        with self._count:
            self.events += events


def test_throttled_logs_lose_no_count_to_sweeps_and_flushes_on_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines, sweeps, flushes, and new logs at once: every event is reported once.

    Keys come and go (an interval passes for some while others are held),
    and ``flush_throttled`` runs meanwhile on every thread; the counts the
    lines report add up to the warnings made.
    """
    logger = logging.getLogger("domainkit_tests.threads.sweep")
    counting = _Counting()
    logger.addHandler(counting)
    monkeypatch.setattr(logger, "propagate", False)
    throttled = ThrottledLog(logger, interval=1.0, keys=4)
    ticks = itertools.count()
    tick = threading.Lock()

    def monotonic() -> float:
        assert throttled._lock.locked()
        with tick:
            return next(ticks) / 100  # An interval passes every 100 reads.

    monkeypatch.setattr(clock_module, "monotonic", monotonic)

    def repeat(index: int) -> None:
        for number in range(2_000):
            throttled.warning("event", f"key{(index + number) % 6}", "A line")
            if number % 250 == 0:
                ThrottledLog(logger)  # Registered, then let go.
                flush_throttled()

    try:
        on_threads(repeat)
        flush_throttled()
    finally:
        logger.removeHandler(counting)

    assert throttled._seen == {}
    assert counting.events == THREADS * 2_000


def test_loops_on_threads_get_their_own_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each loop makes its own engines, reads alone too; closing drops them."""
    engines = CheckedRegistry[asyncio.AbstractEventLoop, Any](connections._lock)
    monkeypatch.setattr(connections, "_engines", engines)
    network = "postgresql+asyncpg://app@db.invalid/app"  # Never connected to.

    async def use(close: bool) -> bool:
        engine = connections.engine()
        reads = connections.session(readonly=True, reads=Reads.STATEMENTS).bind
        same = connections.engine() is engine
        again = connections.session(readonly=True, reads=Reads.STATEMENTS).bind
        if close:
            await connections.close_all()
        return same and again is reads and reads is not engine

    def loops(index: int) -> list[bool]:
        return [asyncio.run(use(n % 2 == 0)) for n in range(LOOPS)]

    with override_settings(DATABASES={"default": {"URL": network}}):
        assert on_threads(loops) == [[True] * LOOPS] * THREADS
        asyncio.run(use(close=True))
    with connections._lock:
        assert len(list(engines)) <= THREADS * LOOPS // 2 + 1  # Unclosed loops'.


def own_listeners(engine: AsyncEngine) -> list[Any]:
    """The listeners domainkit added to ``engine``, its dialect, and its pool."""
    sync = engine.sync_engine
    dialect, pool = sync.dialect.dispatch, sync.pool.dispatch
    found = [
        *dialect.do_execute,
        *dialect.do_execute_no_params,
        *dialect.handle_error,
        *pool.connect,
        *pool.checkout,
        *pool.reset,
        *sync.dispatch.begin,
    ]
    return [listener for listener in found if isinstance(listener, partial)]


def test_engines_never_share_a_listener() -> None:
    """Each engine listens with callables of its own, so no registry key repeats.

    SQLAlchemy keys its listener registry by the ids of the target and the
    function, and is not thread-safe. With one function for every engine, a
    freed pool's reused id repeated a key, and threads making and dropping
    engines raced on it: a ``KeyError`` in the registry's weakref callback.
    """
    network = DatabaseConfig.model_validate(
        {"URL": "postgresql+asyncpg://app@db.invalid/app"}
    )
    local = DatabaseConfig.model_validate({"URL": "sqlite+aiosqlite://"})
    read = [
        _read_alone,
        _read_alone_no_params,
        _drop_detail,
        _require_read_only,
        _take_open,
        _reset_session,
    ]
    for build, config, functions in [
        (create_read_engine, network, read),
        (create_engine, local, [_drop_detail, _begin_snapshot]),
    ]:
        first, second = own_listeners(build(config)), own_listeners(build(config))

        assert [listener.func for listener in first] == functions
        assert [listener.func for listener in second] == functions
        assert not {id(listener) for listener in first} & {
            id(listener) for listener in second
        }


def test_counting_statements_never_fails_another_threads_statement() -> None:
    """Blocks of ``CapturedQueries`` come and go while threads run statements.

    SQLAlchemy reads its listener lists without a lock as each statement
    runs. A block that listened on ``Engine`` and stopped again changed them
    under other threads, whose statements failed now and then ("deque
    mutated during iteration"): 3 runs of 3 failed this way, at the same
    switch interval. The listener now stays, and blocks only come and go
    from the tuple it reads.
    """
    errors: list[BaseException] = []
    stop = threading.Event()

    def run_statements() -> None:
        try:
            while not stop.is_set():
                engine = create_sync_engine("sqlite://")
                with engine.connect() as connection:
                    for _ in range(20):
                        connection.execute(text("SELECT 1"))
                engine.dispose()
        except Exception as error:  # noqa: BLE001 - any failure is the finding.
            errors.append(error)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # Switch threads as often as the GIL allows.
    threads = [threading.Thread(target=run_statements) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(3000):
            with CapturedQueries():
                pass
    finally:
        stop.set()
        for thread in threads:
            thread.join()
        sys.setswitchinterval(interval)

    assert errors == []


def test_subject_keys_kept_by_transactions_on_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactions on many threads keep their own keys; forgetting reaches all.

    Every forget is counted once, and a key read before any forget began
    is never kept, whichever thread forgot what.
    """
    held = CheckedRegistry[Any, dict[str, bytes]](personal._held_lock)
    monkeypatch.setattr(personal, "_held", held)
    sessions = []
    for index in range(THREADS):
        session = AsyncSession()
        session.sync_session.begin()
        mine = {"shared": b"shared", f"s{index}": bytes([index])}
        personal._keep(session, mine, personal._forgotten())
        sessions.append(session)
    began = personal._forgotten()

    def work(index: int) -> list[bool]:
        session, results = sessions[index], []
        for _ in range(LOOPS):
            read = personal._forgotten()
            personal._drop({f"gone{index}"}.__contains__)
            personal._keep(session, {f"late{index}": b"late"}, read)
            kept = personal._kept(session, [f"late{index}", f"s{index}"])
            results.append(kept == {f"s{index}": bytes([index])})
        personal._drop({"shared"}.__contains__)
        results.append(personal._kept(session, ["shared"]) == {})
        return results

    assert on_threads(work) == [[True] * (LOOPS + 1)] * THREADS
    assert personal._forgotten() == began + THREADS * (LOOPS + 1)
    for session in sessions:
        session.sync_session.close()


IMPORT_EVERYTHING = """
import importlib, pkgutil, sys
import domainkit

# These two run on import: one needs AUDIT settings, one is Alembic's env.
SKIP = {"domainkit.contrib.audit.tasks", "domainkit.db.migration_env.env"}
for module in pkgutil.walk_packages(domainkit.__path__, "domainkit."):
    if module.name not in SKIP:
        importlib.import_module(module.name)
print(sys._is_gil_enabled())
"""


def test_one_json_formatter_dates_lines_from_every_thread() -> None:
    """Its cached second is one tuple: no line gets another second's date.

    Each thread formats seconds of its own day, so the cache changes hands
    all the time.
    """
    formatter = JsonFormatter()

    def stamps(index: int) -> list[tuple[str, str]]:
        times = [index * 86_400.0 + second + 0.25 for second in range(300)]
        return [
            (
                formatter.timestamp(created),
                datetime.fromtimestamp(created, UTC).isoformat(timespec="milliseconds"),
            )
            for created in times
        ]

    for written in on_threads(stamps):
        assert all(stamp == expected for stamp, expected in written)


def test_importing_domainkit_keeps_the_gil_off_where_it_is_off() -> None:
    """Neither domainkit nor what it imports turns a free-threaded GIL back on.

    An extension that does not declare free-threading support turns the GIL
    back on when it loads, with a warning, unless ``PYTHON_GIL=0`` forces it
    off: the child runs without that, every warning an error. On a build
    with a GIL there is nothing to turn on.
    """
    environment = {
        name: value for name, value in os.environ.items() if name != "PYTHON_GIL"
    }
    child = subprocess.run(  # noqa: S603 - this interpreter, fixed code.
        [sys.executable, "-W", "error", "-c", IMPORT_EVERYTHING],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert child.returncode == 0, child.stderr
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    assert child.stdout.strip() == str(not free_threaded)


def test_client_groups_are_remembered_alike_from_every_thread() -> None:
    """Threads filling one memory of groups at once each get the parsed group."""
    ratelimit._cached_address_group.cache_clear()
    ratelimit._cached_network.cache_clear()
    networks = {
        f"2001:db8:{n:x}::1": str(ipaddress.IPv6Network(f"2001:db8:{n:x}::/64"))
        for n in range(100)
    }
    addresses = {f"203.0.113.{n}": f"203.0.113.{n}" for n in range(100)}
    groups = networks | addresses

    def group_all(seed: int) -> bool:
        order = random.Random(seed).sample(list(groups), len(groups))  # noqa: S311 - order only.
        return all(client_group(host) == groups[host] for host in order)

    assert all(on_threads(group_all))
    assert ratelimit._cached_address_group.cache_info().currsize == len(groups)
    assert ratelimit._cached_network.cache_info().currsize == len(networks)


def test_a_proxys_remembered_addresses_hold_from_every_thread() -> None:
    """One middleware, many threads parsing hops: the parsed answer each time."""
    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"]):
        proxy = ProxyHeadersMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
    hops = [f"10.0.{n}.1" for n in range(50)] + [
        f"[2001:db8::{n:x}]:443" for n in range(50)
    ]

    def parse_all(seed: int) -> bool:
        order = random.Random(seed).sample(hops, len(hops))  # noqa: S311 - order only.
        return all(proxy.address(hop) == _address(proxy.networks, hop) for hop in order)

    assert all(on_threads(parse_all))


class LockedSeries(dict[tuple[str, str, int], Any]):
    """The kept metric handles, which may only grow with their lock held."""

    def __init__(self, lock: Any) -> None:  # noqa: ANN401 - a lock.
        """Check ``lock`` on every write."""
        super().__init__()
        self.lock = lock

    def __setitem__(self, key: tuple[str, str, int], value: Any) -> None:  # noqa: ANN401 - a handle.
        """Keep a handle, under the lock."""
        assert self.lock.locked()
        super().__setitem__(key, value)


def test_metric_handles_are_kept_once_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every thread counts in the one series; the kept handles stay bounded."""
    monkeypatch.setattr(metrics, "MAX_SERIES", 10)
    counter = MetricsMiddleware(app=lambda *args: None)  # type: ignore[arg-type]
    counter._series = LockedSeries(counter._lock)
    keys = [("GET", f"/threads/{n}", 200) for n in range(30)]

    def count_all(seed: int) -> bool:
        order = random.Random(seed).sample(keys, len(keys))  # noqa: S311 - order only.
        return all(
            counter.series(*key)[0] is REQUESTS.labels(key[0], key[1], str(key[2]))
            for key in order
        )

    assert all(on_threads(count_all))
    assert len(counter._series) == 10


class LockedDict[K, V](dict[K, V]):
    """A cache that may only be written with ``lock`` held; reads need none."""

    def __init__(self, lock: Any) -> None:  # noqa: ANN401 - a lock.
        """Check ``lock`` on every write."""
        super().__init__()
        self.lock = lock

    def __setitem__(self, key: K, value: V) -> None:
        """Keep an entry, under the lock."""
        assert self.lock.locked()
        super().__setitem__(key, value)

    def clear(self) -> None:
        """Forget every entry, under the lock."""
        assert self.lock.locked()
        super().clear()


def test_content_type_keys_are_worked_out_once_from_every_thread(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threads asking at once wait for one of them to work the keys out."""
    use_auth()
    monkeypatch.setattr(content_registry, "_known", (None, {}))
    installed = content_registry.installed_models
    worked_out: list[int] = []

    def counted() -> list[InstalledModel]:
        assert content_registry._lock.locked()
        worked_out.append(threading.get_ident())
        return installed()

    monkeypatch.setattr(content_registry, "installed_models", counted)
    expected = {User: ("auth", "user"), Group: ("auth", "group")}

    def look_up(index: int) -> bool:
        return all(
            content_type_key(cls) == key
            for _ in range(200)
            for cls, key in expected.items()
        )

    assert all(on_threads(look_up))
    assert len(worked_out) == 1


def test_content_type_keys_follow_mappings_made_on_other_threads(
    use_auth: UseAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changes counted on one thread make every thread work the keys out again."""
    use_auth()

    def look_up_or_change(index: int) -> bool:
        if index == 0:
            for _ in range(200):
                content_registry._mapped()
            return True
        return all(content_type_key(User) == ("auth", "user") for _ in range(500))

    before = content_registry._mappings
    assert all(on_threads(look_up_or_change))
    assert content_registry._mappings == before + 200
    content_type_key(User)
    assert content_registry._known[0] == content_registry._state()


def test_domain_settings_are_kept_under_the_lock_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threads reading domain settings at once all get them; the cache stays whole."""
    setup()
    cache: LockedDict[Any, Any] = LockedDict(registry._lock)
    monkeypatch.setattr(registry, "_settings_cache", cache)

    def read(index: int) -> set[str]:
        return {registry.settings(AlphaSettings).GREETING for _ in range(500)}

    assert on_threads(read) == [{"hi"}] * THREADS
    [kept] = cache.values()
    assert kept.source is unwrap_settings()
    with override_settings(ALPHA={"GREETING": "hey"}):
        assert on_threads(read) == [{"hey"}] * THREADS
    assert on_threads(read) == [{"hi"}] * THREADS


def read_only_bind(reads: Reads = Reads.TRANSACTION) -> AsyncEngine:
    """The engine a new read-only session of the running loop binds to."""
    bind = connections.session(readonly=True, reads=reads).bind
    assert isinstance(bind, AsyncEngine)
    return bind


def test_loops_on_threads_keep_their_own_read_only_binds() -> None:
    """Each loop's read-only sessions share one bind per engine; closing drops it."""
    network = "postgresql+asyncpg://app@db.invalid/app"  # Never connected to.
    kept: list[AsyncEngine] = []  # Alive, so no two are told apart by a reused id.

    async def use() -> bool:
        engine = connections.engine()
        binds = [
            read_only_bind(reads) for reads in (Reads.TRANSACTION, Reads.SNAPSHOT) * 2
        ]
        kept.extend(binds[:2])
        await connections.close_all()
        fresh = read_only_bind()
        kept.append(fresh)
        await connections.close_all()
        return (
            binds[0] is binds[2]
            and binds[1] is binds[3]
            and binds[0] is not binds[1]
            and all(bind.sync_engine.pool is engine.sync_engine.pool for bind in binds)
            and fresh is not binds[0]
        )

    def loops(index: int) -> list[bool]:
        return [asyncio.run(use()) for _ in range(LOOPS // 5)]

    with override_settings(DATABASES={"default": {"URL": network}}):
        assert on_threads(loops) == [[True] * (LOOPS // 5)] * THREADS
    assert len({id(bind) for bind in kept}) == len(kept)


def test_idempotency_statements_are_built_once_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threads asking for the same statements at once get the same ones."""
    built: LockedDict[Any, Any] = LockedDict(idempotency_statements._lock)
    monkeypatch.setattr(idempotency_statements, "_built", built)
    build = idempotency_statements._build
    builds: list[tuple[str, bool]] = []

    def counted(dialect: str, database_clock: bool) -> Any:  # noqa: ANN401
        builds.append((dialect, database_clock))
        return build(dialect, database_clock)

    monkeypatch.setattr(idempotency_statements, "_build", counted)

    def statements(index: int) -> list[Any]:
        return [
            idempotency_statements.clock_statements(dialect)
            for dialect in ("postgresql", "sqlite") * 50
        ]

    results = on_threads(statements)
    assert sorted(builds) == [("postgresql", True), ("sqlite", False)]
    assert all(result == results[0] for result in results)
    assert results[0][0] is built[("postgresql", True)]


def test_idempotency_sweeps_are_due_once_per_interval_from_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claims on all threads at once: one sweeps, and one more after a full sweep."""
    sweeps = Sweeps()

    def monotonic() -> float:
        assert sweeps._lock.locked()  # The schedule is read under its lock.
        return 1000.0  # Frozen: SWEEP_EVERY never passes.

    monkeypatch.setattr(clock_module, "monotonic", monotonic)

    def claims(index: int) -> int:
        return sum(sweeps.due() for _ in range(500))

    first = on_threads(claims)
    sweeps.swept(PURGE_BATCH - 1)  # Short: the backlog is gone.
    short = on_threads(claims)
    sweeps.swept(PURGE_BATCH)  # Full: more may wait.
    full = on_threads(claims)

    assert (sum(first), sum(short), sum(full)) == (1, 0, 1)
