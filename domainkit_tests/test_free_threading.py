"""State shared by event loops on several threads stays consistent.

Free-threaded Python (3.14t), and servers that run a loop per thread, share
one rate limiter, one middleware, one hashing pool, one application's live
connection slots, the event bus, an HTTP client's circuit breaker, the
guard's lookup threads, the remembered client addresses, the handles of
the request metrics, and the principal cache between threads, and the
registries that keep each loop's tasks, publishers, and brokers. Each test
drives them from several threads at once and checks what must always hold,
and that the state is only touched with the lock held.
"""

import asyncio
import ipaddress
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
from types import SimpleNamespace
from typing import Any
from weakref import WeakKeyDictionary

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from domainkit import clock as clock_module
from domainkit import tasks
from domainkit.broker import Broker, BrokerHandler, MemoryBroker
from domainkit.conf import override_settings
from domainkit.contrib.audit import personal
from domainkit.contrib.auth import AllowAny
from domainkit.contrib.auth import middleware as middleware_module
from domainkit.contrib.auth.hashers import HashingPool
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.contrib.auth.principals import ALL, Entry, PrincipalCache
from domainkit.contrib.messaging import MessagingSettings, dispatch
from domainkit.db import Reads, connections
from domainkit.ddd import DomainEvent, EventBus
from domainkit.http import live, metrics, ratelimit
from domainkit.http.client import BREAKER_HOSTS, CircuitBreaker
from domainkit.http.guard import GuardResolver
from domainkit.http.live import Slots
from domainkit.http.live_events import LiveEvents
from domainkit.http.metrics import REQUESTS, MetricsMiddleware
from domainkit.http.middleware import ProxyHeadersMiddleware, _address
from domainkit.http.ratelimit import MemoryBackend, client_group
from domainkit.http.shedding import QUEUE_FULL, ShedReport
from domainkit.http.websockets import WebSocketLimitsMiddleware
from domainkit.log import JsonFormatter
from domainkit_tests.conftest import UseAuth

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


def test_subject_keys_kept_by_transactions_on_every_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactions on many threads keep their own keys; forgetting reaches all."""
    held = CheckedRegistry[Any, dict[str, bytes]](personal._held_lock)
    monkeypatch.setattr(personal, "_held", held)

    def work(index: int) -> list[bool]:
        results = []
        for attempt in range(LOOPS):
            session = AsyncSession()
            session.sync_session.begin()
            mine = {f"s{index}": bytes([index]), "shared": bytes([attempt])}
            personal._keep(session, mine)
            personal._drop({f"s{(index + 1) % THREADS}"}.__contains__)
            kept = personal._kept(session, ["shared", f"s{index}"])
            results.append(kept.get("shared") == bytes([attempt]))
            personal._drop({"shared"}.__contains__)
            results.append("shared" not in personal._kept(session, ["shared"]))
            session.sync_session.close()
        return results

    assert on_threads(work) == [[True] * 2 * LOOPS] * THREADS


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
