"""Trusted proxies, error reporters, and timezone-safe timestamps."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Insert, create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import Message, Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import Settings, override_settings
from domainkit.contrib.messaging.outbox import insert_statements, message_row
from domainkit.db import UTCDateTime
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.middleware import ProxyHeadersMiddleware
from domainkit.reporting import flush_reports, report_error, reporters

pytestmark = pytest.mark.anyio

reported: list[tuple[str, Mapping[str, object]]] = []
NOT_CALLABLE = "a string"


def collect(exc: BaseException, context: Mapping[str, object]) -> None:
    """Remember a report."""
    reported.append((str(exc), dict(context)))


def broken(exc: BaseException, context: Mapping[str, object]) -> None:
    """Fail while reporting."""
    raise RuntimeError("reporter down")


@pytest.fixture(autouse=True)
def clear_reports() -> None:
    """Start each test without reports."""
    reported.clear()


async def through_proxy(
    scope_type: str,
    peer: str,
    headers: dict[str, str] | list[tuple[str, str]],
    trusted: list[str],
    scheme: str | None = None,
) -> tuple[object, object]:
    """Return the ``client`` and ``scheme`` the wrapped app sees."""
    seen: list[tuple[object, object]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert await receive() == {"type": "http.disconnect"}
        seen.append((scope.get("client"), scope.get("scheme")))

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        return None

    with override_settings(TRUSTED_PROXIES=trusted):
        middleware = ProxyHeadersMiddleware(app)
    scope = {
        "type": scope_type,
        "client": (peer, 1234),
        "scheme": scheme or ("ws" if scope_type == "websocket" else "http"),
        "headers": [
            (k.encode(), v.encode())
            for k, v in (headers.items() if isinstance(headers, dict) else headers)
        ],
    }
    await middleware(scope, receive, send)
    return seen[0]


@pytest.mark.parametrize(
    ("peer", "headers", "expected"),
    [
        (
            "10.0.0.5",
            {"x-forwarded-for": "203.0.113.9, 10.0.0.7", "x-forwarded-proto": "https"},
            (("203.0.113.9", 0), "https"),
        ),
        (
            "10.0.0.5",
            {"x-forwarded-for": "1.2.3.4, 203.0.113.9"},
            (("203.0.113.9", 0), "http"),
        ),
        (
            "10.0.0.5",
            {"x-forwarded-for": "10.0.0.8, 10.0.0.9"},
            (("10.0.0.8", 0), "http"),
        ),
        ("10.0.0.5", {"x-forwarded-for": "not-an-ip"}, (("10.0.0.5", 1234), "http")),
        ("10.0.0.5", {"x-forwarded-proto": "ftp"}, (("10.0.0.5", 1234), "http")),
        (
            "198.51.100.1",
            {"x-forwarded-for": "203.0.113.9"},
            (("198.51.100.1", 1234), "http"),
        ),
        (
            "testclient",
            {"x-forwarded-for": "203.0.113.9"},
            (("testclient", 1234), "http"),
        ),
    ],
)
async def test_forwarded_headers_only_from_trusted_proxies(
    peer: str, headers: dict[str, str], expected: tuple[object, object]
) -> None:
    """Spoofed hops and untrusted peers never change the client address."""
    assert await through_proxy("http", peer, headers, ["10.0.0.0/8"]) == expected


@pytest.mark.parametrize(
    ("peer", "headers", "expected"),
    [
        (  # A proxy that adds a line: the client's own line comes first.
            "10.0.0.5",
            [
                ("x-forwarded-for", "198.51.100.7"),
                ("x-forwarded-for", "203.0.113.9"),
            ],
            (("203.0.113.9", 0), "http"),
        ),
        (
            "10.0.0.5",
            {"x-forwarded-for": "203.0.113.9:4711, 10.0.0.7:80"},
            (("203.0.113.9", 0), "http"),
        ),
        (
            "10.0.0.5",
            {"x-forwarded-for": "[2001:db8::1]:443, [2001:db8::2]:8"},
            (("2001:db8::2", 0), "http"),
        ),
        *(
            ("10.0.0.5", {"x-forwarded-for": malformed}, (("10.0.0.5", 1234), "http"))
            for malformed in (
                "[2001:db8::1",
                "[2001:db8::1]443",
                "[2001:db8::1]:",
                "[2001:db8::1]:https",
                "203.0.113.9:",
                "203.0.113.9:-1",
            )
        ),
        (
            "::ffff:10.0.0.5",
            {"x-forwarded-for": "::ffff:203.0.113.9, ::ffff:10.0.0.7"},
            (("203.0.113.9", 0), "http"),
        ),
        (  # The client's scheme: at its place from the right.
            "10.0.0.5",
            {
                "x-forwarded-for": "203.0.113.9, 10.0.0.7",
                "x-forwarded-proto": "http, HTTPS, http",
            },
            (("203.0.113.9", 0), "https"),
        ),
        (
            "10.0.0.5",
            [
                ("x-forwarded-for", "203.0.113.9"),
                ("x-forwarded-proto", "https"),
                ("x-forwarded-proto", "http"),
            ],
            (("203.0.113.9", 0), "http"),
        ),
        (  # Every hop trusted: the left-most is the client, two places in.
            "10.0.0.5",
            {
                "x-forwarded-for": "10.0.0.8, 10.0.0.9",
                "x-forwarded-proto": "https, http",
            },
            (("10.0.0.8", 0), "https"),
        ),
        (
            "10.0.0.5",
            {"x-forwarded-proto": "https, http"},
            (("10.0.0.5", 1234), "http"),
        ),
    ],
)
async def test_every_forwarded_line_and_hop_counts(
    peer: str,
    headers: dict[str, str] | list[tuple[str, str]],
    expected: tuple[object, object],
) -> None:
    """A client's own line or value never wins over a proxy's."""
    assert await through_proxy("http", peer, headers, ["10.0.0.0/8"]) == expected


async def test_a_proxy_on_plain_http_makes_the_scheme_http() -> None:
    """The connection from the proxy may be TLS; the client's was not."""
    plain = {"x-forwarded-proto": "http"}

    assert await through_proxy(
        "http", "10.0.0.5", plain, ["10.0.0.0/8"], scheme="https"
    ) == (("10.0.0.5", 1234), "http")
    assert await through_proxy(
        "websocket", "10.0.0.5", plain, ["10.0.0.0/8"], scheme="wss"
    ) == (("10.0.0.5", 1234), "ws")


async def test_proxy_scheme_for_websockets_and_no_proxies() -> None:
    """Websockets get wss/ws; without trusted proxies nothing changes."""
    secure = {"x-forwarded-proto": "https"}
    plain = {"x-forwarded-proto": "http"}
    assert (await through_proxy("websocket", "10.0.0.1", secure, ["10.0.0.0/8"]))[
        1
    ] == "wss"
    assert (await through_proxy("websocket", "10.0.0.1", plain, ["10.0.0.0/8"]))[
        1
    ] == "ws"
    unchanged = await through_proxy(
        "http", "10.0.0.1", {"x-forwarded-for": "1.1.1.1"}, []
    )
    assert unchanged == (("10.0.0.1", 1234), "http")


def test_trusted_proxies_must_be_networks() -> None:
    """Malformed proxy entries fail at startup."""
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings.model_validate({"TRUSTED_PROXIES": ["not a network"]})


def test_reporters_are_validated() -> None:
    """Bad dotted paths fail fast; non-callables are refused."""
    with (
        override_settings(ERROR_REPORTERS=["nowhere.reporter"]),
        pytest.raises(ImproperlyConfigured, match="ERROR_REPORTERS"),
    ):
        reporters()
    path = "domainkit_tests.test_proxies_and_reporting.NOT_CALLABLE"
    with (
        override_settings(ERROR_REPORTERS=[path]),
        pytest.raises(ImproperlyConfigured, match="not callable"),
    ):
        reporters()


def test_report_error_isolates_reporters(caplog: pytest.LogCaptureFixture) -> None:
    """A failing reporter is logged; the others still receive the error."""
    paths = [
        "domainkit_tests.test_proxies_and_reporting.broken",
        "domainkit_tests.test_proxies_and_reporting.collect",
    ]
    with override_settings(ERROR_REPORTERS=paths):
        report_error(ValueError("boom"), request_id="r1")

    flush_reports()  # Reporters run on their own thread.
    assert reported == [("boom", {"request_id": "r1"})]
    assert "Error reporter" in caplog.text


def test_unhandled_request_errors_are_reported() -> None:
    """The request middleware reports what it turns into a 500."""
    with override_settings(
        ERROR_REPORTERS=["domainkit_tests.test_proxies_and_reporting.collect"]
    ):
        client = TestClient(get_asgi_application(), raise_server_exceptions=False)
        response = client.get("/boom")

    flush_reports()  # Reporters run on their own thread.
    ((message, context),) = reported
    assert (message, context["path"], context["method"]) == ("boom", "/boom", "GET")
    assert context["request_id"] == response.headers["X-Request-ID"]


class Pinged(DomainEvent):
    """An event for handler failure tests."""


async def test_in_process_handler_failures_are_reported() -> None:
    """``setup()`` wires the global bus to the reporters."""

    async def failing(event: Pinged) -> None:
        raise RuntimeError("handler broke")

    registry.clear()
    with override_settings(
        ERROR_REPORTERS=["domainkit_tests.test_proxies_and_reporting.collect"]
    ):
        setup()
        event_bus.subscribe(Pinged, failing)
        try:
            event = Pinged()
            await event_bus.publish(event)
        finally:
            event_bus.unsubscribe(Pinged, failing)

    flush_reports()  # Reporters run on their own thread.
    assert reported == [
        ("handler broke", {"event": "Pinged", "event_id": str(event.event_id)})
    ]
    assert EventBus().error_hook is None


def test_utc_datetime_round_trips() -> None:
    """Values are stored in UTC and read back aware; naive input is refused."""
    column = UTCDateTime()
    cairo = timezone(timedelta(hours=3))
    moment = datetime(2026, 1, 1, 12, tzinfo=cairo)
    lite = create_engine("sqlite://").dialect
    pg = create_async_engine("postgresql+asyncpg://app@db/app").dialect

    assert column.process_bind_param(None, lite) is None
    assert column.process_bind_param(moment, lite) == datetime(2026, 1, 1, 9)  # noqa: DTZ001
    assert column.process_bind_param(moment, pg) == datetime(2026, 1, 1, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        column.process_bind_param(datetime(2026, 1, 1), lite)  # noqa: DTZ001
    assert column.process_result_value(None, lite) is None
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 - SQLite returns naive values.
    assert column.process_result_value(naive, lite) == datetime(2026, 1, 1, tzinfo=UTC)
    assert column.process_result_value(moment, pg) == datetime(
        2026, 1, 1, 9, tzinfo=UTC
    )


def test_postgres_deduplicated_inserts_compile() -> None:
    """PostgreSQL skips existing dedupe keys with ON CONFLICT DO NOTHING."""
    row = message_row(
        kind="task", target="t", payload={}, headers={}, max_attempts=1, dedupe_key="k"
    )
    (statement,) = insert_statements("postgresql", [row])

    dialect = create_async_engine("postgresql+asyncpg://app@db/app").dialect
    sql = str(cast(Insert, statement).compile(dialect=dialect))
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql


def test_logging_of_reporters_does_not_leak(caplog: pytest.LogCaptureFixture) -> None:
    """Reporting without reporters configured does nothing."""
    caplog.set_level(logging.DEBUG)
    report_error(RuntimeError("quiet"))
    assert reported == []
