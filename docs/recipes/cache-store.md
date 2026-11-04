# A cache store of your own

This recipe writes a cache store of your own, and makes the default
cache use it. The example keeps its entries in a SQLite file, so a
service on one machine keeps its cache across restarts.

## The problem

A cache keeps its entries in a store: this process's memory, or Redis
([cache.md](../cache.md)). Memory is lost when the process restarts, and
Redis is one more server to run. You want to keep the entries somewhere
else: here, a SQLite file next to the service.

You do not have to write a whole cache. `Cache` builds the keys, encodes
the values, and copes with an outage. The store only keeps bytes under
keys.

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make a project

```bash
domainkit init pricesAPI prices
cd prices
python manage.py makemigrations -m "auth"
python manage.py migrate
```

### Step 2: write the store

A store subclasses `domainkit.cache.Store`, and answers seven calls:
`get_many`, `set_many`, `add`, `delete_many`, `incr`, `touch`, and
`clear`. A `ttl` is in milliseconds; `None` keeps the entry. The store is made as `Store(config)`, once per event loop,
from its `CACHES` entry, whose `URL` is the store's to read.

SQLite calls block, so they run in a thread. Times come from the
framework's clock, in wall time (the file outlives the process), which
tests can freeze:

```python title="src/config/cache.py (new file)"
"""A cache in a SQLite file: entries outlive the process."""

import asyncio
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from domainkit import clock
from domainkit.cache import CacheError, Store
from domainkit.conf import CacheConfig

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS entries "
    "(key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL)"
)


class SqliteStore(Store):
    """Entries in a table of the file the URL names: ``sqlitecache:///path``."""

    def __init__(self, config: CacheConfig) -> None:
        """Open the file, and make the table if it is new."""
        path = urlsplit(config.URL.get_secret_value()).path
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute(SCHEMA)
        self.lock = threading.Lock()

    async def run(self, work, *args):  # type: ignore[no-untyped-def]
        """``work(*args)`` on a thread, one at a time, in a transaction."""

        def locked():  # type: ignore[no-untyped-def]
            with self.lock:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    result = work(clock.now().timestamp(), *args)
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
                self.db.execute("COMMIT")
                return result

        return await asyncio.to_thread(locked)

    def live(self, now: float, key: str) -> tuple[bytes, float | None] | None:
        """The value and expiry of ``key``, unless missing or expired."""
        row = self.db.execute(
            "SELECT value, expires FROM entries WHERE key = ?", (key,)
        ).fetchone()
        if row is None or (row[1] is not None and row[1] <= now):
            return None
        return row[0], row[1]

    def put(self, key: str, value: bytes, expires: float | None) -> None:
        """Keep ``value`` under ``key`` until ``expires``."""
        self.db.execute(
            "INSERT OR REPLACE INTO entries VALUES (?, ?, ?)", (key, value, expires)
        )

    @staticmethod
    def expiry(now: float, ttl: int | None) -> float | None:
        """When an entry stored ``now`` with ``ttl`` milliseconds expires."""
        return None if ttl is None else now + ttl / 1000

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """Each key's value, or None."""

        def read(now: float) -> list[bytes | None]:
            return [
                (found or (None,))[0] for found in (self.live(now, k) for k in keys)
            ]

        return await self.run(read)  # type: ignore[no-any-return]

    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        """Keep every item."""

        def write(now: float) -> None:
            for key, value in items.items():
                self.put(key, value, self.expiry(now, ttl))

        await self.run(write)

    async def add(self, key: str, value: bytes, ttl: int | None) -> bool:
        """Keep ``value`` unless ``key`` holds a live one."""

        def write(now: float) -> bool:
            if self.live(now, key) is not None:
                return False
            self.put(key, value, self.expiry(now, ttl))
            return True

        return await self.run(write)  # type: ignore[no-any-return]

    async def delete_many(self, keys: Sequence[str]) -> int:
        """Forget the keys; count the live ones."""

        def delete(now: float) -> int:
            live = sum(self.live(now, key) is not None for key in keys)
            self.db.executemany(
                "DELETE FROM entries WHERE key = ?", [(k,) for k in keys]
            )
            return live

        return await self.run(delete)  # type: ignore[no-any-return]

    async def incr(self, key: str, delta: int, ttl: int | None) -> int:
        """Add ``delta``; a missing key starts at 0 and gets ``ttl``."""

        def add(now: float) -> int:
            found = self.live(now, key)
            if found is None:
                value, expires = delta, self.expiry(now, ttl)
            elif found[0].lstrip(b"-").isdigit():
                value, expires = int(found[0]) + delta, found[1]
            else:
                raise CacheError("The value is not an integer.")
            self.put(key, str(value).encode(), expires)
            return value

        return await self.run(add)  # type: ignore[no-any-return]

    async def touch(self, key: str, ttl: int | None) -> bool:
        """Give a live ``key`` a new expiry."""

        def renew(now: float) -> bool:
            found = self.live(now, key)
            if found is not None:
                self.put(key, found[0], self.expiry(now, ttl))
            return found is not None

        return await self.run(renew)  # type: ignore[no-any-return]

    async def clear(self, prefix: str) -> int:
        """Forget the keys that start with ``prefix``."""

        def delete(now: float) -> int:
            cursor = self.db.execute(
                "DELETE FROM entries WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
            )
            return cursor.rowcount

        return await self.run(delete)  # type: ignore[no-any-return]

    async def close(self) -> None:
        """Close the file, once no call is running on it (under the lock)."""

        def locked() -> None:
            with self.lock:
                self.db.close()

        await asyncio.to_thread(locked)
```

### Step 3: name it as the default cache

`BACKEND` makes the store, and the `URL` says where its file is, with a
scheme of its own:

```python title="src/config/settings/base.py"
CACHES = {
    "default": {
        "URL": env.str("CACHE_URL", "sqlitecache:///cache.sqlite3"),
        "BACKEND": "config.cache.SqliteStore",
    }
}
```

Everything that caches (`caches["default"]`, `@cached`, `get_or_set`)
now keeps its entries in `cache.sqlite3`.

### Step 4: test the store

The test points the cache at a file of its own. It checks what `Cache`
asks of a store: values, expiry, counters, and keys added only once:

```python title="tests/test_cache_store.py (new file)"
"""The cache keeps values in the file, until they expire."""

from pathlib import Path

import pytest
from domainkit.cache import caches
from domainkit.conf import override_settings
from domainkit.testing import freeze_time

pytestmark = pytest.mark.anyio


def in_file(path: Path) -> dict[str, dict[str, str]]:
    """``CACHES`` with the default cache in ``path``."""
    return {
        "default": {
            "URL": f"sqlitecache://{path}",
            "BACKEND": "config.cache.SqliteStore",
        }
    }


async def test_values_live_in_the_file_until_they_expire(
    db: None, tmp_path: Path
) -> None:
    """Set, read back, count, add once; an entry past its time is gone."""
    with override_settings(CACHES=in_file(tmp_path / "cache.sqlite3")):
        cache = caches["default"]
        with freeze_time("2026-01-01T09:00:00Z") as clock:
            await cache.set("price:42", {"amount": "9.50"}, ttl=60)
            assert await cache.get("price:42") == {"amount": "9.50"}
            assert await cache.incr("hits") == 1
            assert await cache.incr("hits", 2) == 3
            assert await cache.add("lock", "a") is True
            assert await cache.add("lock", "b") is False
            clock.tick(61)
            assert await cache.get("price:42") is None
            assert await cache.delete("lock") is True
        assert (tmp_path / "cache.sqlite3").exists()
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

## How it works

| Call | `Cache` uses it for | The store must |
| --- | --- | --- |
| `get_many(keys)` | `get`, `get_many`, `get_or_set` | Return each key's value, or `None` when it is missing or expired. |
| `set_many(items, ttl)` | `set`, `set_many` | Keep every item until `ttl` milliseconds pass. |
| `add(key, value, ttl)` | `add` | Keep the value only if the key holds no live one; say whether it did. |
| `delete_many(keys)` | `delete`, `delete_many` | Forget the keys; count the live ones. |
| `incr(key, delta, ttl)` | `incr` | Add to an integer; a missing key starts at 0 and gets `ttl`. Raise `CacheError` for a value that is not an integer. |
| `touch(key, ttl)` | `touch` | Give a live key a new expiry; say whether it was live. |
| `clear(prefix)` | `clear`, and `manage.py cache clear` | Forget the keys that start with `prefix`; count them. |

Two calls are optional: `check()`, which raises when the store cannot be
reached (`/ready` and `manage.py cache check` call it), and `close()`,
which releases connections.

A store that cannot be reached raises `domainkit.cache.CacheUnavailable`,
and one that refuses an operation raises `CacheError`. When the store
cannot be reached, `get_or_set` and `@cached` compute the value instead
of failing, and the cache is skipped for `FAILURE_COOLDOWN` seconds. A cache is an
optimisation: an outage slows the service, and never stops it.

## See also

- [Backends](../reference/backends.md#caches): every `CACHES` key.
- [Caches](../cache.md): `get_or_set`, `@cached`, and versions.
- [Extension points](../reference/extension-points.md#backends-from-an-installed-package):
  offer the store by name from a package.
