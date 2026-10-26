"""Rate limiting and Idempotency-Key replay."""

import asyncio
import logging
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import anyio
import pytest
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Response
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from sqlalchemy import false, func, insert, select
from starlette.testclient import WebSocketDenialResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import Settings, override_settings
from domainkit.conf.global_settings import DEADLINE_MIDDLEWARE, DEFAULT_MIDDLEWARE
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.idempotency import apps as idempotency_apps
from domainkit.contrib.idempotency import middleware as idempotency
from domainkit.contrib.idempotency import operations as idempotency_operations
from domainkit.contrib.idempotency.operations import Operation, running_operation
from domainkit.contrib.idempotency.settings import IdempotencySettings
from domainkit.contrib.idempotency.statements import ClockStatements, clock_statements
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.contrib.messaging import enqueue, task
from domainkit.contrib.messaging import outbox as messaging_outbox
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.deadlines import RequestTimeout
from domainkit.http.ratelimit import (
    EVICTIONS_PER_HIT,
    Decision,
    MemoryBackend,
    RateLimitMiddleware,
    _caller,
    client_group,
)
from domainkit.tasks import owned, shutdown
from domainkit.utils.rates import parse_rate
from domainkit_tests import sample_routes
from domainkit_tests.conftest import ALPHA, POSTGRES_VARIABLE
from domainkit_tests.test_http import headerless, started

postgres_only = pytest.mark.skipif(
    not os.environ.get(POSTGRES_VARIABLE), reason=f"set {POSTGRES_VARIABLE}"
)
BASE_MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.middleware.RequestBodyLimitMiddleware",
]
CALLER: Final = (b"x-api-key", b"caller")
"""Names the caller of the keyed requests below, as a project's own
authentication would. Since 0.11 a key that names none is refused
(``IDEMPOTENCY.ANONYMOUS = "refuse"``, DATA-09); these tests are about
what keys do, so their requests name one."""


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
    assert list(backend._callers) == ["b", "c", "d"]
    assert hit("b", 1, 100).allowed is False
    clock[0] = 200
    hit("e", 1, 1)
    assert list(backend._callers) == ["e"]
    clock[0] = 201
    hit("f", 1, 1)
    # "e" refilled at this very moment: forgotten too.
    assert list(backend._callers) == ["f"]


def test_memory_backend_forgets_a_crowd_of_idle_callers_a_few_at_a_time() -> None:
    """After a lull, each request forgets at most ``EVICTIONS_PER_HIT`` (IND-RL)."""
    clock = [0.0]
    backend = MemoryBackend(clock=lambda: clock[0])
    crowd = 3 * EVICTIONS_PER_HIT + 10

    async def lull_then_hits() -> list[int]:
        for caller in range(crowd):
            await backend.hit(f"ip:{caller}", 300, 60)
        clock[0] = 1000.0  # Every one of them is full again.
        left = []
        for _ in range(5):
            await backend.hit("ip:new", 300, 60)
            left.append(len(backend._callers))
        return left

    assert EVICTIONS_PER_HIT == 64
    assert asyncio.run(lull_then_hits()) == [
        crowd - 64 + 1,
        crowd - 128 + 1,
        crowd - 192 + 1,
        1,
        1,
    ]
    assert list(backend._callers) == ["ip:new"]


def test_memory_backend_refills_callers_it_still_remembers() -> None:
    """A caller kept behind a busier one still finds its quota full again."""
    clock = [0.0]
    backend = MemoryBackend(clock=lambda: clock[0])

    asyncio.run(backend.hit("busy", 1, 100))
    asyncio.run(backend.hit("k", 1, 1))
    clock[0] = 5
    again = asyncio.run(backend.hit("k", 1, 1))

    assert list(backend._callers) == ["busy", "k"]
    assert again == Decision(allowed=True, remaining=0, reset_after=1, retry_after=0)


def test_memory_backend_carries_what_a_caller_owes_across_a_rate_change() -> None:
    """A key counted at a new rate owes the same seconds it owed at the old one."""
    clock = [0.0]
    backend = MemoryBackend(clock=lambda: clock[0])

    asyncio.run(backend.hit("k", 2, 60))  # 30 seconds owed.
    clock[0] = 10
    faster = asyncio.run(backend.hit("k", 6, 60))  # 20 owed: two intervals.

    assert faster == Decision(allowed=True, remaining=3, reset_after=30, retry_after=0)


def test_memory_backend_gives_back_a_request_counted() -> None:
    """A refund undoes one hit exactly: the caller is as if it never came."""
    clock = [0.0]
    backend = MemoryBackend(clock=lambda: clock[0])

    def hit(key: str = "k") -> Decision:
        return asyncio.run(backend.hit(key, 2, 60))

    def refund(key: str = "k", limit: int = 2) -> None:
        asyncio.run(backend.refund(key, limit, 60))

    hit()
    hit()
    assert hit().allowed is False
    refund()
    clock[0] = 15  # Half an interval refilled since.
    assert hit() == Decision(allowed=True, remaining=0, reset_after=45, retry_after=0)
    refund()
    refund()  # Owing nothing, the caller is forgotten.
    assert "k" not in backend._callers
    refund()  # Nothing to give back.
    refund("unknown")
    assert hit() == Decision(allowed=True, remaining=1, reset_after=30, retry_after=0)
    refund(limit=4)  # At a new rate, a request owes half the seconds.
    assert backend._callers["k"].owed == 1  # 30 seconds owed is two at 4/minute.
    hit()  # At 2/minute again: half a request owed, then one more.
    clock[0] = 21  # Six seconds paid back: a fifth of a request.
    refund()
    assert backend._callers["k"].owed == pytest.approx(0.3)


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
    assert list(backend._callers) == ["a", "c"]
    assert hit("a").allowed is False
    assert hit("b").allowed is True
    assert list(backend._callers) == ["a", "b"]


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
    assert refused.headers["RateLimit-Limit"] == "1"
    assert int(refused.headers["Retry-After"]) > 0


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
            get_asgi_application(),
            raise_server_exceptions=False,
            headers={CALLER[0].decode(): CALLER[1].decode()},
        ) as client:
            yield client
    registry.clear()


@pytest.fixture
def idempotent(database_url: str) -> Iterator[TestClient]:
    """An app with the idempotency app and middleware on an empty database."""
    with idempotent_app({"default": database_url}) as client:
        yield client


type Freeze = Callable[[datetime], None]


@pytest.fixture
def freeze() -> Iterator[Freeze]:
    """Freeze the clock at a moment, on every database, until the test ends."""
    with ExitStack() as frozen:

        def at(when: datetime) -> None:
            frozen.enter_context(freeze_time(when))

        yield at


def stored_keys() -> list[dict[str, object]]:
    """The rows of the idempotency table."""

    async def read() -> list[dict[str, object]]:
        async with connections.engine().connect() as connection:
            rows = (await connection.execute(select(idempotency_keys))).all()
        await connections.close_all()
        return [dict(row._mapping) for row in rows]

    return asyncio.run(read())


def keyed(key: str) -> str:
    """The hash of ``key`` sent by the test client, as ``CALLER``."""
    scope = {"type": "http", "headers": [CALLER], "client": ("testclient", 50000)}
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


@pytest.mark.usefixtures("idempotent")
def test_headers_are_replayed_byte_for_byte() -> None:
    """Header values are Latin-1 bytes: one that is not UTF-8 is kept as sent.

    Storing it used to raise once the response had gone out, so the key was
    never answered. (The test client cannot read such a header: raw ASGI.)
    """

    async def created(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"location", b"/caf\xe9")]
        await send({"type": "http.response.start", "status": 201, "headers": headers})
        await send({"type": "http.response.body", "body": b"{}"})

    app = idempotency.IdempotencyMiddleware(created)

    async def post() -> list[Message]:
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"{}"}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/created",
            "headers": [(b"idempotency-key", b"latin"), CALLER],
            "client": ("203.0.113.1", 1),
        }
        await app(scope, receive, send)
        return sent

    async def twice() -> tuple[list[Message], list[Message]]:
        try:
            return await post(), await post()
        finally:
            await connections.close_all()

    first, again = asyncio.run(twice())

    assert first[0]["headers"] == [(b"location", b"/caf\xe9")]
    assert again[0]["headers"] == [
        (b"location", b"/caf\xe9"),
        (b"idempotent-replayed", b"true"),
    ]


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
        assert all(row["pinned"] for row in rows)


def test_a_failed_mark_still_keeps_the_key(
    make_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If marking a commit elsewhere fails, the request still never reruns."""

    def unreachable(operation: Operation) -> None:
        raise ConnectionError("The keys' database is away.")

    monkeypatch.setattr(idempotency_apps, "committed_values", unreachable)
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
    idempotent: TestClient, freeze: Freeze
) -> None:
    """A request that died before committing loses its key once its lease ends."""
    later = datetime.now(UTC) + timedelta(minutes=5)
    freeze(later)
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


def test_new_keys_are_leased(idempotent: TestClient, freeze: Freeze) -> None:
    """A claimed key is held for LEASE_SECONDS under a fresh random token."""
    now = datetime.now(UTC)
    freeze(now)
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
    idempotent: TestClient, freeze: Freeze
) -> None:
    """Another request is refused; a committed one is never run again."""
    later = datetime.now(UTC) + timedelta(minutes=5)
    freeze(later)
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
        ({"pinned": True}, False),
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


UNAVAILABLE_AFTER_500 = (
    "The request with this Idempotency-Key already ran (status 500), but its "
    "response was not kept; look up the result instead of retrying."
)


def test_commits_elsewhere_that_raise_keep_their_key(
    make_database: Callable[[str], str],
) -> None:
    """A key pinned for a commit elsewhere is never released: it may have landed.

    A COMMIT whose reply is lost after it landed and one the database refused
    raise alike, so neither runs again: retries learn the request ran.
    """
    databases = {"default": make_database("keys"), "other": make_database("other")}
    with idempotent_app(databases) as client:
        for outcome in ("unreported", "rejected"):
            headers = {"Idempotency-Key": outcome}
            path = f"/pay?outcome={outcome}&database=other"
            first = client.post(path, headers=headers)
            retry = client.post(path, headers=headers)

            assert first.status_code == 500, outcome
            assert (retry.status_code, retry.json()["code"]) == (
                409,
                "idempotency_response_unavailable",
            ), outcome
            assert retry.json()["detail"] == UNAVAILABLE_AFTER_500

        assert payments_on("other") == 1  # The commit whose reply was lost, once.
        assert [
            (row["status_code"], row["body"], row["pinned"], row["committed_at"])
            for row in stored_keys()
        ] == [(500, None, True, None)] * 2


def test_a_request_cancelled_while_committing_elsewhere_keeps_its_key(
    make_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled while it waits for the COMMIT's reply, it still never runs twice."""
    databases = {"default": make_database("keys"), "other": make_database("other")}
    committed = asyncio.Event()
    original = sample_routes.PaymentsUnitOfWork._commit

    async def commit_then_wait(self: sample_routes.PaymentsUnitOfWork) -> None:
        await original(self)  # The COMMIT lands...
        if not committed.is_set():  # ...and the first one's reply never comes.
            committed.set()
            await asyncio.Event().wait()

    async def pay(scope: Scope, receive: Receive, send: Send) -> None:
        async with sample_routes.PaymentsUnitOfWork(database="other") as uow:
            await uow.session.execute(insert(sample_routes.payments))
            await uow.commit()
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"paid"})

    async def scenario() -> list[Message]:
        middleware = idempotency.IdempotencyMiddleware(pay)
        request = asyncio.create_task(
            middleware(KEYED_POST, receive_empty_body, discard)
        )
        async with asyncio.timeout(30):
            await committed.wait()
        request.cancel()  # The client left, or a deadline passed, mid-COMMIT.
        with pytest.raises(asyncio.CancelledError):
            await request  # Once the key is settled.
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        await middleware(KEYED_POST, receive_empty_body, send)
        await connections.close_all()
        return sent

    with idempotent_app(databases):
        monkeypatch.setattr(
            sample_routes.PaymentsUnitOfWork, "_commit", commit_then_wait
        )
        start, body = asyncio.run(scenario())

        assert start["status"] == 409
        assert b'"code":"idempotency_response_unavailable"' in body["body"]
        assert payments_on("other") == 1
        [row] = stored_keys()
        assert (row["status_code"], row["body"], row["pinned"]) == (500, None, True)


def test_requests_that_fail_before_committing_elsewhere_release_their_key(
    make_database: Callable[[str], str],
) -> None:
    """Nothing was pinned, so nothing can have committed: the retry runs."""
    databases = {"default": make_database("keys"), "other": make_database("other")}
    with idempotent_app(databases) as client:
        headers = {"Idempotency-Key": "abandoned"}
        path = "/pay?outcome=abandoned&database=other"
        first = client.post(path, headers=headers)
        retry = client.post(path, headers=headers)

        assert (first.status_code, retry.status_code) == (500, 500)
        assert "Idempotent-Replayed" not in retry.headers
        assert payments_on("other") == 0
        assert stored_keys() == []


def test_commits_elsewhere_note_the_pin_before_they_commit(
    make_database: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operation knows its key is pinned while the COMMIT runs; not on the keys'."""
    databases = {"default": make_database("keys"), "other": make_database("other")}
    seen: dict[str, bool] = {}
    original = sample_routes.PaymentsUnitOfWork._commit

    async def commit(self: sample_routes.PaymentsUnitOfWork) -> None:
        operation = running_operation()
        assert operation is not None
        seen[self.alias] = operation.pinned
        await original(self)

    with idempotent_app(databases) as client:
        monkeypatch.setattr(sample_routes.PaymentsUnitOfWork, "_commit", commit)
        for database in ("other", "default"):
            client.post(
                f"/pay?database={database}", headers={"Idempotency-Key": database}
            )

    assert seen == {"other": True, "default": False}


def test_a_pinned_key_is_kept_whatever_the_request_knows(
    idempotent: TestClient,
) -> None:
    """The release refuses a key pinned in its row, or by its operation.

    A pin whose own reply was lost is in the row only; either one keeps the
    key, with the status and no body. A key neither pinned is released.
    """
    config = registry.settings(IdempotencySettings)
    for key, row_pinned, operation_pinned in (
        ("row", True, False),
        ("operation", False, True),
        ("neither", False, False),
    ):
        plant(key, pinned=row_pinned)
        operation = Operation(keyed(key), "first", pinned=operation_pinned)

        async def finish(operation: Operation = operation) -> None:
            await idempotency._finish(config, operation, {"status": 503}, None)
            await connections.close_all()

        asyncio.run(finish())

    rows = {row["key_hash"]: row for row in stored_keys()}
    assert set(rows) == {keyed("row"), keyed("operation")}
    assert all(
        (row["status_code"], row["body"]) == (503, None) for row in rows.values()
    )


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


@pytest.mark.parametrize("sweeping", [False, True], ids=["purged", "swept"])
def test_keys_live_until_the_instant_they_expire(
    idempotent: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    freeze: Freeze,
    sweeping: bool,
) -> None:
    """At ``expires_at`` a key still replays; it is expired only afterwards.

    Without a worker, the claim's sweep of expired keys leaves it too.
    """
    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "edge"})
    [row] = stored_keys()
    assert isinstance(row["expires_at"], datetime)
    freeze(row["expires_at"])
    if sweeping:
        monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")

    at_expiry = idempotent.post(
        "/counter", json={}, headers={"Idempotency-Key": "edge"}
    )

    assert at_expiry.headers["Idempotent-Replayed"] == "true"
    assert len(sample_routes.created) == 1
    assert len(stored_keys()) == 1  # Neither replaced nor swept.


# The keys' database decides when leases and keys run out (FIN-79) ----------------


def skew_host(monkeypatch: pytest.MonkeyPatch, by: timedelta) -> None:
    """Run this host's clock ``by`` ahead of the database's (behind if negative)."""
    real = clock.now
    monkeypatch.setattr(clock, "now", lambda: real() + by)


def test_a_host_running_ahead_never_takes_a_running_requests_key(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease with a minute left on the database's clock is still held.

    SQLite's clock is the host's, so there the host's view decides.
    """
    on_postgres = bool(os.environ.get(POSTGRES_VARIABLE))
    plant("running")  # Leased for a minute from now.
    skew_host(monkeypatch, timedelta(minutes=5))

    retry = idempotent.post("/pay", headers={"Idempotency-Key": "running"})

    assert retry.status_code == (409 if on_postgres else 201)
    assert payments_on() == (0 if on_postgres else 1)


def test_a_host_running_behind_takes_over_a_lease_that_ran_out(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it leases the key on the database's clock, not its own."""
    on_postgres = bool(os.environ.get(POSTGRES_VARIABLE))
    before = datetime.now(UTC)
    plant("dead", locked_until=before - timedelta(seconds=1))
    skew_host(monkeypatch, timedelta(minutes=-5))

    retry = idempotent.post("/pay", headers={"Idempotency-Key": "dead"})

    assert retry.status_code == (201 if on_postgres else 409)
    [row] = stored_keys()
    assert isinstance(row["locked_until"], datetime)
    assert (row["locked_until"] > before) is on_postgres


def test_a_host_running_ahead_never_runs_a_committed_key_again(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed key lives until it expires on the database's clock.

    Its retries are not run (here a 409: the planted key kept no response),
    and the purge leaves it.
    """
    from domainkit.contrib.idempotency.tasks import purge_expired

    on_postgres = bool(os.environ.get(POSTGRES_VARIABLE))
    now = datetime.now(UTC)
    for key in ("paid", "kept"):
        plant(key, committed_at=now, expires_at=now + timedelta(hours=1))
    skew_host(monkeypatch, timedelta(hours=2))

    retry = idempotent.post("/pay", headers={"Idempotency-Key": "paid"})

    assert retry.status_code == (409 if on_postgres else 201)  # Ran, or runs again.
    assert payments_on() == (0 if on_postgres else 1)

    async def purge() -> None:
        await purge_expired()
        await connections.close_all()

    asyncio.run(purge())
    assert (keyed("kept") in {row["key_hash"] for row in stored_keys()}) is on_postgres


def test_keyed_routes_that_outlast_the_lease_are_problems() -> None:
    """A route's own ``RequestTimeout`` at or past the lease, or none, is reported.

    Its router's dependencies count, and so do dependencies of dependencies
    and the routes of mounted applications. Routes no key reaches (a GET, a
    PUT outside ``METHODS``) and timeouts within the lease are fine.
    """

    def unbounded() -> None:
        """No deadline."""

    def slow(_: None = Depends(RequestTimeout(120))) -> None:
        """A dependency that moves the deadline itself."""

    outer = APIRouter()
    outer.post("/export", dependencies=[Depends(RequestTimeout(300))])(unbounded)
    outer.patch("/edit", dependencies=[Depends(RequestTimeout(60))])(unbounded)
    outer.post("/quick", dependencies=[Depends(RequestTimeout(59.5))])(unbounded)
    outer.post("/nested", dependencies=[Depends(slow)])(unbounded)
    outer.get("/report", dependencies=[Depends(RequestTimeout(None))])(unbounded)
    outer.put("/put", dependencies=[Depends(RequestTimeout(None))])(unbounded)
    outer.api_route(
        "/both",
        methods=["PATCH", "POST", "PUT"],
        dependencies=[Depends(RequestTimeout(None))],
    )(unbounded)
    inner = APIRouter(dependencies=[Depends(RequestTimeout(None))])
    inner.post("/batch")(unbounded)
    mounted = FastAPI()
    mounted.post("/upload", dependencies=[Depends(RequestTimeout(None))])(unbounded)
    hosted = FastAPI()
    hosted.post("/hook", dependencies=[Depends(RequestTimeout(90))])(unbounded)
    app = FastAPI()
    app.include_router(outer)
    app.include_router(inner, prefix="/v1")
    app.mount("/files", mounted)
    app.mount("/raw", headerless)  # An application without routes.
    app.host("hooks.example.com", hosted)

    with override_settings(INSTALLED_DOMAINS=["domainkit.contrib.idempotency", ALPHA]):
        setup()
        problems = registry.get_domain_config("idempotency").check_application(app)
    registry.clear()

    tail = (
        "but IDEMPOTENCY.LEASE_SECONDS is 60: a retry with the same Idempotency-Key "
        "could take the key over while it runs. Raise IDEMPOTENCY.LEASE_SECONDS "
        "above the route's timeout, or shorten the timeout."
    )
    assert problems == [
        f"POST /export may run for 300s (RequestTimeout), {tail}",
        f"PATCH /edit may run for 60s (RequestTimeout), {tail}",
        f"POST /nested may run for 120s (RequestTimeout), {tail}",
        f"PATCH POST /both may run without a deadline (RequestTimeout), {tail}",
        f"POST /v1/batch may run without a deadline (RequestTimeout), {tail}",
        f"POST /files/upload may run without a deadline (RequestTimeout), {tail}",
        f"POST /hook may run for 90s (RequestTimeout), {tail}",
    ]


@pytest.mark.parametrize(
    ("unbounded", "missing"),
    [
        ({"REQUEST_TIMEOUT": None}, "REQUEST_TIMEOUT is None"),
        (
            {
                "MIDDLEWARE": tuple(
                    m for m in DEFAULT_MIDDLEWARE if m != DEADLINE_MIDDLEWARE
                )
            },
            f"{DEADLINE_MIDDLEWARE} is not in MIDDLEWARE",
        ),
    ],
    ids=["no-timeout", "no-deadline-middleware"],
)
def test_keyed_requests_without_any_deadline_are_refused_where_deployed(
    unbounded: dict[str, object], missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing bounds them by the lease: the application refuses (SEC-r2-4).

    A warning in 0.9.1, as 0.9 promised a refusal for 0.10: now an error,
    which ``SILENCED_CHECKS`` cannot name (SEC-10b-8).
    ``IDEMPOTENCY.REQUIRE_DEADLINE = False`` keeps 0.9's warning. Outside
    staging and production, nothing.
    """

    def found(
        options: dict[str, object] | None = None, *, relaxed: bool = False
    ) -> tuple[list[str], list[str]]:
        chosen: dict[str, object] = {"REQUIRE_DEADLINE": False} if relaxed else {}
        registry.clear()
        try:
            with override_settings(
                INSTALLED_DOMAINS=["domainkit.contrib.idempotency", ALPHA],
                IDEMPOTENCY=chosen,
                **(options or {}),
            ):
                setup()  # Workers and commands start: only the application refuses.
                config = registry.get_domain_config("idempotency")
                return config.check_application(FastAPI()), config.deployment_warnings()
        finally:
            registry.clear()

    elsewhere = found(unbounded)
    monkeypatch.setattr(idempotency_apps, "DEPLOYED_ENVIRONMENTS", {"test"})
    message = (
        f"Requests with an Idempotency-Key run without a deadline ({missing}): one "
        "that runs past IDEMPOTENCY.LEASE_SECONDS (60s) can lose its key to a "
        "retry, which runs it again."
    )

    (refused,), warned = found(unbounded)
    relaxed, (only_warned,) = found(unbounded, relaxed=True)

    assert (refused, warned) == (message, [])
    assert getattr(refused, "id", None) == "idempotency.E002"
    assert "IDEMPOTENCY.REQUIRE_DEADLINE = False" in getattr(refused, "hint", "")
    assert (relaxed, only_warned) == ([], message)
    assert getattr(only_warned, "id", None) == "idempotency.W001"
    assert found() == ([], [])
    assert elsewhere == ([], [])


@pytest.mark.parametrize("anonymous", [None, "per-address", "refuse"])
def test_the_anonymous_policy_is_no_warning_where_deployed(
    anonymous: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ANONYMOUS``, set or not, warns nowhere (DATA-09).

    0.10 warned ``idempotency.W002`` in staging and production while it was
    left unset, as its default shared keys behind one address. Since 0.11
    the default refuses them, and ``"per-address"`` is a project's choice.
    """
    chosen = {} if anonymous is None else {"ANONYMOUS": anonymous}

    def warnings() -> list[str]:
        registry.clear()
        try:
            with override_settings(
                INSTALLED_DOMAINS=["domainkit.contrib.idempotency", ALPHA],
                IDEMPOTENCY=chosen,
            ):
                setup()
                return registry.get_domain_config("idempotency").deployment_warnings()
        finally:
            registry.clear()

    elsewhere = warnings()
    monkeypatch.setattr(idempotency_apps, "DEPLOYED_ENVIRONMENTS", {"test"})

    assert (elsewhere, warnings()) == ([], [])


def test_a_key_released_while_claimed_is_in_progress(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row gone between the failed insert and the read means: try again."""
    idempotent.post("/counter", json={}, headers={"Idempotency-Key": "gone"})

    built = clock_statements

    def reading_nothing(dialect: str) -> ClockStatements:
        statements = built(dialect)
        return replace(statements, read_held=statements.read_held.where(false()))

    monkeypatch.setattr(idempotency, "clock_statements", reading_nothing)
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
            "headers": [(b"idempotency-key", b"raw"), CALLER],
        }
        await idempotency.IdempotencyMiddleware(app)(scope, receive, send)
        await connections.close_all()
        return sent

    return asyncio.run(scenario())


KEYED_POST: Scope = {
    "type": "http",
    "method": "POST",
    "path": "/raw",
    "headers": [(b"idempotency-key", b"raw"), CALLER],
}


async def receive_empty_body() -> Message:
    """The body of a keyed POST: empty, in one message."""
    return {"type": "http.request"}


async def discard(message: Message) -> None:
    """Send nowhere."""


def collect(into: list[Message]) -> Send:
    """A ``send`` that keeps what it is sent in ``into``."""

    async def send(message: Message) -> None:
        into.append(message)

    return send


async def cancel_while_running(*, settle: bool = True) -> None:
    """Send a keyed POST, then cancel it as anyio does, mid-request.

    A cancelled cancel scope cancels every wait inside it until the scope
    exits (level-triggered), the cleanup's too. With ``settle``, wait for
    the key's own task to finish afterwards.
    """
    running = asyncio.Event()

    async def handler(scope: Scope, receive: Receive, send: Send) -> None:
        running.set()
        await asyncio.Event().wait()

    async with anyio.create_task_group() as group:
        group.start_soon(
            idempotency.IdempotencyMiddleware(handler),
            KEYED_POST,
            receive_empty_body,
            discard,
        )
        await running.wait()
        group.cancel_scope.cancel()
    if settle and (settling := owned()):
        await asyncio.wait(settling, timeout=5)


@pytest.mark.usefixtures("idempotent")
def test_a_request_cancelled_by_a_cancel_scope_releases_its_key() -> None:
    """Its key is free for a retry at once, not after the lease (R03)."""

    async def scenario() -> None:
        await cancel_while_running()
        await connections.close_all()

    asyncio.run(scenario())
    assert stored_keys() == []


@pytest.mark.parametrize("failure", ["raises", "hangs"])
@pytest.mark.usefixtures("idempotent")
def test_a_key_a_cancelled_request_could_not_settle_is_logged(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No one is left to raise to, so the failure is logged; the lease applies."""

    async def finish(*args: object) -> None:
        await asyncio.sleep(0)
        if failure == "raises":
            raise RuntimeError("database gone")
        await asyncio.Event().wait()

    monkeypatch.setattr(idempotency, "_finish", finish)
    monkeypatch.setattr(idempotency, "SETTLE_TIMEOUT", 0.01)

    async def scenario() -> None:
        await cancel_while_running()
        await asyncio.sleep(0)  # Done callbacks run on the next loop pass.
        await connections.close_all()

    with caplog.at_level(logging.ERROR, "domainkit.contrib.idempotency"):
        asyncio.run(scenario())

    (record,) = caplog.records
    assert record.getMessage() == (
        "Could not store the response of a cancelled request or release its "
        "Idempotency-Key; the key is held until its lease runs out"
    )
    assert record.exc_info
    expected = RuntimeError if failure == "raises" else TimeoutError
    assert type(record.exc_info[1]) is expected
    assert len(stored_keys()) == 1  # Held until its lease runs out.


@pytest.mark.usefixtures("idempotent")
def test_a_key_left_settling_at_shutdown_is_stopped_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Shutdown cancels the settling task like any other, within its budget."""

    async def finish(*args: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(idempotency, "_finish", finish)

    async def scenario() -> list[str]:
        await cancel_while_running(settle=False)
        (settling,) = owned()
        assert settling.get_name() == "domainkit.idempotency.settle"
        stuck = await shutdown(asyncio.get_running_loop().time() + 5)
        await asyncio.sleep(0)
        await connections.close_all()
        return stuck

    with caplog.at_level(logging.ERROR, "domainkit.contrib.idempotency"):
        assert asyncio.run(scenario()) == []
    assert caplog.records == []


@postgres_only
def test_claims_skip_the_expired_keys_another_claim_is_purging(
    idempotent: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a worker, concurrent claims do not queue on one purge (N14)."""
    gone = datetime.now(UTC) - timedelta(seconds=1)
    for key in ("old-1", "old-2"):
        plant(key, expires_at=gone)
    monkeypatch.setattr(idempotency, "MESSAGING", "not.installed")

    async def created(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def scenario() -> list[Message]:
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        async with connections.engine().connect() as purging:
            await purging.begin()
            await purging.execute(
                select(idempotency_keys)
                .where(idempotency_keys.c.expires_at < datetime.now(UTC))
                .with_for_update()
            )
            async with asyncio.timeout(5):  # Without SKIP LOCKED: waits for ever.
                await idempotency.IdempotencyMiddleware(created)(
                    KEYED_POST, receive_empty_body, send
                )
            await purging.rollback()
        await connections.close_all()
        return sent

    assert asyncio.run(scenario())[0]["status"] == 201
    assert len(stored_keys()) == 3  # The two expired keys, locked, and "raw".


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


# What runs after the response (SEC-r2-1) --------------------------------------


PAY_SCOPE: Scope = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": "POST",
    "scheme": "http",
    "path": "/pay",
    "raw_path": b"/pay",
    "root_path": "",
    "query_string": b"",
    "headers": [(b"idempotency-key", b"order-42"), CALLER],
    "client": ("testclient", 50000),
    "server": ("testserver", 80),
}
"""A keyed ``POST /pay``, as a server hands it to the application."""


async def record_payment() -> None:
    """Commit a payment through a unit of work, as a keyed request's work does."""
    async with sample_routes.PaymentsUnitOfWork() as uow:
        await uow.session.execute(insert(sample_routes.payments))
        await uow.commit()


class AfterResponse:
    """A keyed ``POST /pay`` whose provider call runs after the response.

    The first call waits until ``release``; later ones return at once, so
    a second run shows instead of hanging.
    """

    def __init__(
        self,
        *,
        status_code: int = 202,
        fails: bool = False,
        pays: bool = False,
        records: bool = False,
    ) -> None:
        """Answer ``status_code``; with ``fails``, the provider call raises.

        With ``pays``, the handler commits a payment and answers
        ``Cache-Control: no-store``; with ``records``, the provider call
        commits one.
        """
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fails = fails
        self.records = records
        api = FastAPI()

        @api.post("/pay", status_code=status_code)
        async def pay(background: BackgroundTasks, response: Response) -> object:
            background.add_task(self.call_provider)
            if pays:
                await record_payment()
                response.headers["Cache-Control"] = "no-store"
            return {"status": "accepted"}

        self.app = idempotency.IdempotencyMiddleware(api)

    async def call_provider(self) -> None:
        """Charge: once the response is sent, with no deadline left."""
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        if self.records:
            await record_payment()
        if self.fails:
            raise RuntimeError("the provider's answer was lost")

    async def post(self) -> list[Message]:
        """Send ``POST /pay`` with the key ``order-42``; what was sent back."""
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        await self.app(PAY_SCOPE, receive_empty_body, send)
        return sent


@pytest.mark.usefixtures("idempotent")
def test_work_after_a_keyed_response_runs_once_however_long_it_takes() -> None:
    """The key keeps the response before ``BackgroundTasks`` run.

    The deadline is lifted once the response starts, so the provider call
    may outlive the lease; a retry then gets the stored response instead
    of taking the key over and charging again.
    """
    after = AfterResponse()

    async def scenario() -> tuple[list[dict[str, object]], list[Message]]:
        with freeze_time() as frozen:
            first = asyncio.create_task(after.post())
            await after.started.wait()
            during = await asyncio.to_thread(stored_keys)
            frozen.tick(IdempotencySettings().LEASE_SECONDS + 1)
            retried = await after.post()
            after.release.set()
            await first
        await connections.close_all()
        return during, retried

    during, retried = asyncio.run(scenario())

    assert after.calls == 1
    assert [row["status_code"] for row in during] == [202]
    assert retried[0]["status"] == 202
    assert (b"idempotent-replayed", b"true") in retried[0]["headers"]


@pytest.mark.usefixtures("idempotent")
def test_work_that_fails_after_a_kept_response_releases_the_key() -> None:
    """The stored 202 was stored too soon: with nothing committed, a retry runs.

    The response no longer says what happened, so the key is released as
    for any request that raised, and the work that failed runs again
    instead of being replayed as accepted (SEC-r2-7).
    """
    after = AfterResponse(fails=True)
    after.release.set()

    async def scenario() -> list[Message]:
        with pytest.raises(RuntimeError, match="answer was lost"):
            await after.post()
        assert await asyncio.to_thread(stored_keys) == []
        retried: list[Message] = []
        with pytest.raises(RuntimeError, match="answer was lost"):
            await after.app(PAY_SCOPE, receive_empty_body, collect(retried))
        await connections.close_all()
        return retried

    retried = asyncio.run(scenario())

    assert (b"idempotent-replayed", b"true") not in retried[0]["headers"]
    assert after.calls == 2
    assert stored_keys() == []


@pytest.mark.usefixtures("idempotent")
def test_work_that_commits_then_fails_after_the_response_keeps_its_status() -> None:
    """Something committed: the key keeps the status, not the response.

    A retry gets 409 ``idempotency_response_unavailable`` and runs nothing:
    the commit may be all the work did, so the client looks it up.
    """
    after = AfterResponse(fails=True, records=True)
    after.release.set()

    async def scenario() -> list[Message]:
        with pytest.raises(RuntimeError, match="answer was lost"):
            await after.post()
        retried = await after.post()
        await connections.close_all()
        return retried

    retried = asyncio.run(scenario())

    assert retried[0]["status"] == 409
    assert b"idempotency_response_unavailable" in retried[-1]["body"]
    assert (after.calls, payments_on()) == (1, 1)
    assert [(row["status_code"], row["body"]) for row in stored_keys()] == [(202, None)]


@pytest.mark.usefixtures("idempotent")
def test_a_key_to_release_waits_for_the_application_to_return() -> None:
    """A refusal is settled once the work after it ends, as before.

    Such a key is released, unless something after the response
    committed: the release must see that commit, so it waits.
    """
    after = AfterResponse(status_code=402)

    async def scenario() -> tuple[list[dict[str, object]], list[Message]]:
        first = asyncio.create_task(after.post())
        await after.started.wait()
        during = await asyncio.to_thread(stored_keys)
        after.release.set()
        await first
        retried = await after.post()
        await connections.close_all()
        return during, retried

    during, retried = asyncio.run(scenario())

    assert [row["status_code"] for row in during] == [None]  # Still held.
    assert stored_keys() == []  # Released once it returned: nothing committed.
    assert retried[0]["status"] == 402
    assert after.calls == 2


@pytest.mark.usefixtures("idempotent")
def test_a_response_the_key_could_not_keep_stops_the_work_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store fails: the last part is not sent, and the application stops.

    The client gets no whole answer the key cannot stand behind, so it
    retries; work that ran with the key unsettled could run again for that
    retry; and the failed store is not tried again as the error leaves.
    """
    attempts: list[object] = []

    async def unavailable(*args: object) -> None:
        attempts.append(args[-1])
        raise ConnectionError("the keys' database is down")

    monkeypatch.setattr(idempotency, "_finish", unavailable)
    after = AfterResponse()
    after.release.set()

    sent: list[Message] = []

    async def scenario() -> None:
        with pytest.raises(ConnectionError, match="keys' database is down"):
            await after.app(PAY_SCOPE, receive_empty_body, collect(sent))
        await connections.close_all()

    asyncio.run(scenario())

    assert [message["type"] for message in sent] == ["http.response.start"]
    assert after.calls == 0
    assert attempts == [b'{"status":"accepted"}']  # Not settled a second time.


@pytest.mark.usefixtures("idempotent")
@pytest.mark.parametrize("status_code", [201, 399])
def test_a_client_holding_the_whole_response_finds_it_stored(status_code: int) -> None:
    """The key is stored before the last part of the response goes out.

    A client that retries the moment it has the whole response (here, from
    inside ``send``, before the last part's ``send`` returns) gets the
    replay, never a 409 for a key still in progress.
    """
    api = FastAPI()

    @api.post("/pay", status_code=status_code)
    async def pay() -> dict[str, str]:
        return {"status": "paid"}

    keyed = idempotency.IdempotencyMiddleware(api)
    retried: list[Message] = []

    async def send(message: Message) -> None:
        last = message["type"] == "http.response.body" and not message.get(
            "more_body", False
        )
        if last and not retried:
            await keyed(PAY_SCOPE, receive_empty_body, collect(retried))

    async def scenario() -> None:
        await keyed(PAY_SCOPE, receive_empty_body, send)
        await connections.close_all()

    asyncio.run(scenario())

    assert retried[0]["status"] == status_code
    assert (b"idempotent-replayed", b"true") in retried[0]["headers"]
    assert retried[-1]["body"] == b'{"status":"paid"}'


@pytest.mark.usefixtures("idempotent")
def test_a_kept_status_is_settled_before_the_work_after_it() -> None:
    """A committed response the key cannot keep (``no-store``) keeps its status.

    It is settled before the work after it runs, so a retry meanwhile gets
    a 409 ``idempotency_response_unavailable`` rather than running again.
    """
    after = AfterResponse(status_code=201, pays=True)

    async def scenario() -> tuple[list[dict[str, object]], list[Message]]:
        first = asyncio.create_task(after.post())
        await after.started.wait()
        during = await asyncio.to_thread(stored_keys)
        retried = await after.post()
        after.release.set()
        await first
        await connections.close_all()
        return during, retried

    during, retried = asyncio.run(scenario())

    assert [(row["status_code"], row["body"]) for row in during] == [(201, None)]
    assert retried[0]["status"] == 409
    assert b"idempotency_response_unavailable" in retried[-1]["body"]
    assert (after.calls, payments_on()) == (1, 1)


@pytest.mark.usefixtures("idempotent")
def test_a_commit_after_a_refusal_keeps_the_refusal() -> None:
    """A 402 then a commit in the work after it: the key keeps the 402.

    The release waits for the application to return, and sees the commit.
    """
    after = AfterResponse(status_code=402, records=True)
    after.release.set()

    async def scenario() -> list[Message]:
        await after.post()
        retried = await after.post()
        await connections.close_all()
        return retried

    retried = asyncio.run(scenario())

    assert retried[0]["status"] == 402
    assert (b"idempotent-replayed", b"true") in retried[0]["headers"]
    assert retried[-1]["body"] == b'{"status":"accepted"}'
    assert (after.calls, payments_on()) == (1, 1)


# Work queued by a keyed request (SEC-r2-9) ------------------------------------


@task(name="tests.idempotency.ship")
async def ship(order: str) -> None:
    """Ship an order: work a keyed request queues with ``enqueue``."""


async def queued() -> int:
    """How many messages the outbox holds."""
    async with connections.engine().connect() as connection:
        count = await connection.scalar(select(func.count()).select_from(messages))
    return int(count or 0)


async def create_outbox() -> None:
    """The outbox table, beside the keys' and the payments'."""
    async with connections.engine().begin() as connection:
        await connection.run_sync(messages.create)


@pytest.mark.usefixtures("idempotent")
def test_work_queued_by_a_keyed_request_is_queued_once() -> None:
    """``enqueue`` commits on its own: that commit holds the key as a unit of work's.

    The request then fails after its 202 (a background task raises): the
    key keeps its status, so a retry gets a 409 instead of queueing the
    task a second time.
    """
    api = FastAPI()

    async def receipt() -> None:
        raise RuntimeError("the mail server is down")

    @api.post("/pay", status_code=202)
    async def pay(background: BackgroundTasks) -> dict[str, str]:
        await enqueue(ship, order="42")
        background.add_task(receipt)
        return {"status": "accepted"}

    keyed = idempotency.IdempotencyMiddleware(api)

    async def scenario() -> tuple[list[Message], int]:
        await create_outbox()
        with pytest.raises(RuntimeError, match="mail server is down"):
            await keyed(PAY_SCOPE, receive_empty_body, discard)
        retried: list[Message] = []
        await keyed(PAY_SCOPE, receive_empty_body, collect(retried))
        count = await queued()
        await connections.close_all()
        return retried, count

    retried, count = asyncio.run(scenario())

    assert count == 1
    assert retried[0]["status"] == 409
    assert b"idempotency_response_unavailable" in retried[-1]["body"]
    [row] = stored_keys()
    assert (row["status_code"], row["committed_at"] is not None) == (202, True)


@pytest.mark.usefixtures("idempotent")
def test_work_queued_after_the_key_was_taken_over_is_not_queued() -> None:
    """A request that lost its key cannot queue work: its transaction rolls back."""
    plant("order-42", token="first")

    async def scenario() -> int:
        await create_outbox()
        with (
            idempotency_operations.run_operation(Operation(keyed("order-42"), "lost")),
            pytest.raises(idempotency_operations.KeyTakenOver),
        ):
            await enqueue(ship, order="42")
        count = await queued()
        await connections.close_all()
        return count

    assert asyncio.run(scenario()) == 0


@pytest.mark.usefixtures("idempotent")
def test_work_queued_on_another_database_pins_the_key_then_marks_it() -> None:
    """Elsewhere than the keys' database, the key is pinned, then marked.

    As for a unit of work there: pinned before the commit, whatever it
    reports, and marked committed after it.
    """
    plant("order-42", token="first")
    operation = Operation(keyed("order-42"), "first")
    config = registry.get_domain_config("idempotency")
    assert isinstance(config, idempotency_apps.IdempotencyConfig)

    async def scenario() -> list[dict[str, object]]:
        with idempotency_operations.run_operation(operation):
            async with connections.engine().connect() as connection:
                await config.hold_key_in_transaction(connection, "other")
            pinned = await asyncio.to_thread(stored_keys)
            await config.note_transaction_commit("other")
        await connections.close_all()
        return pinned

    pinned = asyncio.run(scenario())

    assert (operation.pinned, operation.committed) == (True, True)
    assert [(row["pinned"], row["committed_at"]) for row in pinned] == [(True, None)]
    [row] = stored_keys()
    assert row["committed_at"] is not None


@pytest.mark.usefixtures("idempotent")
def test_a_commit_on_the_keys_database_is_marked_in_it_not_after() -> None:
    """There the mark joined the transaction: nothing is written after it."""
    plant("order-42", token="first")
    operation = Operation(keyed("order-42"), "first")
    config = registry.get_domain_config("idempotency")
    assert isinstance(config, idempotency_apps.IdempotencyConfig)

    async def scenario() -> None:
        with idempotency_operations.run_operation(operation):
            await config.note_transaction_commit("default")
        await connections.close_all()

    asyncio.run(scenario())

    assert operation.committed
    assert [row["committed_at"] for row in stored_keys()] == [None]


@pytest.mark.usefixtures("idempotent")
def test_a_failed_follow_up_after_enqueue_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The task is queued: what follows its commit cannot take it back."""
    told: list[str] = []
    reports: list[tuple[BaseException, dict[str, object]]] = []
    away = ConnectionError("the keys' database is away")

    async def unreachable(self: object, database: str) -> None:
        told.append(database)
        raise away

    def report(exc: BaseException, **context: object) -> None:
        reports.append((exc, context))

    monkeypatch.setattr(idempotency_apps.IdempotencyConfig, "_note_commit", unreachable)
    monkeypatch.setattr(messaging_outbox, "report_error", report)

    async def scenario() -> int:
        await create_outbox()
        await enqueue(ship, order="42")
        count = await queued()
        await connections.close_all()
        return count

    with caplog.at_level(logging.ERROR, "domainkit.contrib.messaging"):
        assert asyncio.run(scenario()) == 1

    assert caplog.messages == ["After-commit work of 'idempotency' failed"]
    assert told == ["default"]
    assert reports == [(away, {"domain": "idempotency"})]


# Bodies sent by path or file descriptor (SEC-r2-8) ---------------------------


def test_a_file_sent_by_path_is_run_again_not_replayed_empty(
    idempotent: TestClient, tmp_path: Path
) -> None:
    """``FileResponse`` with ``http.response.pathsend``: the body is never seen.

    The key cannot keep it, so it is released, and the retry gets the file.
    """
    export = tmp_path / "export.csv"
    export.write_bytes(b"id,amount\n1,10.00\n")
    api = FastAPI()

    @api.post("/pay")
    async def pay() -> FileResponse:
        return FileResponse(export)

    app = idempotency.IdempotencyMiddleware(api)
    # ASGI 2.4: the file is sent without listening for a disconnect meanwhile.
    by_body = {**PAY_SCOPE, "asgi": {"version": "3.0", "spec_version": "2.4"}}
    by_path = {**by_body, "extensions": {"http.response.pathsend": {}}}

    async def scenario() -> tuple[
        list[Message], list[dict[str, object]], list[Message]
    ]:
        first: list[Message] = []
        retried: list[Message] = []
        await app(by_path, receive_empty_body, collect(first))
        after_first = await asyncio.to_thread(stored_keys)
        await app(by_body, receive_empty_body, collect(retried))
        await connections.close_all()
        return first, after_first, retried

    first, after_first, retried = asyncio.run(scenario())

    assert [message["type"] for message in first][-1] == "http.response.pathsend"
    assert after_first == []
    assert (b"idempotent-replayed", b"true") not in retried[0]["headers"]
    assert b"".join(m.get("body", b"") for m in retried) == export.read_bytes()


class UnseenBody:
    """An application that sends its body as ``kind`` (by path, or by descriptor)."""

    def __init__(self, kind: str, *, commits: bool) -> None:
        """Send ``kind``; with ``commits``, commit a payment first."""
        self.kind = kind
        self.commits = commits
        self.calls = 0
        self.app = idempotency.IdempotencyMiddleware(self.serve)

    async def serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 200, its body sent past the middleware."""
        self.calls += 1
        if self.commits:
            await record_payment()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": self.kind, "path": "/srv/export.csv", "more_body": False})

    def post_twice(self) -> list[Message]:
        """The keyed POST, then its retry; what the retry was sent."""

        async def scenario() -> list[Message]:
            retried: list[Message] = []
            await self.app(PAY_SCOPE, receive_empty_body, discard)
            await self.app(PAY_SCOPE, receive_empty_body, collect(retried))
            await connections.close_all()
            return retried

        return asyncio.run(scenario())


@pytest.mark.usefixtures("idempotent")
@pytest.mark.parametrize("kind", sorted(idempotency.UNSEEN_BODIES))
def test_a_body_the_middleware_never_sees_is_not_replayed(kind: str) -> None:
    """Nothing committed: the key is released, and the retry runs again."""
    unseen = UnseenBody(kind, commits=False)

    retried = unseen.post_twice()

    assert retried[-1]["type"] == kind
    assert unseen.calls == 2
    assert stored_keys() == []


@pytest.mark.usefixtures("idempotent")
@pytest.mark.parametrize("kind", sorted(idempotency.UNSEEN_BODIES))
def test_a_body_never_seen_after_a_commit_keeps_only_the_status(kind: str) -> None:
    """Committed: the key keeps the status, and the retry gets a 409."""
    unseen = UnseenBody(kind, commits=True)

    retried = unseen.post_twice()

    assert retried[0]["status"] == 409
    assert unseen.calls == 1
    assert [(row["status_code"], row["body"]) for row in stored_keys()] == [(200, None)]
