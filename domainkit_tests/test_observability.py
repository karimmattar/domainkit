"""Tracing spans, log correlation, and Prometheus metrics."""

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from domainkit import setup, tracing
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.messaging import Worker, enqueue
from domainkit.contrib.messaging.tables import messages
from domainkit.db import connections
from domainkit.http import get_asgi_application
from domainkit.log import ContextFilter, JsonFormatter
from domainkit.management import call_command
from domainkit_tests.test_db import Note, NoteId, NotesUnitOfWork, notes
from domainkit_tests.test_messaging import remember

EXPORTER = InMemorySpanExporter()
METRICS_MIDDLEWARE = [
    "domainkit.http.metrics.MetricsMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
]


@pytest.fixture(scope="module", autouse=True)
def tracer_provider() -> None:
    """Install an SDK provider that keeps spans in memory (once per process)."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        trace.set_tracer_provider(provider)


@pytest.fixture(autouse=True)
def fresh_spans() -> None:
    """Forget spans of earlier tests."""
    EXPORTER.clear()


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
def database(tmp_path: Path) -> Iterator[None]:
    """Messaging installed on a SQLite file with the outbox and notes tables."""
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            "domainkit.contrib.messaging",
            "domainkit_tests.sample_domains.alpha",
        ],
        DATABASES={"default": {"URL": f"sqlite+aiosqlite:///{tmp_path / 'o.sqlite3'}"}},
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

    names = [span.name for span in EXPORTER.get_finished_spans()]
    assert names == ["uow.commit", "message.process"]
    assert sample("messaging_messages_total", kind="task", outcome="done") == before + 1


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
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert right.status_code == 200
    assert (refused.status_code, refused.json()["code"]) == (403, "metrics_disabled")


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
