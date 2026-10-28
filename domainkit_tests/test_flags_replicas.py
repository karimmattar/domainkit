"""A flag change reaches processes sharing the flags' cache without a stampede.

PERF-17: a change deleted the one cache entry every process reads, so in
every process sharing it (Redis) the next read missed, and each loaded the
flags at once: 20 replicas, 20 loads. The process that made the change
now loads them and replaces the entry, and the others are served the old
one until it lands: one load. The entry is kept under the count of changes
(``VERSION_KEY``), so a load that read before a change, or another's
reload, never replaces what readers read after it (SEC-12b-1).

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
from domainkit.contrib.flags.store import (
    VERSION_KEY,
    current,
    entry_key,
    invalidate,
)
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
        self.caches = [
            Cache("default", CacheConfig(TTL=300), store) for _ in range(REPLICAS)
        ]

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
    cache = replicas[0].caches[0]
    counter = cache.store._entries[cache.make_key(VERSION_KEY)]  # type: ignore[attr-defined]
    assert counter[1] is None  # The change counter never expires.
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


def test_a_change_counted_after_another_loads_the_count_readers_use(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another change was counted meanwhile: its count's entry is loaded again.

    The entry is kept under the count of changes (SEC-12b-1); this change
    loaded the next count's entry, but the count it got is one more.
    """
    shared, loads = replicas
    loads.release()

    async def scenario() -> tuple[list[bool], int, int]:
        cache = shared.caches[0]
        incr = cache.incr

        async def counted_twice(key: str, delta: int = 1, ttl: Any = None) -> int:  # noqa: ANN401 - as Cache.incr.
            await incr(key, delta, ttl)  # Another change, counted first.
            return await incr(key, delta, ttl)

        monkeypatch.setattr(cache, "incr", counted_twice)
        await shared.run(0, invalidate())
        stored = await cache.store.get_many(
            [cache.make_key(entry_key(count)) for count in (1, 2)]
        )
        return (
            [entry is not None for entry in stored],
            loads.count,
            await cache.get(VERSION_KEY),
        )

    stored, count_of_loads, count = asyncio.run(scenario())

    assert (stored, count_of_loads, count) == ([True, True], 2, 2)


def test_a_load_that_fails_still_counts_the_change_and_raises(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readers move to the new count, miss, and load the flags themselves."""
    shared, loads = replicas
    loads.release()

    async def broken() -> dict[str, Any]:
        raise RuntimeError("the database went away")

    async def scenario() -> tuple[Any, bool]:
        await set_everyone("kill", on=True)
        cache = shared.caches[0]
        await shared.run(0, current())  # Cached under count 0: on.
        await set_everyone("kill", on=False)
        load = store_module._load
        monkeypatch.setattr(store_module, "_load", broken)
        with pytest.raises(RuntimeError, match="went away"):
            await shared.run(0, invalidate())
        monkeypatch.setattr(store_module, "_load", load)
        rules = (await shared.run(1, current())).flags
        return await cache.get(VERSION_KEY), rules["kill"]["everyone"]

    assert asyncio.run(scenario()) == (1, False)


def test_a_change_with_the_cache_down_is_refused_as_before(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting it fails: ``CacheUnavailable`` is raised, as deleting raised."""
    shared, _ = replicas

    async def down(*args: object) -> int:
        raise CacheUnavailable("down")

    monkeypatch.setattr(shared.caches[0].store, "incr", down)

    async def scenario() -> None:
        await shared.run(0, invalidate())

    with pytest.raises(CacheUnavailable):
        asyncio.run(scenario())


def test_flags_are_read_while_the_cache_cannot_count(
    replicas: tuple[Replicas, Loads], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count reads as 0, and the flags come from the database, as on a miss."""
    shared, loads = replicas
    store = shared.caches[0].store

    async def down(keys: Any) -> list[bytes | None]:  # noqa: ANN401 - as Store.
        raise CacheUnavailable("down")

    async def scenario() -> object:
        await set_everyone("kill", on=True)
        monkeypatch.setattr(store, "get_many", down)
        return (await shared.run(0, current())).flags["kill"]["everyone"]

    assert asyncio.run(scenario()) is True
    assert loads.count == 1
