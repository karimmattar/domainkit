"""``live_server``: the application on a port of the test process."""

import asyncio
import socket
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

# Loaded before pytester takes its snapshot of the loaded modules (see
# test_testing.py): the routes define tables, Alembic keeps global state.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes  # noqa: F401
from domainkit.conf import override_settings
from domainkit.testing import IN_MEMORY, LiveServer
from domainkit_tests.conftest import MIDDLEWARE
from domainkit_tests.test_testing import prefix, run_plugin

__all__ = ["prefix"]

STARTUP_BOUND = 30
"""Seconds a request on the stream may take to begin: a bound, not a wait."""


@pytest.fixture
def auth_settings(auth_project: str) -> Iterator[None]:
    """The inner session's project: auth, on a database the plugin makes."""
    with override_settings(
        DATABASES={"default": {"URL": IN_MEMORY}},
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=MIDDLEWARE[:3],
    ):
        yield


def test_browsers_and_other_processes_reach_the_test_databases(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """A user the test stored signs in through a real socket, from another process.

    Each test gets its own server, on its own port, stopped after it.
    """
    pytester.makepyfile(
        test_live=f"""
        import subprocess
        import sys

        import httpx2

        from domainkit.contrib.auth.testing import create_user
        from domainkit.management.base import run_async
        from domainkit.testing import token_for

        PORTS = []

        def test_another_process(live_server):
            assert live_server.url.startswith("http://127.0.0.1:")
            PORTS.append(live_server.port)
            ann = run_async(create_user(username="ann"))
            token = run_async(token_for(ann))
            code = (
                "import httpx2, sys; "
                "r = httpx2.get(sys.argv[1], "
                "headers={{'Authorization': sys.argv[2]}}); "
                "print(r.status_code, r.json()['user'])"
            )
            seen = subprocess.run(
                [{sys.executable!r}, "-c", code, live_server.url + "/private",
                 f"Bearer {{token}}"],
                capture_output=True, text=True, check=True,
            )
            assert seen.stdout.split() == ["200", "ann"]

        def test_its_own_server(live_server):
            PORTS.append(live_server.port)
            assert PORTS[0] != PORTS[1]
            assert httpx2.get(live_server.url + "/private").status_code == 401
            try:
                httpx2.get(f"http://127.0.0.1:{{PORTS[0]}}/private")
            except httpx2.ConnectError:
                pass
            else:
                raise AssertionError("the first test's server still answers")
        """
    )

    run_plugin(pytester).assert_outcomes(passed=2)


def test_a_clock_frozen_first_is_the_server_s_too(
    auth_settings: None, prefix: str, pytester: pytest.Pytester
) -> None:
    """A token made at a frozen moment is valid on the server, then expires there."""
    pytester.makepyfile(
        test_clock="""
        from datetime import timedelta

        import httpx2
        import pytest

        from domainkit.contrib.auth.testing import create_user
        from domainkit.management.base import run_async
        from domainkit.testing import token_for

        @pytest.fixture
        def frozen(freeze_time):
            return freeze_time("2020-01-01T09:00:00Z")

        def test_expiry(frozen, live_server):
            token = run_async(token_for(run_async(create_user())))
            headers = {"Authorization": f"Bearer {token}"}
            url = live_server.url + "/private"
            assert httpx2.get(url, headers=headers).status_code == 200
            frozen.tick(timedelta(days=30))
            assert httpx2.get(url, headers=headers).status_code == 401
        """
    )

    run_plugin(pytester).assert_outcomes(passed=1)


def test_a_failed_startup_is_raised_and_frees_the_port(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The lifespan's error is uvicorn's to log; entering says it did not start."""

    @asynccontextmanager
    async def broken(app: FastAPI) -> AsyncIterator[None]:
        raise RuntimeError("no database")
        yield  # pragma: no cover - never reached.

    server = LiveServer(FastAPI(lifespan=broken))
    with (
        pytest.raises(
            RuntimeError,
            match=r"^The live server did not start: its application's startup "
            r"failed; uvicorn's log says why\.$",
        ),
        server,
    ):
        pass  # pragma: no cover - never entered.
    assert server.port != 0
    assert "no database" in caplog.text


def test_the_server_stops_when_the_block_ends() -> None:
    """Entered, it answers on its port; after the block, nothing listens there."""
    app = FastAPI()

    @app.get("/")
    def root() -> dict[str, str]:
        return {"python": sys.version.split()[0]}

    server = LiveServer(app)
    assert server.port == 0  # Until entered.
    server.__exit__(None, None, None)  # Not started: nothing to stop.
    with server:
        assert httpx2.get(server.url).json() == {"python": sys.version.split()[0]}
        (thread,) = [
            thread
            for thread in threading.enumerate()
            if thread.name == f"live-server-{server.port}"
        ]
        assert thread.daemon  # An interrupted run never waits for it.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((server.host, server.port)).close()
    server.__exit__(None, None, None)  # Stopped already.


def test_uvicorn_guesses_nothing_and_adds_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The application is named a factory and ASGI 3: uvicorn calls it only to serve.

    Called with no arguments, as uvicorn does to find a factory, the first
    wrapper returns a coroutine rather than refusing; the second is a plain
    function returning one, which uvicorn would take for ASGI 2. Neither
    writes access lines of its own, adds its ``Server`` header, or takes a
    client's ``X-Forwarded-For``: those are the application's.
    """
    app = FastAPI()

    @app.get("/")
    def root(request: Request) -> dict[str, str | None]:
        return {"client": request.client.host if request.client else None}

    class AnyArguments:
        async def __call__(self, *args: Any) -> None:  # noqa: ANN401 - ASGI's.
            await app(*args)

    class Plain:
        def __call__(self, *args: Any) -> Awaitable[None]:  # noqa: ANN401 - ASGI's.
            return app(*args)

    for wrapped in (AnyArguments(), Plain()):
        with LiveServer(wrapped) as server:
            seen = httpx2.get(server.url, headers={"X-Forwarded-For": "203.0.113.9"})
            assert seen.json() == {"client": "127.0.0.1"}
            assert "server" not in seen.headers
    assert [r.name for r in caplog.records if r.name.startswith("uvicorn")] == []


def test_a_startup_the_application_does_not_answer_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An application that raises in its lifespan is not served without it."""

    async def raw(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("no lifespan here")

    with pytest.raises(RuntimeError, match="did not start"), LiveServer(raw):
        pass  # pragma: no cover - never entered.
    assert "no lifespan here" in caplog.text


def test_a_stream_still_open_does_not_hold_the_stop() -> None:
    """Requests still open after ``stop_seconds`` are cancelled, not waited for."""
    app = FastAPI()
    opened = threading.Event()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def forever() -> AsyncIterator[bytes]:
            opened.set()
            yield b"first\n"
            await asyncio.Event().wait()  # Never set: only a cancel ends it.

        return StreamingResponse(forever())

    with LiveServer(app, stop_seconds=0) as server:
        reader = httpx2.Client(base_url=server.url)
        response = reader.send(reader.build_request("GET", "/stream"), stream=True)
        assert opened.wait(STARTUP_BOUND)
    response.close()
    reader.close()
