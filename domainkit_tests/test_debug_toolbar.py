"""The debug toolbar: SQL per request, likely N+1 queries, profiles; DEBUG only."""

import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import text

from domainkit.conf import override_settings
from domainkit.conf.global_settings import DEBUG_TOOLBAR_MIDDLEWARE
from domainkit.db import connections
from domainkit.debug import DebugToolbarMiddleware, record_queries, statement_shape
from domainkit.debug import middleware as middleware_module
from domainkit.debug import queries as queries_module
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application

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
        pytest.raises(ImproperlyConfigured, match="for development, not production"),
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
