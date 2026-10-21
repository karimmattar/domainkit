"""State shared by event loops on several threads stays consistent.

Free-threaded Python (3.14t), and servers that run a loop per thread, share
one rate limiter, one middleware, one hashing pool, an HTTP client's
circuit breaker, and the guard's lookup threads between threads. Each test
drives them from several threads at once and checks what must always hold,
and that the state is only touched with the lock held.
"""

import asyncio
import os
import random
import socket
import subprocess
import sys
import sysconfig
import threading
from collections.abc import Callable

import pytest

from domainkit import clock as clock_module
from domainkit.contrib.auth import middleware as middleware_module
from domainkit.contrib.auth.hashers import HashingPool
from domainkit.contrib.auth.middleware import AuthenticationMiddleware
from domainkit.http.client import BREAKER_HOSTS, CircuitBreaker
from domainkit.http.guard import GuardResolver
from domainkit.http.ratelimit import MemoryBackend
from domainkit.http.shedding import QUEUE_FULL, ShedReport
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
