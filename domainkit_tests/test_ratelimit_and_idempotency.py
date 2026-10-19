"""Rate limiting and Idempotency-Key replay."""

import asyncio
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, false, insert, select
from sqlalchemy.sql import Select
from starlette.testclient import WebSocketDenialResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import Settings, override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.idempotency import apps as idempotency_apps
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.idempotency.operations import Operation
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.ratelimit import (
    Decision,
    MemoryBackend,
    RateLimitMiddleware,
    _caller,
    client_group,
)
from domainkit.utils.rates import parse_rate
from domainkit_tests import sample_routes
from domainkit_tests.test_http import headerless, started

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


def test_memory_backend_forgets_idle_callers() -> None:
    """Callers whose quota is full again are forgotten, longest-unseen first."""
    clock = [0.0]
    backend = MemoryBackend(clock=lambda: clock[0])

    def hit(key: str, limit: int, period: float) -> Decision:
        return asyncio.run(backend.hit(key, limit, period))

    hit("a", 1, 1)
    hit("b", 1, 100)
    clock[0] = 0.5
    hit("c", 1, 1)
    clock[0] = 2
    hit("d", 1, 1)
    # "a" is full again and goes; "b" is not, so "c" waits behind it.
    assert list(backend._arrival) == ["b", "c", "d"]
    assert hit("b", 1, 100).allowed is False
    clock[0] = 200
    hit("e", 1, 1)
    assert list(backend._arrival) == ["e"]
    clock[0] = 201
    hit("f", 1, 1)
    # "e" refilled at this very moment: forgotten too.
    assert list(backend._arrival) == ["f"]


def test_memory_backend_keeps_at_most_max_keys_callers() -> None:
    """Past ``max_keys``, the longest-unseen caller is forgotten and starts afresh."""
    assert MemoryBackend().max_keys == 100_000
    backend = MemoryBackend(max_keys=2, clock=lambda: 0.0)

    def hit(key: str) -> Decision:
        return asyncio.run(backend.hit(key, 1, 60))

    assert [hit(key).allowed for key in ("a", "b", "a", "c")] == [
        True,
        True,
        False,
        True,
    ]
    # The refusal counted as seeing "a", so "b" was the one forgotten.
    assert list(backend._arrival) == ["a", "c"]
    assert hit("a").allowed is False
    assert hit("b").allowed is True
    assert list(backend._arrival) == ["a", "b"]


def test_ipv6_clients_are_counted_per_network() -> None:
    """IPv4 as is; IPv6 by its /64 (``RATE_LIMIT_IPV6_PREFIX``)."""
    assert client_group("203.0.113.7") == "203.0.113.7"
    assert client_group("2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert client_group("2001:DB8:1:2::9") == "2001:db8:1:2::/64"
    assert client_group("fe80::1%eth0") == "fe80::/64"
    assert client_group("::ffff:203.0.113.7") == "203.0.113.7"
    assert client_group("testclient") == "testclient"
    with override_settings(RATE_LIMIT_IPV6_PREFIX=56):
        assert client_group("2001:db8:1:2ff::1") == "2001:db8:1:200::/56"
    with override_settings(RATE_LIMIT_IPV6_PREFIX=128):
        assert client_group("2001:db8::1") == "2001:db8::1/128"
    for prefix in (0, 129):
        with pytest.raises(ValueError, match="RATE_LIMIT_IPV6_PREFIX"):
            Settings.model_validate({"RATE_LIMIT_IPV6_PREFIX": prefix})
    assert Settings.model_validate({"RATE_LIMIT_IPV6_PREFIX": 1})


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
    assert refused.json()["detail"] == "Too many requests; slow down."
    assert int(refused.headers["Retry-After"]) > 0
    assert exempt == [200, 200, 200]


def test_rate_limits_count_websocket_handshakes() -> None:
    """Each connection counts once; over the limit, its handshake gets a 429."""
    with override_settings(
        RATE_LIMIT="1/minute",
        MIDDLEWARE=[*BASE_MIDDLEWARE, "domainkit.http.ratelimit.RateLimitMiddleware"],
    ):
        client = TestClient(get_asgi_application())
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_text() == "hi"
            accepted: dict[bytes, bytes] = dict(socket.extra_headers or [])
        with (
            pytest.raises(WebSocketDenialResponse) as refused,
            client.websocket_connect("/ws"),
        ):
            pass

    assert (accepted[b"ratelimit-limit"], accepted[b"ratelimit-remaining"]) == (
        b"1",
        b"0",
    )
    assert refused.value.status_code == 429
    assert refused.value.json()["code"] == "rate_limited"
    assert int(refused.value.headers["Retry-After"]) > 0
    assert refused.value.headers["X-Request-ID"]


def test_rate_limited_websockets_close_without_denial_responses() -> None:
    """A server that cannot send a 429 gets the handshake closed with 1013."""
    sent: list[object] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        sent.append("reached the application")

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(RATE_LIMIT="1/minute"):
        middleware = RateLimitMiddleware(app)
    scope = {
        "type": "websocket",
        "path": "/ws",
        "headers": [],
        "client": ("203.0.113.9", 1),
    }
    asyncio.run(middleware(scope, receive, send))
    asyncio.run(middleware(scope, receive, send))

    assert sent == [
        "reached the application",
        {"type": "websocket.close", "code": 1013},
    ]


def test_raw_applications_may_omit_headers_and_clients() -> None:
    """A start or an accept without headers still gets the quota headers.

    ASGI makes the message's ``headers`` and the scope's ``client`` optional;
    callers without an address share one bucket.
    """
    with override_settings(RATE_LIMIT="2/minute"):
        limited = RateLimitMiddleware(headerless)

    starts = [started(limited, kind) for kind in ("http", "websocket")]

    assert [start[b"ratelimit-remaining"] for start in starts] == [b"1", b"0"]


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


def test_rate_limits_count_ipv6_clients_per_network() -> None:
    """Changing address within a /64 does not escape the limit."""
    with override_settings(
        RATE_LIMIT="1/minute",
        MIDDLEWARE=[*BASE_MIDDLEWARE, "domainkit.http.ratelimit.RateLimitMiddleware"],
    ):
        app = get_asgi_application()

        def status_from(address: str) -> int:
            return TestClient(app, client=(address, 1)).get("/ok").status_code

        statuses = [
            status_from(address)
            for address in ("2001:db8::1", "2001:db8::ffff:2", "2001:db8:0:1::1")
        ]

    assert statuses == [200, 429, 200]


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
    names = IdempotencySettings().CREDENTIAL_HEADERS
    with bind_request(RequestContext("r", None, "GET", "/", actor="alice")):
        assert _caller(scope) == "actor:alice"
        assert idempotency._caller(scope, names) == "actor:alice"
    anonymous = idempotency._caller({"type": "http", "headers": [], **scope}, names)
    assert anonymous.startswith("anonymous:203.0.113.4:")
    assert idempotency._caller({"type": "http", "headers": []}, names).startswith(
        "anonymous:unknown:"
    )
    # Without an actor, different credentials never share keys.
    seen = {anonymous}
    for headers in (
        [(b"authorization", b"x")],
        [(b"proxy-authorization", b"x")],
        [(b"cookie", b"x")],
        [(b"x-api-key", b"alice")],
        [(b"x-api-key", b"bob")],
        [(b"x-api-key", b"alice"), (b"x-api-key", b"bob")],
        [(b"authorization", b"cookie"), (b"cookie", b"x")],  # Counts keep them apart.
        [(b"authorization", b"cookie"), (b"authorization", b"x")],
        # Without counts, these two would hash the same parts.
        [(b"authorization", b"proxy-authorization"), (b"authorization", b"None")],
        [
            (b"proxy-authorization", b"proxy-authorization"),
            (b"proxy-authorization", b"None"),
        ],
    ):
        credentialed = {"type": "http", "headers": headers, **scope}
        caller = idempotency._caller(credentialed, names)
        assert caller not in seen
        seen.add(caller)
    other_header = {"type": "http", "headers": [(b"x-token", b"x")], **scope}
    assert idempotency._caller(other_header, names) == anonymous


def test_hashes_are_length_prefixed() -> None:
    """Each part is prefixed by its length, so parts never run together."""
    assert idempotency._hash("POST", b"body") == (
        "9f1b2cf8e2f8a57ec1e0de928a6deb9a6789a6dfe4b04c823249e3b3765bbcf8"
    )
    assert idempotency._hash("ab", "c") != idempotency._hash("a", "bc")


@contextmanager
def idempotent_app(databases: dict[str, str]) -> Iterator[TestClient]:
    """The idempotency app and middleware, keys on ``default``, payments on each."""
    sample_routes.created.clear()
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit.contrib.idempotency",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={alias: {"URL": url} for alias, url in databases.items()},
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
            for alias in databases:
                async with connections.engine(alias).begin() as connection:
                    await connection.run_sync(sample_routes.payments.create)
            await connections.close_all()

        asyncio.run(create())
        with TestClient(
            get_asgi_application(), raise_server_exceptions=False
        ) as client:
            yield client
    registry.clear()


@pytest.fixture
def idempotent(database_url: str) -> Iterator[TestClient]:
    """An app with the idempotency app and middleware on an empty database."""
    with idempotent_app({"default": database_url}) as client:
        yield client


def stored_keys() -> list[dict[str, object]]:
    """The rows of the idempotency table."""

    async def read() -> list[dict[str, object]]:
        async with connections.engine().connect() as connection:
            rows = (await connection.execute(select(idempotency_keys))).all()
        await connections.close_all()
        return [dict(row._mapping) for row in rows]

    return asyncio.run(read())


def keyed(key: str) -> str:
    """The hash of ``key`` sent by the test client without credentials."""
    scope = {"type": "http", "headers": [], "client": ("testclient", 50000)}
    caller = idempotency._caller(scope, IdempotencySettings().CREDENTIAL_HEADERS)
    return idempotency._hash(caller, key)


def plant(key: str, path: str = "/pay", *, body: bytes = b"", **values: object) -> None:
    """Store ``key`` as held by a request on ``path`` with token ``first``."""
    now = datetime.now(UTC)
    row = {
        "key_hash": keyed(key),
        "fingerprint": idempotency._hash("POST", path, b"", body),
        "created_at": now,
        "expires_at": now + timedelta(hours=1),
        "token": "first",
        "locked_until": now + timedelta(minutes=1),
        **values,
    }

    async def store() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(insert(idempotency_keys).values(**row))
        await connections.close_all()

    asyncio.run(store())


def payments_on(alias: str = "default") -> int:
    """How many payments ``alias`` holds."""

    async def count() -> int:
        async with connections.engine(alias).connect() as connection:
            rows = (await connection.execute(select(sample_routes.payments))).all()
        await connections.close_all()
        return len(rows)

    return asyncio.run(count())


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
    assert (b"idempotent-replayed", b"true") in again.headers.raw
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
    assert reused.json()["detail"] == (
        "This Idempotency-Key was used for a different request."
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
        assert response.json()["detail"] == (
            "Idempotency-Key must be 1 to 255 printable ASCII characters."
        )

    plant("busy", "/counter", body=b'{"a":1}')
    busy = idempotent.post(
        "/counter", json={"a": 1}, headers={"Idempotency-Key": "busy"}
    )
    assert (busy.status_code, busy.json()["code"]) == (409, "idempotency_key_in_use")
    assert busy.json()["detail"] == (
        "A request with this Idempotency-Key is still in progress."
    )
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
    """Server errors and oversized bodies release their key, and only theirs."""
    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "kept"})
    for path in ("/unavailable", "/crash", "/large"):
        first = idempotent.post(path, json={}, headers={"Idempotency-Key": path})
        retry = idempotent.post(path, json={}, headers={"Idempotency-Key": path})
        assert "Idempotent-Replayed" not in retry.headers
        assert first.status_code == retry.status_code

    assert [row["status_code"] for row in stored_keys()] == [201]


def test_committed_requests_never_run_twice(idempotent: TestClient) -> None:
    """After a commit the key is kept: a retry learns the request ran."""
    for outcome, status_code in (
        ("crash", 500),
        ("unavailable", 503),
        ("large", 201),
        ("unreported", 500),
    ):
        headers = {"Idempotency-Key": outcome}
        first = idempotent.post(f"/pay?outcome={outcome}", headers=headers)
        retry = idempotent.post(f"/pay?outcome={outcome}", headers=headers)
        assert first.status_code == status_code, outcome
        assert (retry.status_code, retry.json()["code"]) == (
            409,
            "idempotency_response_unavailable",
        )
        assert retry.json()["detail"] == (
            "The request with this Idempotency-Key already ran (status "
            f"{status_code}), but its response was not kept; look up the result "
            "instead of retrying."
        )
        assert "Retry-After" not in retry.headers

    assert payments_on() == 4
    assert all(row["committed_at"] is not None for row in stored_keys())


def test_committed_responses_are_replayed(idempotent: TestClient) -> None:
    """A kept response replays; requests without a key commit as usual."""
    headers = {"Idempotency-Key": "pay"}
    first = idempotent.post("/pay", headers=headers)
    retry = idempotent.post("/pay", headers=headers)
    unkeyed = idempotent.post("/pay")

    assert (first.status_code, first.content) == (201, b"paid")
    assert (retry.status_code, retry.content) == (201, b"paid")
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert unkeyed.status_code == 201
    assert payments_on() == 2
    [row] = stored_keys()
    assert row["committed_at"] is not None


def test_a_rejected_commit_releases_the_key(idempotent: TestClient) -> None:
    """The mark commits with the changes: a failed commit leaves no mark."""
    headers = {"Idempotency-Key": "rejected"}
    first = idempotent.post("/pay?outcome=rejected", headers=headers)
    retry = idempotent.post("/pay?outcome=rejected", headers=headers)

    assert (first.status_code, retry.status_code) == (500, 500)
    assert payments_on() == 0
    assert stored_keys() == []


def test_commits_elsewhere_are_marked_after_them(
    make_database: Callable[[str], str],
) -> None:
    """Changes on another database mark the key right after their commit."""
    databases = {"default": make_database("keys"), "other": make_database("other")}
    with idempotent_app(databases) as client:
        for outcome in ("crash", "ok"):
            headers = {"Idempotency-Key": outcome}
            path = f"/pay?outcome={outcome}&database=other"
            first = client.post(path, headers=headers)
            retry = client.post(path, headers=headers)
            assert first.status_code == {"crash": 500, "ok": 201}[outcome]
            assert retry.status_code == {"crash": 409, "ok": 201}[outcome]

        assert payments_on("other") == 2
        rows = stored_keys()
        assert len(rows) == 2
        assert all(row["committed_at"] is not None for row in rows)
        # Pinned before the commit: no retry may take these keys over.
        assert all(row["locked_until"] == row["expires_at"] for row in rows)


def test_a_failed_mark_still_keeps_the_key(
    make_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If marking a commit elsewhere fails, the request still never reruns."""

    def unreachable(operation: Operation) -> None:
        raise ConnectionError("The keys' database is away.")

    monkeypatch.setattr(idempotency_apps, "mark_committed", unreachable)
    databases = {"default": make_database("keys"), "other": make_database("other")}
    with idempotent_app(databases) as client:
        headers = {"Idempotency-Key": "crash"}
        first = client.post("/pay?outcome=crash&database=other", headers=headers)
        retry = client.post("/pay?outcome=crash&database=other", headers=headers)

        assert (first.status_code, retry.status_code) == (500, 409)
        assert payments_on("other") == 1
        [row] = stored_keys()
        assert row["committed_at"] is None


def test_keys_of_dead_requests_are_taken_over(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that died before committing loses its key once its lease ends."""
    later = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(idempotency, "_now", lambda: later)
    plant("dead", locked_until=later - timedelta(microseconds=1))
    plant("dying", locked_until=later)

    taken = idempotent.post("/pay", headers={"Idempotency-Key": "dead"})
    replayed = idempotent.post("/pay", headers={"Idempotency-Key": "dead"})
    held = idempotent.post("/pay", headers={"Idempotency-Key": "dying"})

    assert (taken.status_code, taken.content) == (201, b"paid")
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert (held.status_code, held.json()["code"]) == (409, "idempotency_key_in_use")
    assert payments_on() == 1
    rows = {row["key_hash"]: row for row in stored_keys()}
    assert rows[keyed("dead")]["token"] not in {"first", rows[keyed("dying")]["token"]}
    assert len(str(rows[keyed("dead")]["token"])) == 32
    assert rows[keyed("dead")]["locked_until"] == later + timedelta(seconds=60)


def test_new_keys_are_leased(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed key is held for LEASE_SECONDS under a fresh random token."""
    now = datetime.now(UTC)
    monkeypatch.setattr(idempotency, "_now", lambda: now)
    for key in ("one", "two"):
        idempotent.post("/pay", headers={"Idempotency-Key": key})

    one, two = stored_keys()
    assert one["locked_until"] == now + timedelta(seconds=60)
    assert one["token"] != two["token"]


def test_writes_touch_only_their_own_key(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tokens guard writes, but the key picks the row, even if tokens collide."""
    plant("other", token="shared")
    monkeypatch.setattr(secrets, "token_hex", lambda size: "shared")

    idempotent.post("/pay", headers={"Idempotency-Key": "mine"})

    rows = {row["key_hash"]: row for row in stored_keys()}
    mine, other = rows[keyed("mine")], rows[keyed("other")]
    assert mine["status_code"] == 201
    assert (other["status_code"], other["committed_at"]) == (None, None)


def test_dead_requests_are_not_taken_over_when_unlike_or_committed(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another request is refused; a committed one is never run again."""
    later = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(idempotency, "_now", lambda: later)
    expired = later - timedelta(microseconds=1)
    plant("unlike", "/counter", locked_until=expired)
    plant("committed", locked_until=expired, committed_at=expired)
    plant("committing", locked_until=later, committed_at=expired)

    unlike = idempotent.post("/pay", headers={"Idempotency-Key": "unlike"})
    committed = idempotent.post("/pay", headers={"Idempotency-Key": "committed"})
    committing = idempotent.post("/pay", headers={"Idempotency-Key": "committing"})

    assert (unlike.status_code, unlike.json()["code"]) == (
        422,
        "idempotency_key_reused",
    )
    assert (committed.status_code, committed.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    assert committed.json()["detail"] == (
        "The request with this Idempotency-Key already ran, but its response was "
        "not kept; look up the result instead of retrying."
    )
    assert (committing.status_code, committing.json()["code"]) == (
        409,
        "idempotency_key_in_use",
    )
    assert payments_on() == 0


def test_taking_over_needs_the_row_as_it_was_read(idempotent: TestClient) -> None:
    """The holder may have finished or committed, or a retry won the key first."""
    config = registry.settings(IdempotencySettings)
    middleware = idempotency.IdempotencyMiddleware(get_asgi_application())
    gone = datetime.now(UTC) - timedelta(seconds=1)
    for changed, taken in (
        ({}, True),
        ({"token": "other"}, False),
        ({"status_code": 201}, False),
        ({"committed_at": gone}, False),
    ):
        asyncio.run(clear_keys())
        plant("race", locked_until=gone, **changed)

        async def attempt() -> bool:
            operation = Operation(keyed("race"), "retry")
            result = await middleware._take_over(config, operation, "first")
            await connections.close_all()
            return result

        assert asyncio.run(attempt()) is taken, changed
        assert [row["token"] for row in stored_keys()] == [
            "retry" if taken else changed.get("token", "first")
        ]


def test_a_request_that_lost_its_key_cannot_commit_through_it(
    idempotent: TestClient,
) -> None:
    """Once a retry holds the key, the first request saves and stores nothing."""
    lost = idempotent.post(
        "/pay?outcome=overtaken", headers={"Idempotency-Key": "overtaken"}
    )
    crashed = idempotent.post(
        "/pay?outcome=overtaken-crash", headers={"Idempotency-Key": "crashed"}
    )

    assert (lost.status_code, lost.json()["code"]) == (
        409,
        "idempotency_key_taken_over",
    )
    assert lost.json()["detail"] == (
        "This request ran past its Idempotency-Key lease, and a retry took the "
        "key over; its changes were not saved."
    )
    assert crashed.status_code == 500
    assert payments_on() == 0
    rows = stored_keys()
    assert [(row["token"], row["status_code"]) for row in rows] == [("retry", None)] * 2


def test_refusals_and_secrets_after_a_commit_are_settled(
    idempotent: TestClient,
) -> None:
    """A 4xx that committed is replayed; a 5xx or no-store response is never kept."""
    declined = {"Idempotency-Key": "declined"}
    secret = {"Idempotency-Key": "secret"}
    failed = {"Idempotency-Key": "failed"}

    first = idempotent.post("/pay?outcome=declined", headers=declined)
    again = idempotent.post("/pay?outcome=declined", headers=declined)
    token = idempotent.post("/pay?outcome=secret", headers=secret)
    retry = idempotent.post("/pay?outcome=secret", headers=secret)
    error = idempotent.post("/pay?outcome=failed", headers=failed)
    after_error = idempotent.post("/pay?outcome=failed", headers=failed)

    assert (first.status_code, again.status_code) == (402, 402)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert (token.status_code, token.content) == (201, b"token")
    assert (retry.status_code, retry.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    assert error.status_code == 500  # A server error, even after a commit, is not kept.
    assert (after_error.status_code, after_error.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    assert payments_on() == 3
    assert {row["key_hash"]: row["body"] for row in stored_keys()}[
        keyed("secret")
    ] is None


def test_without_a_worker_claims_sweep_expired_keys(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no hourly purge, each claim deletes a few expired keys."""
    gone = datetime.now(UTC) - timedelta(seconds=1)
    for key in ("old-1", "old-2", "old-3"):
        plant(key, expires_at=gone)
    plant("live")
    with_worker = idempotent.post("/pay", headers={"Idempotency-Key": "a"})
    kept = {row["key_hash"] for row in stored_keys()}
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")
    monkeypatch.setattr(idempotency, "PURGE_BATCH", 2)

    idempotent.post("/pay", headers={"Idempotency-Key": "b"})

    assert with_worker.status_code == 201
    assert {keyed(key) for key in ("old-1", "old-2", "old-3")} <= kept
    left = {row["key_hash"] for row in stored_keys()}
    assert len(left & {keyed(key) for key in ("old-1", "old-2", "old-3")}) == 1
    assert {keyed("live"), keyed("a"), keyed("b")} <= left


def test_a_lost_key_refuses_commits_elsewhere_too(
    make_database: Callable[[str], str],
) -> None:
    """The key is pinned before a commit elsewhere, which a lost key refuses."""
    databases = {"default": make_database("keys"), "other": make_database("other")}
    with idempotent_app(databases) as client:
        headers = {"Idempotency-Key": "late"}
        lost = client.post("/pay?outcome=overtaken&database=other", headers=headers)

        assert (lost.status_code, lost.json()["code"]) == (
            409,
            "idempotency_key_taken_over",
        )
        assert payments_on("other") == 0
        [row] = stored_keys()
        assert (row["token"], row["status_code"]) == ("retry", None)


def test_storage_limits_are_inclusive(idempotent: TestClient) -> None:
    """A body of exactly MAX_RESPONSE_BYTES is stored; every 5xx, even 500, is not.

    Nor is a 4xx when nothing committed, or a response that must not be
    stored (``Cache-Control: no-store``).
    """
    for query, replayed in (
        ("size=1024", True),
        ("size=1025", False),
        ("size=600&parts=2", False),
        ("status_code=399", True),
        ("status_code=400", False),  # Refused, nothing committed: runs again.
        ("status_code=499", False),
        ("status_code=500", False),
        ("cache=no-store", False),
        ("cache=private,%20No-Store", False),
        ("cache=no-store,private", False),
        ("cache=no-cache", True),
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
    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "other"})

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
    assert again.json() == {"count": 3}
    # Claiming a key clears only that key; the purge clears the others.
    assert len(stored_keys()) == 2

    asyncio.run(expire())

    async def purge() -> int:
        await purge_expired()
        async with connections.engine().connect() as connection:
            count = len((await connection.execute(select(idempotency_keys))).all())
        await connections.close_all()
        return count

    assert asyncio.run(purge()) == 0


def test_keys_live_until_the_instant_they_expire(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At ``expires_at`` a key still replays; it is expired only afterwards."""
    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "edge"})
    [row] = stored_keys()
    monkeypatch.setattr(idempotency, "_now", lambda: row["expires_at"])

    at_expiry = idempotent.post(
        "/counter", json={}, headers={"Idempotency-Key": "edge"}
    )

    assert at_expiry.headers["Idempotent-Replayed"] == "true"
    assert len(sample_routes.created) == 1


def test_a_key_released_while_claimed_is_in_progress(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row gone between the failed insert and the read means: try again."""
    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "gone"})

    def nothing(table: Table) -> Select[tuple[object, ...]]:
        return select(table).where(false())

    monkeypatch.setattr(idempotency, "select", nothing)
    retry = idempotent.post("/counter", json={}, headers={"Idempotency-Key": "gone"})

    assert (retry.status_code, retry.json()["code"]) == (409, "idempotency_key_in_use")


def run_raw(app: ASGIApp, *incoming: Message) -> list[Message]:
    """Send one keyed POST through the middleware straight to ``app``.

    The request sends ``incoming``, or else an empty body.
    """
    messages = list(incoming or [{"type": "http.request"}])

    async def scenario() -> list[Message]:
        sent: list[Message] = []

        async def receive() -> Message:
            return messages.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/raw",
            "headers": [(b"idempotency-key", b"raw")],
        }
        await idempotency.IdempotencyMiddleware(app)(scope, receive, send)
        await connections.close_all()
        return sent

    return asyncio.run(scenario())


async def clear_keys() -> None:
    """Forget every stored key."""
    async with connections.engine().begin() as connection:
        await connection.execute(idempotency_keys.delete())
    await connections.close_all()


def test_raw_responses_may_omit_their_parts(idempotent: TestClient) -> None:
    """Missing optional ASGI keys count as empty; no response is a 500."""
    runs: list[str] = []

    async def answer(scope: Scope, receive: Receive, send: Send) -> None:
        runs.append("answer")
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "more_body": True})
        await send({"type": "http.response.body", "body": b"ok"})

    run_raw(answer)
    replayed = run_raw(answer)
    assert runs == ["answer"]
    # A scope without a query string has the same fingerprint as an empty one.
    [row] = stored_keys()
    assert row["fingerprint"] == idempotency._hash("POST", "/raw", b"", b"")
    assert replayed[1] == {"type": "http.response.body", "body": b"ok"}

    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        runs.append("silent")

    asyncio.run(clear_keys())
    run_raw(silent)
    run_raw(silent)
    assert runs == ["answer", "silent", "silent"]
    assert stored_keys() == []


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
        read = await idempotency._read_body(receive)
        bare = await idempotency._read_body(receive_bare)
        assert read is not None
        assert bare is not None
        body, replay = read
        first, second = await replay(), await replay()
        return body, first, second, bare[0]

    async def receive_bare() -> Message:
        return {"type": "http.request"}

    body, first, second, bare = asyncio.run(scenario())
    assert body == b"abc"
    assert first == {"type": "http.request", "body": b"abc", "more_body": False}
    assert second == {"type": "http.disconnect"}
    assert bare == b""


def test_bodies_cut_short_by_a_disconnect_are_not_read() -> None:
    """A disconnect before the last chunk, or before any, leaves no body."""
    disconnect: Message = {"type": "http.disconnect"}
    part: Message = {"type": "http.request", "body": b"ab", "more_body": True}
    for incoming in ([disconnect], [part, disconnect]):

        async def receive(messages: list[Message] = incoming) -> Message:
            return messages.pop(0)

        assert asyncio.run(idempotency._read_body(receive)) is None


def test_requests_cut_short_claim_and_run_nothing(idempotent: TestClient) -> None:
    """A dropped upload never runs on part of its body; its retry runs in full."""
    bodies: list[bytes] = []

    async def record(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        bodies.append(message["body"])
        await send({"type": "http.response.start", "status": 201})
        await send({"type": "http.response.body", "body": b"done"})

    part = {"type": "http.request", "body": b'{"amount": 1', "more_body": True}
    cut = run_raw(record, part, {"type": "http.disconnect"})

    assert (cut, bodies, stored_keys()) == ([], [], [])
    whole = run_raw(record, {"type": "http.request", "body": b'{"amount": 100}'})
    assert whole[0]["status"] == 201
    assert bodies == [b'{"amount": 100}']


def test_purge_is_scheduled_only_with_messaging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready() imports the hourly purge only when messaging is installed."""
    imported: list[str] = []
    monkeypatch.setattr(idempotency_apps, "import_module", imported.append)
    domains = ["domainkit.contrib.idempotency", "domainkit_tests.sample_domains.alpha"]
    for installed in (domains, ["domainkit.contrib.messaging", *domains]):
        registry.clear()
        with override_settings(INSTALLED_DOMAINS=installed):
            setup()
    registry.clear()

    assert imported == ["domainkit.contrib.idempotency.tasks"]
