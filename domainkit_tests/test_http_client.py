"""The outgoing HTTP client: deadlines, retries, circuits, the guard, and stubs."""

import asyncio
import gzip
import ipaddress
import json
import logging
import math
import socket
import ssl
import sys
import threading
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpcore2
import httpx2
import pytest
from pydantic import ValidationError

from domainkit import deadlines, lifecycle, tracing
from domainkit.clock import freeze_time
from domainkit.conf import Settings, override_settings, settings
from domainkit.context import RequestContext, bind_request
from domainkit.http import client as client_module
from domainkit.http import guard
from domainkit.http.client import (
    BREAKER_HOSTS,
    CallTimeout,
    CircuitBreaker,
    CircuitOpen,
    GuardedBackend,
    HttpClient,
    ResponseTooLarge,
    UnsafeURL,
    backoff,
    close_all,
    retry_after,
    stub,
    tls_context,
)
from domainkit.log import build_logging_config, configure_logging, flush_logs
from domainkit_tests.test_drivers import client_certificate  # noqa: F401 - fixture.
from domainkit_tests.test_observability import tracer_provider  # noqa: F401 - fixture.

pytestmark = pytest.mark.anyio

LOGGER = "domainkit.http.client"
URL = "https://api.example.com/v1/rates"
LOCAL = (ipaddress.ip_network("127.0.0.0/8"),)

type Answer = Callable[[httpx2.Request], httpx2.Response]


def status(code: int, **headers: str) -> Answer:
    """Answer ``code`` with ``headers``."""
    return lambda request: httpx2.Response(code, headers=headers, content=b"{}")


def fail(error: type[httpx2.TransportError]) -> Answer:
    """Fail the attempt with ``error``."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        raise error("failed", request=request)

    return answer


class Upstream:
    """A stubbed server: answers in turn (the last one again), keeps requests."""

    def __init__(self, *answers: Answer) -> None:
        """Answer with ``answers``, in order."""
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record ``request``; give the next answer."""
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer(request)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits before retries, recorded instead of slept."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(client_module, "_sleep", record)
    return recorded


@pytest.fixture
def no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls to this machine go straight to it, whatever the environment says."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def circuits(api: HttpClient) -> dict[str, Any]:
    """The circuits a client keeps, by origin."""
    assert api.breaker is not None
    return dict(api.breaker._circuits)


def options(**http_client: object) -> Any:  # noqa: ANN401 - a context manager.
    """``HTTP_CLIENT`` changed by ``http_client``."""
    return override_settings(HTTP_CLIENT=http_client)


# Calls, stubs, and what they carry ----------------------------------------------


async def test_calls_go_to_the_stub_with_what_they_carry() -> None:
    """Base URL, headers (the call's win), the request id, and the timeouts."""
    upstream = Upstream(lambda request: httpx2.Response(200, json={"ok": True}))
    rates = HttpClient(
        base_url="https://api.example.com/v1/", headers={"X-Client": "rates"}
    )
    context = RequestContext("req-1", None, "GET", "/")
    with stub(upstream), bind_request(context):
        response = await rates.get(
            "rates/EUR", params={"day": "today"}, headers={"X-Client": "mine"}
        )

    assert response.json() == {"ok": True}
    [sent] = upstream.requests
    assert str(sent.url) == "https://api.example.com/v1/rates/EUR?day=today"
    assert sent.headers["user-agent"] == "domainkit"
    assert sent.headers["x-request-id"] == "req-1"
    assert sent.headers["x-client"] == "mine"
    assert "traceparent" not in sent.headers
    assert "accept-encoding" not in sent.headers or (
        sent.headers["accept-encoding"] != "identity"
    )
    timeouts = sent.extensions["timeout"]
    assert timeouts["connect"] == 5.0
    assert 9.0 < timeouts["read"] <= 10.0


async def test_headers_replace_others_of_the_same_name_whatever_their_case() -> None:
    """The client's replace the defaults, and the call's the client's: one each."""
    upstream = Upstream(status(200))
    api = HttpClient(headers={"user-agent": "rates/1.0", "X-Client": "rates"})
    context = RequestContext("req-1", None, "GET", "/")
    with stub(upstream), bind_request(context):
        await api.get(URL, headers={"x-client": "mine", "x-request-id": "own"})

    [sent] = upstream.requests
    assert sent.headers.get_list("user-agent") == ["rates/1.0"]
    assert sent.headers.get_list("x-client") == ["mine"]
    assert sent.headers.get_list("x-request-id") == ["own"]


async def test_a_base_url_keeps_calls_on_its_origin() -> None:
    """Paths are appended to it; a URL elsewhere is refused, not followed."""
    upstream = Upstream(status(200))
    api = HttpClient(base_url="https://api.example.com/v1")
    with stub(upstream):
        await api.get("/rates")
        await api.get("rates?day=1")
        await api.get("https://api.example.com/v2/other")
        await HttpClient(base_url="https://api.example.com/X/").get("X/y")
        await HttpClient(base_url="http://api.example.com").get(
            "http://api.example.com:80/z"
        )
        for elsewhere in (
            "https://evil.example/",
            "//evil.example/x",
            "http://api.example.com/v1/rates",
            "https://api.example.com:8443/v1/rates",
        ):
            with pytest.raises(
                UnsafeURL, match=r"^The URL leaves the client's base URL\.$"
            ):
                await api.get(elsewhere)

    assert [str(r.url) for r in upstream.requests] == [
        "https://api.example.com/v1/rates",
        "https://api.example.com/v1/rates?day=1",
        "https://api.example.com/v2/other",
        "https://api.example.com/X/X/y",
        "http://api.example.com/z",
    ]
    message = (
        r"^A client's base URL names a scheme and a host, and has no query "
        r"or fragment\.$"
    )
    for bad in ("/v1", "https://api.example.com/?key=1", "https://a.example/#x"):
        with pytest.raises(ValueError, match=message):
            HttpClient(base_url=bad)


@pytest.mark.parametrize(
    "climbing",
    [
        "../admin",
        "./../admin",
        "rates/../../admin",
        "..",
        "rates/.",
        "%2e%2e/admin",
        "%2E%2E/admin",
        ".%2e/admin",
        "%2e./admin",
        "%252e%252e/admin",  # Decoded twice by some servers.
        "..%2fadmin",
        "rates%2F..%2F..%2Fadmin",
        "..\\admin",
        "rates\\..\\..\\admin",
        "..%5cadmin",
        "../admin?page=1",
    ],
)
async def test_a_relative_url_may_not_climb_out_of_the_base_path(
    climbing: str,
) -> None:
    """A ``.`` or ``..`` segment, spelled out or percent-encoded, is refused."""
    upstream = Upstream(status(200))
    api = HttpClient(base_url="https://api.example.com/v1/accounts/")
    with (
        stub(upstream),
        pytest.raises(UnsafeURL, match=r"^The URL leaves the client's base URL\.$"),
    ):
        await api.get(climbing)

    assert upstream.requests == []


async def test_relative_urls_without_dot_segments_stay_under_the_base_path() -> None:
    """Dots inside a segment, or in the query string, are no climb."""
    upstream = Upstream(status(200))
    api = HttpClient(base_url="https://api.example.com/v1/accounts/")
    with stub(upstream):
        for path in ("42", "a.b/...", "..x/.y", "42?next=../../admin", "%2e%2ex"):
            await api.get(path)

    assert [r.url.raw_path for r in upstream.requests] == [
        b"/v1/accounts/42",
        b"/v1/accounts/a.b/...",
        b"/v1/accounts/..x/.y",
        b"/v1/accounts/42?next=../../admin",
        b"/v1/accounts/%2e%2ex",
    ]


@pytest.mark.usefixtures("tracer_provider")
async def test_calls_carry_the_trace_context() -> None:
    """The active span's traceparent goes with the call."""
    upstream = Upstream(status(200))
    with stub(upstream), tracing.span("calling"):
        await HttpClient().get(URL)
        expected = tracing.trace_headers()

    assert upstream.requests[0].headers["traceparent"] == expected["traceparent"]


async def test_every_method_has_a_shortcut() -> None:
    """get, head, options, post, put, patch, delete, and request itself.

    Each passes its options on.
    """
    upstream = Upstream(status(204))
    api = HttpClient()
    shortcuts = ("get", "head", "options", "post", "put", "patch", "delete")
    with stub(upstream):
        for name in shortcuts:
            await getattr(api, name)(URL, headers={"X-Shortcut": name})
        await api.request("trace", URL)

    assert [(r.method, r.headers.get("x-shortcut")) for r in upstream.requests] == [
        ("GET", "get"),
        ("HEAD", "head"),
        ("OPTIONS", "options"),
        ("POST", "post"),
        ("PUT", "put"),
        ("PATCH", "patch"),
        ("DELETE", "delete"),
        ("TRACE", None),
    ]


async def test_bodies_are_sent_as_httpx2_sends_them() -> None:
    """JSON, form data, and raw content."""
    upstream = Upstream(status(201))
    api = HttpClient()
    with stub(upstream):
        await api.post(URL, json={"a": 1})
        await api.post(URL, data={"a": "1"})
        await api.post(URL, content=b"raw")

    assert [r.content for r in upstream.requests] == [b'{"a":1}', b"a=1", b"raw"]


async def test_async_handlers_and_transports_stub_calls() -> None:
    """A stub takes an async handler, or a transport as it is."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="async")

    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, text="t"))
    with stub(handler):
        assert (await HttpClient().get(URL)).text == "async"
    with stub(transport):
        assert (await HttpClient().get(URL)).text == "t"
    own = HttpClient(transport=transport)
    assert (await own.get(URL)).text == "t"
    await own.aclose()


async def test_options_are_checked() -> None:
    """Timeouts finite and above 0, retries from 0 to 10, a printable user agent."""
    for refused in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError, match="a finite number above 0"):
            HttpClient(timeout=refused)
        with pytest.raises(ValueError, match="a finite number above 0"):
            await HttpClient().get(URL, timeout=refused)
    with pytest.raises(ValueError, match="0 to 10 times"):
        HttpClient(retries=11)
    assert HttpClient(retries=10).retries == 10
    with stub(Upstream(status(200))):
        assert (await HttpClient().get(URL, timeout=0.5)).status_code == 200
    with pytest.raises(ValidationError, match="USER_AGENT"):
        Settings.model_validate({"HTTP_CLIENT": {"USER_AGENT": "a\r\nX-Evil: 1"}})


@pytest.mark.parametrize(
    ("key", "highest"),
    [
        ("TIMEOUT", 3600.0),
        ("MAX_BACKOFF", 600.0),
        ("BREAKER_COOLDOWN", 3600.0),
        ("MAX_RESPONSE_BYTES", 1_073_741_824),
    ],
)
def test_http_client_settings_have_upper_bounds(key: str, highest: float) -> None:
    """Generous, but bounded; the defaults stay well inside."""
    assert Settings.model_validate({"HTTP_CLIENT": {key: highest}})
    with pytest.raises(ValidationError, match=key):
        Settings.model_validate({"HTTP_CLIENT": {key: highest * 2}})


def test_a_guarded_client_takes_no_transport() -> None:
    """Its address checks are made as it connects; a transport would skip them."""
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200))
    with pytest.raises(
        ValueError,
        match=(
            r"^A guarded client checks each address as it connects, so it "
            r"cannot use a transport of its own; in tests, use stub\(\)\.$"
        ),
    ):
        HttpClient(guard=True, transport=transport)


# Retries ------------------------------------------------------------------------


async def test_idempotent_calls_are_retried_after_failures(waits: list[float]) -> None:
    """A 503, then a read timeout, then success: three attempts, jittered waits."""
    upstream = Upstream(status(503), fail(httpx2.ReadTimeout), status(200))
    with options(BACKOFF=0.1, MAX_BACKOFF=1.0), stub(upstream):
        response = await HttpClient().get(URL)

    assert response.status_code == 200
    assert len(upstream.requests) == 3
    assert len(waits) == 2
    assert 0 <= waits[0] <= 0.1
    assert 0 <= waits[1] <= 0.2


@pytest.mark.parametrize(
    ("answers", "retry", "attempts"),
    [
        ((status(503), status(200)), None, 1),  # POST: maybe processed.
        ((fail(httpx2.ReadTimeout), status(200)), None, 1),  # Maybe processed.
        ((fail(httpx2.ConnectError), status(200)), None, 2),  # Never sent.
        ((fail(httpx2.ConnectTimeout), status(200)), None, 2),
        ((fail(httpx2.PoolTimeout), status(200)), None, 2),
        ((status(503), status(200)), True, 2),  # Asked to.
        ((fail(httpx2.ConnectError), status(200)), False, 1),  # Told not to.
    ],
)
async def test_other_calls_are_retried_only_when_safe_or_asked(
    waits: list[float], answers: tuple[Answer, ...], retry: bool | None, attempts: int
) -> None:
    """A POST is sent again only if nothing was sent, or when retry=True."""
    upstream = Upstream(*answers)
    with options(BACKOFF=0.0), stub(upstream), suppress(httpx2.TransportError):
        await HttpClient().post(URL, retry=retry)

    assert len(upstream.requests) == attempts


async def test_a_retry_waits_for_real() -> None:
    """Without a stand-in for the wait, the client sleeps its backoff."""
    with options(BACKOFF=0.0), stub(Upstream(status(503), status(200))):
        assert (await HttpClient().get(URL)).status_code == 200


async def test_retries_end_with_the_last_answer_or_error(waits: list[float]) -> None:
    """Out of retries: the last answer is returned, or the last error raised."""
    with options(RETRIES=1, BACKOFF=0.0), stub(Upstream(status(503))):
        assert (await HttpClient().get(URL)).status_code == 503
    with (
        stub(Upstream(fail(httpx2.RemoteProtocolError))),
        pytest.raises(httpx2.RemoteProtocolError),
    ):
        await HttpClient(retries=2).get(URL)
    with stub(Upstream(status(400))):
        assert (await HttpClient().get(URL)).status_code == 400  # Not retried.
    assert len(waits) == 3


async def test_retry_after_is_honoured_up_to_max_backoff(waits: list[float]) -> None:
    """Retry-After sets the wait; one longer than MAX_BACKOFF is not waited."""
    with options(BACKOFF=0.0, MAX_BACKOFF=2.0):
        with stub(Upstream(status(429, **{"Retry-After": "1"}), status(200))):
            assert (await HttpClient().get(URL)).status_code == 200
        with stub(Upstream(status(429, **{"Retry-After": "3"}), status(200))):
            assert (await HttpClient().get(URL)).status_code == 429
        with stub(Upstream(status(429, **{"Retry-After": "2"}), status(200))):
            assert (await HttpClient().get(URL)).status_code == 200
        with stub(Upstream(status(503, **{"Retry-After": "soon"}), status(200))):
            assert (await HttpClient().get(URL)).status_code == 200

    assert waits == [1.0, 2.0, 0.0]


async def test_a_wait_longer_than_the_time_left_is_not_waited(
    waits: list[float],
) -> None:
    """Retrying makes no sense when the call would run out of time waiting."""
    with (
        options(BACKOFF=0.0, MAX_BACKOFF=5.0),
        stub(Upstream(status(503, **{"Retry-After": "2"}), status(200))),
    ):
        response = await HttpClient(timeout=1.0).get(URL)
    with (
        options(BACKOFF=0.0, MAX_BACKOFF=5.0),
        freeze_time("2026-01-01"),
        stub(Upstream(status(503, **{"Retry-After": "2"}), status(200))),
    ):
        exactly = await HttpClient(timeout=2.0).get(URL)  # Nothing left after it.

    assert response.status_code == 503
    assert exactly.status_code == 503
    assert waits == []


def test_backoff_spreads_waits_up_to_a_doubling_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full jitter: anywhere from 0 up to base times 2^attempt, capped."""
    monkeypatch.setattr("domainkit.http.client.random.random", lambda: 1.0)
    assert [backoff(attempt, 0.1, 0.5) for attempt in range(4)] == [
        0.1,
        0.2,
        0.4,
        0.5,
    ]
    monkeypatch.setattr("domainkit.http.client.random.random", lambda: 0.5)
    assert backoff(2, 0.1, 10.0) == pytest.approx(0.2)


def test_retry_after_reads_seconds_or_a_date() -> None:
    """A number of seconds, or an HTTP date from now; anything else is ignored."""
    with freeze_time("2026-10-21T07:28:00Z"):
        assert retry_after(None) is None
        assert retry_after(" 3 ") == 3.0
        assert retry_after("Wed, 21 Oct 2026 07:28:02 GMT") == 2.0
        assert retry_after("Wed, 21 Oct 2026 07:28:05 -0000") == 5.0
        assert retry_after("Wed, 21 Oct 2026 07:27:00 GMT") == 0.0
        for nonsense in ("soon", "-1", "1.5", "²"):
            assert retry_after(nonsense) is None


# Timeouts and the deadline ------------------------------------------------------


async def hang(request: httpx2.Request) -> httpx2.Response:
    """A server that never answers."""
    await asyncio.Event().wait()
    raise AssertionError  # pragma: no cover - never reached.


async def test_a_call_is_bounded_by_its_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past its timeout a call raises CallTimeout, which counts against the host."""
    caplog.set_level(logging.WARNING, LOGGER)
    with stub(hang), pytest.raises(CallTimeout):
        await HttpClient(timeout=0.05).get(URL)  # One failure of five: still closed.
    caplog.clear()
    slow = HttpClient(timeout=0.05)
    with options(BREAKER_FAILURES=1), stub(hang):
        with pytest.raises(CallTimeout, match=r"took longer than 0\.05s"):
            await slow.get(URL + "?key=SECRET")
        with pytest.raises(CircuitOpen):
            await slow.get(URL)

    logged = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert logged == [
        (
            "The circuit to https://api.example.com:443 is open for 30s after 1 "
            "failures in a row"
        ),
        f"GET {URL} took longer than its 0.05s",
    ]


async def test_the_deadline_bounds_every_call() -> None:
    """The work's deadline cuts the call; the host is not blamed for it."""
    api = HttpClient(timeout=60.0)
    outer = deadlines.timeout(0.05)

    async def call_within_deadline() -> None:
        async with outer:
            await api.get(URL)

    with stub(hang), pytest.raises(TimeoutError) as raised:
        await call_within_deadline()
    assert type(raised.value) is TimeoutError  # The block that set the deadline.
    assert outer.expired()
    assert circuits(api) == {}

    upstream = Upstream(status(200))
    with freeze_time("2026-01-01") as clock, stub(upstream):
        async with deadlines.timeout(1.0):
            clock.tick(1)
            with pytest.raises(deadlines.DeadlineExceeded):
                await api.get(URL)
    assert upstream.requests == []


async def test_a_call_from_a_task_started_inside_the_deadline_keeps_it() -> None:
    """The task times itself, and still raises DeadlineExceeded, not CallTimeout."""
    api = HttpClient(timeout=60.0)
    with freeze_time("2026-01-01") as clock, stub(hang):
        async with deadlines.timeout(60.0):
            clock.tick(59.95)  # 0.05s left, on the clock deadlines read.
            task = asyncio.create_task(api.get(URL))
            with pytest.raises(deadlines.DeadlineExceeded):
                await task
    assert circuits(api) == {}


async def test_a_timeout_error_from_inside_the_call_is_not_its_timeout() -> None:
    """Raised by the transport, it goes on as it is, and blames no host."""

    def timing_out(request: httpx2.Request) -> httpx2.Response:
        raise TimeoutError("The transport's own.")

    api = HttpClient(retries=0)
    with stub(timing_out), pytest.raises(TimeoutError) as raised:
        await api.get(URL)
    assert type(raised.value) is TimeoutError
    assert circuits(api) == {}


async def test_each_attempt_is_told_the_time_left() -> None:
    """httpx2's own timeouts never exceed what the deadline leaves."""
    upstream = Upstream(status(200))
    with freeze_time("2026-01-01"), stub(upstream):
        async with deadlines.timeout(2.0):
            await HttpClient().get(URL)

    timeouts = upstream.requests[0].extensions["timeout"]
    assert (timeouts["read"], timeouts["connect"], timeouts["pool"]) == (2.0, 2.0, 2.0)

    with freeze_time("2026-01-01"), stub(upstream):
        await HttpClient(timeout=60.0).get(URL)  # Longer than HTTP_CLIENT's 10.
    timeouts = upstream.requests[1].extensions["timeout"]
    assert (timeouts["read"], timeouts["connect"]) == (60.0, 5.0)


# The circuit breaker ------------------------------------------------------------


async def test_a_failing_host_trips_its_circuit_then_is_tried_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failures in a row open it; after the cooldown one trial closes it."""
    caplog.set_level(logging.WARNING, LOGGER)
    upstream = Upstream(status(500), status(502), status(200))
    api = HttpClient(retries=0)
    with (
        options(BREAKER_FAILURES=2, BREAKER_COOLDOWN=30.0),
        freeze_time("2026-01-01") as clock,
        stub(upstream),
    ):
        assert (await api.get(URL)).status_code == 500
        assert (await api.get(URL)).status_code == 502
        with pytest.raises(CircuitOpen, match=r"api\.example\.com:443") as refused:
            await api.get(URL)
        assert refused.value.request.url == URL
        assert len(upstream.requests) == 2
        assert (await api.get("https://other.example.com/")).status_code == 200
        clock.tick(30)
        assert (await api.get(URL)).status_code == 200  # The trial.
        assert (await api.get(URL)).status_code == 200  # Closed again.

    opened = [r.getMessage() for r in caplog.records if "is open" in r.getMessage()]
    assert opened == [
        (
            "The circuit to https://api.example.com:443 is open for 30s after 2 "
            "failures in a row"
        )
    ]


async def test_circuits_are_kept_per_origin_with_its_port() -> None:
    """An http origin's default port is 80 (https's, 443), written or not."""
    api = HttpClient(retries=0)
    with options(BREAKER_FAILURES=1), stub(Upstream(status(503))):
        await api.get("http://api.example.com/")
        with pytest.raises(
            CircuitOpen,
            match=(
                r"^Calls to http://api\.example\.com:80 fail at once: its circuit "
                r"is open\.$"
            ),
        ):
            await api.get("http://api.example.com:80/other")
        assert (await api.get("http://api.example.com:8080/")).status_code == 503


async def test_one_trial_at_a_time_and_a_cancelled_trial_frees_the_next() -> None:
    """While a trial runs, other calls fail at once; cancelled, it lets one try."""
    started = asyncio.Event()

    async def slow(request: httpx2.Request) -> httpx2.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover - never reached.

    api = HttpClient(retries=0)
    with (
        options(BREAKER_FAILURES=1, BREAKER_COOLDOWN=10.0),
        freeze_time("2026-01-01") as clock,
    ):
        with stub(Upstream(status(503))):
            await api.get(URL)
        clock.tick(10)
        with stub(slow):
            trial = asyncio.create_task(api.get(URL))
            await started.wait()
            with pytest.raises(CircuitOpen):
                await api.get(URL)
            trial.cancel()
            with pytest.raises(asyncio.CancelledError):
                await trial
        with stub(Upstream(status(200))):
            assert (await api.get(URL)).status_code == 200


async def test_a_failed_trial_opens_the_circuit_again() -> None:
    """The cooldown starts over."""
    api = HttpClient(retries=0)
    upstream = Upstream(status(503))
    with (
        options(BREAKER_FAILURES=1, BREAKER_COOLDOWN=10.0),
        freeze_time("2026-01-01") as clock,
        stub(upstream),
    ):
        await api.get(URL)
        clock.tick(10)
        await api.get(URL)  # The trial fails.
        clock.tick(5)
        with pytest.raises(CircuitOpen):
            await api.get(URL)
    assert len(upstream.requests) == 2


def test_the_breaker_forgets_hosts_that_answer_and_the_oldest() -> None:
    """Successes (4xx included) close a circuit; at most BREAKER_HOSTS are kept."""
    breaker = CircuitBreaker()
    assert breaker.failed("a", 2) is False
    breaker.succeeded("a")
    assert breaker._circuits == {}
    for host in range(BREAKER_HOSTS + 1):
        breaker.failed(str(host), 5)
    assert len(breaker._circuits) == BREAKER_HOSTS
    assert "0" not in breaker._circuits
    breaker.abandoned("nobody")  # Unknown hosts are no trouble.
    assert breaker.admits("nobody", 30.0)


async def test_a_client_without_a_breaker_never_refuses_a_call(
    waits: list[float],
) -> None:
    """One caller's failing URLs on a host never refuse another's calls.

    A guarded client shared by tenants: tenant A's URLs fail, tenant B's
    call to the same host still goes out. Failures, timeouts, successes,
    and cancelled calls leave nothing behind.
    """

    def upstream(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500 if request.url.path.startswith("/broken") else 200)

    shared = HttpClient(guard=True, retries=0, breaker=False)
    assert shared.breaker is None
    with options(BREAKER_FAILURES=1), stub(upstream):
        for tenant_a in range(5):
            response = await shared.get(f"https://hooks.example/broken/{tenant_a}")
            assert response.status_code == 500
        assert (await shared.post("https://hooks.example/tenant-b")).status_code == 200
    with options(BREAKER_FAILURES=1):
        with stub(fail(httpx2.ConnectError)), pytest.raises(httpx2.ConnectError):
            await shared.get("https://hooks.example/down")
        with stub(hang), pytest.raises(CallTimeout):
            await shared.get("https://hooks.example/slow", timeout=0.01)
        with (
            stub(lambda request: httpx2.Response(200, content=b"x" * 2048)),
            options(MAX_RESPONSE_BYTES=1024),
            pytest.raises(ResponseTooLarge),
        ):
            await shared.get("https://hooks.example/large")
        with stub(Upstream(status(200))):
            assert (await shared.get("https://hooks.example/")).status_code == 200

    shared_with_breaker = HttpClient(guard=True, retries=0)
    with options(BREAKER_FAILURES=1), stub(upstream):
        await shared_with_breaker.get("https://hooks.example/broken/0")
        with pytest.raises(CircuitOpen):
            await shared_with_breaker.post("https://hooks.example/tenant-b")


async def test_answers_below_500_count_as_success() -> None:
    """A 404 is the host working: it resets the count."""
    api = HttpClient(retries=0)
    with options(BREAKER_FAILURES=2), stub(Upstream(status(500), status(404))):
        await api.get(URL)
        await api.get(URL)
    assert circuits(api) == {}


# Bounded answers ----------------------------------------------------------------


async def test_answers_are_read_up_to_max_response_bytes() -> None:
    """Longer ones fail, counted after decompression."""
    exactly = Upstream(lambda request: httpx2.Response(200, content=b"x" * 1024))
    longer = Upstream(lambda request: httpx2.Response(200, content=b"x" * 1025))
    bomb = gzip.compress(b"\0" * 4096)
    zipped = Upstream(
        lambda request: httpx2.Response(
            200, content=bomb, headers={"Content-Encoding": "gzip"}
        )
    )
    with options(MAX_RESPONSE_BYTES=1024):
        with stub(exactly):
            assert len((await HttpClient().get(URL)).content) == 1024
        with stub(longer), pytest.raises(ResponseTooLarge, match="1024 bytes"):
            await HttpClient().get(URL)
        with stub(zipped), pytest.raises(ResponseTooLarge):
            await HttpClient().get(URL)
    assert len(bomb) < 1024


async def test_an_answer_too_long_is_not_read_to_its_end() -> None:
    """Chunks are counted together, and reading stops at the first one too many."""
    read: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        for _ in range(100):
            read.append(b"x" * 512)
            yield read[-1]

    async def chunked(*parts: bytes) -> AsyncIterator[bytes]:
        for part in parts:
            yield part

    endless = stub(lambda request: httpx2.Response(200, content=body()))
    short = stub(lambda request: httpx2.Response(200, content=chunked(b"ab", b"cd")))
    with options(MAX_RESPONSE_BYTES=1024):
        with endless, pytest.raises(ResponseTooLarge) as raised:
            await HttpClient().get(URL)
        with short:
            assert (await HttpClient().get(URL)).content == b"abcd"

    assert len(read) == 3
    assert raised.value.request.url == URL


async def test_a_compressed_answer_is_decoded_and_counted_as_it_streams() -> None:
    """A gzip bomb read through the stub's stream stops at the limit.

    The answer arrives in compressed chunks, as from a socket, and is
    decompressed a piece at a time: reading stops once the decompressed
    size passes MAX_RESPONSE_BYTES, long before the end of the stream.
    """
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024), compresslevel=9)
    chunks = [bomb[start : start + 1024] for start in range(0, len(bomb), 1024)]
    sent: list[bytes] = []

    async def compressed() -> AsyncIterator[bytes]:
        for chunk in chunks:
            sent.append(chunk)
            yield chunk

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"Content-Encoding": "gzip"}, content=compressed()
        )

    with (
        options(MAX_RESPONSE_BYTES=1024 * 1024),
        stub(answer),
        pytest.raises(ResponseTooLarge, match="1048576 bytes"),
    ):
        await HttpClient().get(URL)

    assert len(chunks) > 50
    assert len(sent) < len(chunks) // 2  # Never decompressed to its end.


async def test_an_answer_is_bounded_as_it_comes_not_only_inflated() -> None:
    """Bytes that inflate to nothing still count: reading stops at the limit.

    A gzip member is followed by bytes the decoder drops; decompressed,
    the answer is empty, so only the bytes as they come can stop it.
    """
    sent: list[bytes] = []

    async def padded() -> AsyncIterator[bytes]:
        yield gzip.compress(b"")
        for _ in range(100):
            sent.append(b"\0" * 512)
            yield sent[-1]

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"Content-Encoding": "gzip"}, content=padded()
        )

    with (
        options(MAX_RESPONSE_BYTES=1024),
        stub(answer),
        pytest.raises(ResponseTooLarge, match="1024 bytes") as raised,
    ):
        await HttpClient().get(URL)

    assert len(sent) == 2  # The member, then 512 + 512 more: one past the limit.
    assert raised.value.request.url == URL


def encoded(coding: str, body: bytes = b"{}") -> Answer:
    """Answer 200 with ``body`` compressed as ``coding`` says (gzip or deflate)."""
    first = coding.split(",")[0]
    data = gzip.compress(body) if first == "gzip" else zlib.compress(body)
    return lambda request: httpx2.Response(
        200, headers={"Content-Encoding": coding}, content=data
    )


@pytest.mark.parametrize("coding", ["gzip", "deflate", "gzip, identity"])
async def test_a_guarded_client_refuses_an_answer_it_did_not_ask_to_inflate(
    coding: str,
) -> None:
    """It asked for ``identity``: a compressed answer is refused, never inflated."""
    first = coding.split(",")[0]
    with (
        stub(Upstream(encoded(coding))),
        pytest.raises(httpx2.DecodingError, match=rf"compressed \({first}\)") as raised,
    ):
        await HttpClient(guard=True, retries=0).get("https://hooks.example/")

    assert "hooks.example" not in str(raised.value)
    assert raised.value.request.url == "https://hooks.example/"


@pytest.mark.parametrize(
    ("accept", "coding"),
    [
        ("gzip", "gzip"),
        ("deflate, gzip;q=0.5", "gzip"),
        ("*", "deflate"),
        ("GZIP", "gzip"),
        ("gzip;q=high", "gzip"),  # A weight that is no number: not a refusal.
        ("gzip;q=0, deflate", "deflate"),  # The codings after a refused one count.
        ("identity", "identity"),
    ],
)
async def test_a_guarded_client_inflates_what_its_call_asked_for(
    accept: str, coding: str
) -> None:
    """A call that sends its own ``Accept-Encoding`` takes what it accepts."""
    answer = status(200) if coding == "identity" else encoded(coding)
    with stub(Upstream(answer)):
        response = await HttpClient(guard=True).get(
            "https://hooks.example/", headers={"Accept-Encoding": accept}
        )

    assert response.json() == {}


async def test_a_refusal_names_the_codings_and_takes_empty_ones_for_none() -> None:
    """Every coding used is named; an empty ``Content-Encoding`` is identity."""
    both = Upstream(
        lambda request: httpx2.Response(
            200, headers={"Content-Encoding": "deflate, gzip"}, content=b"x"
        )
    )
    empty = Upstream(
        lambda request: httpx2.Response(
            200, headers={"Content-Encoding": ""}, content=b"{}"
        )
    )
    guarded = HttpClient(guard=True, retries=0)
    with stub(both), pytest.raises(httpx2.DecodingError) as raised:
        await guarded.get("https://hooks.example/")
    with stub(empty):
        assert (await guarded.get("https://hooks.example/")).json() == {}

    assert str(raised.value) == (
        "The answer is compressed (deflate, gzip), which the request did not accept."
    )


@pytest.mark.parametrize("accept", ["gzip;q=0", "gzip; q=0.000, deflate", "br"])
async def test_a_coding_weighed_at_nothing_is_not_accepted(accept: str) -> None:
    """``q=0`` says the coding is not acceptable at all."""
    with stub(Upstream(encoded("gzip"))), pytest.raises(httpx2.DecodingError):
        await HttpClient(guard=True, retries=0).get(
            "https://hooks.example/", headers={"Accept-Encoding": accept}
        )


@pytest.mark.parametrize(
    ("method", "code"), [("HEAD", 200), ("GET", 204), ("GET", 304)]
)
async def test_an_answer_without_a_body_is_not_refused_for_its_encoding(
    method: str, code: int
) -> None:
    """A server may name the encoding the body would have had."""
    answer = Upstream(
        lambda request: httpx2.Response(code, headers={"Content-Encoding": "gzip"})
    )
    with stub(answer):
        response = await HttpClient(guard=True).request(
            method, "https://hooks.example/"
        )

    assert response.status_code == code


async def test_an_unguarded_client_inflates_what_it_is_sent() -> None:
    """Outside the guard, httpx2's own codings hold, as before."""
    with stub(Upstream(encoded("gzip", b'{"rate": 1}'))):
        response = await HttpClient().get(URL)

    assert response.json() == {"rate": 1}


# The guard ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://api.example.com/", r"must use https\.$"),
        ("https://localhost/", "local or metadata host"),
        ("https://user:pw@api.example.com/", "user name or password"),
        ("https://169.254.169.254/latest", "cloud metadata address"),
        ("https://10.0.0.1/", "private, loopback"),
    ],
)
async def test_a_guarded_client_checks_urls_before_calling(
    url: str, error: str
) -> None:
    """Refused before anything is sent; the message never quotes the URL."""
    upstream = Upstream(status(200))
    with stub(upstream), pytest.raises(UnsafeURL, match=error) as refused:
        await HttpClient(guard=True).get(url)

    assert upstream.requests == []
    assert url not in str(refused.value)
    assert refused.value.code == "unsafe_url"


async def test_allowed_networks_let_their_addresses_through() -> None:
    """A private address the client was told it may call."""
    upstream = Upstream(status(200))
    inside = HttpClient(
        guard=True, allowed_networks=[ipaddress.ip_network("10.0.0.0/8")]
    )
    with stub(upstream):
        assert (await inside.get("https://10.1.2.3/")).status_code == 200


async def test_a_guarded_client_tells_others_nothing() -> None:
    """No request id or trace context, uncompressed answers; http when allowed."""
    upstream = Upstream(status(200))
    context = RequestContext("req-1", None, "GET", "/")
    with stub(upstream), bind_request(context):
        await HttpClient(guard=True, allow_http=True).get("http://hooks.example/")
        await HttpClient(guard=True, propagate=True).get("https://hooks.example/")

    quiet, told = upstream.requests
    assert "x-request-id" not in quiet.headers
    assert quiet.headers["accept-encoding"] == "identity"
    assert told.headers["x-request-id"] == "req-1"


@dataclass
class Server:
    """A small HTTP/1.1 server on this machine, answering 200 ``ok``."""

    port: int
    heads: list[bytes] = field(default_factory=list)
    connections: int = 0


@asynccontextmanager
async def serving() -> AsyncIterator[Server]:
    """Serve on 127.0.0.1 until the block ends."""
    server = Server(0)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        server.connections += 1
        try:
            while True:
                server.heads.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    listening = await asyncio.start_server(handle, "127.0.0.1", 0)
    server.port = listening.sockets[0].getsockname()[1]
    try:
        yield server
    finally:
        listening.close()
        await listening.wait_closed()


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace DNS with a table: addresses, or an error to raise."""
    table: dict[str, Any] = {}

    async def lookup(host: str, port: int) -> list[guard.Address]:
        answer = table[host]
        if isinstance(answer, BaseException):
            raise answer
        if answer == "hang":
            await asyncio.Event().wait()
        return [ipaddress.ip_address(address) for address in answer]

    monkeypatch.setattr(guard, "lookup", lookup)
    return table


async def test_a_guarded_client_connects_to_the_address_it_checked(
    resolver: dict[str, Any],
) -> None:
    """The host name is kept for the Host header and the pool; the IP is checked."""
    resolver["service.test"] = ["127.0.0.1"]
    api = HttpClient(guard=True, allow_http=True, allowed_networks=LOCAL)
    async with serving() as server:
        url = f"http://service.test:{server.port}/hello?x=1"
        assert (await api.get(url)).text == "ok"
        assert (await api.get(url)).text == "ok"
        await api.aclose()

    assert server.connections == 1  # Pooled by host name.
    assert server.heads[0].startswith(b"GET /hello?x=1 HTTP/1.1\r\n")
    assert f"host: service.test:{server.port}".encode() in server.heads[0].lower()


async def test_a_guarded_client_refuses_addresses_it_may_not_call(
    resolver: dict[str, Any],
) -> None:
    """A private address, or a lookup that fails or takes too long."""
    resolver.update(
        {
            "inside.test": ["10.0.0.1"],
            "local.test": ["127.0.0.1"],
            "missing.test": socket.gaierror("no such host"),
            "slow.test": "hang",
        }
    )
    api = HttpClient(guard=True, allow_http=True, retries=0)
    with pytest.raises(UnsafeURL, match="resolves to a private, loopback"):
        await api.get("http://inside.test/")
    with pytest.raises(UnsafeURL, match="resolves to a private, loopback"):
        await api.get("http://local.test/")
    with pytest.raises(httpx2.ConnectError, match="could not be resolved"):
        await api.get("http://missing.test/")
    with (
        options(CONNECT_TIMEOUT=0.01),
        pytest.raises(
            httpx2.ConnectTimeout, match=r"^Resolving the host took too long\.$"
        ),
    ):
        await HttpClient(guard=True, allow_http=True, retries=0).get(
            "http://slow.test/"
        )
    await api.aclose()


async def test_the_guarded_backend_calls_hosts_only() -> None:
    """Never a local socket; its sleep is the backend's."""
    backend = GuardedBackend(())
    with pytest.raises(UnsafeURL, match=r"^The URL names a local socket\.$"):
        await backend.connect_unix_socket("/run/docker.sock")
    await backend.sleep(0)


async def test_the_guarded_backend_connects_as_httpcore2_asks(
    resolver: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """To the checked address, with the port and options it was given."""
    resolver["service.test"] = ["127.0.0.1"]
    calls: list[tuple[str, int, dict[str, Any]]] = []
    connected = object()

    class Underneath:
        async def connect_tcp(
            self,
            host: str,
            port: int,
            **options: Any,  # noqa: ANN401
        ) -> object:
            calls.append((host, port, options))
            return connected

    backend = GuardedBackend(LOCAL)
    monkeypatch.setattr(backend, "_backend", Underneath())
    stream = await backend.connect_tcp(
        "service.test",
        8080,
        timeout=3.0,
        local_address="127.0.0.2",
        socket_options=[(1, 2, 3)],
    )

    assert stream is connected
    assert calls == [
        (
            "127.0.0.1",
            8080,
            {
                "timeout": 3.0,
                "local_address": "127.0.0.2",
                "socket_options": [(1, 2, 3)],
            },
        )
    ]


# Pools, closing, and logs -------------------------------------------------------


@pytest.mark.usefixtures("no_proxies")
async def test_a_client_pools_connections_until_closed() -> None:
    """One pool per loop, kept between calls; close_all and the lifespan close it."""
    api = HttpClient()
    async with serving() as server:
        url = f"http://127.0.0.1:{server.port}/"
        await api.get(url)
        await api.get(url)
        assert server.connections == 1
        await close_all()
        await api.get(url)
        assert server.connections == 2
        await lifecycle.close_resources()
        await api.get(url)
        assert server.connections == 3
        await lifecycle.close_http_clients()
    await api.aclose()  # Nothing left to close.


async def test_redirects_are_returned_not_followed() -> None:
    """A 3xx is the caller's to judge, stubbed or not."""
    upstream = Upstream(status(302, Location=URL))
    with stub(upstream):
        assert (await HttpClient().get(URL)).status_code == 302
    assert len(upstream.requests) == 1


def test_pools_are_built_from_http_client(
    monkeypatch: pytest.MonkeyPatch,
    client_certificate: tuple[Path, Path],  # noqa: F811 - the fixture.
) -> None:
    """Limits, the CA bundle, no redirects; guarded, no proxy from the environment."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    with options(
        MAX_CONNECTIONS=7,
        MAX_KEEPALIVE_CONNECTIONS=3,
        CA_BUNDLE=str(client_certificate[0]),
    ):
        config = settings.HTTP_CLIENT
        plain = HttpClient()._open(config)
        guarded = HttpClient(guard=True)._open(config)

    for pool, context in (
        (plain, tls_context(client_certificate[0])),
        (guarded, guard.tls_context(client_certificate[0])),
    ):
        assert pool.follow_redirects is False
        transport = pool._transport
        assert isinstance(transport, httpx2.AsyncHTTPTransport)
        connections = transport._pool
        assert connections._ssl_context is context
        assert connections._max_connections == 7
        assert connections._max_keepalive_connections == 3
        assert connections._keepalive_expiry == 5.0
    assert plain.trust_env
    assert plain._mounts
    assert not guarded.trust_env
    assert guarded._mounts == {}


async def test_the_lifespan_needs_no_client_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without domainkit.http.client imported, there is nothing to close."""
    monkeypatch.delitem(sys.modules, "domainkit.http.client")
    await lifecycle.close_http_clients()


async def test_each_event_loop_has_a_pool_of_its_own() -> None:
    """Connections belong to one loop; another loop gets another pool."""
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200))
    api = HttpClient(transport=transport)
    here = await api._pool(settings.HTTP_CLIENT)
    assert await api._pool(settings.HTTP_CLIENT) is here
    there: list[httpx2.AsyncClient] = []

    async def other() -> None:
        there.append(await api._pool(settings.HTTP_CLIENT))
        await api.aclose()

    thread = threading.Thread(target=asyncio.run, args=(other(),))
    thread.start()
    thread.join()
    assert there[0] is not here
    await api.aclose()


def test_tls_contexts_are_built_once_and_need_tls_1_2(
    client_certificate: tuple[Path, Path],  # noqa: F811 - the fixture.
) -> None:
    """The system's CAs, or a bundle of the project's."""
    default = tls_context(None)
    assert tls_context(None) is default
    assert default.minimum_version == ssl.TLSVersion.TLSv1_2
    bundle = tls_context(client_certificate[0])
    assert bundle is not default
    assert bundle.verify_mode == ssl.CERT_REQUIRED


async def test_logs_name_the_call_without_secrets(
    caplog: pytest.LogCaptureFixture, waits: list[float]
) -> None:
    """Method, origin, and path; never the query string or a header."""
    caplog.set_level(logging.DEBUG, LOGGER)
    secret = {"Authorization": "Bearer SECRET-TOKEN"}
    with options(RETRIES=1, BACKOFF=0.0):
        with stub(Upstream(status(503), status(200))):
            await HttpClient().get(URL + "?a=1?key=SECRET-KEY", headers=secret)
        with (
            stub(Upstream(fail(httpx2.ConnectError))),
            pytest.raises(httpx2.ConnectError),
        ):
            await HttpClient().get(URL + "?key=SECRET-KEY", headers=secret)
        with stub(Upstream(status(200))):
            await HttpClient(guard=True).get(URL + "?key=SECRET-KEY")

    logged = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert not [line for line in logged if "SECRET" in line]
    assert len(logged) == 5
    assert logged[0] == f"Retrying GET {URL} in 0.00s after 503 (attempt 1 of 2)"
    assert logged[1].startswith(f"GET {URL} 200 in ")
    assert logged[2] == (
        f"Retrying GET {URL} in 0.00s after ConnectError (attempt 1 of 2)"
    )
    assert logged[3] == f"GET {URL} failed (ConnectError) after 2 attempt(s)"
    assert logged[4].startswith("GET https://api.example.com 200 in ")


@pytest.mark.usefixtures("restore_logging")
async def test_production_logging_keeps_query_strings_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """httpx2's and httpcore2's own INFO lines, with the full URL, are not written.

    The configuration production uses, run through a plain and a guarded
    call: no line names a query string; their warnings still pass.
    """
    configure_logging(build_logging_config(log_format="json", level="INFO"))
    with stub(Upstream(status(200))):
        await HttpClient(base_url="https://rates.example.com").get(
            "/v1/rates", params={"api_key": "SECRET-API-KEY"}
        )
        await HttpClient(guard=True).get("https://hooks.example/cb?token=SECRET")
    with stub(Upstream(status(503))):
        await HttpClient(retries=0).get(URL + "?key=SECRET-KEY")
    logging.getLogger("httpx2").warning("httpx2 warnings still pass")
    assert flush_logs()

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not [line for line in lines if "SECRET" in json.dumps(line)]
    assert [line["logger"] for line in lines] == ["httpx2"]
    for name in ("httpx2", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


async def test_logs_say_how_long_an_answer_took(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In milliseconds, on the framework's clock."""
    caplog.set_level(logging.DEBUG, LOGGER)
    with freeze_time("2026-01-01") as clock:

        def slow(request: httpx2.Request) -> httpx2.Response:
            clock.tick(0.25)
            return httpx2.Response(200)

        with stub(slow):
            await HttpClient().get(URL)

    logged = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert logged == [f"GET {URL} 200 in 250.0ms"]


def test_guarded_connections_go_through_httpcore2() -> None:
    """The guarded transport pools with httpcore2, through the guarded backend."""
    transport = client_module.GuardedTransport(
        LOCAL, tls_context(None), httpx2.Limits(max_connections=3)
    )
    pool = transport._pool
    assert isinstance(pool, httpcore2.AsyncConnectionPool)
    assert isinstance(pool._network_backend, GuardedBackend)
    assert pool._network_backend.allowed == LOCAL
