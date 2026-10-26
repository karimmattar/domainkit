"""SEC-11c-1: a proxy that adds its ``X-Request-ID`` line leaves the client's first.

WEB-13 (ADR 0099) takes the request id only from a trusted proxy, and asks
the proxy to "set or overwrite" the header. Proxies that set it by adding a
line keep the client's own line in front of theirs: HAProxy's
``unique-id-header X-Request-ID`` (``http_add_header``), Apache's
``RequestHeader add``, Caddy's ``header_up +X-Request-ID``. ASGI servers
pass both lines on, and ``RequestContextMiddleware`` reads the first one:
the client's. So behind such a proxy a caller still chooses the id its log
lines, error reports, audit rows, outgoing calls and queued work carry.

``ProxyHeadersMiddleware`` already reads every line of its headers, and
takes the scheme from the nearest proxy (the right-most value), never one
a client wrote further left; the request id should follow the same rule.
"""

import asyncio
import re
from collections.abc import Iterator
from typing import Final

import pytest
from starlette.types import Message, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.context import current_request
from domainkit.http.middleware import ProxyHeadersMiddleware, RequestContextMiddleware

PROXY: Final = ("10.0.0.2", 51000)
"""A trusted proxy's address, in ``10.0.0.0/8``."""
CLIENT: Final = "203.0.113.9"
"""A caller on the internet, behind the proxy."""
CHOSEN: Final = "6f1c7e6a7d4b4c559f0e1f2a3b4c5d6e"
"""The id of another request, which the caller copied from an error body."""
PROXYS: Final = "c0a8000a-1f90-6730d2a4-0000-0042"
"""The id the proxy made for this request (HAProxy's ``unique-id-format``)."""
NEW_ID: Final = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(autouse=True)
def trusted_network() -> Iterator[None]:
    """``10.0.0.0/8`` holds the proxies, which set ``X-Request-ID``."""
    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"]):
        yield


def serve(headers: list[tuple[bytes, bytes]]) -> tuple[str, list[str]]:
    """The request id one request was served under, and the one echoed to it."""
    seen: list[str] = []
    echoed: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = current_request()
        assert request is not None
        seen.append(request.request_id)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            echoed.extend(
                value.decode()
                for name, value in message["headers"]
                if name == b"x-request-id"
            )

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"api.example.com"), *headers],
        "client": PROXY,
    }
    stack = ProxyHeadersMiddleware(RequestContextMiddleware(app))
    asyncio.run(stack(scope, receive, send))
    (request_id,) = seen
    return request_id, echoed


def test_the_proxys_added_line_names_the_request_not_the_clients() -> None:
    """The client's line, sent on ahead of the proxy's, must not be the id.

    The proxy wrote the last ``X-Request-ID`` line, as it wrote the last
    ``X-Forwarded-For`` hop; anything to its left came from the client.
    """
    request_id, echoed = serve(
        [
            (b"x-request-id", CHOSEN.encode()),  # The client's, passed on.
            (b"x-forwarded-for", CLIENT.encode()),  # Added by the proxy.
            (b"x-request-id", PROXYS.encode()),  # Added by the proxy.
        ]
    )

    assert request_id != CHOSEN, (
        "a caller behind a trusted proxy that adds its X-Request-ID line "
        f"chose its request's id: {request_id!r}"
    )
    assert request_id == PROXYS or NEW_ID.fullmatch(request_id)
    assert echoed == [request_id]


def test_one_line_from_a_proxy_is_still_taken() -> None:
    """The fix keeps WEB-13's rule for a proxy that overwrites the header."""
    request_id, _ = serve(
        [(b"x-forwarded-for", CLIENT.encode()), (b"x-request-id", PROXYS.encode())]
    )

    assert request_id == PROXYS
