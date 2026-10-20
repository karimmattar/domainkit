"""ASGI servers: Granian beside uvicorn, and what each reads before the app does."""

import asyncio
import http.client
import io
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from domainkit.conf import override_settings
from domainkit.conf.env import project_prefix
from domainkit.http import get_asgi_application
from domainkit.http.servers import (
    UVICORN_WS_MAX_SIZE,
    ServerLimits,
    limit_warnings,
    server_limits,
)
from domainkit.management import call_command
from domainkit.management.base import CommandError

ROOT = Path(__file__).parents[1]
MiB = 1024 * 1024


UVICORN = "/venv/bin/uvicorn"
GRANIAN = "/venv/bin/granian"
DEFAULT_UVICORN = ServerLimits("uvicorn", True, 16 * MiB)
GRANIAN_WS = ServerLimits("granian", True, None)
GRANIAN_NO_WS = ServerLimits("granian", False, None)


@pytest.mark.parametrize(
    ("argv", "environ", "found"),
    [
        ([UVICORN, "app:app"], {}, DEFAULT_UVICORN),
        ([UVICORN, "--ws-max-size", "4096"], {}, ServerLimits("uvicorn", True, 4096)),
        (
            ["/lib/uvicorn/__main__.py", "--ws-max-size=1024", "--ws", "none"],
            {},
            ServerLimits("uvicorn", False, 1024),
        ),
        (
            ["manage.py", "check"],
            {"UVICORN_WS_MAX_SIZE": "2048", "UVICORN_WS": "websockets"},
            ServerLimits("uvicorn", True, 2048),
        ),
        (["manage.py", "check"], {"UVICORN_PORT": "80"}, DEFAULT_UVICORN),
        ([GRANIAN, "--interface", "asgi"], {}, GRANIAN_WS),
        (["/lib/granian/__main__.py", "--no-ws"], {}, GRANIAN_NO_WS),
        (["manage.py"], {"GRANIAN_WEBSOCKETS": "false"}, GRANIAN_NO_WS),
        (["manage.py"], {"GRANIAN_WEBSOCKETS": "true"}, GRANIAN_WS),
        (["manage.py", "check"], {"HOME": "/root"}, None),
        ([], {}, None),
        ([UVICORN, "--ws-max-size", "lots"], {}, None),
        ([UVICORN, "--ws-max-size"], {}, DEFAULT_UVICORN),
    ],
)
def test_the_server_is_read_from_its_command_line_or_environment(
    argv: list[str], environ: dict[str, str], found: ServerLimits | None
) -> None:
    """Uvicorn and Granian, as a program, ``python -m``, or their variables."""
    assert server_limits(argv, environ) == found


def test_this_process_is_read_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without arguments, this process's command line and environment count."""
    monkeypatch.setattr(sys, "argv", ["/venv/bin/granian"])

    assert server_limits() == ServerLimits("granian", True, None)


def test_limits_past_the_body_limit_are_named() -> None:
    """A WebSocket limit above MAX_REQUEST_BODY_SIZE, or none, gets a warning."""
    uvicorn = ServerLimits("uvicorn", True, UVICORN_WS_MAX_SIZE)
    assert limit_warnings(MiB, uvicorn) == [
        (
            "uvicorn reads WebSocket messages of up to 16777216 bytes before "
            "MAX_REQUEST_BODY_SIZE (1048576) refuses them; start it with "
            "--ws-max-size 1048576 (UVICORN_WS_MAX_SIZE)."
        )
    ]
    assert limit_warnings(MiB, GRANIAN_WS) == [
        (
            "granian reads each WebSocket message whole, at any size, before "
            "MAX_REQUEST_BODY_SIZE (1048576) refuses it; start it with --no-ws "
            "(GRANIAN_WEBSOCKETS=false) if the API takes no WebSockets, or "
            "limit messages in the proxy in front."
        )
    ]
    assert limit_warnings(MiB, ServerLimits("uvicorn", True, MiB)) == []
    assert limit_warnings(MiB, ServerLimits("uvicorn", False, 16 * MiB)) == []
    assert limit_warnings(MiB, ServerLimits("granian", False, None)) == []
    assert limit_warnings(MiB, None) == []


PRODUCTION = {"ENVIRONMENT": "production", "API_DOCS": False}


def test_check_warns_about_server_limits_where_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``check`` reads the server's variables; outside deployments it says nothing."""
    monkeypatch.setenv("UVICORN_WS_MAX_SIZE", str(4 * MiB))
    output = io.StringIO()

    with override_settings(**PRODUCTION, MAX_REQUEST_BODY_SIZE=MiB):
        call_command("check", stdout=output)
    call_command("check", stdout=(local := io.StringIO()))

    assert (
        "  Warning: uvicorn reads WebSocket messages of up to 4194304 bytes "
        "before MAX_REQUEST_BODY_SIZE (1048576) refuses them"
    ) in output.getvalue()
    assert "Warning" not in local.getvalue()


def test_deployed_applications_log_server_limits_when_built(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The server's own command line is read where the application starts."""
    monkeypatch.setattr(sys, "argv", ["/venv/bin/uvicorn", "config.asgi:application"])

    with override_settings(**PRODUCTION), caplog.at_level(logging.WARNING):
        get_asgi_application()
    deployed = caplog.messages[:]
    caplog.clear()
    get_asgi_application()

    assert any(
        message.startswith("uvicorn reads WebSocket messages of up to 16777216")
        for message in deployed
    )
    assert not any("reads WebSocket" in message for message in caplog.messages)


@pytest.mark.parametrize(
    ("event_loop", "loop"), [("asyncio", "asyncio"), ("auto", "uvloop")]
)
def test_runserver_serves_with_granian(
    monkeypatch: pytest.MonkeyPatch, event_loop: str, loop: str
) -> None:
    """``--server granian`` runs Granian's ASGI interface, with LOGGING.

    ``auto`` is resolved first, to uvloop when it is installed (a stand-in
    here): Granian's own ``auto`` would prefer rloop.
    """
    uvloop = types.ModuleType("uvloop")
    uvloop.new_event_loop = asyncio.new_event_loop  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvloop", uvloop)
    from granian import server
    from granian.constants import Interfaces, Loops

    made: list[tuple[str, dict[str, Any]]] = []

    class Recorded:
        def __init__(self, target: str, **options: Any) -> None:  # noqa: ANN401 - Granian's options.
            made.append((target, options))

        def serve(self) -> None:
            made.append(("served", {}))

    monkeypatch.setattr(server, "Server", Recorded)
    monkeypatch.setattr("granian.Granian", Recorded)
    with override_settings(
        ASGI_APPLICATION="config.asgi:application", EVENT_LOOP=event_loop
    ):
        call_command("runserver", "0.0.0.0:9000", server="granian", reload=False)

    (target, options), served = made
    assert target == "config.asgi:application"
    assert options["interface"] is Interfaces.ASGI
    assert (options["address"], options["port"]) == ("0.0.0.0", 9000)  # noqa: S104 - not bound.
    assert options["loop"] is Loops(loop)
    assert options["reload"] is False
    assert options["log_access"] is False
    assert options["log_dictconfig"] == {
        "version": 1,
        "disable_existing_loggers": False,
    }
    assert served == ("served", {})


def test_runserver_names_the_granian_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without Granian installed, the error says which extra to install."""
    monkeypatch.setitem(sys.modules, "granian", None)

    with (
        override_settings(ASGI_APPLICATION="config.asgi:application"),
        pytest.raises(CommandError) as missing,
    ):
        call_command("runserver", server="granian")

    assert str(missing.value) == (
        "--server granian needs the granian extra: pip install 'domainkit[granian]'."
    )


def test_runserver_refuses_an_unknown_server() -> None:
    """Only uvicorn and Granian are offered."""
    with pytest.raises(SystemExit):
        call_command("runserver", "--server", "hypercorn")
    with pytest.raises(CommandError, match="ASGI_APPLICATION"):
        call_command("runserver", server="granian")


def free_port() -> int:
    """A port nothing listens on now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def granian() -> Iterator[tuple[subprocess.Popen[str], int]]:
    """Granian serving the framework's application on a free port."""
    port = free_port()
    prefix = project_prefix()
    environment = {
        **os.environ,
        f"{prefix}SETTINGS_MODULE": "domainkit_tests.settings",
        f"{prefix}ENV_FILE": "",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), *sys.path])),
    }
    with subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments.
        [
            sys.executable,
            "-m",
            "granian",
            "--interface",
            "asgi",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "domainkit.http:get_asgi_application",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        try:
            assert process.stdout is not None
            for line in process.stdout:  # Waits on Granian's own log, not a clock.
                if "Started worker-1" in line:
                    break
            else:  # pragma: no cover - only when Granian fails to start.
                pytest.fail("Granian ended before its worker started")
            yield process, port
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:  # pragma: no cover - a stuck shutdown.
                process.kill()
                raise


def get(port: int, path: str) -> tuple[int, bytes]:
    """GET ``path``, once the worker accepts connections, within 30 seconds.

    Granian's worker listens a moment after it says it started; until then,
    connections are refused, which is retried.
    """
    deadline = time.monotonic() + 30
    while True:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request("GET", path, headers={"Host": "testserver"})
            response = connection.getresponse()
            return response.status, response.read()
        except ConnectionRefusedError:
            if time.monotonic() > deadline:  # pragma: no cover - a dead worker.
                raise
            time.sleep(0.05)
        finally:
            connection.close()


def test_the_application_runs_under_granian(
    granian: tuple[subprocess.Popen[str], int],
) -> None:
    """Granian's ASGI interface serves the application, lifespan and all."""
    process, port = granian

    status, body = get(port, "/health")

    assert (status, body) == (200, b'{"status":"ok"}')
    process.send_signal(signal.SIGINT)
    assert process.wait(30) == 0
    assert process.stdout is not None
    assert "Traceback" not in process.stdout.read()
