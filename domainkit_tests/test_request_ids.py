"""Request ids: taken from trusted proxies only, in a bounded format (WEB-13).

Until 0.11 any caller chose its request's id with ``X-Request-ID``: the id
log lines, error reports, audit rows, and outgoing calls carry. A client
could give its requests another request's id, or one id to all of them,
and make the logs say what it liked about which lines belong together.
"""

import asyncio
import logging
import re
from collections.abc import Iterator, Mapping
from typing import Final

import httpx2
import pytest
from starlette.types import Message, Receive, Scope, Send

from domainkit.conf import Settings, override_settings
from domainkit.context import current_request
from domainkit.http.client import HttpClient, stub
from domainkit.http.middleware import (
    ProxyHeadersMiddleware,
    RequestContextMiddleware,
)
from domainkit.log import ContextFilter
from domainkit.reporting import flush_reports

URL: Final = "https://api.example.com/v1/rates"
NEW_ID: Final = re.compile(r"[0-9a-f]{32}")
PROXY: Final = ("10.0.0.2", 51000)
"""A trusted proxy's address, in ``10.0.0.0/8``."""
CLIENT: Final = ("203.0.113.9", 4711)
"""A caller on the internet."""

reported: list[Mapping[str, object]] = []


def collect(exc: BaseException, context: Mapping[str, object]) -> None:
    """An error reporter that remembers what it was told."""
    reported.append(dict(context))


class Seen:
    """What one request carried: in its context, its log line, and its call."""

    def __init__(self) -> None:
        """Nothing seen yet."""
        self.context: list[str] = []
        self.logged: list[str] = []
        self.sent: list[str] = []
        self.echoed: list[str] = []


async def _nothing() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def serve(
    seen: Seen,
    *,
    request_id: str | None,
    peer: tuple[str, int] | None = CLIENT,
    path: str = "/",
    forwarded_for: str | None = None,
) -> None:
    """Send one request through the default stack's first middleware."""
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "handled", (), None)

    def upstream(request: httpx2.Request) -> httpx2.Response:
        seen.sent.append(request.headers.get("x-request-id", ""))
        return httpx2.Response(200, content=b"{}")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = current_request()
        assert request is not None
        seen.context.append(request.request_id)
        ContextFilter().filter(record)
        seen.logged.append(record.__dict__["request_id"])
        with stub(upstream):
            await HttpClient().get(URL)
        if path == "/boom":
            raise RuntimeError("boom")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            seen.echoed.extend(
                value.decode()
                for name, value in message["headers"]
                if name == b"x-request-id"
            )

    headers = [(b"host", b"api.example.com")]
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode("latin-1")))
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "client": peer,
    }
    stack = ProxyHeadersMiddleware(RequestContextMiddleware(app))
    asyncio.run(stack(scope, _nothing, send))


@pytest.fixture(autouse=True)
def trusted_network() -> Iterator[None]:
    """``10.0.0.0/8`` holds the proxies; error reports are collected."""
    reported.clear()
    with override_settings(
        TRUSTED_PROXIES=["10.0.0.0/8"],
        ERROR_REPORTERS=["domainkit_tests.test_request_ids.collect"],
    ):
        yield


def test_a_client_cannot_choose_its_request_id() -> None:
    """Its own ``X-Request-ID`` reaches no log line, report, or outgoing call.

    At 0.10 the caller's value was the request's id everywhere, and was
    echoed back to it.
    """
    seen = Seen()

    serve(seen, request_id="6f1c7e6a7d4b4c559f0e1f2a3b4c5d6e")
    serve(seen, request_id="6f1c7e6a7d4b4c559f0e1f2a3b4c5d6e", path="/boom")

    flush_reports()  # Reporters run on their own thread.
    assert "6f1c7e6a7d4b4c559f0e1f2a3b4c5d6e" not in {
        *seen.context,
        *seen.logged,
        *seen.sent,
        *seen.echoed,
        *(report["request_id"] for report in reported),
    }
    first, second = seen.context
    assert NEW_ID.fullmatch(first)
    assert NEW_ID.fullmatch(second)
    assert seen.logged == seen.sent == seen.context
    assert seen.echoed == [first, second]  # The 500 carries its id too.
    assert [report["request_id"] for report in reported] == [second]


def test_clients_cannot_share_or_take_another_requests_id() -> None:
    """Two callers sending one id get two ids: lines stay apart."""
    seen = Seen()

    serve(seen, request_id="shared-id", peer=CLIENT)
    serve(seen, request_id="shared-id", peer=("198.51.100.4", 80))

    first, second = seen.context
    assert first != second
    assert "shared-id" not in seen.context


def test_a_client_behind_a_trusted_proxy_cannot_choose_it_either() -> None:
    """The proxy's own hop is trusted; a client's header it passes on is too.

    That is the proxy's to overwrite, as with the scheme: what is believed
    is what the trusted proxy sends (docs/operations.md).
    """
    seen = Seen()

    serve(seen, request_id="from-the-proxy", peer=PROXY, forwarded_for=CLIENT[0])

    assert seen.context == seen.sent == seen.echoed == ["from-the-proxy"]


def test_unix_socket_proxies_are_trusted_by_name() -> None:
    """``"unix"`` in ``TRUSTED_PROXIES`` trusts the socket's peer (ADR 0072)."""
    seen = Seen()

    with override_settings(TRUSTED_PROXIES=["unix"]):
        serve(seen, request_id="via-the-socket", peer=None)
    serve(seen, request_id="no-socket-trusted", peer=None)

    accepted, refused = seen.context
    assert accepted == "via-the-socket"
    assert NEW_ID.fullmatch(refused)


@pytest.mark.parametrize(
    "supplied",
    [
        "bad id",
        "line\nbreak",
        'quote"d',
        "per%cent",
        "-starts-badly",
        "x" * 129,
        "caf\xe9",
        "",
    ],
)
def test_a_proxys_id_out_of_format_is_replaced(supplied: str) -> None:
    """Up to 128 letters, digits, ``.``, ``_``, ``:``, ``-``, from a proxy too."""
    seen = Seen()

    serve(seen, request_id=supplied, peer=PROXY)

    assert seen.context != [supplied]
    assert NEW_ID.fullmatch(seen.context[0])


def test_ids_from_anyone_or_from_nobody() -> None:
    """``ACCEPT_REQUEST_ID``: ``"anyone"`` is 0.10's rule; ``"never"`` makes one."""
    seen = Seen()

    with override_settings(ACCEPT_REQUEST_ID="anyone"):
        serve(seen, request_id="clients-own", peer=CLIENT)
        serve(seen, request_id="bad id", peer=CLIENT)
    with override_settings(ACCEPT_REQUEST_ID="never"):
        serve(seen, request_id="from-the-proxy", peer=PROXY)

    anyone, out_of_format, never = seen.context
    assert anyone == "clients-own"
    assert NEW_ID.fullmatch(out_of_format)
    assert NEW_ID.fullmatch(never)


def test_the_setting_takes_its_three_rules_only() -> None:
    """``proxies`` by default; anything else fails at startup."""
    assert Settings().ACCEPT_REQUEST_ID == "proxies"
    with pytest.raises(ValueError, match="ACCEPT_REQUEST_ID"):
        Settings.model_validate({"ACCEPT_REQUEST_ID": "clients"})
