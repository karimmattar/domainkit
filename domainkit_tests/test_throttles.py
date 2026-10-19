"""Per-route throttles (``Throttle``), apart from the global ``RATE_LIMIT``."""

import asyncio
import hashlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Final

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import State
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.ratelimit import (
    TOO_MANY_REQUESTS,
    Decision,
    MemoryBackend,
    Throttle,
    Throttled,
)

MIDDLEWARE: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit_tests.sample_platform_routes.ActorFromHeader",
]


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of an application with the throttled sample routes."""
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_platform_routes.router",
        MIDDLEWARE=MIDDLEWARE,
    ):
        yield TestClient(get_asgi_application())


def test_routes_are_throttled_per_caller(client: TestClient) -> None:
    """Past the rate, a route answers 429 with when to come back; others pass."""
    first, second, refused = (client.get("/throttled") for _ in range(3))
    other = client.get("/unthrottled")

    assert [first.status_code, second.status_code, other.status_code] == [200] * 3
    assert refused.status_code == 429
    assert refused.json()["code"] == "throttled"
    assert refused.json()["detail"] == TOO_MANY_REQUESTS
    assert refused.json()["request_id"] == refused.headers["X-Request-ID"]
    # One request refills in 30 seconds; the burst of two after 60.
    assert refused.headers["Retry-After"] in {"29", "30"}
    assert refused.headers["RateLimit-Limit"] == "2"
    assert refused.headers["RateLimit-Remaining"] == "0"
    assert refused.headers["RateLimit-Reset"] in {"59", "60"}
    assert "RateLimit-Limit" not in first.headers  # Only the global limit adds them.


def test_users_are_counted_apart_from_their_address(client: TestClient) -> None:
    """``key="user"``: each actor has a count; anonymous callers, their address."""
    alice = {"X-Test-Actor": "alice"}
    statuses = [client.get("/throttled", headers=alice).status_code for _ in range(3)]
    anonymous = [client.get("/throttled").status_code for _ in range(3)]
    bob = client.get("/throttled", headers={"X-Test-Actor": "bob"})

    assert statuses == anonymous == [200, 200, 429]
    assert bob.status_code == 200


def test_address_throttles_count_users_by_address(client: TestClient) -> None:
    """``key="address"``: users share their address's count."""
    first = client.get("/by-address", headers={"X-Test-Actor": "alice"})
    second = client.get("/by-address", headers={"X-Test-Actor": "bob"})
    elsewhere = TestClient(client.app, client=("203.0.113.9", 1)).get("/by-address")

    assert [first.status_code, second.status_code] == [200, 429]
    assert elsewhere.status_code == 200


@pytest.mark.parametrize(
    ("path", "header"), [("/by-key", "X-API-Key"), ("/by-tenant", "X-Tenant")]
)
def test_functions_name_the_caller(client: TestClient, path: str, header: str) -> None:
    """A key function, sync or async, counts by what it returns; None, by address."""
    first = client.get(path, headers={header: "one"})
    again = client.get(path, headers={header: "one"})
    other = client.get(path, headers={header: "two"})
    anonymous = client.get(path)
    anonymous_again = client.get(path)

    assert [first.status_code, again.status_code, other.status_code] == [200, 429, 200]
    assert [anonymous.status_code, anonymous_again.status_code] == [200, 429]


def test_a_scope_shares_one_count(client: TestClient) -> None:
    """Routes with the same scope draw from one count."""
    assert client.get("/exports/a").status_code == 200
    assert client.get("/exports/b").status_code == 429


def test_each_application_has_its_own_counts() -> None:
    """The default backend belongs to the application, made on first use."""
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_platform_routes.router",
        MIDDLEWARE=MIDDLEWARE,
    ):
        first, second = (
            TestClient(get_asgi_application()),
            TestClient(get_asgi_application()),
        )
        statuses = [first.get("/by-address").status_code for _ in range(2)]
        other = second.get("/by-address").status_code

    assert statuses == [200, 429]
    assert other == 200
    assert isinstance(first.app.state.throttle_backend, MemoryBackend)  # type: ignore[attr-defined]


def test_throttled_websockets_close_with_try_again_later(client: TestClient) -> None:
    """A handshake over the rate is closed with 1013 before it is accepted."""
    with client.websocket_connect("/throttled-ws") as socket:
        assert socket.receive_text() == "hi"
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/throttled-ws"),
    ):
        pass

    assert refused.value.code == 1013


def connection(
    *,
    client: tuple[str, int] | None = ("203.0.113.5", 1),
    endpoint: object | None = None,
    kind: str = "http",
) -> HTTPConnection:
    """A request on its own, for calling a throttle directly."""
    scope: dict[str, object] = {
        "type": kind,
        "headers": [],
        "app": SimpleNamespace(state=State()),
        "client": client,
    }
    if endpoint is not None:
        scope["endpoint"] = endpoint
    return HTTPConnection(scope)


class Recorder:
    """A backend that remembers the keys it counted, and refuses after one."""

    def __init__(self) -> None:
        """Count nothing yet."""
        self.keys: list[str] = []

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        """Allow the first hit of each key only."""
        allowed = key not in self.keys
        self.keys.append(key)
        return Decision(allowed, 0, period, 0.0 if allowed else 1.5)


def view() -> None:
    """An endpoint to throttle."""


def test_keys_name_the_scope_and_the_caller() -> None:
    """Keys are ``throttle:<scope>:<caller>``; custom keys are hashed."""
    recorder = Recorder()
    digest = hashlib.sha256(b"secret-key").hexdigest()[:32]

    async def run() -> None:
        await Throttle("1/minute", backend=recorder)(connection(endpoint=view))
        await Throttle("1/minute", key="address", scope="s", backend=recorder)(
            connection(client=None)
        )
        with bind_request(RequestContext("r", None, "GET", "/", actor="alice")):
            await Throttle("1/minute", scope="s", backend=recorder)(connection())
            await Throttle("1/minute", key="address", scope="s", backend=recorder)(
                connection()
            )
        await Throttle(
            "1/minute", key=lambda c: "secret-key", scope="s", backend=recorder
        )(connection())

    asyncio.run(run())

    assert recorder.keys == [
        f"throttle:{__name__}.view:ip:203.0.113.5",
        "throttle:s:ip:unknown",
        "throttle:s:actor:alice",
        "throttle:s:ip:203.0.113.5",
        f"throttle:s:key:{digest}",
    ]


def test_refusals_carry_the_backend_decision() -> None:
    """The 429 says when to retry, rounded up; a WebSocket gets 1013."""

    async def twice(kind: str) -> BaseException:
        throttle = Throttle("3/hour", scope="s", backend=Recorder())
        await throttle(connection(kind=kind))
        with pytest.raises((Throttled, WebSocketException)) as refused:
            await throttle(connection(kind=kind))
        return refused.value

    refused = asyncio.run(twice("http"))
    assert isinstance(refused, Throttled)
    assert refused.status_code == 429
    assert refused.headers == {
        "RateLimit-Limit": "3",
        "RateLimit-Remaining": "0",
        "RateLimit-Reset": "3600",
        "Retry-After": "2",
    }
    socket = asyncio.run(twice("websocket"))
    assert isinstance(socket, WebSocketException)
    assert socket.code == 1013


def test_throttles_are_checked_when_made() -> None:
    """Bad rates, keys, and scopes fail at import time; a scope or a route is needed."""
    with pytest.raises(ValueError, match="must look like"):
        Throttle("lots")
    with pytest.raises(ValueError, match="'user', 'address', or a function"):
        Throttle("1/minute", key="tenant")  # type: ignore[arg-type]
    for scope in ("", "a b", "a:b", "x" * 129):
        with pytest.raises(ValueError, match="128 letters"):
            Throttle("1/minute", scope=scope)
    assert Throttle("1/minute", scope="A-z_0.9" + "x" * 121).scope
    with pytest.raises(
        ImproperlyConfigured, match=r"^Throttle without a scope must guard a route\.$"
    ):
        asyncio.run(Throttle("1/minute", backend=Recorder())(connection()))
