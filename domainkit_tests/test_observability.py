"""Tracing spans, log correlation, and Prometheus metrics."""

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from starlette.types import Receive, Scope, Send

from domainkit import setup, tracing
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.messaging import Worker, enqueue
from domainkit.contrib.messaging.registry import qualified_name
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit.http import get_asgi_application
from domainkit.http.application import MEASURED, Application
from domainkit.log import ContextFilter, JsonFormatter
from domainkit.management import call_command
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, notes
from domainkit_tests.test_messaging import remember

EXPORTER = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
LOGS = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
METRICS_MIDDLEWARE = [
    "domainkit.http.metrics.MetricsMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
]


TRACES = TracerProvider()
TRACES.add_span_processor(SimpleSpanProcessor(EXPORTER))
METERS = MeterProvider(metric_readers=[METRICS])
LOGGERS = LoggerProvider()
LOGGERS.add_log_record_processor(SimpleLogRecordProcessor(LOGS))


@pytest.fixture(scope="module", autouse=True)
def tracer_provider() -> None:
    """Install SDK providers that keep spans, metrics, and logs in memory.

    OpenTelemetry's global providers are the process's, and are set only
    once: the first module that needs them in a process (each pytest-xdist
    worker is one) installs these, and they stay. A provider some other code
    installed first would leave these exporters empty, and every assertion
    on them failing for no visible reason, so it is refused by name.
    """
    if trace.get_tracer_provider() is not TRACES:
        trace.set_tracer_provider(TRACES)
    if metrics.get_meter_provider() is not METERS:
        metrics.set_meter_provider(METERS)
    if _logs.get_logger_provider() is not LOGGERS:
        _logs.set_logger_provider(LOGGERS)
    installed = {
        "tracer": (trace.get_tracer_provider(), TRACES),
        "meter": (metrics.get_meter_provider(), METERS),
        "logger": (_logs.get_logger_provider(), LOGGERS),
    }
    foreign = {
        kind: found for kind, (found, ours) in installed.items() if found is not ours
    }
    assert not foreign, (
        f"OpenTelemetry's providers were already set in this process: {foreign!r}; "
        f"these tests need their in-memory ones."
    )


@pytest.fixture(autouse=True)
def fresh_spans() -> None:
    """Forget spans and log records of earlier tests."""
    EXPORTER.clear()
    LOGS.clear()


def fastapi_requests_measured() -> int:
    """Requests FastAPI's own metrics have counted in this process."""
    data = METRICS.get_metrics_data()
    return sum(
        point.count
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        if scope.scope.name == "fastapi"
        for metric in scope.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
    )


def sample(name: str, **labels: str) -> float:
    """Read one metric sample (0 when absent)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_spans_carry_attributes_and_ids_reach_logs() -> None:
    """Spans record attributes; log records inside them get the ids."""
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "inside", (), None)
    with tracing.span("work", kind="test"):
        ids = tracing.current_ids()
        ContextFilter().filter(record)

    assert ids is not None
    (finished,) = EXPORTER.get_finished_spans()
    assert (finished.name, dict(finished.attributes or {})) == (
        "work",
        {"kind": "test"},
    )
    entry = json.loads(JsonFormatter().format(record))
    assert (entry["trace_id"], entry["span_id"]) == ids
    assert tracing.current_ids() is None


def test_tracing_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without OpenTelemetry the helpers do nothing."""
    monkeypatch.setattr(tracing, "trace", None)

    with tracing.span("ignored"):
        assert tracing.current_ids() is None
    assert EXPORTER.get_finished_spans() == ()


@pytest.fixture
def database(database_url: str) -> Iterator[None]:
    """Messaging installed on an empty database with the outbox and notes tables."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": database_url}},
    ):
        setup()

        async def create() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(messages.create)
                await connection.run_sync(notes.create)
            await connections.close_all()

        asyncio.run(create())
        yield
    registry.clear()


def test_commits_and_messages_are_traced(database: None) -> None:
    """Units of work and worker messages create spans; runs are counted."""
    before = sample("messaging_messages_total", kind="task", outcome="done")

    async def scenario() -> None:
        async with NotesUnitOfWork() as uow:
            await uow.notes.add(Note(id=NoteId(uuid.uuid4()), text="traced"))
            uow.defer(remember, value=uuid.uuid4())
            await uow.commit()
        await Worker().run_once()
        await connections.close_all()

    asyncio.run(scenario())

    spans = EXPORTER.get_finished_spans()
    assert [span.name for span in spans] == ["uow.commit", "message.process"]
    assert dict(spans[-1].attributes or {}) == {
        "kind": "task",
        "target": qualified_name(remember),
    }
    assert sample("messaging_messages_total", kind="task", outcome="done") == before + 1


def test_fastapi_records_nothing_of_its_own(database: None) -> None:
    """FastAPI's own telemetry stays off, even with every SDK provider set.

    Its spans would carry query strings, and its logs exception messages,
    which domainkit keeps out of what it records. domainkit traces its own
    work; requests are traced by ``opentelemetry-instrument`` when a
    deployment asks for it.
    """
    measured = fastapi_requests_measured()
    client = TestClient(get_asgi_application(), raise_server_exceptions=False)

    assert client.get("/ok?token=s3cret").status_code == 200
    assert client.post("/typed", json={"count": "s3cret"}).status_code == 422
    assert client.get("/boom").status_code == 500
    assert client.get("/no/such").status_code == 404
    assert EXPORTER.get_finished_spans() == ()
    assert LOGS.get_finished_logs() == ()
    assert fastapi_requests_measured() == measured


partner = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
"""A FastAPI application of a project's own, with FastAPI's defaults: telemetry on."""


@partner.get("/lookup")
async def lookup(q: str) -> dict[str, int]:
    """Answer with the query's length."""
    return {"length": len(q)}


@partner.get("/boom")
async def partner_boom() -> None:
    """Fail with a message domainkit keeps out of what it records."""
    raise RuntimeError("card 4111111111111111 refused for alice@example.com")


@partner.websocket("/socket")
async def partner_socket(websocket: WebSocket) -> None:
    """Greet, then close."""
    await websocket.accept()
    await websocket.send_text("hello")
    await websocket.close()


mounting = APIRouter()
mounting.mount("/partner", partner)


@mounting.get("/mark")
async def mark(request: Request) -> dict[str, object]:
    """How the request is marked for FastAPI's telemetry."""
    return {"mark": request.scope.get(MEASURED, "absent")}


def fastapi_records() -> tuple[int, int, int]:
    """FastAPI's spans and log records so far, and requests it measured."""
    fastapi_spans = [
        span
        for span in EXPORTER.get_finished_spans()
        if span.instrumentation_scope is not None
        and span.instrumentation_scope.name == "fastapi"
    ]
    logs = [
        each
        for each in LOGS.get_finished_logs()
        if each.instrumentation_scope is not None
        and each.instrumentation_scope.name == "fastapi"
    ]
    return len(fastapi_spans), len(logs), fastapi_requests_measured()


def call_partner(client: TestClient, prefix: str = "/partner") -> None:
    """A query string with a secret, a failure, and a WebSocket."""
    assert client.get(f"{prefix}/lookup?q=x&api_key=SECRET-42").status_code == 200
    assert client.get(f"{prefix}/boom").status_code == 500
    with client.websocket_connect(f"{prefix}/socket") as websocket:
        assert websocket.receive_text() == "hello"


def test_the_mounted_application_records_on_its_own() -> None:
    """Served alone, the project's application traces, logs, and measures.

    So the providers are set, and the next test would see what it records.
    """
    before = fastapi_records()

    call_partner(TestClient(partner, raise_server_exceptions=False), prefix="")

    spans, logs, measured = fastapi_records()
    assert spans > before[0]
    assert logs > before[1]
    assert measured == before[2] + 2


@pytest.mark.parametrize(
    "middleware",
    [None, [], ["domainkit.http.middleware.RequestContextMiddleware"]],
    ids=["default", "none", "custom"],
)
def test_mounted_fastapi_applications_record_nothing(
    middleware: list[str] | None,
) -> None:
    """FastAPI's telemetry stays off in every FastAPI application below.

    ``get_asgi_application`` marks each HTTP and WebSocket request as
    measured before any application sees it, so a project's FastAPI
    application mounted in domainkit's skips its own telemetry: no span
    with the query string, no log of the exception's message, no metric.
    It does so whatever ``MIDDLEWARE`` lists.
    """
    before = fastapi_records()
    settings = {} if middleware is None else {"MIDDLEWARE": middleware}
    with override_settings(ROOT_ROUTER=f"{__name__}.mounting", **settings):
        client = TestClient(get_asgi_application(), raise_server_exceptions=False)
        call_partner(client)

    assert fastapi_records() == before
    assert all(
        "SECRET-42" not in str(dict(span.attributes or {}))
        for span in EXPORTER.get_finished_spans()
    )


def test_requests_are_marked_and_lifespans_are_not() -> None:
    """Each HTTP request gets the mark; the lifespan's scope is left alone.

    A mark an enclosing application set, because it measures the request
    itself, is kept as it is.
    """
    with override_settings(ROOT_ROUTER=f"{__name__}.mounting", MIDDLEWARE=[]):
        app = get_asgi_application()
    seen: list[Scope] = []

    async def enclosing(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        if scope.get("query_string") == b"above":
            scope[MEASURED] = "measured above"
        await app(scope, receive, send)

    with TestClient(enclosing) as client:
        marks = [client.get("/mark").json(), client.get("/mark?above").json()]

    assert marks == [{"mark": None}, {"mark": "measured above"}]
    assert type(app) is Application
    assert [scope["type"] for scope in seen] == ["lifespan", "http", "http"]
    assert MEASURED not in seen[0]


def test_request_metrics_use_route_templates(database: None) -> None:
    """Requests are counted per route template; unknown paths share one label."""
    before = sample("http_requests_total", method="GET", route="/ok", status="200")
    with override_settings(MIDDLEWARE=METRICS_MIDDLEWARE):
        client = TestClient(get_asgi_application())
        client.get("/ok")
        client.get("/no/such/1")
        client.get("/no/such/2")
        asyncio.run(enqueue(remember, value=uuid.uuid4()))
        client.get("/ready")
        text = client.get("/metrics").text

    assert sample("http_requests_total", method="GET", route="/ok", status="200") == (
        before + 1
    )
    assert (
        sample("http_requests_total", method="GET", route="unmatched", status="404")
        >= 2
    )
    assert sample("http_request_duration_seconds_count", method="GET", route="/ok") >= 1
    assert 'messaging_outbox_messages{status="pending"} 1.0' in text
    assert 'db_pool_connections{database="default",state="size"}' in text


def test_unknown_methods_share_one_label(database: None) -> None:
    """Any method a server lets through counts as ``OTHER``, so labels stay few."""
    unmatched = {"route": "unmatched", "status": "404"}
    before = sample("http_requests_total", method="OTHER", **unmatched)
    with override_settings(MIDDLEWARE=METRICS_MIDDLEWARE):
        client = TestClient(get_asgi_application())
        for method in ("FOO", "BAR", "PURGE", "PATCH"):
            client.request(method, "/no/such")

    other = sample("http_requests_total", method="OTHER", **unmatched)
    assert other == before + 3
    assert sample("http_requests_in_progress", method="OTHER") == 0  # Back down.
    assert sample("http_requests_total", method="FOO", **unmatched) == 0
    assert sample("http_requests_total", method="PATCH", **unmatched) >= 1


def test_metrics_endpoint_protection() -> None:
    """A token is checked when set; deployments without one are refused."""
    with override_settings(METRICS_TOKEN="s3cret"):
        client = TestClient(get_asgi_application())
        missing = client.get("/metrics")
        wrong = client.get("/metrics", headers={"Authorization": "Bearer nope"})
        right = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    with override_settings(ENVIRONMENT="staging"):
        refused = TestClient(get_asgi_application()).get("/metrics")

    assert (missing.status_code, missing.json()["code"]) == (401, "unauthorized")
    assert missing.json()["detail"] == "A valid metrics token is required."
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert right.status_code == 200
    assert (refused.status_code, refused.json()["code"]) == (403, "metrics_disabled")
    assert refused.json()["detail"] == (
        "Set METRICS_TOKEN to expose metrics in staging and production."
    )


def test_requests_are_timed_and_failures_count_as_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The time from call to answer, by its clock; no answer at all is a 500."""
    from domainkit.http import metrics

    readings = iter([100.0, 100.25, 200.0, 200.5])
    monkeypatch.setattr(
        metrics, "time", SimpleNamespace(perf_counter=lambda: next(readings))
    )
    route = SimpleNamespace(path=f"/timed/{uuid.uuid4().hex}")
    bodies: list[bytes] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        scope["route"] = route
        bodies.append((await receive())["body"])
        if scope["path"] == "/fail":
            raise RuntimeError("no answer")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    client = TestClient(metrics.MetricsMiddleware(app), raise_server_exceptions=False)
    client.post("/ok", content=b"body")
    client.post("/fail", content=b"more")

    labels = {"method": "POST", "route": route.path}
    assert bodies == [b"body", b"more"]
    assert sample("http_requests_total", status="204", **labels) == 1
    assert sample("http_requests_total", status="500", **labels) == 1
    assert sample("http_request_duration_seconds_sum", **labels) == 0.75


def test_worker_metrics_server(database: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """``runworker --metrics-port`` serves metrics from the worker process."""
    started: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "prometheus_client.start_http_server",
        lambda port, addr: started.append((port, addr)),
    )

    call_command("runworker", "--once", "--metrics-port", "9101", stdout=io.StringIO())

    assert started == [(9101, "127.0.0.1")]


def test_metrics_wiring_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refreshers register once; lifespan passes through; no Prometheus, no gauges."""
    from domainkit.contrib.messaging import apps as messaging_apps
    from domainkit.http import metrics

    async def refresh() -> None:
        return None

    metrics.register_refresher(refresh)
    metrics.register_refresher(refresh)
    assert metrics._refreshers.count(refresh) == 1
    metrics._refreshers.remove(refresh)

    with (
        override_settings(MIDDLEWARE=METRICS_MIDDLEWARE),
        TestClient(get_asgi_application()) as client,
    ):
        assert client.get("/ok").status_code == 200

    imported: list[str] = []
    monkeypatch.setattr(messaging_apps, "find_spec", lambda name: None)
    monkeypatch.setattr(messaging_apps, "import_module", imported.append)
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit_tests.sample_domains.alpha",
        ]
    ):
        setup()
    registry.clear()
    assert imported == []


def test_route_templates_and_failing_refreshers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nested routers keep their prefixes; a failing gauge does not fail scrapes."""
    from domainkit.http import metrics

    class Route:
        path = "/plain/{id}"

    assert metrics.route_template({"route": Route()}) == "/plain/{id}"
    assert metrics.route_template({"fastapi": "unexpected"}) == "unmatched"
    assert metrics.route_template({}) == "unmatched"

    async def broken() -> None:
        raise RuntimeError("gauge source down")

    metrics.register_refresher(broken)
    try:
        with override_settings(MIDDLEWARE=METRICS_MIDDLEWARE):
            client = TestClient(get_asgi_application())
            client.get("/ok")
            response = client.get("/metrics")
    finally:
        metrics._refreshers.remove(broken)

    assert response.status_code == 200
    assert "Metrics refresher" in caplog.text
