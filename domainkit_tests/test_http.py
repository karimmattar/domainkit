"""HTTP integration: application factory, middleware, errors, and health."""

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.middleware import (
    AllowedHostsMiddleware,
    RequestBodyLimitMiddleware,
    RequestBodyTooLarge,
    host_allowed,
    security_headers,
)
from domainkit.log import SECURITY_LOGGER
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


def test_responses_carry_security_headers_and_request_ids(client: TestClient) -> None:
    """Headers are added unless set; valid caller request ids are echoed."""
    response = client.get("/ok")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.headers["X-Request-ID"]) == 32

    assert client.get("/framed").headers["X-Frame-Options"] == "SAMEORIGIN"
    supplied = client.get("/ok", headers={"X-Request-ID": "abc-123"})
    assert supplied.headers["X-Request-ID"] == "abc-123"
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
    client.get("/ok", headers={"Host": "x" * 300})
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


def test_request_body_limits(make_client: MakeClient) -> None:
    """Declared and streamed bodies over the limit get 413; bad lengths 400."""
    client = make_client(MAX_REQUEST_BODY_SIZE=10)

    assert client.post("/echo", content=b"x" * 10).json() == {"size": 10}
    declared = client.post("/echo", content=b"x" * 11)
    assert (declared.status_code, declared.json()["code"]) == (413, "content_too_large")
    invalid = client.post("/echo", content=b"x", headers={"Content-Length": "x"})
    assert (invalid.status_code, invalid.json()["code"]) == (
        400,
        "invalid_content_length",
    )

    def chunks() -> Iterator[bytes]:
        yield b"x" * 6
        yield b"x" * 6

    streamed = client.post("/echo", content=chunks())
    assert (streamed.status_code, streamed.json()["code"]) == (413, "content_too_large")


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
        "/missing": logging.WARNING,
    }
    assert "secret" not in caplog.text


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

    assert (app.title, app.version, app.debug) == ("billing", "1.2.3", True)
    assert (app.docs_url, app.redoc_url) == ("/docs", "/redoc")
    assert (hidden.docs_url, hidden.redoc_url, hidden.openapi_url) == (None,) * 3
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
