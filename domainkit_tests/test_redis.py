"""Redis and Valkey: the store, TLS, a least-privilege user, rate limits.

Every test runs on fakeredis, an in-memory Redis with Lua, so they all run
without a server. Set ``DOMAINKIT_TEST_REDIS_URL`` (``redis://host:6379/0``),
``DOMAINKIT_TEST_VALKEY_URL``, and ``DOMAINKIT_TEST_REDIS_TLS_URL`` with
``DOMAINKIT_TEST_REDIS_CA`` (the CA of a ``rediss://`` server) to run them on
real servers too, as CI does; the TLS and ACL tests need a real server. Each
test uses its own key prefix, so tests can share servers.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, Final

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient
from pydantic import BaseModel

from domainkit.cache import Cache, CacheError, CacheUnavailable, cached, caches
from domainkit.cache import redis as redis_cache
from domainkit.cache.core import split
from domainkit.cache.redis import RedisStore
from domainkit.conf import CacheConfig, override_settings
from domainkit.contrib.auth.principals import (
    ALL,
    STAMP_TTL,
    read_stamps,
    stamp_key,
    write_stamps,
)
from domainkit.http import get_asgi_application
from domainkit.http.ratelimit import RedisBackend

FAKE: Final = "redis://fakeredis:6379/0"
SERVERS: Final = {
    "fake": FAKE,
    "redis": os.environ.get("DOMAINKIT_TEST_REDIS_URL", ""),
    "valkey": os.environ.get("DOMAINKIT_TEST_VALKEY_URL", ""),
    "redis-tls": os.environ.get("DOMAINKIT_TEST_REDIS_TLS_URL", ""),
}
CA: Final = os.environ.get("DOMAINKIT_TEST_REDIS_CA")
ACL_COMMANDS: Final = (
    "get",
    "mget",
    "set",
    "del",
    "unlink",
    "exists",
    "pexpire",
    "persist",
    "incrby",
    "time",
    "scan",
    "evalsha",
    "script|load",
)
"""What a cache user needs besides ``+@connection`` (docs/cache.md shows it)."""
MIDDLEWARE: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
]

pytestmark = pytest.mark.anyio


class Invoice(BaseModel):
    """A cached read model."""

    id: uuid.UUID
    total: int


class FakeRedis(FakeAsyncRedis):
    """fakeredis, keeping SCAN's promise while keys are deleted mid-scan.

    Redis returns every key that exists for the whole scan, whatever else is
    deleted meanwhile (``clear`` relies on it). fakeredis's cursor is a
    position in its key list, so deletions would make it skip keys.
    """

    async def scan_iter(  # type: ignore[override]  # Typed as the sync client's.
        self,
        match: str | None = None,
        count: int | None = None,
        _type: str | None = None,
        **kwargs: Any,  # noqa: ANN401 - redis-py's options.
    ) -> AsyncIterator[Any]:
        """Finish the scan, then hand out its keys."""
        scan = super().scan_iter(match, count, _type, **kwargs)
        keys = [key async for key in scan]
        for key in keys:
            yield key


def options_for(name: str) -> dict[str, object]:
    """Cache settings for server ``name``, with a key prefix of the test's own."""
    url = SERVERS[name]
    if not url:
        pytest.skip(f"set DOMAINKIT_TEST_{name.upper().replace('-', '_')}_URL")
    options: dict[str, object] = {"URL": url, "KEY_PREFIX": f"t{uuid.uuid4().hex}"}
    if url.startswith("rediss://"):
        options["TLS_CA"] = CA
    return options


@pytest.fixture(params=list(SERVERS))
def server(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """Settings for each server: fakeredis always, real ones when configured."""
    if request.param == "fake":
        fake = FakeServer()
        monkeypatch.setattr(
            redis_cache, "create_client", lambda config: FakeRedis(server=fake)
        )
    return options_for(request.param)


@pytest.fixture(params=[name for name in SERVERS if name != "fake"])
def real_server(request: pytest.FixtureRequest) -> dict[str, object]:
    """Settings for each real server (TLS and users need one)."""
    return options_for(request.param)


@pytest.fixture
async def cache(server: dict[str, object]) -> AsyncIterator[Cache]:
    """A cache on the test server; its keys are deleted afterwards."""
    config = CacheConfig.model_validate(server)
    opened = Cache("default", config, RedisStore.from_config(config))
    yield opened
    await opened.clear()
    await opened.close()


@pytest.fixture
def shared(server: dict[str, object]) -> Iterator[dict[str, object]]:
    """Make the test server the ``shared`` cache in settings."""
    with override_settings(
        CACHES={"default": {}, "shared": server}, RATE_LIMIT_CACHE="shared"
    ):
        yield server


async def test_values_counters_and_expiry(cache: Cache) -> None:
    """Every operation works on the server, with millisecond expiry."""
    invoice = Invoice(id=uuid.uuid4(), total=9)
    await cache.set("invoice", invoice, ttl=60)
    await cache.set_many({"a": 1, "b": [2]})
    client = cache.store.client  # type: ignore[attr-defined]

    assert await cache.get("invoice", Invoice) == invoice
    assert await cache.get_many(["a", "b", "c"]) == {"a": 1, "b": [2]}
    assert 59_000 < await client.pttl(cache.make_key("invoice")) <= 60_000
    assert 0 < await client.pttl(cache.make_key("b")) <= 300_000  # TTL, pipelined.
    assert (await cache.add("a", 5), await cache.add("new", 5)) == (False, True)
    assert 0 < await client.pttl(cache.make_key("new")) <= 300_000
    assert await cache.delete_many(["a", "new", "missing"]) == 2
    assert await cache.delete_many([]) == 0
    assert await cache.store.delete_many([]) == 0  # DEL needs a key; not sent.
    assert [await cache.incr("n", 2, ttl=30), await cache.incr("n")] == [2, 3]
    assert 0 < await client.pttl(cache.make_key("n")) <= 30_000
    assert await cache.incr("forever", ttl=None) == 1
    assert await client.pttl(cache.make_key("forever")) == -1
    with pytest.raises(CacheError, match="not an integer"):
        await cache.incr("b")

    assert await cache.touch("b", 0.05)
    assert await cache.touch("invoice", None)
    assert not await cache.touch("missing")
    assert await client.pttl(cache.make_key("invoice")) == -1
    await asyncio.sleep(0.15)
    assert await cache.get("b") is None


async def test_get_or_set_stores_expiry_and_cost(cache: Cache) -> None:
    """Entries written by get_or_set carry what XFetch needs."""

    async def compute() -> Invoice:
        return Invoice(id=uuid.UUID(int=4), total=1)

    first = await cache.get_or_set("invoice", compute, Invoice, ttl=60)
    (raw,) = await cache.store.get_many([cache.make_key("invoice")])
    payload, expires, delta = split(raw or b"")

    assert first == await cache.get("invoice", Invoice)
    assert Invoice.model_validate_json(payload) == first
    assert expires is not None
    assert delta >= 0


async def test_clear_stays_in_its_namespace(server: dict[str, object]) -> None:
    """Clearing deletes this cache's keys in batches, and nobody else's."""
    mine = CacheConfig.model_validate(server)
    theirs = mine.model_copy(update={"KEY_PREFIX": f"{mine.KEY_PREFIX}x"})
    cache = Cache("mine", mine, RedisStore.from_config(mine))
    other = Cache("theirs", theirs, RedisStore.from_config(theirs))
    try:
        for start in range(0, 1200, 400):
            await cache.set_many({f"k{n}": n for n in range(start, start + 400)})
        await other.set("k0", "kept")

        assert await cache.clear() == 1200
        assert await other.get("k0") == "kept"
    finally:
        await other.clear()
        await cache.close()
        await other.close()


async def test_clear_matches_its_prefix_literally(cache: Cache) -> None:
    """Glob characters in a prefix match only themselves."""
    store = cache.store
    prefix = cache.namespace
    await store.set_many({f"{prefix}a*b:1": b"1", f"{prefix}axb:1": b"1"}, 60_000)

    assert await store.clear(f"{prefix}a*b:") == 1
    assert await store.get_many([f"{prefix}axb:1"]) == [b"1"]


async def test_refused_commands_are_cache_errors(cache: Cache) -> None:
    """A key of the wrong type is an error, not an outage."""
    await cache.store.client.lpush(cache.make_key("list"), "x")  # type: ignore[attr-defined]

    assert await cache.get("list") is None  # MGET reads other types as missing.
    with pytest.raises(CacheError, match="WRONGTYPE") as raised:
        await cache.incr("list")
    assert not isinstance(raised.value, CacheUnavailable)


@pytest.mark.skipif(not SERVERS["redis-tls"], reason="set DOMAINKIT_TEST_REDIS_TLS_URL")
async def test_tls_needs_a_trusted_certificate() -> None:
    """Without the server's CA, the system's CAs refuse its certificate."""
    config = CacheConfig(URL=SERVERS["redis-tls"], KEY_PREFIX="tls")
    cache = Cache("default", config, RedisStore.from_config(config))
    try:
        with pytest.raises(CacheUnavailable, match="certificate verify failed"):
            await cache.check()
    finally:
        await cache.close()


async def test_a_least_privilege_user_is_enough(
    real_server: dict[str, object],
) -> None:
    """The ACL in docs/cache.md covers every operation, the rate limiter too."""
    server = real_server
    admin = CacheConfig.model_validate(server)
    admin_client = RedisStore.from_config(admin).client
    user, password = f"u{uuid.uuid4().hex[:12]}", uuid.uuid4().hex
    prefix = admin.KEY_PREFIX
    await admin_client.acl_setuser(
        user,
        enabled=True,
        passwords=[f"+{password}"],
        reset_keys=True,
        keys=[f"{prefix}:*"],
        reset_channels=True,
        categories=["-@all", "+@connection"],
        commands=[f"+{command}" for command in ACL_COMMANDS],
    )
    url = str(server["URL"]).replace("://", f"://{user}:{password}@", 1)
    config = CacheConfig.model_validate({**server, "URL": url})
    cache = Cache("default", config, RedisStore.from_config(config))
    try:
        await cache.check()
        await cache.set_many({"a": 1, "b": 2}, ttl=60)
        assert await cache.add("c", 3)
        assert await cache.get_many(["a", "b", "c"]) == {"a": 1, "b": 2, "c": 3}
        assert await cache.incr("n", ttl=60) == 1
        assert await cache.touch("a", None)
        assert await cache.touch("a", 60)
        assert await cache.delete("b")
        store = cache.store
        assert isinstance(store, RedisStore)
        assert (await store.gcra(f"{prefix}:ratelimit:x", 1e6, 0.0))[0]
        assert await cache.clear() == 4
        outside = config.model_copy(update={"KEY_PREFIX": "elsewhere"})
        with pytest.raises(CacheError, match="No permissions"):
            await Cache("outside", outside, store).set("k", 1)
    finally:
        await cache.close()
        await admin_client.acl_deluser(user)
        await admin_client.aclose()


async def test_cached_functions_use_the_shared_cache(
    shared: dict[str, object],
) -> None:
    """@cached works the same on Redis."""
    calls = []

    @cached(cache="shared", ttl=60)
    async def summary(invoice_id: uuid.UUID) -> Invoice:
        calls.append(invoice_id)
        return Invoice(id=invoice_id, total=2)

    try:
        invoice_id = uuid.UUID(int=5)
        assert await summary(invoice_id) == await summary(invoice_id)
        assert len(calls) == 1
        assert await summary.invalidate(invoice_id)
    finally:
        await caches["shared"].clear()
        await caches.close_all()


async def test_rate_limits_are_shared_between_replicas(
    shared: dict[str, object],
) -> None:
    """Two backends (two replicas) draw on one count, on the server's clock."""
    first, second = RedisBackend(), RedisBackend()
    try:
        allowed = await first.hit("ip:203.0.113.7", 2, 1)
        also = await second.hit("ip:203.0.113.7", 2, 1)
        refused = await first.hit("ip:203.0.113.7", 2, 1)
        other = await second.hit("ip:203.0.113.8", 2, 1)
        key = f"{shared['KEY_PREFIX']}:ratelimit:ip:203.0.113.7"
        ttl = await caches["shared"].store.client.pttl(key)  # type: ignore[attr-defined]
    finally:
        await caches["shared"].clear()
        await caches.close_all()

    assert (allowed.allowed, allowed.remaining, also.allowed, also.remaining) == (
        True,
        1,
        True,
        0,
    )
    assert 0.4 < allowed.reset_after <= 0.5
    assert not refused.allowed
    assert 0 < refused.retry_after <= 0.5
    assert 0.5 < refused.reset_after <= 1.0
    assert (other.allowed, other.remaining) == (True, 1)
    assert 0 < ttl <= 1000  # Kept only until the quota is full again.


async def test_principal_stamps_are_shared_between_replicas(
    shared: dict[str, object],
) -> None:
    """A stamp one replica writes, another reads; each is new, and expires."""
    targets = ["user:3f2b", "token:9c1d"]
    other_config = CacheConfig.model_validate(shared)
    other = Cache("shared", other_config, RedisStore.from_config(other_config))
    keys = [stamp_key(ALL), *map(stamp_key, targets)]
    try:
        before = await read_stamps("shared", targets)
        await write_stamps("shared", targets)
        first = await other.get_many(keys, str)  # Another replica's client.
        await write_stamps("shared", targets[:1])
        second = await read_stamps("shared", targets)
        key = caches["shared"].make_key(stamp_key(targets[0]))
        ttl = await caches["shared"].store.client.pttl(key)  # type: ignore[attr-defined]
    finally:
        await caches["shared"].clear()
        await caches.close_all()
        await other.close()

    assert before == (None, None, None)
    assert set(first) == set(keys[1:])
    assert second[0] is None
    assert second[1] != first[keys[1]]
    assert second[2] == first[keys[2]]
    assert all(len(stamp or "") == 16 for stamp in second[1:])
    stamp_ms = STAMP_TTL.total_seconds() * 1000
    assert stamp_ms - 60_000 < ttl <= stamp_ms


async def test_refused_rate_limits_let_requests_through(
    server: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read-only replica or a full Redis lets requests through, then cools."""
    scripts: list[str] = []

    async def refuse(self: RedisStore, key: str, *args: float) -> tuple[int, float]:
        scripts.append(key)
        raise CacheError("READONLY You can't write against a read only replica.")

    monkeypatch.setattr(RedisStore, "gcra", refuse)
    cooling = {**server, "FAILURE_COOLDOWN": 0.2}
    with (
        override_settings(
            CACHES={"default": {}, "shared": cooling}, RATE_LIMIT_CACHE="shared"
        ),
        caplog.at_level(logging.WARNING, logger="domainkit.http.ratelimit"),
    ):
        backend = RedisBackend()
        try:
            decisions = [await backend.hit("ip:203.0.113.7", 1, 60) for _ in range(2)]
            await asyncio.sleep(0.25)
            decisions.append(await backend.hit("ip:203.0.113.7", 1, 60))
        finally:
            await caches.close_all()

    assert [
        (d.allowed, d.remaining, d.reset_after, d.retry_after) for d in decisions
    ] == [(True, 0, 0.0, 0.0)] * 3
    assert len(scripts) == 2  # The second hit came while Redis was skipped.
    warning = (
        "Redis refused the rate limit; letting requests through for 0.2s: "
        "READONLY You can't write against a read only replica."
    )
    assert [record.getMessage() for record in caplog.records] == [warning] * 2


def test_rate_limit_middleware_on_redis(shared: dict[str, object]) -> None:
    """The middleware refuses the third request across the shared count."""
    with override_settings(
        RATE_LIMIT="2/minute",
        RATE_LIMIT_BACKEND="domainkit.http.ratelimit.RedisBackend",
        MIDDLEWARE=MIDDLEWARE,
    ):
        app = get_asgi_application()

        async def clear() -> int:
            return await caches["shared"].clear()

        with TestClient(app, client=("203.0.113.20", 1)) as client:
            codes = [client.get("/ok").status_code for _ in range(3)]
            refused = client.get("/ok")
            assert client.portal is not None
            cleared = client.portal.call(clear)

    assert codes == [200, 200, 429]
    assert refused.headers["RateLimit-Remaining"] == "0"
    assert int(refused.headers["Retry-After"]) > 0
    assert cleared == 1


def test_readiness_with_a_live_cache(server: dict[str, object]) -> None:
    """A reachable cache marked READINESS keeps /ready green."""
    with (
        override_settings(
            CACHES={"default": {}, "shared": {**server, "READINESS": True}}
        ),
        TestClient(get_asgi_application()) as client,
    ):
        response = client.get("/ready")

    assert response.status_code == 200
