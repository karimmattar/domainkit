"""ASGI servers: Granian beside uvicorn, and what each reads before the app does."""

import asyncio
import http.client
import io
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from domainkit.conf import override_settings
from domainkit.conf.env import project_prefix
from domainkit.http import get_asgi_application, websockets
from domainkit.http.servers import (
    UNIX_SOCKET_WARNING,
    UVICORN_WS_MAX_SIZE,
    ServerLimits,
    limit_warnings,
    listens_on_unix_socket,
    server_limits,
    unix_socket_warnings,
)
from domainkit.management import call_command
from domainkit.management.base import CommandError

ROOT = Path(__file__).parents[1]
MiB = 1024 * 1024


UVICORN = "/venv/bin/uvicorn"
GRANIAN = "/venv/bin/granian"
GUNICORN = "/venv/bin/gunicorn"
FASTAPI = "/venv/bin/fastapi"
DEFAULT_UVICORN = ServerLimits("uvicorn", True, 16 * MiB)
GRANIAN_WS = ServerLimits("granian", True, None)
GRANIAN_NO_WS = ServerLimits("granian", False, None)
UNDER_GUNICORN = ServerLimits("uvicorn", True, 16 * MiB, launcher="gunicorn")
UNDER_FASTAPI = ServerLimits("uvicorn", True, 16 * MiB, launcher="fastapi")
WORKER = "uvicorn.workers.UvicornWorker"


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
        (
            ["manage.py"],
            {"UVICORN_WS": "none"},
            ServerLimits("uvicorn", False, MiB * 16),
        ),
        ([GRANIAN, "--interface", "asgi"], {}, GRANIAN_WS),
        (["/lib/granian/__main__.py", "--no-ws"], {}, GRANIAN_NO_WS),
        (["manage.py"], {"GRANIAN_WEBSOCKETS": "false"}, GRANIAN_NO_WS),
        (["manage.py"], {"GRANIAN_WEBSOCKETS": "true"}, GRANIAN_WS),
        (["manage.py", "check"], {"HOME": "/root"}, None),
        ([], {}, None),
        ([UVICORN, "--ws-max-size", "lots"], {}, None),
        ([UVICORN, "--ws-max-size"], {}, DEFAULT_UVICORN),
        # The program decides before stray variables of another server do.
        ([GRANIAN], {"UVICORN_WS_MAX_SIZE": "1024"}, GRANIAN_WS),
        ([UVICORN], {"GUNICORN_CMD_ARGS": f"-k {WORKER}"}, DEFAULT_UVICORN),
    ],
)
def test_the_server_is_read_from_its_command_line_or_environment(
    argv: list[str], environ: dict[str, str], found: ServerLimits | None
) -> None:
    """Uvicorn and Granian, as a program, ``python -m``, or their variables."""
    assert server_limits(argv, environ) == found


@pytest.mark.parametrize(
    ("argv", "environ"),
    [
        (["manage.py", "check"], {"UVICORN_WS_MAX_SIZE": "\u00b2"}),
        (["manage.py", "check"], {"UVICORN_WS_MAX_SIZE": "9" * 5000}),
        ([UVICORN, "--ws-max-size", "\u00b9\u2070\u00b2\u2074"], {}),
        ([UVICORN, f"--ws-max-size={'1' * 4301}"], {}),
        ([UVICORN, "--ws-max-size", "-1"], {}),
        ([UVICORN, "--ws-max-size", " 1024"], {}),
        (["manage.py"], {"GUNICORN_CMD_ARGS": '-k "uvicorn'}),
    ],
)
def test_values_that_cannot_be_read_are_unknown(
    argv: list[str], environ: dict[str, str]
) -> None:
    """Superscripts, too many digits, unbalanced quotes: unknown, never an error.

    ``str.isdigit`` takes superscript digits, which ``int`` refuses, as it
    refuses a number of more than 4,300 digits.
    """
    assert server_limits(argv, environ) is None


def test_other_decimal_digits_are_read_as_uvicorn_reads_them() -> None:
    """Digits of other scripts are numbers to ``int``, as to uvicorn's own parser."""
    arabic_indic = "\u0661\u0660\u0662\u0664"

    assert server_limits([UVICORN, "--ws-max-size", arabic_indic], {}) == ServerLimits(
        "uvicorn", True, 1024
    )


@pytest.mark.parametrize(
    ("argv", "environ", "found"),
    [
        ([GUNICORN, "-k", WORKER, "config.asgi:application"], {}, UNDER_GUNICORN),
        ([GUNICORN, "--worker-class=uvicorn_worker.UvicornWorker"], {}, UNDER_GUNICORN),
        (
            [GUNICORN, "--worker-class", "uvicorn.workers.UvicornH11Worker"],
            {},
            UNDER_GUNICORN,
        ),
        (
            ["/lib/gunicorn/__main__.py", "-kuvicorn_worker.UvicornH11Worker"],
            {},
            UNDER_GUNICORN,
        ),
        # The worker reads no UVICORN_* variable: the default stands.
        ([GUNICORN, "-k", WORKER], {"UVICORN_WS_MAX_SIZE": "1024"}, UNDER_GUNICORN),
        ([GUNICORN, "-k", "sync", "-k", WORKER], {}, UNDER_GUNICORN),
        ([GUNICORN, "-k", WORKER, "-k", "sync"], {}, None),
        ([GUNICORN, "config.wsgi:application"], {}, None),
        ([GUNICORN, "-k", "project.workers.LimitedWorker"], {}, None),
        ([GUNICORN, "-k"], {}, None),
        # The command line wins over GUNICORN_CMD_ARGS, as in gunicorn.
        ([GUNICORN], {"GUNICORN_CMD_ARGS": f"--workers 4 -k {WORKER}"}, UNDER_GUNICORN),
        ([GUNICORN, "-k", "sync"], {"GUNICORN_CMD_ARGS": f"-k {WORKER}"}, None),
        (
            ["manage.py", "check"],
            {"GUNICORN_CMD_ARGS": f"-k {WORKER}", "UVICORN_WS_MAX_SIZE": "1024"},
            UNDER_GUNICORN,
        ),
        (
            ["manage.py", "check"],
            {"GUNICORN_CMD_ARGS": "--workers 4", "UVICORN_WS_MAX_SIZE": "1024"},
            ServerLimits("uvicorn", True, 1024),
        ),
        ([FASTAPI, "run", "main.py"], {}, UNDER_FASTAPI),
        (["/lib/fastapi/__main__.py", "--verbose", "dev"], {}, UNDER_FASTAPI),
        ([FASTAPI, "run"], {"UVICORN_WS_MAX_SIZE": "1024"}, UNDER_FASTAPI),
        ([FASTAPI, "--version"], {}, None),
        ([FASTAPI], {}, None),
        # Hypercorn is not recognised (its limit is websocket_max_message_size).
        (["/venv/bin/hypercorn", "config.asgi:application"], {}, None),
    ],
)
def test_uvicorn_is_found_under_gunicorn_and_fastapi(
    argv: list[str], environ: dict[str, str], found: ServerLimits | None
) -> None:
    """The uvicorn workers of gunicorn, and ``fastapi run``, serve with 16 MiB."""
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
    assert limit_warnings(MiB, UNDER_GUNICORN) == [
        (
            "uvicorn reads WebSocket messages of up to 16777216 bytes before "
            "MAX_REQUEST_BODY_SIZE (1048576) refuses them; gunicorn's uvicorn "
            "worker reads neither --ws-max-size nor UVICORN_WS_MAX_SIZE: use a "
            'subclass of it whose CONFIG_KWARGS set "ws_max_size": 1048576.'
        )
    ]
    assert limit_warnings(MiB, UNDER_FASTAPI) == [
        (
            "uvicorn reads WebSocket messages of up to 16777216 bytes before "
            "MAX_REQUEST_BODY_SIZE (1048576) refuses them; fastapi run cannot "
            "set it: run uvicorn with --ws-max-size 1048576."
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


@pytest.mark.parametrize("value", ["\u00b2", "9" * 5000], ids=["²", "5000 digits"])
def test_a_stray_variable_never_stops_an_application(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A deployed application builds, and ``check`` runs, whatever the value."""
    monkeypatch.setattr(sys, "argv", [UVICORN, "config.asgi:application"])
    monkeypatch.setenv("UVICORN_WS_MAX_SIZE", value)

    with override_settings(**PRODUCTION):
        get_asgi_application()
        call_command("check", stdout=(output := io.StringIO()))

    assert "System check identified no issues" in output.getvalue()


def test_failing_to_tell_the_server_never_stops_an_application(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever goes wrong finding the server is logged, and the start goes on."""

    def broken() -> ServerLimits:
        raise RuntimeError("unreadable")

    def failures() -> list[logging.LogRecord]:
        message = "Could not tell which server runs the application"
        return [r for r in caplog.records if r.getMessage() == message]

    monkeypatch.setattr(websockets, "server_limits", broken)

    with override_settings(**PRODUCTION):
        get_asgi_application()
        (failure,) = failures()
        caplog.clear()
        call_command("check", stdout=(output := io.StringIO()))

    assert failure.exc_info is not None
    assert failures()
    assert "System check identified no issues" in output.getvalue()


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
    ("argv", "environ", "unix"),
    [
        ([UVICORN, "--uds", "/run/api.sock", "app:app"], {}, True),
        ([UVICORN, "--uds=/run/api.sock"], {}, True),
        ([UVICORN, "--host", "0.0.0.0"], {}, False),  # noqa: S104 - a command line.
        ([GRANIAN, "--uds", "/run/api.sock"], {}, True),
        ([GUNICORN, "-b", "unix:/run/api.sock", "-k", WORKER], {}, True),
        ([GUNICORN, "--bind=unix:/run/api.sock"], {}, True),
        ([GUNICORN, "-bunix:/run/api.sock"], {}, True),
        ([GUNICORN, "--bind", "0.0.0.0:8000", "--bind", "unix:/run/a"], {}, True),
        ([GUNICORN, "--bind", "0.0.0.0:8000"], {}, False),
        ([GUNICORN, "-b"], {}, False),
        ([GUNICORN, "app:app"], {}, False),
        (["manage.py", "check"], {"UVICORN_UDS": "/run/api.sock"}, True),
        (["manage.py", "check"], {"GRANIAN_UDS": "/run/api.sock"}, True),
        (["manage.py", "check"], {"UVICORN_UDS": ""}, False),
        (["manage.py", "check"], {"GUNICORN_CMD_ARGS": "--bind unix:/run/a"}, True),
        (["manage.py", "check"], {"GUNICORN_CMD_ARGS": "--bind 'unix:/run/a"}, False),
        (["manage.py", "check"], {}, False),
        ([], {}, False),
    ],
)
def test_servers_on_unix_sockets_are_found(
    argv: list[str], environ: dict[str, str], unix: bool
) -> None:
    """``--uds``, a ``unix:`` bind, or the variables that set them."""
    assert listens_on_unix_socket(argv, environ) is unix


def test_a_unix_socket_without_unix_in_trusted_proxies_is_warned() -> None:
    """Callers there have no address, unless the proxy on the socket is believed."""
    uds = [UVICORN, "--uds", "/run/api.sock"]

    assert unix_socket_warnings((), uds, {}) == [UNIX_SOCKET_WARNING]
    assert unix_socket_warnings(("unix",), uds, {}) == []
    assert unix_socket_warnings((), [UVICORN], {}) == []
    assert 'Add "unix" to TRUSTED_PROXIES' in UNIX_SOCKET_WARNING


def test_check_and_deployed_applications_warn_about_unix_sockets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Where deployed: ``check`` from the variables, the start from its argv."""
    monkeypatch.setenv("UVICORN_UDS", "/run/api.sock")
    monkeypatch.setattr(sys, "argv", [UVICORN, "--uds", "/run/api.sock"])

    with override_settings(**PRODUCTION):
        call_command("check", stdout=(deployed := io.StringIO()))
        with caplog.at_level(logging.WARNING):
            get_asgi_application()
    with override_settings(**PRODUCTION, TRUSTED_PROXIES=["unix"]):
        call_command("check", stdout=(trusted := io.StringIO()))
    call_command("check", stdout=(local := io.StringIO()))

    assert f"  Warning: {UNIX_SOCKET_WARNING}" in deployed.getvalue()
    assert UNIX_SOCKET_WARNING in caplog.messages
    assert UNIX_SOCKET_WARNING not in trusted.getvalue()
    assert UNIX_SOCKET_WARNING not in local.getvalue()


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


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a unix socket: the server's own path, which no other can take."""

    def __init__(self, path: Path) -> None:
        """Connect to the socket at ``path``."""
        super().__init__("localhost", timeout=30)
        self._path = path

    def connect(self) -> None:
        """Open the unix socket instead of a TCP connection."""
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self._path))
        except OSError:
            connection.close()
            raise
        self.sock = connection


@pytest.fixture
def granian() -> Iterator[tuple[subprocess.Popen[str], Path]]:
    """Granian serving the framework's application on a unix socket of its own.

    Not a TCP port: a port found free and closed again can be taken by
    another process (another pytest-xdist worker's server) before Granian
    binds it, and a request would then reach that process. A socket file in
    a new directory belongs to this test alone.
    """
    # Short, so the path fits a unix socket's 104 bytes on every platform.
    directory = Path(tempfile.mkdtemp(prefix="dk-"))
    path = directory / "granian.sock"
    prefix = project_prefix()
    environment = {
        **os.environ,
        f"{prefix}SETTINGS_MODULE": "domainkit_tests.settings",
        f"{prefix}ENV_FILE": "",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), *sys.path])),
    }
    try:
        with subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments.
            [
                sys.executable,
                "-m",
                "granian",
                "--interface",
                "asgi",
                "--factory",
                "--uds",
                str(path),
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
                yield process, path
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(30)
                except (
                    subprocess.TimeoutExpired
                ):  # pragma: no cover - a stuck shutdown.
                    process.kill()
                    raise
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def get(path: Path, target: str) -> tuple[int, bytes]:
    """GET ``target`` through the socket at ``path``, once the worker accepts.

    Granian's worker listens a moment after it says it started; until then
    the socket is missing or refuses connections, which is retried for up
    to 30 seconds.
    """
    deadline = time.monotonic() + 30
    while True:
        connection = UnixHTTPConnection(path)
        try:
            connection.request("GET", target, headers={"Host": "testserver"})
            response = connection.getresponse()
            return response.status, response.read()
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() > deadline:  # pragma: no cover - a dead worker.
                raise
            time.sleep(0.05)
        finally:
            connection.close()


def test_the_application_runs_under_granian(
    granian: tuple[subprocess.Popen[str], Path],
) -> None:
    """Granian's ASGI interface serves the application, lifespan and all."""
    process, path = granian

    status, body = get(path, "/health")

    assert (status, body) == (200, b'{"status":"ok"}')
    process.send_signal(signal.SIGINT)
    assert process.wait(30) == 0
    assert process.stdout is not None
    assert "Traceback" not in process.stdout.read()
