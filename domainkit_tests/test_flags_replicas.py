"""A flag change reaches processes sharing the flags' cache without a stampede.

PERF-17: a change deleted the one cache entry every process reads, so in
every process sharing it (Redis) the next read missed, and each loaded the
flags at once: 20 replicas, 20 loads. The process that made the change
now loads them and replaces the entry, and the others are served the old
one until it lands: one load. A change that raced another's reload is
counted (``VERSION_KEY``) and loads again, so neither is lost.

Each replica here is a ``Cache`` of its own, as in a process of its own,
over one store, as over one Redis.
"""

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import insert, update

from domainkit.cache import Cache, CacheUnavailable, MemoryStore
from domainkit.conf import CacheConfig
from domainkit.contrib.flags import store as store_module
from domainkit.contrib.flags.store import CACHE_KEY, current, invalidate
from domainkit.contrib.flags.tables import flags
from domainkit.db import connections
from domainkit_tests.test_flags import (  # noqa: F401 - fixtures.
    declared,
    project,
)

pytestmark = pytest.mark.usefixtures("project")
REPLICAS = 20
READERS = 5
"""Requests each replica serves while the change is loaded."""
replica: contextvars.ContextVar[Cache] = contextvars.ContextVar("replica")


class Replicas:
    """``caches`` for the flags' store: the running replica's cache."""

    def __init__(self, store: MemoryStore) -> None:
        """One cache per replica, all over ``store``."""
        self.caches = [Cache("default", CacheConfig(), store) for _ in range(REPLICAS)]

    def __getitem__(self, alias: str) -> Cache:
        """The cache of the replica the caller runs in."""
        return replica.get()

    def run[T](self, index: int, work: Awaitable[T]) -> "asyncio.Task[T]":
        """``work`` as a task of replica ``index``."""
        context = contextvars.copy_context()
        context.run(replica.set, self.caches[index])
        return asyncio.get_running_loop().create_task(work, context=context)  # type: ignore[arg-type]


class Loads:
    """Count the flags' loads; while ``held``, each waits, once read, to be let go."""

    def __init__(self, load: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        """Wrap the store's ``load``."""
        self.load = load
        self.count = 0
        self.held = False
        self.read = [asyncio.Event() for _ in range(REPLICAS * READERS)]
        """Set once load ``n`` has read the database."""
        self.gates = [asyncio.Event() for _ in range(REPLICAS * READERS)]

    async def __call__(self) -> dict[str, Any]:
        """Read the database, then wait until this load is let go."""
        index = self.count
        self.count += 1
        loaded = await self.load()
        self.read[index].set()
        if self.held:
            await self.gates[index].wait()
        return loaded

    def release(self, index: int | None = None) -> None:
        """Let load ``index`` go, or every one."""
        for gate in self.gates if index is None else [self.gates[index]]:
            gate.set()


@pytest.fixture
def replicas(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Replicas, Loads]]:
    """Replicas over one store, and the flags' loads counted and held."""
    shared = Replicas(MemoryStore(max_entries=100))
    loads = Loads(store_module._load)
    monkeypatch.setattr(store_module, "caches", shared)
    monkeypatch.setattr(store_module, "_load", loads)
    yield shared, loads
    asyncio.run(connections.close_all())


async def settle() -> None:
    """Let every task run until it waits on something."""
    for _ in range(100):
        await asyncio.sleep(0)


async def everyone(name: str) -> dict[str, Any]:
    """Flag ``name``'s rules, as the running replica reads them."""
    return (await current()).flags[name]


async def set_everyone(name: str, on: bool) -> None:
    """Make or change flag ``name`` in the database, as a unit of work commits it."""
    async with connections.engine().begin() as connection:
        changed = await connection.execute(
            update(flags).where(flags.c.id == name).values(everyone=on)
        )
        if not changed.rowcount:
            row = {"id": name, "everyone": on, "percentage": 0, "users": []}
            await connection.execute(
                insert(flags).values(**row, groups=[], note="", version=1)
            )


def test_a_change_is_loaded_once_for_every_replica(
    replicas: tuple[Replicas, Loads],
) -> None:
    """20 replicas reading as the change lands: one load, where there were 20."""
    shared, loads = replicas

    async def scenario() -> tuple[int, list[Any], list[Any]]:
        await set_everyone("beta", on=False)
        first = await shared.run(0, everyone("beta"))
        assert first["everyone"] is False  # Cached for every replica.
        loads.count, loads.held = 0, True
        await set_everyone("beta", on=True)
        writer = shared.run(0, invalidate())
        await settle()
        reads = [
            shared.run(index, everyone("beta"))
            for index in range(REPLICAS)
            for _ in range(READERS)
        ]
        await settle()
        during = loads.count
        await asyncio.gather(*(read.wait() for read in loads.read[:during]))
        loads.release()
        await writer
        served = [read["everyone"] for read in await asyncio.gather(*reads)]
        after = [
            (await shared.run(index, everyone("beta")))["everyone"]
            for index in range(REPLICAS)
        ]
        return during, served, after

    during, served, after = asyncio.run(scenario())

    assert during == 1  # The writer's: no replica missed.
    assert set(served) == {False}  # The old rules, until the new ones landed.
    assert set(after) == {True}


def test_racing_changes_are_both_kept(replicas: tuple[Replicas, Loads]) -> None:
    """A reload that read before another change, and stored after it, loads again."""
    shared, loads = replicas

    async def scenario() -> tuple[bool, bool, int]:
        await set_everyone("b", on=False)
        loads.held = True
        await set_everyone("a", on=True)
        first = shared.run(0, invalidate())
        await loads.read[0].wait()  # It read a on, b off; held.
        await set_everyone("b", on=True)
        second = shared.run(1, invalidate())
        await loads.read[1].wait()  # It read both on.
        loads.release(1)
        await second
        loads.held = False
        loads.release(0)  # Stores a on, b off over it, then sees the count.
        await first
        rules = (await shared.run(2, current())).flags
        return rules["a"]["everyone"], rules["b"]["everyone"], loads.count

    a, b, count = asyncio.run(scenario())

    assert (a, b) == (True, True)
    assert count == 3  # The first change loaded again.


def test_changes_that_keep_racing_leave_the_next_read_to_load(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``REFRESHES`` loads that each saw another change, the entry goes."""
    shared, loads = replicas
    loads.release()
    count = iter(range(100))

    async def racing(key: str) -> Any:  # noqa: ANN401 - as Cache.get.
        return next(count)  # Never the change's own count.

    async def scenario() -> tuple[bool, int]:
        cache = shared.caches[0]
        monkeypatch.setattr(cache, "get", racing)
        await shared.run(0, invalidate())
        kept = await cache.store.get_many([cache.make_key(CACHE_KEY)])
        return kept != [None], loads.count

    kept, count_of_loads = asyncio.run(scenario())

    assert not kept
    assert count_of_loads == store_module.REFRESHES


def test_a_load_that_fails_drops_the_entry_and_raises(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next read loads the flags, as when a change deleted the entry."""
    shared, loads = replicas
    loads.release()

    async def broken() -> dict[str, Any]:
        raise RuntimeError("the database went away")

    async def scenario() -> list[bytes | None]:
        cache = shared.caches[0]
        await cache.set(CACHE_KEY, {"flags": {}, "settings": {}})
        monkeypatch.setattr(store_module, "_load", broken)
        with pytest.raises(RuntimeError, match="went away"):
            await shared.run(0, invalidate())
        return await cache.store.get_many([cache.make_key(CACHE_KEY)])

    assert asyncio.run(scenario()) == [None]


def test_a_change_with_the_cache_down_is_refused_as_before(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting it fails first: ``CacheUnavailable``, as deleting raised."""
    shared, _ = replicas

    async def down(*args: object) -> int:
        raise CacheUnavailable("down")

    monkeypatch.setattr(shared.caches[0].store, "incr", down)

    async def scenario() -> None:
        await shared.run(0, invalidate())

    with pytest.raises(CacheUnavailable):
        asyncio.run(scenario())
