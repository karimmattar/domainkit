"""HTTP integration: application factory, middleware, errors, and health."""

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import State
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from domainkit.apps import registry
from domainkit.cache import caches
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request, current_request
from domainkit.contrib.idempotency.operations import (
    Operation,
    run_operation,
    running_operation,
)
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application, health
from domainkit.http import middleware as middleware_module
from domainkit.http.middleware import (
    AllowedHostsMiddleware,
    RequestBodyLimitMiddleware,
    RequestBodyTooLarge,
    RequestContextMiddleware,
    host_allowed,
    request_path,
    security_headers,
)
from domainkit.ledger import ledgers
from domainkit.log import SECURITY_LOGGER
from domainkit.tasks import owned
from domainkit.testing import AsyncTestClient
from domainkit.tracing import current_ids, remote_parent
from domainkit_tests.sample_domains.alpha.apps import AlphaConfig

type MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client() -> Iterator[MakeClient]:
    """Build a client for the sample routes with optional setting overrides."""
    with ExitStack() as stack:

        def make(
            *, raise_server_exceptions: bool = True, **overrides: object
        ) -> TestClient:
            stack.enter_context(override_settings(**overrides))
            app = get_asgi_application()
            return stack.enter_context(
                TestClient(app, raise_server_exceptions=raise_server_exceptions)
            )

        yield make


@pytest.fixture
def client(make_client: MakeClient) -> TestClient:
    """Client with the framework test settings."""
    return make_client()


@pytest.mark.parametrize(
    ("failure", "status_code", "code"),
    [
        ("pool", 503, "database_busy"),
        ("57014", 503, "database_busy"),
        ("55P03", 503, "database_busy"),
        ("40001", 503, "database_busy"),
        ("40P01", 503, "database_busy"),
        ("22021", 422, "invalid_value"),
        ("22001", 422, "invalid_value"),
        ("23502", 500, "internal_error"),
        ("None", 500, "internal_error"),
    ],
)
def test_database_errors_that_are_not_bugs_answer_as_such(
    make_client: MakeClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    status_code: int,
    code: str,
) -> None:
    """Busy is a 503 to retry, a value the column refuses a 422; neither a bug."""
    reported: list[BaseException] = []
    monkeypatch.setattr(
        middleware_module, "report_error", lambda exc, **_: reported.append(exc)
    )
    client = make_client(raise_server_exceptions=False)
    failure_path = f"/database/{failure}"

    with caplog.at_level(logging.WARNING, logger="domainkit.http.middleware"):
        response = client.get(failure_path)

    assert (response.status_code, response.json()["code"]) == (status_code, code)
    refused = [r for r in caplog.records if r.getMessage().startswith("Database")]
    if status_code == 500:
        assert (len(reported), refused) == (1, [])
        return
    assert reported == []
    reason = "TimeoutError" if failure == "pool" else f"SQLSTATE {failure}"
    # The route's template, never the path as sent (WEB-10).
    assert [(r.levelname, r.getMessage()) for r in refused] == [
        ("WARNING", f"Database refused GET /database/{{failure}}: {code} ({reason})")
    ]
    assert refused[0].exc_info is None
    if status_code == 503:
        assert response.headers["Retry-After"] == "1"
        assert response.json()["detail"] == "The database is busy; try again shortly."
    else:
        assert "Retry-After" not in response.headers
        assert response.json()["detail"] == (
            "A value cannot be stored: it is too long, out of range, or holds a "
            "character the database refuses."
        )


@pytest.mark.parametrize(("failure", "close_code"), [("pool", 1013), ("22021", 1007)])
def test_websockets_the_database_refuses_are_closed_so(
    make_client: MakeClient, failure: str, close_code: int
) -> None:
    """Busy is "try again later" (1013); a refused value "invalid data" (1007)."""
    client = make_client(raise_server_exceptions=False)

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect(f"/ws/database/{failure}"),
    ):
        pass

    assert closed.value.code == close_code


def test_security_headers_follow_settings() -> None:
    """``SECURE_*`` settings translate into headers."""
    minimal = security_headers(
        hsts_seconds=0,
        hsts_include_subdomains=False,
        content_type_nosniff=False,
        referrer_policy=None,
        frame_options="DENY",
    )
    full = security_headers(
        hsts_seconds=60,
        hsts_include_subdomains=True,
        content_type_nosniff=True,
        referrer_policy="same-origin",
        frame_options="SAMEORIGIN",
    )

    assert dict(minimal) == {"Cache-Control": "no-store", "X-Frame-Options": "DENY"}
    assert dict(full)["Strict-Transport-Security"] == "max-age=60; includeSubDomains"
    assert dict(full)["Referrer-Policy"] == "same-origin"
    assert dict(full)["X-Content-Type-Options"] == "nosniff"
    hsts_only = security_headers(
        hsts_seconds=5,
        hsts_include_subdomains=False,
        content_type_nosniff=True,
        referrer_policy=None,
        frame_options="DENY",
    )
    assert dict(hsts_only)["Strict-Transport-Security"] == "max-age=5"


@pytest.mark.parametrize(
    ("host", "allowed", "expected"),
    [
        ("api.example.com", ["api.example.com"], True),
        ("API.example.com:8443", ["api.example.com"], True),
        ("api.example.com.", ["api.example.com"], True),
        ("eu.api.example.com", ["*.example.com"], True),
        ("example.com", ["*.example.com"], False),
        ("anything", ["*"], True),
        ("[::1]:8000", ["[::1]"], True),
        ("[::1", ["[::1]"], False),
        ("", ["*"], False),
        ("evil.com", ["api.example.com"], False),
        ("203.0.113.9:80", ["203.0.113.9"], True),
        ("[2001:DB8::1]", ["[2001:db8::1]"], True),
        ("api-1.example.com", ["*.example.com"], True),
        # Not hosts at all: refused even by "*", so no pattern matches a tail.
        ("evil.com/.example.com", ["*.example.com"], False),
        ("evil.com?.example.com", ["*.example.com"], False),
        ("evil.com#.example.com", ["*.example.com"], False),
        ("evil.com@api.example.com", ["*.example.com"], False),
        ("evil.com\\.example.com", ["*.example.com"], False),
        ("api example.com", ["*"], False),
        ("api.example.com\n", ["api.example.com"], False),
        (" api.example.com", ["api.example.com"], False),
        ("api.example.com:port", ["*"], False),
        ("api.example.com:80:1", ["*"], False),
        ("api.example.com:", ["*"], False),
        ("::1", ["*"], False),
        ("[::1]x", ["*"], False),
        ("[fe80::1%eth0]", ["*"], False),
        ("bücher.example.com", ["*"], False),
    ],
)
def test_host_matching(host: str, allowed: list[str], expected: bool) -> None:
    """Host patterns match exact names, subdomain wildcards, and any host.

    Values that are not a name, an IPv4 address, or a bracketed IPv6 address
    (with an optional port) are refused first, as Django does.
    """
    assert host_allowed(host, allowed) is expected


@pytest.mark.parametrize(
    ("seconds", "subdomains", "expected"),
    [
        (1, False, "max-age=1"),
        (1, True, "max-age=1; includeSubDomains"),
        (0, True, None),
    ],
)
def test_hsts_follows_its_settings(
    make_client: MakeClient, seconds: int, subdomains: bool, expected: str | None
) -> None:
    """One second is enough to send it; subdomains are included when asked."""
    client = make_client(
        MIDDLEWARE=["domainkit.http.middleware.SecurityHeadersMiddleware"],
        SECURE_HSTS_SECONDS=seconds,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=subdomains,
    )

    assert client.get("/ok").headers.get("Strict-Transport-Security") == expected


def test_responses_carry_security_headers_and_request_ids(client: TestClient) -> None:
    """Headers are added unless set; a caller's own request id is not taken.

    Up to 0.10 a valid one was (WEB-13): only a trusted proxy's is now
    (``test_request_ids.py``).
    """
    response = client.get("/ok")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.headers["X-Request-ID"]) == 32

    assert client.get("/framed").headers["X-Frame-Options"] == "SAMEORIGIN"
    supplied = client.get("/ok", headers={"X-Request-ID": "abc-123"})
    assert re.fullmatch(r"[0-9a-f]{32}", supplied.headers["X-Request-ID"])
    forged = client.get("/ok", headers={"X-Request-ID": "bad id\n"})
    assert forged.headers["X-Request-ID"] != "bad id\n"


def test_unhandled_errors_become_generic_500(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Outside debug mode clients see no internals; the error is logged once."""
    client = make_client(raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error."
    assert response.json()["code"] == "internal_error"
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "Unhandled error in GET /boom" in caplog.text


def test_debug_mode_and_started_responses_reraise(make_client: MakeClient) -> None:
    """Debug mode shows the error; a started response cannot be replaced."""
    with pytest.raises(RuntimeError, match="mid-stream"):
        make_client().get("/stream-boom")
    with pytest.raises(RuntimeError, match="boom"):
        make_client(DEBUG=True).get("/boom")


def test_untrusted_hosts_are_rejected(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Requests and websockets for unknown hosts are refused and logged."""
    response = client.get("/ok", headers={"Host": "evil.example"})

    assert (response.status_code, response.json()["code"]) == (400, "invalid_host")
    assert response.json()["detail"] == "Invalid host header."
    # Another caller: one caller's repeats are throttled (test_security_log).
    other = TestClient(client.app, client=("198.51.100.7", 50000))
    other.get("/ok", headers={"Host": "x" * 300})
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == SECURITY_LOGGER
    ] == [
        "Rejected untrusted Host header 'evil.example'",
        f"Rejected untrusted Host header {'x' * 255!r}",
    ]
    with override_settings(ALLOWED_HOSTS=["*.example.com"]):
        wildcard = TestClient(get_asgi_application())
        sneaky = wildcard.get("/ok", headers={"Host": "evil.com/.example.com"})
        fine = wildcard.get("/ok", headers={"Host": "api.example.com"})
    assert (sneaky.status_code, sneaky.json()["code"]) == (400, "invalid_host")
    assert fine.status_code == 200
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws", headers={"Host": "evil.example"}),
    ):
        pass
    assert closed.value.code == 1008


def test_requests_without_a_host_are_rejected() -> None:
    """No Host header at all is refused, even where any host is allowed."""
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("The request reached the application.")

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(ALLOWED_HOSTS=["*"]):
        middleware = AllowedHostsMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert sent[0]["status"] == 400


def test_websockets_pass_through_http_middleware(client: TestClient) -> None:
    """Non-HTTP scopes are not touched by HTTP-only middleware."""
    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_text() == "hi"


def test_websockets_run_in_a_request_context(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Method WS, the path, and a request id of its own, echoed when accepting.

    The caller's ``X-Request-ID`` is not taken, as for HTTP (WEB-13).
    """
    caplog.set_level(logging.DEBUG, logger="domainkit.access")
    with client.websocket_connect("/ws/context") as socket:
        context = socket.receive_json()
        headers: dict[bytes, bytes] = dict(socket.extra_headers or [])
    with client.websocket_connect(
        "/ws/context", headers={"X-Request-ID": "abc-123"}
    ) as socket:
        supplied = socket.receive_json()["request_id"]

    assert {key: context[key] for key in ("method", "path", "client_ip")} == {
        "method": "WS",
        "path": "/ws/context",
        "client_ip": "testclient",
    }
    assert len(context["request_id"]) == 32
    assert headers[b"x-request-id"] == context["request_id"].encode()
    assert re.fullmatch(r"[0-9a-f]{32}", supplied)
    assert supplied != context["request_id"]
    (line, _) = [
        (record.getMessage(), record.levelno)
        for record in caplog.records
        if record.name == "domainkit.access"
    ]
    assert line[0].startswith("WS /ws/context 101 ")
    assert line[1] == logging.INFO


def test_refused_and_failing_websockets_are_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A close before accepting logs 403; an error closes with 1011, logged once."""
    caplog.set_level(logging.DEBUG)
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws/refused"),
    ):
        pass
    with (
        pytest.raises(WebSocketDisconnect) as crashed,
        client.websocket_connect("/ws/crash"),
    ):
        pass

    assert (refused.value.code, crashed.value.code) == (1000, 1011)
    access = [
        (record.getMessage().split()[:3], record.levelno)
        for record in caplog.records
        if record.name == "domainkit.access"
    ]
    assert access == [
        (["WS", "/ws/refused", "403"], logging.WARNING),
        (["WS", "/ws/crash", "-"], logging.ERROR),
    ]
    errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == "domainkit.http.middleware"
    ]
    assert errors == ["Unhandled error in WS /ws/crash"]


def test_failing_websockets_are_raised_in_debug_mode(make_client: MakeClient) -> None:
    """Debug mode lets the error reach the server, as for HTTP."""
    client = make_client(DEBUG=True)

    with (
        pytest.raises(RuntimeError, match=r"^socket crashed$"),
        client.websocket_connect("/ws/crash"),
    ):
        pass


async def headerless(scope: Scope, receive: Receive, send: Send) -> None:
    """Accept, or answer 200, without headers: ASGI makes them optional."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200})
    await send({"type": "http.response.body", "body": b"ok"})


def started(app: ASGIApp, kind: str) -> dict[bytes, bytes]:
    """The headers ``app`` starts its response (or accept) with, for a raw scope.

    The scope names no client, which ASGI also makes optional.
    """
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    async def call() -> None:
        scope = {"type": kind, "method": "GET", "path": "/raw", "headers": []}
        await app(scope, receive, send)

    asyncio.run(call())
    return dict(sent[0]["headers"])


def test_raw_applications_may_omit_headers_and_clients() -> None:
    """The request id is added to a start or an accept that has no headers."""
    seen: list[str | None] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = current_request()
        seen.append(request.client_ip if request else "no context")
        await headerless(scope, receive, send)

    starts = [
        started(RequestContextMiddleware(app), kind) for kind in ("http", "websocket")
    ]

    assert seen == [None, None]
    assert [len(start[b"x-request-id"]) for start in starts] == [32, 32]


def test_request_body_limits(make_client: MakeClient) -> None:
    """Declared and streamed bodies over the limit get 413; bad lengths 400."""
    client = make_client(MAX_REQUEST_BODY_SIZE=10)

    assert client.post("/echo", content=b"x" * 10).json() == {"size": 10}
    declared = client.post("/echo", content=b"x" * 11)
    assert (declared.status_code, declared.json()["code"]) == (413, "content_too_large")
    assert declared.json()["detail"] == "Request body too large."
    invalid = client.post("/echo", content=b"x", headers={"Content-Length": "x"})
    assert (invalid.status_code, invalid.json()["code"]) == (
        400,
        "invalid_content_length",
    )
    assert invalid.json()["detail"] == "Invalid Content-Length header."

    def chunks() -> Iterator[bytes]:
        yield b"x" * 6
        yield b"x" * 6

    streamed = client.post("/echo", content=chunks())
    assert (streamed.status_code, streamed.json()["code"]) == (413, "content_too_large")
    assert streamed.json()["detail"] == "Request body too large."


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("conflict", 409, "conflict"),
        ("denied", 403, "permission_denied"),
        ("rule", 422, "business_rule_violation"),
        ("domain", 400, "domain_error"),
        ("custom", 409, "fully_booked"),
        ("concurrent", 409, "concurrent_update"),
    ],
)
def test_domain_errors_map_to_statuses(
    client: TestClient, kind: str, status: int, code: str
) -> None:
    """Each base error has a status; the body carries message, code, request id."""
    response = client.get(f"/errors/{kind}")

    assert response.status_code == status
    body = response.json()
    assert body["detail"] == f"{kind} happened"
    assert body["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_access_log_levels(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Probes log at DEBUG, client errors at WARNING, success at INFO."""
    caplog.set_level(logging.DEBUG, logger="domainkit.access")

    client.get("/health")
    client.get("/ok?token=secret")
    client.get("/missing")

    levels = {
        record.getMessage().split()[1]: record.levelno
        for record in caplog.records
        if record.name == "domainkit.access"
    }
    assert levels == {
        "/health": logging.DEBUG,
        "/ok": logging.INFO,
        "unmatched": logging.WARNING,  # No route: no path, which could be anything.
    }
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("/ready", "", "/ready"),
        ("/api/ready", "/api", "/ready"),
        ("/api/ready", "/api/", "/ready"),
        ("/api", "/api", ""),
        ("/apiary/ready", "/api", "/apiary/ready"),  # Not at a "/": kept whole.
        ("/ready", "/api", "/ready"),  # A server that left the root out.
        ("/ready", "/", "/ready"),
    ],
)
def test_request_paths_leave_the_root_path_out(
    path: str, root: str, expected: str
) -> None:
    """Every list of paths in settings is compared with this path."""
    assert request_path({"path": path, "root_path": root}) == expected
    assert request_path({"path": path}) == path


def test_exempt_and_quiet_paths_hold_behind_a_root_path(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Under ``--root-path /api`` probes are neither counted nor logged at INFO."""
    caplog.set_level(logging.DEBUG, logger="domainkit.access")
    app = make_client(
        RATE_LIMIT="1/minute",
        MIDDLEWARE=[
            "domainkit.http.middleware.RequestContextMiddleware",
            "domainkit.http.ratelimit.RateLimitMiddleware",
        ],
    ).app
    client = TestClient(app, root_path="/api")

    probes = [client.get("/api/health").status_code for _ in range(3)]
    counted = [client.get("/api/ok").status_code for _ in range(2)]

    assert probes == [200, 200, 200]
    assert counted == [200, 429]
    levels = {
        record.getMessage().split()[1]: record.levelno
        for record in caplog.records
        if record.name == "domainkit.access"
    }
    # Templates, without the root path; refused before routing: unmatched.
    assert levels == {
        "/health": logging.DEBUG,
        "/ok": logging.INFO,
        "unmatched": logging.WARNING,
    }


def test_refusals_are_logged_once_per_caller_a_while(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A flood of bad hosts or bodies from one caller writes a line, then counts.

    Another caller is written at once; the count comes with the caller's
    next line after ``THROTTLE_INTERVAL``.
    """
    app = make_client(MAX_REQUEST_BODY_SIZE=10).app
    flood = TestClient(app, client=("203.0.113.9", 1))
    other = TestClient(app, client=("198.51.100.7", 1))

    for _ in range(3):
        flood.get("/ok", headers={"Host": "evil.example"})
        flood.post("/echo", content=b"x" * 11)
    other.get("/ok", headers={"Host": "evil.example"})

    assert [r.getMessage() for r in caplog.records if r.name == SECURITY_LOGGER] == [
        "Rejected untrusted Host header 'evil.example'",
        "Rejected request body of 11 bytes (limit 10)",
        "Rejected untrusted Host header 'evil.example'",
    ]


def test_cors_preflight(make_client: MakeClient) -> None:
    """Allowed origins get CORS headers when CORSMiddleware is installed."""
    client = make_client(
        MIDDLEWARE=[
            "domainkit.http.middleware.SecurityHeadersMiddleware",
            "domainkit.http.middleware.CORSMiddleware",
            "domainkit.http.middleware.RequestContextMiddleware",
        ],
        CORS_ALLOWED_ORIGINS=["https://app.example"],
    )

    response = client.options(
        "/ok",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example"


def test_cors_answers_with_every_setting(make_client: MakeClient) -> None:
    """Methods, headers, and credentials come from ``CORS_*``; the request id shows."""
    client = make_client(
        MIDDLEWARE=[
            "domainkit.http.middleware.CORSMiddleware",
            "domainkit.http.middleware.RequestContextMiddleware",
        ],
        CORS_ALLOWED_ORIGINS=["https://app.example"],
        CORS_ALLOWED_METHODS=["GET", "PATCH"],
        CORS_ALLOWED_HEADERS=["Authorization", "X-Trace"],
        CORS_ALLOW_CREDENTIALS=True,
    )
    origin = {"Origin": "https://app.example"}

    preflight = client.options(
        "/ok",
        headers={
            **origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "X-Trace",
        },
    )
    simple = client.get("/ok", headers=origin)

    assert preflight.headers["Access-Control-Allow-Methods"] == "GET, PATCH"
    assert preflight.headers["Access-Control-Allow-Credentials"] == "true"
    assert "x-trace" in preflight.headers["Access-Control-Allow-Headers"].lower()
    assert simple.headers["Access-Control-Allow-Credentials"] == "true"
    assert simple.headers["Access-Control-Expose-Headers"] == "X-Request-ID"


def test_health_and_readiness(client: TestClient) -> None:
    """Liveness always answers; readiness hides failing names by default."""
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}

    AlphaConfig.healthy = False
    response = client.get("/ready")
    assert (response.status_code, response.json()) == (503, {"status": "unavailable"})


def test_readiness_details_are_opt_in(make_client: MakeClient) -> None:
    """``READINESS_DETAILS`` names failing domains and databases."""
    client = make_client(
        READINESS_DETAILS=True,
        DATABASES={"default": {"URL": "sqlite+aiosqlite:////nonexistent/dir/db"}},
    )
    AlphaConfig.healthy = False

    assert client.get("/ready").json()["failing"] == ["alpha", "database:default"]


def test_readiness_results_are_cached(make_client: MakeClient) -> None:
    """Within ``READINESS_CACHE_SECONDS`` probes reuse the last result."""
    client = make_client(READINESS_CACHE_SECONDS=60)
    assert client.get("/ready").status_code == 200

    AlphaConfig.healthy = False

    assert client.get("/ready").status_code == 200


def test_readiness_probes_share_one_round_of_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probes during a round wait for it; its result is cached from its end.

    The cache's clock moves only when the test moves it, so the round takes
    ten seconds of it and the probe after it comes four seconds later, on
    any machine.
    """
    rounds: list[int] = []
    timeouts: list[float] = []
    now = [0.0]
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: now[0]))
    begun, finish = asyncio.Event(), asyncio.Event()

    async def slow_check(seconds: float) -> list[str]:
        rounds.append(1)
        timeouts.append(seconds)
        begun.set()
        await finish.wait()
        return []

    async def quick_check(seconds: float) -> list[str]:
        timeouts.append(seconds)
        return []

    monkeypatch.setattr(registry, "check", slow_check)
    for checked in (connections, ledgers, caches):
        monkeypatch.setattr(checked, "check_all", quick_check)
    request = SimpleNamespace(app=SimpleNamespace(state=State()))

    async def probe_flood() -> list[list[str]]:
        check = health._failing_checks
        probes = [asyncio.ensure_future(check(request)) for _ in range(20)]  # type: ignore[arg-type]
        await begun.wait()  # Every probe has joined the round by now.
        probes[0].cancel()  # A probe that gives up stops no one else.
        now[0] = 10.0
        finish.set()  # The round ends ten seconds after it began.
        answers = await asyncio.gather(*probes[1:])
        now[0] = 14.0
        again = await health._failing_checks(request)  # type: ignore[arg-type]
        assert rounds == [1]  # Cached 5 s from its end (10), not its start (0).
        now[0] = 15.0
        begun.clear()
        finish.clear()
        expired = asyncio.ensure_future(check(request))  # type: ignore[arg-type]
        await begun.wait()
        finish.set()
        return [*answers, again, await expired]

    # A ledger configured: readiness checks ledgers only where one is (PERF-15).
    with override_settings(
        READINESS_CACHE_SECONDS=5,
        READINESS_TIMEOUT=0.5,
        LEDGERS={"audit": {"URL": "memory://"}},
    ):
        answers = asyncio.run(probe_flood())

    assert rounds == [1, 1]  # At 15 s the result is stale: a second round.
    assert answers == [[]] * 21
    assert timeouts == [0.5] * 8  # Each kind of check, bounded by the setting.


TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_readiness_checks_every_kind_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Domains, databases, ledgers, and caches are checked concurrently.

    Each kind waits until all four have started: run one after another,
    the first would wait for ever.
    """
    started: list[float] = []
    contexts: list[object] = []
    everyone: list[asyncio.Event] = []  # Made in the probe's loop.
    rounds: set[str] = set()

    async def check(seconds: float) -> list[str]:
        started.append(seconds)
        contexts.append((current_request(), running_operation(), current_ids()))
        rounds.update(task.get_name() for task in owned())
        if len(started) == 4:
            everyone[0].set()
        await everyone[0].wait()
        return [f"failing{len(started)}"] if len(started) == 4 else []

    monkeypatch.setattr(registry, "check", check)
    for checked in (connections, ledgers, caches):
        monkeypatch.setattr(checked, "check_all", check)
    request = SimpleNamespace(app=SimpleNamespace(state=State()))
    context = RequestContext("req-probe", None, "GET", "/ready")

    async def probe() -> list[str]:
        everyone.append(asyncio.Event())
        with (
            bind_request(context),
            run_operation(Operation("hash", "token")),
            remote_parent({"traceparent": TRACEPARENT}),
        ):
            async with asyncio.timeout(5):
                return await health._failing_checks(request)  # type: ignore[arg-type]

    # A ledger configured: readiness checks ledgers only where one is (PERF-15).
    with override_settings(
        READINESS_CACHE_SECONDS=0, LEDGERS={"audit": {"URL": "memory://"}}
    ):
        assert asyncio.run(probe()) == ["failing4"] * 4
    assert started == [5.0] * 4
    assert contexts == [(None, None, None)] * 4  # The probe's request stays its own.
    assert rounds == {"domainkit.readiness"}


def test_a_round_whose_checks_raise_is_kept_as_failing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A group that raises fails, by name, and is cached: a 503, not a 500 each.

    The other checks finish first, the error is logged once by the round,
    and the round's own task logs nothing.
    """
    finished: list[str] = []
    calls: list[str] = []

    async def broken(seconds: float) -> list[str]:
        calls.append("domains")
        raise RuntimeError("checks broke")

    async def slower(seconds: float) -> list[str]:
        for _ in range(50):  # Many loop passes: longer than the error takes.
            await asyncio.sleep(0)
        finished.append("databases")
        return ["database:default"]

    async def quick(seconds: float) -> list[str]:
        return []

    monkeypatch.setattr(registry, "check", broken)
    monkeypatch.setattr(connections, "check_all", slower)
    for checked in (ledgers, caches):
        monkeypatch.setattr(checked, "check_all", quick)
    request = SimpleNamespace(app=SimpleNamespace(state=State()))

    async def probe() -> list[list[str]]:
        first = await health._failing_checks(request)  # type: ignore[arg-type]
        assert finished == ["databases"]  # None left running behind.
        again = await health._failing_checks(request)  # type: ignore[arg-type]
        await asyncio.sleep(0)  # Done callbacks run on the next loop pass.
        return [first, again]

    with (
        override_settings(READINESS_CACHE_SECONDS=60),
        caplog.at_level(logging.DEBUG, "domainkit.tasks"),
    ):
        answers = asyncio.run(probe())
    assert answers == [["domains", "database:default"]] * 2
    assert calls == ["domains"]  # The second probe read the kept round.
    assert [r for r in caplog.records if r.name == "domainkit.tasks"] == []
    [logged] = [r for r in caplog.records if r.name == "domainkit.http.health"]
    assert logged.getMessage() == "Readiness checks of domains raised"
    assert logged.exc_info is not None
    assert str(logged.exc_info[1]) == "checks broke"


def test_a_raising_readiness_check_answers_503(
    make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the route: unavailable, and the group named with details on."""

    async def broken(seconds: float) -> list[str]:
        raise RuntimeError("checks broke")

    monkeypatch.setattr(caches, "check_all", broken)
    client = make_client(READINESS_DETAILS=True)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "failing": ["caches"]}


def test_a_cancelled_group_still_cancels_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only errors count as failing; a group cancelled under it is not one."""

    async def cancelled(seconds: float) -> list[str]:
        raise asyncio.CancelledError

    async def quick(seconds: float) -> list[str]:
        return []

    monkeypatch.setattr(registry, "check", quick)
    monkeypatch.setattr(connections, "check_all", cancelled)
    for checked in (ledgers, caches):
        monkeypatch.setattr(checked, "check_all", quick)
    state = State()

    async def check() -> None:
        with pytest.raises(asyncio.CancelledError):
            await health._check(state)

    asyncio.run(check())
    assert not hasattr(state, "readiness")


def test_a_round_left_on_another_loop_is_not_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round a finished event loop left pending is replaced, not awaited."""

    async def no_failures(seconds: float) -> list[str]:
        return []

    monkeypatch.setattr(registry, "check", no_failures)
    loop = asyncio.new_event_loop()
    stranded = loop.create_future()
    loop.close()
    state = State()
    state.readiness_round = stranded
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    assert asyncio.run(health._failing_checks(request)) == []  # type: ignore[arg-type]
    assert state.readiness_round is not stranded


def test_lifespan_runs_domain_hooks() -> None:
    """Domains start with the application and stop with it."""
    with TestClient(get_asgi_application()):
        assert AlphaConfig.calls == ["ready", "startup"]
    assert AlphaConfig.calls == ["ready", "startup", "shutdown"]


def test_api_docs_can_be_disabled(make_client: MakeClient) -> None:
    """``API_DOCS = False`` removes the documentation routes."""
    assert make_client().get("/openapi.json").status_code == 200
    assert make_client(API_DOCS=False).get("/openapi.json").status_code == 404


def test_the_application_describes_the_project() -> None:
    """Title, version, and debug come from settings; so do the docs routes."""
    with override_settings(PROJECT_NAME="billing", VERSION="1.2.3", DEBUG=True):
        app = get_asgi_application()
    with override_settings(API_DOCS=False):
        hidden = get_asgi_application()

    paths = {getattr(route, "path", "") for route in app.routes}
    hidden_paths = {getattr(route, "path", "") for route in hidden.routes}
    assert (app.title, app.version, app.debug) == ("billing", "1.2.3", True)
    assert {"/openapi.json", "/docs", "/redoc"} <= paths  # domainkit's own pages.
    assert (app.docs_url, app.redoc_url) == (None, None)  # Not FastAPI's.
    assert hidden.openapi_url is None
    assert not {"/openapi.json", "/docs", "/redoc"} & hidden_paths
    responses = app.openapi()["paths"]["/ok"]["get"]["responses"]
    assert responses["413"]["description"] == "Request body too large"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ROOT_ROUTER": "domainkit_tests.sample_routes.missing"}, "^ROOT_ROUTER: "),
        (
            {"ROOT_ROUTER": "domainkit_tests.sample_routes.NOT_A_ROUTER"},
            r"^ROOT_ROUTER: .* is not a APIRouter\.$",
        ),
        ({"MIDDLEWARE": ["domainkit.http.middleware.Nope"]}, "^MIDDLEWARE: "),
        (
            {"MIDDLEWARE": ["domainkit.http.middleware.REQUEST_ID_HEADER"]},
            r"^MIDDLEWARE: .* is not a type\.$",
        ),
    ],
)
def test_invalid_application_settings(
    overrides: dict[str, object], message: str
) -> None:
    """Bad dotted paths fail when the application is built."""
    with (
        override_settings(**overrides),
        pytest.raises(ImproperlyConfigured, match=message),
    ):
        get_asgi_application()


def test_application_without_root_router() -> None:
    """Without ROOT_ROUTER the application serves only the docs."""
    with override_settings(ROOT_ROUTER=None):
        app = get_asgi_application()

    paths = {getattr(route, "path", "") for route in app.routes}
    assert paths >= {"/docs", "/openapi.json"}
    assert "/ok" not in paths


@pytest.mark.parametrize(
    ("environment", "logged"), [("staging", True), ("test", False)]
)
def test_server_warnings_are_logged_where_the_service_is_deployed(
    environment: str,
    logged: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Staging and production log what the server reads past the limits."""
    monkeypatch.setattr(
        "domainkit.http.application.server_warnings",
        lambda app: ["The server reads 16 MiB WebSocket messages."],
    )
    with (
        override_settings(ENVIRONMENT=environment, ROOT_ROUTER=None),
        caplog.at_level(logging.WARNING, logger="domainkit.http.application"),
    ):
        get_asgi_application()

    expected = ["The server reads 16 MiB WebSocket messages."] if logged else []
    assert caplog.messages == expected


@pytest.mark.parametrize(
    ("status_code", "level"),
    [
        (399, logging.INFO),
        (400, logging.WARNING),
        (499, logging.WARNING),
        (500, logging.ERROR),
        (501, logging.ERROR),
    ],
)
def test_access_log_lines_carry_their_level_fields_and_time(
    status_code: int,
    level: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warnings from 400, errors from 500; fields for JSON logs; milliseconds."""
    readings = iter([10.0, 10.01234])
    monkeypatch.setattr(
        middleware_module, "time", SimpleNamespace(perf_counter=lambda: next(readings))
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status_code})
        await send({"type": "http.response.body", "body": b""})

    scope = {"type": "http", "method": "GET", "path": "/logged", "headers": []}
    with override_settings(ACCESS_LOG_PATH="raw"):
        middleware = RequestContextMiddleware(app)
    with caplog.at_level(logging.DEBUG, logger="domainkit.access"):
        asyncio.run(middleware(scope, receive_nothing, send_nowhere))

    [record] = caplog.records
    assert (record.levelno, record.getMessage()) == (
        level,
        f"GET /logged {status_code} 12.3ms",
    )
    assert record.access == {  # type: ignore[attr-defined]
        "method": "GET",
        "path": "/logged",
        "status": status_code,
        "duration_ms": 12.3,
    }


def test_only_waits_for_the_body_have_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole ``REQUEST_BODY_TIMEOUT`` for the body; none once it is in.

    A message without ``more_body`` is the whole body: waiting for the
    client to leave afterwards is not timed. The deadlines are recorded,
    not waited for.
    """
    deadlines: list[float] = []

    def timeout(delay: float) -> asyncio.Timeout:
        deadlines.append(delay)
        return asyncio.timeout(delay)

    monkeypatch.setattr(middleware_module, "asyncio", SimpleNamespace(timeout=timeout))
    received: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        received.extend([await receive(), await receive()])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[Message] = [
        {"type": "http.request", "body": b"whole"},
        {"type": "http.disconnect"},
    ]
    messages = iter(sent)

    async def receive() -> Message:
        return next(messages)

    with override_settings(REQUEST_BODY_TIMEOUT=7.0):
        limited = RequestBodyLimitMiddleware(app)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    asyncio.run(limited(scope, receive, send_nowhere))

    assert [message["type"] for message in received] == [
        "http.request",
        "http.disconnect",
    ]
    assert deadlines == [7.0]


async def receive_nothing() -> Message:
    """A client that says nothing more."""
    return {"type": "http.disconnect"}


async def send_nowhere(message: Message) -> None:
    """A server that takes any message."""


def test_websocket_messages_have_the_body_limit(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A message over MAX_REQUEST_BODY_SIZE, in UTF-8 bytes, closes with 1009."""
    client = make_client(MAX_REQUEST_BODY_SIZE=10)

    with (
        caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER),
        client.websocket_connect("/ws/echo") as socket,
    ):
        socket.send_bytes(b"x" * 10)
        assert socket.receive_json() == {"size": 10}
        socket.send_text("\u00e9" * 5)  # Ten bytes in UTF-8.
        assert socket.receive_json() == {"size": 10}
        socket.send_text("\u00e9" * 6)  # Six characters, twelve bytes.
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()

    assert closed.value.code == 1009  # Message too big.
    assert "Rejected request body of 12 bytes (limit 10)" in caplog.messages


@pytest.mark.anyio
async def test_websockets_closed_for_size_hear_nothing_more() -> None:
    """The application is told the client left; what it sends then is dropped."""
    told: list[Message] = []
    sent: list[Message] = []
    messages: list[Message] = [
        {"type": "websocket.receive", "text": "ok"},
        {"type": "websocket.receive", "bytes": b"x" * 11},
    ]
    incoming = iter(messages)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        told.extend([await receive(), await receive()])
        await send({"type": "websocket.send", "text": "too late"})

    async def receive() -> Message:
        return next(incoming)

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(MAX_REQUEST_BODY_SIZE=10):
        middleware = RequestBodyLimitMiddleware(app)
    await middleware({"type": "websocket", "headers": []}, receive, send)

    assert told == [
        {"type": "websocket.receive", "text": "ok"},
        {"type": "websocket.disconnect", "code": 1009},
    ]
    assert sent == [{"type": "websocket.close", "code": 1009}]


@pytest.mark.anyio
async def test_body_limit_ignores_other_messages() -> None:
    """Disconnect messages pass through the body counter untouched."""
    received: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(await receive())

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise AssertionError("nothing is sent")

    middleware = RequestBodyLimitMiddleware(app)
    await middleware({"type": "http", "headers": []}, receive, send)

    assert received == [{"type": "http.disconnect"}]


@pytest.mark.anyio
async def test_body_limit_answers_for_middleware_that_reads_bodies() -> None:
    """Middleware reading an oversized body outside FastAPI still gets a 413."""
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body"):
            pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 6, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(MAX_REQUEST_BODY_SIZE=10):
        middleware = RequestBodyLimitMiddleware(app)
    await middleware({"type": "http", "headers": []}, receive, send)

    assert sent[0]["status"] == 413
    assert b'"content_too_large"' in sent[1]["body"]
    assert b'"Request body too large."' in sent[1]["body"]


@pytest.mark.anyio
async def test_body_limit_reraises_once_a_response_started() -> None:
    """After the response has started, the limit cannot send its own."""
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive()).get("more_body"):
            pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x" * 6, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(MAX_REQUEST_BODY_SIZE=10):
        middleware = RequestBodyLimitMiddleware(app)
    with pytest.raises(RequestBodyTooLarge):
        await middleware({"type": "http", "headers": []}, receive, send)

    assert [message["type"] for message in sent] == ["http.response.start"]


@pytest.mark.anyio
async def test_body_limit_refuses_non_ascii_digits() -> None:
    """A Content-Length of Unicode digits ("²") is a 400, not a crash."""
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the request is refused first")

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(app)
    scope = {"type": "http", "headers": [(b"content-length", b"\xb2")]}
    await middleware(scope, receive, send)

    assert sent[0]["status"] == 400
    assert b'"invalid_content_length"' in sent[1]["body"]


def http_request(*bodies: Message) -> tuple[Receive, list[Message]]:
    """A client's ``receive`` sending ``bodies`` in turn, then waiting for good.

    Returns it with the list of what the application sends.
    """
    pending = list(bodies)
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        await asyncio.Event().wait()  # The rest of the body never comes.
        raise AssertionError  # pragma: no cover - cut off first.

    return receive, sent


async def read_body(scope: Scope, receive: Receive, send: Send) -> None:
    """An application that reads the whole body, then answers 200."""
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"read"})


@pytest.mark.anyio
async def test_a_body_that_does_not_arrive_in_time_gets_408(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Headers, a chunk, then nothing: 408, the connection closed, and logged."""
    caplog.set_level(logging.WARNING, SECURITY_LOGGER)
    receive, sent = http_request(
        {"type": "http.request", "body": b"{", "more_body": True}
    )

    async def send(message: Message) -> None:
        sent.append(message)

    with override_settings(REQUEST_BODY_TIMEOUT=0.01):
        middleware = RequestBodyLimitMiddleware(read_body)
    async with asyncio.timeout(30):  # Only if the body were not timed.
        await middleware({"type": "http", "headers": []}, receive, send)

    assert sent[0]["status"] == 408
    assert (b"connection", b"close") in sent[0]["headers"]
    assert b'"code":"request_timeout"' in sent[1]["body"]
    assert b'"The request body took too long to arrive."' in sent[1]["body"]
    assert [r.getMessage() for r in caplog.records] == [
        "Rejected request body that took over 0.01s to arrive"
    ]


@pytest.mark.anyio
async def test_the_body_time_counts_every_wait_and_nothing_else() -> None:
    """Waits for chunks add up; the application's own work between them does not."""
    with freeze_time("2026-01-01") as clock:

        def ticking(seconds: float, *, more: bool) -> Receive:
            async def receive() -> Message:
                clock.tick(seconds)  # As if the chunk took this long to come.
                await asyncio.sleep(0)
                return {"type": "http.request", "body": b"x", "more_body": more}

            return receive

        waits = iter([ticking(1801, more=True), ticking(1801, more=True)])

        async def slow_client() -> Message:
            try:
                return await next(waits)()
            except StopIteration:
                await asyncio.Event().wait()  # The third chunk never comes.
                raise AssertionError from None  # pragma: no cover - cut off first.

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with override_settings(REQUEST_BODY_TIMEOUT=3600.0):
            middleware = RequestBodyLimitMiddleware(read_body)
        async with asyncio.timeout(30):  # Only if the waits were not added up.
            await middleware({"type": "http", "headers": []}, slow_client, send)
        assert sent[0]["status"] == 408

        working = iter([ticking(0, more=True), ticking(0, more=False)])

        async def busy_app(scope: Scope, receive: Receive, send: Send) -> None:
            await receive()
            clock.tick(7200)  # The application works for two hours.
            await read_body(scope, receive, send)

        sent.clear()
        with override_settings(REQUEST_BODY_TIMEOUT=3600.0):
            patient = RequestBodyLimitMiddleware(busy_app)
        await patient({"type": "http", "headers": []}, lambda: next(working)(), send)
        assert sent[0]["status"] == 200


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["body", "answer", "disconnect"])
async def test_the_body_time_ends_with_the_body_or_the_response(
    ending: str,
) -> None:
    """Waiting after the body, once answering, or after a disconnect is not timed.

    The first message took longer than the whole budget; the next wait is
    still not cut off.
    """
    answering = ending == "answer"
    waiting, left = asyncio.Event(), asyncio.Event()
    told: list[Message] = []

    with freeze_time("2026-01-01") as clock:

        async def receive() -> Message:
            if not told:
                clock.tick(20)  # More than the whole budget went on the body.
                told.append(
                    {"type": "http.disconnect"}
                    if ending == "disconnect"
                    else {"type": "http.request", "body": b"x", "more_body": answering}
                )
                return told[-1]
            waiting.set()
            await left.wait()
            return {"type": "http.disconnect"}

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await receive()
            if answering:
                await send({"type": "http.response.start", "status": 200})
            listening = asyncio.ensure_future(receive())
            await waiting.wait()
            left.set()
            told.append(await listening)

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with override_settings(REQUEST_BODY_TIMEOUT=10.0):
            middleware = RequestBodyLimitMiddleware(app)
        await middleware({"type": "http", "headers": []}, receive, send)

    assert len(told) == 2  # The second wait ended with the client, not cut off.
    assert told[-1] == {"type": "http.disconnect"}
    assert [message["status"] for message in sent] == ([200] if answering else [])


@pytest.mark.anyio
async def test_a_slow_body_through_the_application_gets_408() -> None:
    """FastAPI passes the refusal on as it reads the body; the error is the usual."""
    gate = asyncio.Event()

    async def slowly() -> AsyncGenerator[bytes]:
        yield b'{"name":'
        await gate.wait()
        yield b'"widget"}'  # pragma: no cover - never read.

    body = slowly()
    with override_settings(
        ROOT_ROUTER="domainkit_tests.deadline_routes.router", REQUEST_BODY_TIMEOUT=0.01
    ):
        async with AsyncTestClient(get_asgi_application(), lifespan=False) as client:
            response = await client.post(
                "/echo", content=body, headers={"Content-Type": "application/json"}
            )
    await body.aclose()

    assert response.status_code == 408
    assert response.headers["Connection"] == "close"
    assert response.json() == {
        "detail": "The request body took too long to arrive.",
        "code": "request_timeout",
        "request_id": response.headers["X-Request-ID"],
    }


def test_http_errors_use_the_common_format(client: TestClient) -> None:
    """Unknown routes, wrong methods, and HTTPException share one body shape."""
    missing = client.get("/nope")
    assert missing.json() == {
        "detail": "Not Found",
        "code": "not_found",
        "request_id": missing.headers["X-Request-ID"],
    }
    wrong_method = client.delete("/ok")
    assert (wrong_method.status_code, wrong_method.json()["code"]) == (
        405,
        "method_not_allowed",
    )
    assert wrong_method.headers["Allow"] == "GET"

    teapot = client.get("/http/418")
    assert teapot.json()["code"] == "i_m_a_teapot"
    assert teapot.headers["X-Reason"] == "test"
    structured = client.get("/http/400", params={"structured": "true"})
    assert structured.json()["detail"] == "Bad request"
    unknown = client.get("/http/599", params={"detail": "Custom."})
    assert (unknown.json()["code"], unknown.json()["detail"]) == (
        "http_error",
        "Custom.",
    )
    not_modified = client.get("/http/304")
    assert (not_modified.status_code, not_modified.content) == (304, b"")

    limited = client.get("/api-error")
    assert (limited.status_code, limited.json()["code"]) == (429, "rate_limited")
    assert limited.headers["Retry-After"] == "1"


def test_validation_errors_never_echo_input(client: TestClient) -> None:
    """422 bodies say where and why, never what was sent."""
    response = client.post("/typed", json={"count": "hunter2", "token": "s3cret"})

    body = response.json()
    assert (response.status_code, body["code"]) == (422, "validation_error")
    assert [error["loc"] for error in body["errors"]] == [
        ["body", "count"],
        ["body", "token"],
    ]
    assert "hunter2" not in response.text
    assert "s3cret" not in response.text


def test_openapi_documents_the_error_body(client: TestClient) -> None:
    """The 422 schema in OpenAPI is the body the API really returns."""
    schema = client.get("/openapi.json").json()

    typed = schema["paths"]["/typed"]["post"]["responses"]["422"]
    assert typed["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert "HTTPValidationError" not in schema["components"]["schemas"]
