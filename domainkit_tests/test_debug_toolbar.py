"""The debug toolbar: SQL per request, likely N+1 queries, profiles; DEBUG only."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.datastructures import MutableHeaders

from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.conf.global_settings import DEBUG_TOOLBAR_MIDDLEWARE
from domainkit.db import connections
from domainkit.debug import (
    DebugToolbarMiddleware,
    QueryLog,
    record_queries,
    statement_shape,
)
from domainkit.debug import middleware as middleware_module
from domainkit.debug import queries as queries_module
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.testing import CapturedQueries

SECRET = "hunter2-secret"
MIDDLEWARE = [
    "domainkit.http.middleware.RequestContextMiddleware",
    DEBUG_TOOLBAR_MIDDLEWARE,
]

router = APIRouter()


@router.get("/books")
async def list_books() -> dict[str, int]:
    """Run one query per book: an N+1."""
    async with connections.engine().connect() as connection:
        for book in range(6):
            await connection.execute(
                text("SELECT :title, :id"), {"title": SECRET, "id": book}
            )
        await connection.execute(text(f"SELECT '{SECRET}'"))
    return {"books": 6}


@router.post("/echo")
async def echo(book: dict[str, str]) -> dict[str, str]:
    """Answer with the body."""
    return book


@router.websocket("/socket")
async def socket(websocket: WebSocket) -> None:
    """Accept and close."""
    await websocket.accept()
    await websocket.close()


class Toolbar(DebugToolbarMiddleware):
    """The toolbar under another name, as a project might subclass it."""


type MakeClient = Callable[..., TestClient]


@pytest.fixture
def make_client() -> Iterator[MakeClient]:
    """Clients of the routes above, behind the toolbar."""
    with ExitStack() as stack:

        def make(**overrides: object) -> TestClient:
            settings = {
                "ROOT_ROUTER": "domainkit_tests.test_debug_toolbar.router",
                "MIDDLEWARE": MIDDLEWARE,
                "DEBUG": True,
                **overrides,
            }
            stack.enter_context(override_settings(**settings))
            return stack.enter_context(TestClient(get_asgi_application()))

        yield make


def test_requests_report_their_queries(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """``Server-Timing`` always, ``X-Debug-*`` on request, N+1 in the log."""
    client = make_client(DEBUG_TOOLBAR={"HEADERS": True})

    with caplog.at_level(logging.WARNING, logger="domainkit.debug.middleware"):
        response = client.get("/books")

    timing = response.headers["Server-Timing"]
    assert timing.startswith("db;dur=")
    assert '"7 queries", app;dur=' in timing
    assert response.headers["X-Debug-Queries"] == "7"
    assert response.headers["X-Debug-Repeated-Queries"] == "1"
    assert float(response.headers["X-Debug-Query-Time"]) >= 0
    (warning,) = [r for r in caplog.records if "N+1" in r.getMessage()]
    assert warning.getMessage() == "Possible N+1 in GET /books: 6 runs of SELECT ?, ?"
    assert SECRET not in caplog.text
    assert SECRET not in str(response.headers)


def test_quiet_unless_asked(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Without ``HEADERS`` only ``Server-Timing``; a higher threshold, no warning."""
    client = make_client(DEBUG_TOOLBAR={"REPEATED_QUERIES": 7})

    with caplog.at_level(logging.WARNING, logger="domainkit.debug.middleware"):
        response = client.get("/books")

    assert "Server-Timing" in response.headers
    assert "X-Debug-Queries" not in response.headers
    assert "N+1" not in caplog.text


def test_nothing_happens_without_debug(make_client: MakeClient) -> None:
    """With DEBUG off the middleware passes requests through untouched."""
    response = make_client(DEBUG=False).get("/books")

    assert "Server-Timing" not in response.headers


def test_websockets_pass_through(make_client: MakeClient) -> None:
    """Only HTTP requests are timed."""
    with make_client().websocket_connect("/socket") as connection:
        assert connection.receive() == {
            "type": "websocket.close",
            "code": 1000,
            "reason": "",
        }


def test_profiles_are_written_on_request(
    make_client: MakeClient, tmp_path: Path
) -> None:
    """``X-Debug-Profile: 1`` leaves a cProfile file in ``PROFILE_DIR``."""
    client = make_client(DEBUG_TOOLBAR={"PROFILE_DIR": tmp_path})

    plain = client.get("/books")
    profiled = client.get("/books", headers={"X-Debug-Profile": "1"})

    assert "X-Debug-Profile" not in plain.headers
    name = profiled.headers["X-Debug-Profile"]
    assert [path.name for path in tmp_path.iterdir()] == [name]
    assert (tmp_path / name).stat().st_size > 0


def test_one_profile_at_a_time(
    make_client: MakeClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While another request is profiled, a request is not."""
    client = make_client(DEBUG_TOOLBAR={"PROFILE_DIR": tmp_path})
    monkeypatch.setattr(middleware_module, "_profiling", [object()])

    response = client.get("/books", headers={"X-Debug-Profile": "1"})

    assert "X-Debug-Profile" not in response.headers
    assert list(tmp_path.iterdir()) == []


def test_profiles_need_a_directory(make_client: MakeClient) -> None:
    """Without ``PROFILE_DIR`` the header does nothing."""
    response = make_client().get("/books", headers={"X-Debug-Profile": "1"})

    assert "X-Debug-Profile" not in response.headers


def test_deployments_refuse_the_toolbar() -> None:
    """Staging and production refuse it in MIDDLEWARE, and subclasses at startup."""
    deployed = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
    }
    with (
        pytest.raises(ImproperlyConfigured, match="is for development; remove it"),
        override_settings(**deployed, MIDDLEWARE=MIDDLEWARE),
    ):
        pass
    with (
        override_settings(**deployed),
        pytest.raises(
            ImproperlyConfigured,
            match=r"^DebugToolbarMiddleware is for development, not production\.$",
        ),
    ):
        Toolbar(lambda scope, receive, send: None)  # type: ignore[arg-type,return-value]


def test_statement_shapes_hide_every_value() -> None:
    """Literals, placeholders of every driver, and lists become ``?``."""
    shape = statement_shape(
        "SELECT t1.name, 'it''s' FROM t1\n  WHERE id = $1 AND n IN (?, ?, ?) "
        "AND x = %(x)s AND y = %s AND z > -2.5e3 AND col2 = 42"
    )

    assert shape == (
        "SELECT t1.name, ? FROM t1 WHERE id = ? AND n IN (...) AND x = ? AND y = ? "
        "AND z > ? AND col2 = ?"
    )
    long = statement_shape("SELECT " + "x, " * 400)
    assert len(long) == 503
    assert long.endswith("...")


@pytest.mark.anyio
async def test_queries_are_recorded_only_inside_the_block(
    database_url: str,
) -> None:
    """Statements count while recording, by shape; outside, none are."""
    with override_settings(DATABASES={"default": {"URL": database_url}}):
        async with connections.engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
            with record_queries() as log:
                await connection.execute(text("SELECT 2"))
                await connection.execute(text("SELECT 3"))
            await connection.execute(text("SELECT 4"))
        await connections.close_all()

    assert (log.count, dict(log.shapes)) == (2, {"SELECT ?": 2})
    assert log.repeated(2) == [("SELECT ?", 2)]
    assert log.repeated(3) == []


def test_statements_that_started_before_recording_are_skipped() -> None:
    """An end without a start (recording began meanwhile) is not counted."""
    connection = SimpleNamespace(info={})
    with record_queries() as log:
        queries_module._after(connection, None, "SELECT 1", None, None, False)  # type: ignore[arg-type]
    assert log.count == 0


def test_listeners_are_added_once() -> None:
    """Recording twice listens once."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    with record_queries(), record_queries():
        pass

    assert event.contains(Engine, "after_cursor_execute", queries_module._after)


def test_request_bodies_reach_the_application(make_client: MakeClient) -> None:
    """The toolbar passes the request through as it came."""
    response = make_client().post("/echo", json={"title": "Dune"})

    assert response.json() == {"title": "Dune"}
    assert "Server-Timing" in response.headers


def test_the_toolbar_counts_what_assert_num_queries_counts(
    make_client: MakeClient,
) -> None:
    """One statement each, an ``executemany`` too; the request's only."""
    client = make_client(DEBUG_TOOLBAR={"HEADERS": True})

    with CapturedQueries() as queries:
        response = client.get("/books")

    assert response.headers["X-Debug-Queries"] == str(len(queries)) == "7"


def test_timings_are_in_milliseconds() -> None:
    """``db`` is the statements' time; ``app`` the time to the response."""
    with override_settings(DEBUG=True, DEBUG_TOOLBAR={"HEADERS": True}):
        toolbar = DebugToolbarMiddleware(None)  # type: ignore[arg-type]
    log = QueryLog()
    log.add("SELECT 1", 0.25)
    log.add("SELECT 2", 0.125)
    message: dict[str, Any] = {"type": "http.response.start", "headers": []}

    toolbar._annotate(message, log, time.perf_counter() - 2, None)

    headers = dict(MutableHeaders(scope=message).items())
    database, app = headers["server-timing"].split(", ")
    assert database == 'db;dur=375.0;desc="2 queries"'
    assert 2000 <= float(app.removeprefix("app;dur=")) < 60_000
    assert headers["x-debug-query-time"] == "375.0"


def test_profiles_are_named_by_time(make_client: MakeClient, tmp_path: Path) -> None:
    """``<UTC time>-<8 hex digits>.prof``."""
    client = make_client(DEBUG_TOOLBAR={"PROFILE_DIR": tmp_path})

    with freeze_time(datetime(2026, 10, 19, 13, 30, 5, tzinfo=UTC)):
        name = client.get("/books", headers={"X-Debug-Profile": "1"}).headers[
            "X-Debug-Profile"
        ]

    assert re.fullmatch(r"20261019T133005-[0-9a-f]{8}\.prof", name)


def test_query_times_add_up(database_url: str) -> None:
    """Each statement's time adds to the log's, measured forwards."""
    log = QueryLog()
    log.add("SELECT 1", 1.5)
    log.add("SELECT 1", 0.5)

    assert (log.count, log.seconds) == (2, 2.0)
    with override_settings(DATABASES={"default": {"URL": database_url}}):

        async def query() -> float:
            async with connections.engine().connect() as connection:
                with record_queries() as recorded:
                    await connection.execute(text("SELECT 1"))
            await connections.close_all()
            return recorded.seconds

        assert 0 <= asyncio.run(query()) < 60


def test_shapes_keep_five_hundred_characters() -> None:
    """A shape of exactly ``MAX_SHAPE`` characters is kept whole."""
    statement = "SELECT " + "x" * (queries_module.MAX_SHAPE - len("SELECT "))

    assert statement_shape(statement) == statement


def test_the_response_time_is_measured_from_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``app`` is the milliseconds from the request to its response."""
    with override_settings(DEBUG=True):
        toolbar = DebugToolbarMiddleware(None)  # type: ignore[arg-type]
    clock = SimpleNamespace(perf_counter=lambda: 12.5)
    monkeypatch.setattr(middleware_module, "time", clock)
    message: dict[str, Any] = {"type": "http.response.start", "headers": []}

    toolbar._annotate(message, QueryLog(), 10.0, None)

    timing = MutableHeaders(scope=message)["server-timing"]
    assert timing == 'db;dur=0.0;desc="0 queries", app;dur=2500.0'
