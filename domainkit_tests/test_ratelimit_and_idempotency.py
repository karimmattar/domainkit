"""Rate limiting and Idempotency-Key replay."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from starlette.types import Message

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import Settings, override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.ratelimit import Decision, MemoryBackend, _caller
from domainkit.utils.rates import parse_rate
from domainkit_tests import sample_routes

BASE_MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]


def test_rates_parse_and_validate() -> None:
    """Rates read as count/period; bad ones fail at startup."""
    assert parse_rate("100/minute") == (100, 60)
    assert parse_rate(" 5 / second ") == (5, 1)
    for bad in ("0/minute", "ten/minute", "5/week"):
        with pytest.raises(ValueError, match="must look like"):
            parse_rate(bad)
    with pytest.raises(ValueError, match="must look like"):
        Settings.model_validate({"RATE_LIMIT": "lots"})


def test_gcra_allows_bursts_then_refills() -> None:
    """``limit`` requests pass at once; then one per interval."""
    clock = [1000.0]
    backend = MemoryBackend(clock=lambda: clock[0])

    first = asyncio.run(backend.hit("k", 2, 10))
    second = asyncio.run(backend.hit("k", 2, 10))
    refused = asyncio.run(backend.hit("k", 2, 10))
    assert (first.allowed, first.remaining) == (True, 1)
    assert (second.allowed, second.remaining) == (True, 0)
    assert (refused.allowed, refused.retry_after) == (False, 5.0)

    clock[0] += 5
    assert asyncio.run(backend.hit("k", 2, 10)).allowed is True
    assert asyncio.run(backend.hit("other", 2, 10)).allowed is True


def test_gcra_decisions_report_exact_waits() -> None:
    """Remaining, reset, and retry times follow from the arrival times."""
    clock = [100.0]
    backend = MemoryBackend(clock=lambda: clock[0])

    first = asyncio.run(backend.hit("k", 2, 10))
    asyncio.run(backend.hit("k", 2, 10))
    refused = asyncio.run(backend.hit("k", 2, 10))

    assert first == Decision(allowed=True, remaining=1, reset_after=5, retry_after=0)
    assert refused == Decision(
        allowed=False, remaining=0, reset_after=10, retry_after=5
    )


@pytest.mark.parametrize("now", [0.02, 127.99, 511.96, 4095.94, 524287.84])
def test_gcra_admits_new_callers_whatever_the_clock_reads(now: float) -> None:
    """At these readings now + 60 - 60 rounds above now; a new caller still passes.

    ``time.monotonic()`` counts from boot, so a freshly started CI runner sits
    in these windows for a good part of its first minutes.
    """
    backend = MemoryBackend(clock=lambda: now)

    first = asyncio.run(backend.hit("k", 1, 60))
    refused = asyncio.run(backend.hit("k", 1, 60))

    assert (first.allowed, first.remaining) == (True, 0)
    assert refused.allowed is False
    assert refused.retry_after == pytest.approx(60)


def test_memory_backend_purges_idle_callers() -> None:
    """Callers whose quota has refilled are forgotten once the map outgrows max_keys."""
    clock = [0.0]
    backend = MemoryBackend(max_keys=2, clock=lambda: clock[0])
    asyncio.run(backend.hit("a", 1, 1))
    clock[0] += 10
    asyncio.run(backend.hit("b", 1, 1))
    assert set(backend._arrival) == {"a", "b"}

    asyncio.run(backend.hit("c", 1, 1))
    assert set(backend._arrival) == {"b", "c"}


def test_rate_limit_middleware() -> None:
    """Over-limit callers get 429 with quota headers; exempt paths pass."""
    with override_settings(
        RATE_LIMIT="2/minute",
        MIDDLEWARE=[*BASE_MIDDLEWARE, "domainkit.http.ratelimit.RateLimitMiddleware"],
    ):
        client = TestClient(get_asgi_application())
        first, second, refused = (client.get("/ok") for _ in range(3))
        exempt = [client.get("/health").status_code for _ in range(3)]

    assert (first.headers["RateLimit-Limit"], first.headers["RateLimit-Remaining"]) == (
        "2",
        "1",
    )
    # One request refills in 30s; the burst of two is whole again after 60s.
    assert first.headers["RateLimit-Reset"] == "30"
    assert refused.headers["RateLimit-Reset"] in {"59", "60"}
    assert second.status_code == 200
    assert (refused.status_code, refused.json()["code"]) == (429, "rate_limited")
    assert int(refused.headers["Retry-After"]) > 0
    assert exempt == [200, 200, 200]


def test_rate_limits_count_http_requests_per_caller() -> None:
    """Lifespan events pass uncounted; each address has its own bucket."""
    with override_settings(
        RATE_LIMIT="1/minute",
        MIDDLEWARE=[*BASE_MIDDLEWARE, "domainkit.http.ratelimit.RateLimitMiddleware"],
    ):
        app = get_asgi_application()
        with TestClient(app, client=("203.0.113.1", 1)) as first:
            echoed = first.post("/echo", content=b"body")
            refused = first.post("/echo", content=b"body")
        other = TestClient(app, client=("203.0.113.2", 1)).post("/echo", content=b"x")

    assert (echoed.status_code, echoed.json()) == (200, {"size": 4})
    assert echoed.headers["RateLimit-Remaining"] == "0"
    assert (refused.status_code, other.status_code) == (429, 200)


def test_rate_limit_disabled_and_misconfigured() -> None:
    """No RATE_LIMIT means no headers; a bad backend path fails clearly."""
    middleware = [*BASE_MIDDLEWARE, "domainkit.http.ratelimit.RateLimitMiddleware"]
    with override_settings(MIDDLEWARE=middleware):
        response = TestClient(get_asgi_application()).get("/ok")
    assert "RateLimit-Limit" not in response.headers

    with (
        override_settings(
            MIDDLEWARE=middleware,
            RATE_LIMIT="1/second",
            RATE_LIMIT_BACKEND="no.Backend",
        ),
        pytest.raises(ImproperlyConfigured, match="RATE_LIMIT_BACKEND"),
    ):
        TestClient(get_asgi_application()).get("/ok")


def test_callers_are_actors_or_addresses() -> None:
    """Authenticated callers are counted by actor, others by address."""
    scope = {"client": ("203.0.113.4", 1)}
    assert _caller(scope) == "ip:203.0.113.4"
    assert _caller({}) == "ip:unknown"
    with bind_request(RequestContext("r", None, "GET", "/", actor="alice")):
        assert _caller(scope) == "actor:alice"
        assert idempotency._caller(scope) == "actor:alice"
    anonymous = idempotency._caller({"type": "http", "headers": [], **scope})
    assert anonymous.startswith("anonymous:203.0.113.4:")
    assert idempotency._caller({"type": "http", "headers": []}).startswith(
        "anonymous:unknown:"
    )
    # Without an actor, different credentials never share keys.
    for name in (b"authorization", b"cookie"):
        credentialed = {"type": "http", "headers": [(name, b"x")], **scope}
        assert idempotency._caller(credentialed) != anonymous


@pytest.fixture
def idempotent(database_url: str) -> Iterator[TestClient]:
    """An app with the idempotency app and middleware on an empty database."""
    sample_routes.created.clear()
    url = database_url
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": url}},
        IDEMPOTENCY={"MAX_RESPONSE_BYTES": 1024},
        MIDDLEWARE=[
            *BASE_MIDDLEWARE,
            "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
        ],
    ):
        setup()

        async def create() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(idempotency_keys.create)
            await connections.close_all()

        asyncio.run(create())
        with TestClient(
            get_asgi_application(), raise_server_exceptions=False
        ) as client:
            yield client
    registry.clear()


def post(client: TestClient, path: str, key: str, body: object) -> object:
    """POST ``body`` with an Idempotency-Key."""
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def test_retries_replay_the_first_response(idempotent: TestClient) -> None:
    """The same key and request run once; repeats get the stored response."""
    first = idempotent.post(
        "/counter", json={"a": 1}, headers={"Idempotency-Key": "k1"}
    )
    again = idempotent.post(
        "/counter", json={"a": 1}, headers={"Idempotency-Key": "k1"}
    )
    other = idempotent.post(
        "/counter", json={"a": 1}, headers={"Idempotency-Key": "k2"}
    )
    plain = idempotent.post("/counter", json={"a": 1})

    assert (first.status_code, first.json()) == (201, {"count": 1})
    assert (again.status_code, again.json()) == (201, {"count": 1})
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.headers["Location"] == "/counter/1"
    assert (other.json(), plain.json()) == ({"count": 2}, {"count": 3})


def test_key_misuse_is_refused(idempotent: TestClient) -> None:
    """A different request, a malformed key, or a key in flight is refused."""
    idempotent.post("/counter", json={"a": 1}, headers={"Idempotency-Key": "k"})
    reused = idempotent.post(
        "/counter", json={"a": 2}, headers={"Idempotency-Key": "k"}
    )
    assert (reused.status_code, reused.json()["code"]) == (
        422,
        "idempotency_key_reused",
    )

    requery = idempotent.post(
        "/counter?page=2", json={"a": 1}, headers={"Idempotency-Key": "k"}
    )
    assert requery.status_code == 422
    longest = idempotent.post(
        "/counter", json={"a": 1}, headers={"Idempotency-Key": "x" * 255}
    )
    assert longest.status_code == 201

    for bad in ("", "x" * 256, "tab\tkey"):
        response = idempotent.post(
            "/counter", json={}, headers={"Idempotency-Key": bad}
        )
        assert (response.status_code, response.json()["code"]) == (
            400,
            "invalid_idempotency_key",
        )

    async def in_flight() -> None:
        now = datetime.now(UTC)
        async with connections.engine().begin() as connection:
            await connection.execute(
                insert(idempotency_keys).values(
                    key_hash=idempotency._hash(
                        f"anonymous:testclient:{idempotency._hash('', '')}", "busy"
                    ),
                    fingerprint=idempotency._hash("POST", "/counter", b"", b'{"a":1}'),
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
        await connections.close_all()

    asyncio.run(in_flight())
    busy = idempotent.post(
        "/counter", json={"a": 1}, headers={"Idempotency-Key": "busy"}
    )
    assert (busy.status_code, busy.json()["code"]) == (409, "idempotency_key_in_use")
    assert busy.headers["Retry-After"] == "1"

    read = idempotent.get("/ok", headers={"Idempotency-Key": "k"})
    assert read.status_code == 200


def test_oversized_streamed_bodies_get_413(idempotent: TestClient) -> None:
    """A streamed body over the limit is a 413 with a key too, not a 500."""

    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b"x" * 600_000

    response = idempotent.post(
        "/counter", content=chunks(), headers={"Idempotency-Key": "big"}
    )

    assert (response.status_code, response.json()["code"]) == (
        413,
        "content_too_large",
    )


def test_failures_and_large_responses_are_not_stored(idempotent: TestClient) -> None:
    """Server errors and oversized bodies release the key for a retry."""
    for path in ("/unavailable", "/crash", "/large"):
        first = idempotent.post(path, json={}, headers={"Idempotency-Key": path})
        retry = idempotent.post(path, json={}, headers={"Idempotency-Key": path})
        assert "Idempotent-Replayed" not in retry.headers
        assert first.status_code == retry.status_code

    async def stored() -> list[object]:
        async with connections.engine().connect() as connection:
            rows = (await connection.execute(select(idempotency_keys))).all()
        await connections.close_all()
        return list(rows)

    assert asyncio.run(stored()) == []


def test_storage_limits_are_inclusive(idempotent: TestClient) -> None:
    """A body of exactly MAX_RESPONSE_BYTES is stored; every 5xx, even 500, is not."""
    for query, replayed in (
        ("size=1024", True),
        ("size=1025", False),
        ("size=600&parts=2", False),
        ("status_code=499", True),
        ("status_code=500", False),
    ):
        headers = {"Idempotency-Key": query}
        first = idempotent.post(f"/respond?{query}", headers=headers)
        retry = idempotent.post(f"/respond?{query}", headers=headers)
        assert ("Idempotent-Replayed" in retry.headers) is replayed, query
        assert (retry.status_code, retry.content) == (first.status_code, first.content)
        if replayed:
            # Stored names stay lowercase, as ASGI requires.
            assert (b"content-type", b"text/plain; charset=utf-8") in retry.headers.raw


def test_expired_keys_run_again_and_are_purged(idempotent: TestClient) -> None:
    """Past the TTL a key is forgotten; the periodic task purges old keys."""
    from domainkit.contrib.idempotency.tasks import purge_expired

    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "old"})

    async def expire() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                idempotency_keys.update().values(
                    expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
        await connections.close_all()

    asyncio.run(expire())
    again = idempotent.post("/counter", json={}, headers={"Idempotency-Key": "old"})
    assert again.json() == {"count": 2}

    asyncio.run(expire())

    async def purge() -> int:
        await purge_expired()
        async with connections.engine().connect() as connection:
            count = len((await connection.execute(select(idempotency_keys))).all())
        await connections.close_all()
        return count

    assert asyncio.run(purge()) == 0


def test_request_bodies_are_read_and_replayed() -> None:
    """Chunked bodies are joined; the app still sees the disconnect afterwards."""
    incoming: list[Message] = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"c", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive() -> Message:
        return incoming.pop(0)

    async def scenario() -> tuple[bytes, Message, Message, bytes]:
        body, replay = await idempotency._read_body(receive)
        first, second = await replay(), await replay()
        empty, _ = await idempotency._read_body(receive_disconnect)
        return body, first, second, empty

    async def receive_disconnect() -> Message:
        return {"type": "http.disconnect"}

    body, first, second, empty = asyncio.run(scenario())
    assert body == b"abc"
    assert first == {"type": "http.request", "body": b"abc", "more_body": False}
    assert second == {"type": "http.disconnect"}
    assert empty == b""


def test_purge_is_scheduled_only_with_messaging(tmp_path: Path) -> None:
    """Without messaging installed, no periodic purge is registered."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
    ):
        setup()
        assert registry.is_installed("domainkit.contrib.idempotency")
    registry.clear()
