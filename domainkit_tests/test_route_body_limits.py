"""Routes' own body limits (``body_limit``), above or below the global one."""

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Final

import pytest
from fastapi import APIRouter, Request, UploadFile
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.http import get_asgi_application
from domainkit.http.access import Public
from domainkit.http.middleware import (
    MAX_ROUTE_BODY_SIZE,
    RequestBodyLimitMiddleware,
    body_limit,
    largest_body_limit,
    route_body_limit,
)
from domainkit.log import SECURITY_LOGGER

UPLOAD_LIMIT: Final = 100_000
LOGIN_LIMIT: Final = 64
CHUNK: Final = 65_536
"""What a server hands the application at a time, as uvicorn does."""
BOUNDARY: Final = "domainkit-test-boundary"

router = APIRouter(dependencies=[Public])


@router.post("/avatars")
@body_limit(UPLOAD_LIMIT)
async def upload(file: UploadFile) -> dict[str, int]:
    """Take one file: the route's limit is above the global one, or below it."""
    return {"size": len(await file.read())}


@body_limit(LOGIN_LIMIT)  # Above the route's decorator: it marks the same function.
@router.post("/login")
async def login(request: Request) -> dict[str, int]:
    """Take a small body only."""
    return {"size": len(await request.body())}


@router.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    """Take a body under the global limit."""
    return {"size": len(await request.body())}


@router.post("/ignore")
async def ignore() -> dict[str, bool]:
    """Never read the body."""
    return {"read": False}


nested = APIRouter(dependencies=[Public])
nested.include_router(router, prefix="/inner")

ROUTES: Final = "domainkit_tests.test_route_body_limits.router"
NESTED: Final = "domainkit_tests.test_route_body_limits.nested"
LIMITS: Final = ("domainkit.http.middleware.RequestBodyLimitMiddleware",)


class ReadsFirst:
    """Middleware that reads the whole body before routing, as idempotency does."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read every body message, then replay them to the application."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        messages = [await receive()]
        while messages[-1].get("more_body"):
            messages.append(await receive())
        messages.reverse()

        async def replay() -> Message:
            return messages.pop() if messages else await receive()

        await self.app(scope, replay, send)


type MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client() -> Iterator[MakeClient]:
    """Build a client of these routes, with setting overrides."""
    with ExitStack() as stack:

        def make(**overrides: object) -> TestClient:
            values: dict[str, object] = {
                "ROOT_ROUTER": ROUTES,
                "MIDDLEWARE": LIMITS,
                **overrides,
            }
            stack.enter_context(override_settings(**values))
            return stack.enter_context(TestClient(get_asgi_application()))

        yield make


def multipart(size: int) -> Iterator[bytes]:
    """A form with one file of ``size`` bytes, in chunks of ``CHUNK``."""
    yield (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
    ).encode()
    sent = 0
    while sent < size:
        part = min(CHUNK, size - sent)
        yield b"\0" * part
        sent += part
    yield f"\r\n--{BOUNDARY}--\r\n".encode()


async def post(
    app: ASGIApp, path: str, chunks: Iterator[bytes], *, length: int | None = None
) -> tuple[int, int]:
    """POST ``chunks`` to ``app`` as a server would; the status, and bytes read."""
    headers = [
        (b"host", b"testserver"),
        (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
    ]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.9", 4711),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    pending.reverse()
    read = 0
    statuses: list[int] = []

    async def receive() -> Message:
        nonlocal read
        if not pending:
            return {"type": "http.disconnect"}
        body = pending.pop()
        read += len(body)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    await app(scope, receive, send)
    return statuses[0], read


@pytest.mark.anyio
async def test_an_upload_over_its_routes_limit_stops_being_read_at_the_limit() -> None:
    """20 MiB to a 100 kB route: 413 after the limit and one chunk, not 20 MiB."""
    size = 20 * 2**20
    with override_settings(
        ROOT_ROUTER=ROUTES, MIDDLEWARE=LIMITS, MAX_REQUEST_BODY_SIZE=50 * 2**20
    ):
        app = get_asgi_application()
        streamed = await post(app, "/avatars", multipart(size))
        declared = await post(app, "/avatars", multipart(size), length=size + 200)
        within = await post(app, "/avatars", multipart(UPLOAD_LIMIT // 2))

    assert streamed[0] == 413
    assert UPLOAD_LIMIT < streamed[1] <= UPLOAD_LIMIT + CHUNK
    assert declared == (413, 0)  # Refused by its length, before a byte is read.
    assert within[0] == 200


def test_a_route_takes_more_than_the_global_limit(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The upload route takes 50 kB; every other route stays at the global 1 kB."""
    client = make_client(MAX_REQUEST_BODY_SIZE=1_000)
    form = {"file": ("a.png", b"\0" * 50_000, "image/png")}

    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        uploaded = client.post("/avatars", files=form)
        echoed = client.post("/echo", content=b"x" * 50_000)
        within = client.post("/echo", content=b"x" * 1_000)
        beyond = client.post("/ignore", content=b"x" * (UPLOAD_LIMIT + 1))

    assert (uploaded.status_code, uploaded.json()) == (200, {"size": 50_000})
    assert (echoed.status_code, echoed.json()["code"]) == (413, "content_too_large")
    assert echoed.json()["detail"] == "Request body too large."
    assert within.json() == {"size": 1_000}
    # Over every limit: refused by its length before the application runs.
    assert (beyond.status_code, beyond.json()["code"]) == (413, "content_too_large")
    # One line per caller and reason, then a count (ThrottledLog).
    assert caplog.messages[0] == "Rejected request body of 50000 bytes (limit 1000)"


def test_a_route_takes_less_than_the_global_limit(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The login route takes 64 bytes, declared or streamed; the others 1 MiB."""
    client = make_client()

    def chunks() -> Iterator[bytes]:
        yield b"x" * 40
        yield b"x" * 40

    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        exact = client.post("/login", content=b"x" * LOGIN_LIMIT)
        declared = client.post("/login", content=b"x" * (LOGIN_LIMIT + 1))
        streamed = client.post("/login", content=chunks())
        other = client.post("/echo", content=b"x" * 10_000)

    assert exact.json() == {"size": LOGIN_LIMIT}
    assert (declared.status_code, declared.json()["code"]) == (413, "content_too_large")
    assert (streamed.status_code, streamed.json()["code"]) == (413, "content_too_large")
    assert other.json() == {"size": 10_000}
    assert caplog.messages == ["Rejected request body of 65 bytes (limit 64)"]


def test_a_body_read_before_routing_is_held_to_its_routes_limit(
    make_client: MakeClient,
) -> None:
    """Middleware that reads first (idempotency) meets the route's limit too."""
    client = make_client(
        MAX_REQUEST_BODY_SIZE=1_000,
        MIDDLEWARE=(
            *LIMITS,
            "domainkit_tests.test_route_body_limits.ReadsFirst",
        ),
    )

    small = client.post("/login", content=b"x" * (LOGIN_LIMIT + 1))
    large = client.post(
        "/avatars", files={"file": ("a.png", b"\0" * 50_000, "image/png")}
    )
    unrouted = client.post("/missing", content=b"x" * 1_001)

    assert small.status_code == 413
    assert large.json() == {"size": 50_000}
    assert unrouted.status_code == 413  # No route: the global limit.


def test_routes_of_nested_routers_keep_their_limits(make_client: MakeClient) -> None:
    """Not flattened (ADR 0060), an included route still has its own limit."""
    client = make_client(
        ROOT_ROUTER=NESTED, FLATTEN_ROUTERS=False, MAX_REQUEST_BODY_SIZE=1_000
    )

    uploaded = client.post(
        "/inner/avatars", files={"file": ("a.png", b"\0" * 50_000, "image/png")}
    )
    small = client.post("/inner/login", content=b"x" * (LOGIN_LIMIT + 1))

    assert uploaded.json() == {"size": 50_000}
    assert small.status_code == 413


def test_the_largest_limit_is_found_below_mounts_and_routers() -> None:
    """Included routers and mounts are walked; none set is 0."""
    mount = Mount("/files", routes=list(router.routes))

    assert largest_body_limit([mount]) == UPLOAD_LIMIT
    assert largest_body_limit(nested.routes) == UPLOAD_LIMIT
    assert largest_body_limit([]) == 0
    assert route_body_limit(mount) is None


@pytest.mark.anyio
async def test_without_an_application_the_global_limit_holds() -> None:
    """A bare ASGI application has no routes to read limits from."""
    seen: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(len((await receive())["body"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 10}

    async def send(message: Message) -> None:
        pass

    with override_settings(MAX_REQUEST_BODY_SIZE=10):
        middleware = RequestBodyLimitMiddleware(app)
    await middleware({"type": "http", "headers": []}, receive, send)

    assert seen == [10]


@pytest.mark.parametrize("size", [0, -1, MAX_ROUTE_BODY_SIZE + 1])
def test_a_routes_limit_is_bounded(size: int) -> None:
    """1 byte to 1 GiB; the message names the bound."""
    with pytest.raises(ValueError, match=f"1 to {MAX_ROUTE_BODY_SIZE} bytes, not"):
        body_limit(size)


def test_limits_mark_the_endpoint() -> None:
    """Either side of the route's decorator, the endpoint carries the limit."""

    async def endpoint() -> None:
        """Take nothing."""

    assert body_limit(MAX_ROUTE_BODY_SIZE)(endpoint) is endpoint
    assert route_body_limit(SimpleNamespace(endpoint=endpoint)) == MAX_ROUTE_BODY_SIZE
    assert [route_body_limit(route) for route in router.routes] == [
        UPLOAD_LIMIT,
        LOGIN_LIMIT,
        None,
        None,
    ]
