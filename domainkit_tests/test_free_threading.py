"""State shared by event loops on several threads stays consistent.

Free-threaded Python (3.14t), and servers that run a loop per thread, share
one rate limiter, one middleware, one hashing pool, one application's live
connection slots, and the event bus between threads.
Each test drives them from several threads at once and checks what must
always hold, and that the state is only touched with the lock held.
"""

import asyncio
import os
import subprocess
import sys
import sysconfig
import threading
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.datastructures import State
from starlette.requests import HTTPConnection
from starlette.types import Message, Receive, Scope, Send

from domainkit.contrib.auth import AllowAny
from domainkit.contrib.auth import middleware as middleware_module
from domainkit.contrib.auth.hashers import HashingPool
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.ddd import DomainEvent, EventBus
from domainkit.http import live
from domainkit.http.live import Slots
from domainkit.http.live_events import LiveEvents
from domainkit.http.ratelimit import MemoryBackend
from domainkit.http.websockets import WebSocketLimitsMiddleware
from domainkit_tests.conftest import UseAuth

THREADS = 8


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


def test_the_hashing_pool_counts_calls_from_every_thread() -> None:
    """Loops on several threads share one count, changed under its lock."""
    pool = CheckedPool(threads=2, queue=1_000)

    async def calls(index: int) -> list[int]:
        return [await pool.run(lambda n: n * 2, index) for _ in range(20)]

    results = on_threads(lambda index: asyncio.run(calls(index)))

    assert results == [[index * 2] * 20 for index in range(THREADS)]
    assert pool.pending == 0


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
