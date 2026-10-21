"""State shared by event loops on several threads stays consistent.

Free-threaded Python (3.14t), and servers that run a loop per thread, share
one rate limiter, one middleware, and one hashing pool between threads, and
the registries that keep each loop's tasks, publishers, and brokers. Each
test drives them from several threads at once and checks what must always
hold, and that the state is only touched with the lock held.
"""

import asyncio
import os
import subprocess
import sys
import sysconfig
import threading
from collections.abc import Callable, Iterator
from typing import Any
from weakref import WeakKeyDictionary

import pytest

from domainkit import tasks
from domainkit.broker import Broker, BrokerHandler, MemoryBroker
from domainkit.conf import override_settings
from domainkit.contrib.auth import middleware as middleware_module
from domainkit.contrib.auth.hashers import HashingPool
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.contrib.messaging import MessagingSettings, dispatch
from domainkit.http.ratelimit import MemoryBackend
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
