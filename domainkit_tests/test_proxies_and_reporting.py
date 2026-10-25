"""Trusted proxies, error reporters, and timezone-safe timestamps."""

import ipaddress
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import Message, Receive, Scope, Send

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import freeze_time
from domainkit.conf import Settings, override_settings
from domainkit.contrib.messaging.outbox import insert_statements, message_row
from domainkit.db import UTCDateTime
from domainkit.ddd import DomainEvent, EventBus, event_bus
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.middleware import PEER, UNIX_PEER, ProxyHeadersMiddleware
from domainkit.log import SECURITY_LOGGER, THROTTLE_INTERVAL
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
    peer: str | None,
    headers: dict[str, str] | list[tuple[str, str]],
    trusted: list[str],
    scheme: str | None = None,
    **options: object,
) -> tuple[object, object]:
    """Return the ``client`` and ``scheme`` the wrapped app sees.

    A ``peer`` of None is a unix socket's; ``options`` are other settings.
    """
    seen: list[tuple[object, object]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert await receive() == {"type": "http.disconnect"}
        seen.append((scope.get("client"), scope.get("scheme")))

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        return None

    with override_settings(TRUSTED_PROXIES=trusted, **options):
        middleware = ProxyHeadersMiddleware(app)
    scope = {
        "type": scope_type,
        "client": None if peer is None else (peer, 1234),
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
        (  # Brackets without a port.
            "10.0.0.5",
            {"x-forwarded-for": "[2001:db8::3]"},
            (("2001:db8::3", 0), "http"),
        ),
        *(
            ("10.0.0.5", {"x-forwarded-for": malformed}, (("10.0.0.5", 1234), "http"))
            for malformed in (
                "[2001:db8::1",
                "[2001:db8::1]443",
                "[2001:db8::1]:",
                "[2001:db8::1]:https",
                "[fe80::1%a]b]",  # The host ends at the first bracket.
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


PROXIED_MIDDLEWARE = [
    "domainkit.http.middleware.ProxyHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",
]


@pytest.mark.parametrize(
    ("hops", "expected"),
    [
        ("garbage, 203.0.113.9", "203.0.113.9"),
        ("garbage, 203.0.113.9, 10.0.0.7", "203.0.113.9"),
        ("[2001:db8::1, 203.0.113.9:80", "203.0.113.9"),
        ("unknown, 198.51.100.4, 10.0.0.7", "198.51.100.4"),
    ],
)
async def test_a_clients_own_malformed_hop_is_never_read(
    hops: str, expected: str
) -> None:
    """The walk stops at the client's hop: what it wrote further left is not read."""
    assert await through_proxy(
        "http", "10.0.0.5", {"x-forwarded-for": hops}, ["10.0.0.0/8"]
    ) == ((expected, 0), "http")


def test_client_hop_names_the_client_and_its_place() -> None:
    """Counted from the right; None where the client's hop is no address."""
    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"]):
        proxy = ProxyHeadersMiddleware(app=lambda *args: None)  # type: ignore[arg-type]

    assert proxy.client_hop(["203.0.113.9", "10.0.0.7"]) == ("203.0.113.9", 2)
    assert proxy.client_hop(["10.0.0.8", "10.0.0.9"]) == ("10.0.0.8", 2)
    # The client's hop is no address: the proxy that wrote it, at the client's place.
    assert proxy.client_hop(["junk", "10.0.0.7"]) == ("10.0.0.7", 2)
    assert proxy.client_hop(["203.0.113.9", "junk"]) is None
    assert proxy.client_hop([]) is None


async def test_an_unreadable_hop_where_the_client_is_counts_the_last_proxy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trusted proxy wrote a hop that is no address: its own is the client's.

    The walk never goes past it, and the warning names the proxy, never
    the hop's text.
    """
    trusted = ["10.0.0.0/8"]
    forwarded = {
        "x-forwarded-for": "203.0.113.9, secret-name, 10.0.0.7",
        "x-forwarded-proto": "https, http",
    }

    seen = await through_proxy("http", "10.0.0.5", forwarded, trusted)
    rightmost = await through_proxy(
        "http", "10.0.0.5", {"x-forwarded-for": "secret-name"}, trusted
    )

    assert seen == (("10.0.0.7", 0), "https")  # The scheme at the client's place.
    assert rightmost == (("10.0.0.5", 1234), "http")
    warnings = [r.getMessage() for r in caplog.records if r.name == SECURITY_LOGGER]
    said = (
        "X-Forwarded-For from trusted proxy 10.0.0.5 holds a hop that is no address "
        "where the client's should be; counting the client as"
    )
    assert warnings == [f"{said} 10.0.0.7", f"{said} 10.0.0.5"]
    assert "secret-name" not in caplog.text


async def test_unreadable_hops_are_logged_once_a_while(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A proxy that keeps writing them gets a line, then a count, not a flood."""
    seen: list[object] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["client"])

    with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"]):
        middleware = ProxyHeadersMiddleware(app)
    scope = {
        "type": "http",
        "client": ("10.0.0.5", 1234),
        "scheme": "http",
        "headers": [(b"x-forwarded-for", b"unknown")],
    }
    with freeze_time("2026-01-01") as clock:
        for _ in range(3):
            await middleware(dict(scope), None, None)  # type: ignore[arg-type]
        clock.tick(THROTTLE_INTERVAL)
        await middleware(dict(scope), None, None)  # type: ignore[arg-type]

    assert seen == [("10.0.0.5", 1234)] * 4
    warnings = [r.getMessage() for r in caplog.records if r.name == SECURITY_LOGGER]
    assert len(warnings) == 2
    assert warnings[1].endswith("(and 2 more like it since the last line)")


def test_a_malformed_hop_of_its_own_keeps_a_client_in_its_bucket() -> None:
    """Rate limits count the client, not the proxy, whatever it sends left."""
    with override_settings(
        MIDDLEWARE=PROXIED_MIDDLEWARE,
        TRUSTED_PROXIES=["10.0.0.0/8"],
        RATE_LIMIT="2/minute",
    ):
        client = TestClient(get_asgi_application(), client=("10.0.0.5", 1234))
        own = {"X-Forwarded-For": "203.0.113.9"}
        spoofed = {"X-Forwarded-For": "garbage, 203.0.113.9"}
        answers = [client.get("/ok", headers=headers) for headers in (own, own)]
        answers.append(client.get("/ok", headers=spoofed))
        answers.append(client.get("/ok", headers={"X-Forwarded-For": "198.51.100.2"}))

    assert [answer.status_code for answer in answers] == [200, 200, 429, 200]


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_a_unix_socket_peer_is_trusted_when_named(scope_type: str) -> None:
    """``"unix"`` believes the proxy on a unix socket, which has no address."""
    forwarded = {"x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https"}

    believed = await through_proxy(scope_type, None, forwarded, ["unix"])
    networks_only = await through_proxy(scope_type, None, forwarded, ["10.0.0.0/8"])
    tcp_peer = await through_proxy(scope_type, "10.0.0.5", forwarded, ["unix"])
    no_header = await through_proxy(scope_type, None, {}, ["unix"])

    secure, plain = ("wss", "ws") if scope_type == "websocket" else ("https", "http")
    assert believed == (("203.0.113.9", 0), secure)
    assert networks_only == (None, plain)
    assert tcp_peer == (("10.0.0.5", 1234), plain)
    assert no_header == (None, plain)


async def test_a_unix_socket_peer_is_kept_as_no_address() -> None:
    """``PEER`` names the socket, which no network of addresses holds."""
    seen: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    with override_settings(TRUSTED_PROXIES=["unix", "10.0.0.0/8"]):
        middleware = ProxyHeadersMiddleware(app)
    scope = {
        "type": "http",
        "client": None,
        "scheme": "http",
        "headers": [(b"x-forwarded-for", b"10.0.0.9, 203.0.113.9")],
    }
    await middleware(scope, None, None)  # type: ignore[arg-type]

    assert seen[0][PEER] == UNIX_PEER == ("unix", 0)
    assert seen[0]["client"] == ("203.0.113.9", 0)
    assert (middleware.networks, middleware.unix) == (
        (ipaddress.ip_network("10.0.0.0/8"),),
        True,
    )


def test_callers_behind_a_unix_socket_get_buckets_of_their_own() -> None:
    """One caller over the limit no longer refuses everyone behind the socket."""
    with override_settings(
        MIDDLEWARE=PROXIED_MIDDLEWARE, TRUSTED_PROXIES=["unix"], RATE_LIMIT="2/minute"
    ):
        client = TestClient(get_asgi_application(), client=None)  # type: ignore[arg-type]
        answers = [
            client.get("/ok", headers={"X-Forwarded-For": address})
            for address in ("203.0.113.9", "203.0.113.9", "203.0.113.9", "192.0.2.1")
        ]

    assert [answer.status_code for answer in answers] == [200, 200, 429, 200]


def test_trusted_proxies_take_unix_but_no_other_word() -> None:
    """``"unix"`` is the one name; prefix 0 is still refused where deployed."""
    loaded = Settings.model_validate({"TRUSTED_PROXIES": ["unix", "10.0.0.0/8"]})
    assert ("unix", ipaddress.ip_network("10.0.0.0/8")) == loaded.TRUSTED_PROXIES
    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings.model_validate({"TRUSTED_PROXIES": ["socket"]})
    with pytest.raises(ValueError, match="not every address"):
        Settings.model_validate(
            {
                "ENVIRONMENT": "production",
                "API_DOCS": False,
                "TRUSTED_PROXIES": ["unix", "0.0.0.0/0"],
            }
        )


@pytest.mark.parametrize(
    ("headers", "options", "expected"),
    [
        (  # The proxy writes no scheme: the client's own line is not read.
            {"x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "http"},
            {"PROXY_SCHEME_HEADER": None},
            (("203.0.113.9", 0), "https"),
        ),
        (  # The proxy's own header, and only it.
            {
                "x-forwarded-for": "203.0.113.9",
                "x-forwarded-proto": "http",
                "x-forwarded-scheme": "https",
            },
            {"PROXY_SCHEME_HEADER": "X-Forwarded-Scheme"},
            (("203.0.113.9", 0), "https"),
        ),
        (  # Fewer values than hops: the nearest proxy's, not the left-most.
            {
                "x-forwarded-for": "203.0.113.9, 10.0.0.7, 10.0.0.8",
                "x-forwarded-proto": "http, https",
            },
            {},
            (("203.0.113.9", 0), "https"),
        ),
    ],
)
async def test_the_scheme_comes_only_from_what_the_proxy_writes(
    headers: dict[str, str], options: dict[str, object], expected: tuple[object, ...]
) -> None:
    """``PROXY_SCHEME_HEADER`` names the proxy's header; None believes none."""
    assert (
        await through_proxy(
            "http", "10.0.0.5", headers, ["10.0.0.0/8"], scheme="https", **options
        )
        == expected
    )


def test_the_proxy_scheme_header_is_a_header_name() -> None:
    """Anything but a header name, or None, fails at startup."""
    assert Settings().PROXY_SCHEME_HEADER == "X-Forwarded-Proto"
    assert (
        Settings.model_validate({"PROXY_SCHEME_HEADER": None}).PROXY_SCHEME_HEADER
        is None
    )
    with pytest.raises(ValueError, match="PROXY_SCHEME_HEADER"):
        Settings.model_validate({"PROXY_SCHEME_HEADER": "X-Proto: https"})


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
    sql = str(statement.compile(dialect=dialect))
    assert "ON CONFLICT (dedupe_key) DO NOTHING" in sql


def test_logging_of_reporters_does_not_leak(caplog: pytest.LogCaptureFixture) -> None:
    """Reporting without reporters configured does nothing."""
    caplog.set_level(logging.DEBUG)
    report_error(RuntimeError("quiet"))
    assert reported == []
