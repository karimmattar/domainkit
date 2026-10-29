"""``live_server``: the application on a port of the test process."""

import socket
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from fastapi import FastAPI

# Loaded before pytester takes its snapshot of the loaded modules (see
# test_testing.py): the routes define tables, Alembic keeps global state.
import domainkit.db.migrations  # noqa: F401
import domainkit_tests.sample_auth_routes  # noqa: F401
from domainkit.conf import override_settings
from domainkit.testing import IN_MEMORY, LiveServer
from domainkit_tests.conftest import MIDDLEWARE
from domainkit_tests.test_testing import prefix, run_plugin

__all__ = ["prefix"]


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
    with pytest.raises(RuntimeError, match="did not start"), server:
        pass  # pragma: no cover - never entered.
    assert server.port != 0
    assert "no database" in caplog.text


def test_the_server_stops_when_the_block_ends() -> None:
    """Entered, it answers on its port; after the block, nothing listens there."""
    app = FastAPI()

    @app.get("/")
    def root() -> dict[str, str]:
        return {"python": sys.version.split()[0]}

    with LiveServer(app) as server:
        assert httpx2.get(server.url).json() == {"python": sys.version.split()[0]}
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((server.host, server.port)).close()
