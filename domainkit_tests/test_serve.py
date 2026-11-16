"""``serve``: the production server, from the settings, and the proxy's idle timeout.

``serve`` starts Granian, or uvicorn, with the options "Server limits"
gives, read from ``SERVE`` (user report S1). ``check`` warns
(``servers.W003``) when the proxy in front keeps idle connections as
long as the server, or longer.
"""

import contextlib
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from domainkit.conf import ServeConfig, override_settings, settings
from domainkit.conf.env import project_prefix
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import servers
from domainkit.http.servers import (
    GRANIAN_KEEP_ALIVE,
    UVICORN_KEEP_ALIVE,
    KeepAlive,
    ServerLimits,
    keep_alive_warnings,
    limit_warnings,
    listens_on_unix_socket,
    serve_keep_alive,
    server_keep_alive,
    server_limits,
)
from domainkit.log import build_logging_config
from domainkit.management import call_command
from domainkit.management.base import CommandError
from domainkit.management.commands import serve
from domainkit.management.commands.logconfig import portable
from domainkit_tests.support.servers import get

ROOT = Path(__file__).parents[1]
MANAGE: Final = "/app/manage.py"
PRODUCTION: Final = {"ENVIRONMENT": "production", "API_DOCS": False}
APPLICATION: Final = "config.asgi:application"


class Served:
    """A stand-in for ``granian.Granian``: it records its options and serves nothing."""

    made: list[dict[str, Any]]

    def __init__(self, **options: Any) -> None:  # noqa: ANN401 - Granian's options.
        """Record ``options``."""
        type(self).made.append(options)

    def serve(self) -> None:
        """Serve nothing."""
        type(self).made.append({"served": True})


@pytest.fixture
def granian(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What ``serve`` gives Granian, then ``{"served": True}`` once it serves."""
    made: list[dict[str, Any]] = []
    monkeypatch.setattr(Served, "made", made, raising=False)
    monkeypatch.setattr("granian.Granian", Served)
    return made


@pytest.fixture
def uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What ``serve`` gives ``uvicorn.run``."""
    given: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda **options: given.append(options))
    return given


def test_serve_starts_granian_with_the_limits_of_the_reference(
    granian: list[dict[str, Any]],
) -> None:
    """Granian where installed: its ASGI interface, with "Server limits"' options.

    An idle connection stays its most, 60 seconds; a worker that dies is
    started again; workers are killed 10 seconds plus ``SHUTDOWN_TIMEOUT``
    after the stop. ``LOGGING`` reaches its main process as ``logconfig``
    writes it, and it never reloads (user report S1).
    """
    from granian.constants import Interfaces, Loops
    from granian.http import HTTP1Settings

    logging_config = build_logging_config(log_format="json", level="INFO")
    with override_settings(ASGI_APPLICATION=APPLICATION, LOGGING=logging_config):
        call_command("serve")
        written = portable(settings.LOGGING, "LOGGING")

    (options, served) = granian
    assert options == {
        "target": APPLICATION,
        "address": "0.0.0.0",  # noqa: S104 - not bound.
        "port": 8000,
        "uds": None,
        "interface": Interfaces.ASGI,
        "workers": 1,
        "loop": Loops.uvloop,
        "backlog": 2048,
        "backpressure": 1200,
        "http1_settings": HTTP1Settings(
            header_read_timeout=60_000, max_buffer_size=16_384
        ),
        "websockets": True,
        "respawn_failed_workers": True,
        "workers_kill_timeout": 25,
        "log_dictconfig": written,
        "log_access": False,
        "reload": False,
    }
    assert served == {"served": True}


def test_serve_starts_uvicorn_with_the_limits_of_the_reference(
    uvicorn: list[dict[str, Any]],
) -> None:
    """With ``SERVE["SERVER"] = "uvicorn"``, uvicorn, with those options too.

    It reads WebSocket messages up to ``MAX_REQUEST_BODY_SIZE``, no proxy
    header, and writes no ``Server`` header and no access line.
    """
    with override_settings(
        ASGI_APPLICATION=APPLICATION,
        SERVE={"SERVER": "uvicorn"},
        MAX_REQUEST_BODY_SIZE=2_000_000,
    ):
        call_command("serve", "127.0.0.1:9000")
        written = portable(settings.LOGGING, "LOGGING")

    assert uvicorn == [
        {
            "app": APPLICATION,
            "host": "127.0.0.1",
            "port": 9000,
            "uds": None,
            "workers": 1,
            "loop": "uvloop",
            "limit_concurrency": 1200,
            "backlog": 2048,
            "timeout_keep_alive": 5,
            "timeout_graceful_shutdown": 10,
            "h11_max_incomplete_event_size": 16_384,
            "ws": "auto",
            "ws_max_size": 2_000_000,
            "proxy_headers": False,
            "server_header": False,
            "log_config": written,
            "access_log": False,
            "reload": False,
        }
    ]


@pytest.mark.parametrize(
    ("installed", "started"), [(True, "granian"), (False, "uvicorn")]
)
def test_serve_starts_granian_where_it_is_installed(
    monkeypatch: pytest.MonkeyPatch,
    granian: list[dict[str, Any]],
    uvicorn: list[dict[str, Any]],
    installed: bool,
    started: str,
) -> None:
    """``SERVE["SERVER"] = "auto"`` (the default): Granian if found, else uvicorn."""
    monkeypatch.setattr(
        servers, "find_spec", lambda name: object() if installed else None
    )

    with override_settings(ASGI_APPLICATION=APPLICATION):
        call_command("serve")

    assert settings.SERVE.SERVER == "auto"
    assert ("granian" if granian else "uvicorn") == started
    assert bool(uvicorn) is not installed


SET: Final = {
    "WORKERS": 4,
    "MAX_CONNECTIONS": 500,
    "BACKLOG": 512,
    "KEEP_ALIVE": 30,
    "GRACEFUL_TIMEOUT": 5,
    "MAX_HEADER_SIZE": 32_768,
    "WEBSOCKETS": False,
}
"""Every limit of ``SERVE``, set."""


def test_serve_reads_its_limits_from_the_settings(
    granian: list[dict[str, Any]], uvicorn: list[dict[str, Any]]
) -> None:
    """Each key of ``SERVE`` reaches either server, in its own units.

    Granian's kill timeout is ``GRACEFUL_TIMEOUT`` plus ``SHUTDOWN_TIMEOUT``
    rounded up: its application's shutdown runs in that time. The loop
    follows ``EVENT_LOOP``.
    """
    from granian.constants import Loops
    from granian.http import HTTP1Settings

    common = {
        "ASGI_APPLICATION": APPLICATION,
        "SHUTDOWN_TIMEOUT": 7.5,
        "EVENT_LOOP": "asyncio",
    }
    with override_settings(**common, SERVE=SET):
        call_command("serve", "8080", uds=Path("/run/api.sock"))
    with override_settings(**common, SERVE={**SET, "SERVER": "uvicorn"}):
        call_command("serve", "8080", uds=Path("/run/api.sock"))

    options = granian[0]
    assert (options["address"], options["port"]) == ("0.0.0.0", 8080)  # noqa: S104
    assert options["uds"] == Path("/run/api.sock")
    assert options["workers"] == 4
    assert options["loop"] is Loops.asyncio
    assert (options["backlog"], options["backpressure"]) == (512, 500)
    assert options["http1_settings"] == HTTP1Settings(
        header_read_timeout=30_000, max_buffer_size=32_768
    )
    assert options["websockets"] is False
    assert options["workers_kill_timeout"] == 5 + math.ceil(7.5)
    (given,) = uvicorn
    assert given["uds"] == "/run/api.sock"
    assert given["workers"] == 4
    assert given["loop"] == "asyncio"
    assert (given["limit_concurrency"], given["backlog"]) == (500, 512)
    assert (given["timeout_keep_alive"], given["timeout_graceful_shutdown"]) == (30, 5)
    assert given["h11_max_incomplete_event_size"] == 32_768
    assert given["ws"] == "none"


@pytest.mark.parametrize(
    ("configured", "environ", "workers"),
    [
        (None, {}, 1),
        (None, {"WEB_CONCURRENCY": "3"}, 3),
        (None, {"WEB_CONCURRENCY": " 2 "}, 2),
        (None, {"WEB_CONCURRENCY": ""}, 1),
        (6, {"WEB_CONCURRENCY": "3"}, 6),
    ],
)
def test_serve_takes_its_workers_from_web_concurrency_unless_set(
    configured: int | None, environ: dict[str, str], workers: int
) -> None:
    """``SERVE["WORKERS"]``, else ``WEB_CONCURRENCY`` (as uvicorn reads it), else 1."""
    with override_settings(SERVE={"WORKERS": configured}):
        assert serve.worker_count(environ) == workers


@pytest.mark.parametrize("value", ["0", "1025", "four", "²", "1" * 5000, "-1"])
def test_serve_refuses_workers_it_cannot_start(value: str) -> None:
    """A ``WEB_CONCURRENCY`` that is not 1 to 1024 stops it, with the value."""
    with pytest.raises(CommandError) as refused:
        serve.worker_count({"WEB_CONCURRENCY": value})

    assert str(refused.value).startswith(f"WEB_CONCURRENCY is {value.strip()!r}:")
    assert str(refused.value).endswith('from 1 to 1024, or set SERVE["WORKERS"].')


def test_serve_refuses_a_keep_alive_granian_cannot_keep(
    granian: list[dict[str, Any]],
) -> None:
    """Granian keeps an idle connection 60 seconds at most: longer is refused.

    Named, ``SERVE`` refuses it as it loads; chosen by ``auto``, ``serve``
    refuses it as it starts. uvicorn takes it.
    """
    with (
        pytest.raises(ImproperlyConfigured, match="Granian keeps an idle connection"),
        override_settings(SERVE={"SERVER": "granian", "KEEP_ALIVE": 61}),
    ):
        pass  # pragma: no cover - refused as it loads.
    with (
        override_settings(ASGI_APPLICATION=APPLICATION, SERVE={"KEEP_ALIVE": 61}),
        pytest.raises(CommandError) as refused,
    ):
        call_command("serve")

    assert str(refused.value) == (
        'SERVE["KEEP_ALIVE"] is 61, but Granian keeps an idle connection 60 '
        "seconds at most: set it to 60 or less, or serve with uvicorn "
        '(SERVE["SERVER"] = "uvicorn").'
    )
    assert granian == []
    assert ServeConfig(SERVER="uvicorn", KEEP_ALIVE=61).KEEP_ALIVE == 61


def test_serve_names_the_granian_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SERVE["SERVER"] = "granian"`` without Granian says what to install."""
    monkeypatch.setitem(sys.modules, "granian.constants", None)

    with (
        override_settings(ASGI_APPLICATION=APPLICATION, SERVE={"SERVER": "granian"}),
        pytest.raises(CommandError) as missing,
    ):
        call_command("serve")

    assert str(missing.value) == (
        'SERVE["SERVER"] is "granian", which needs the granian extra: '
        "pip install 'domainkit[granian]'."
    )


def test_serve_says_why_granian_did_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configuration Granian refuses stops ``serve`` with a message."""
    from granian.errors import ConfigurationError

    class Refusing:
        def __init__(self, **options: Any) -> None:  # noqa: ANN401 - Granian's options.
            pass

        def serve(self) -> None:
            raise ConfigurationError("workers_lifetime")

    monkeypatch.setattr("granian.Granian", Refusing)
    with (
        override_settings(ASGI_APPLICATION=APPLICATION),
        pytest.raises(CommandError) as refused,
    ):
        call_command("serve")

    assert str(refused.value) == (
        "Granian could not start (workers_lifetime); its log above says why."
    )


def test_serve_needs_an_application_and_logging_json_can_hold(
    granian: list[dict[str, Any]],
) -> None:
    """No ``ASGI_APPLICATION``, or a ``LOGGING`` no file could hold, stops it first."""
    with pytest.raises(CommandError, match="Set ASGI_APPLICATION"):
        call_command("serve")
    with (
        override_settings(
            ASGI_APPLICATION=APPLICATION,
            LOGGING={"version": 1, "filters": {"quiet": {"()": lambda record: True}}},
        ),
        pytest.raises(
            CommandError, match=r"^LOGGING\['filters'\]\['quiet'\]\['\(\)'\]"
        ),
    ):
        call_command("serve")
    with (
        override_settings(ASGI_APPLICATION=APPLICATION),
        pytest.raises(CommandError, match="not a valid port"),
    ):
        call_command("serve", "0.0.0.0:http")

    assert granian == []


def test_serve_loads_only_the_settings() -> None:
    """Each worker imports the application, as under the server's command line."""
    assert serve.Command.requires_setup is False


SETTINGS: Final = """\
from domainkit.log import build_logging_config
from domainkit_tests.settings import *  # noqa: F403

ASGI_APPLICATION = "serve_asgi:application"
LOGGING = build_logging_config(log_format="json", level="INFO")
SERVE = {{"SERVER": "{server}"}}
"""
APPLICATION_MODULE: Final = """\
from domainkit.http import get_asgi_application

application = get_asgi_application()
"""
STARTED: Final = {"granian": "Started worker-1", "uvicorn": "Uvicorn running on"}
"""The line each server writes once it listens."""
MAIN_PROCESS: Final = {
    "granian": "Starting granian",
    "uvicorn": "Started server process",
}
"""A line of each server's own, not the application's."""
STOPPED: Final = {"granian": 0, "uvicorn": -signal.SIGTERM}
"""Each server's exit status on ``SIGTERM``: uvicorn raises it again once stopped."""
SERVE_WAIT: Final = 60.0
"""Seconds the server gets to listen, and then to stop: well under pytest's
timeout, whose report a pytest-xdist worker loses with the worker."""


class ServerLines:
    """The server's output, read on a thread so that every wait has a deadline."""

    def __init__(self, process: subprocess.Popen[str], started: str) -> None:
        """Read ``process``'s output until it ends; note the line ``started``."""
        self.lines: list[str] = []
        self.listening = threading.Event()
        self.settled = threading.Event()
        """Set once the server listens, or once its output ended first."""
        self.ended = threading.Event()
        self._process = process
        self._marker = started
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self.lines.append(line)
            if self._marker in line:
                self.listening.set()
                self.settled.set()
        self.ended.set()
        self.settled.set()

    def stacks(self) -> str:  # pragma: no cover - only when the server hangs.
        """Have each process of the server write its threads' stacks, then end.

        The server runs in a session of its own with ``PYTHONFAULTHANDLER``:
        ``SIGABRT`` makes a process write every thread's stack to the output
        before it dies. One process at a time, so that their stacks do not
        interleave: the server's own first, so that it starts no worker
        again, then its workers.
        """
        for pid in self._processes():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGABRT)
            time.sleep(1)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._process.pid, signal.SIGKILL)
        self.ended.wait(10)
        return "".join(self.lines)

    def _processes(self) -> list[int]:  # pragma: no cover - only for ``stacks``.
        """The server's processes, by its process group; the server's own first."""
        group = self._process.pid
        found = []
        with contextlib.suppress(OSError):  # No /proc: the server's alone.
            for entry in Path("/proc").iterdir():
                with contextlib.suppress(OSError, ValueError):
                    pid = int(entry.name)
                    if pid != group and os.getpgid(pid) == group:
                        found.append(pid)
        return [group, *found]


@pytest.fixture(params=["granian", "uvicorn"])
def served(request: pytest.FixtureRequest) -> Iterator[tuple[str, Path, list[str]]]:
    """``python -m domainkit serve --uds``: the server, its socket, and its lines.

    On a unix socket of its own, as the Granian test of ``test_servers``.
    """
    server: str = request.param
    directory = Path(tempfile.mkdtemp(prefix="dk-"))
    path = directory / "serve.sock"
    (directory / "serve_settings.py").write_text(SETTINGS.format(server=server))
    (directory / "serve_asgi.py").write_text(APPLICATION_MODULE)
    prefix = project_prefix()
    environment = {
        **os.environ,
        f"{prefix}SETTINGS_MODULE": "serve_settings",
        f"{prefix}ENV_FILE": "",
        "PYTHONPATH": os.pathsep.join([str(directory), str(ROOT), *sys.path]),
        "PYTHONFAULTHANDLER": "1",
    }
    environment.pop("WEB_CONCURRENCY", None)
    try:
        with subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments.
            [sys.executable, "-m", "domainkit", "serve", "--uds", str(path)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        ) as process:
            output = ServerLines(process, STARTED[server])
            try:
                if not output.settled.wait(SERVE_WAIT):  # pragma: no cover - a hang.
                    pytest.fail(
                        f"{server} did not listen within {SERVE_WAIT:g} seconds; "
                        f"its output and stacks:\n{output.stacks()}"
                    )
                if not output.listening.is_set():  # pragma: no cover - a failed start.
                    pytest.fail(f"serve ended before {server} listened: {output.lines}")
                yield server, path, output.lines
                process.send_signal(signal.SIGTERM)
                if not output.ended.wait(SERVE_WAIT):  # pragma: no cover - a hang.
                    pytest.fail(
                        f"{server} did not stop within {SERVE_WAIT:g} seconds of "
                        f"SIGTERM; its output and stacks:\n{output.stacks()}"
                    )
                assert process.wait(30) == STOPPED[server], output.lines
            finally:
                if process.poll() is None:  # pragma: no cover - a stuck shutdown.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_serve_serves_the_application_and_writes_its_lines_through_logging(
    served: tuple[str, Path, list[str]],
) -> None:
    """The server answers on its socket; its own lines are ``LOGGING``'s JSON.

    Its main process, which never imports the application, writes them
    as the application does (user report S1).
    """
    server, path, lines = served

    assert get(path, "/health") == (200, b'{"status":"ok"}')
    records = [json.loads(line) for line in lines if line.startswith("{")]
    assert any(
        MAIN_PROCESS[server] in record["message"]
        and record["logger"] in {"_granian", "uvicorn.error"}
        for record in records
    ), lines


@pytest.mark.parametrize(
    ("server", "websockets", "size"),
    [
        ("granian", True, None),
        ("uvicorn", True, 1_048_576),
        ("uvicorn", False, 1_048_576),
    ],
)
def test_the_application_knows_the_server_serve_started(
    server: str, websockets: bool, size: int | None
) -> None:
    """Under ``manage.py serve``, the server and its limits are ``SERVE``'s.

    uvicorn reads WebSocket messages up to ``MAX_REQUEST_BODY_SIZE``;
    Granian's are unbounded, so a deployed application with WebSocket
    routes logs ``servers.W001``, saying how to turn them off in ``SERVE``.
    """
    with override_settings(SERVE={"SERVER": server, "WEBSOCKETS": websockets}):
        found = server_limits([MANAGE, "serve"], {"UVICORN_WS_MAX_SIZE": "1"})

    assert found == ServerLimits(server, websockets, size, launcher="serve")
    assert server_limits(["/venv/bin/domainkit", "serve", "8000"], {}) is not None
    assert server_limits([MANAGE, "check"], {}) is None
    assert server_limits(["/venv/bin/uvicorn", "serve"], {}) == ServerLimits(
        "uvicorn", True, servers.UVICORN_WS_MAX_SIZE
    )


def test_serve_with_granian_is_told_to_turn_websockets_off_in_serve() -> None:
    """``servers.W001`` names ``SERVE["WEBSOCKETS"]``, not Granian's option."""
    found = limit_warnings(1024, ServerLimits("granian", True, None, launcher="serve"))

    assert found == [
        (
            "granian reads each WebSocket message whole, at any size, before "
            'MAX_REQUEST_BODY_SIZE (1024) refuses it; set SERVE["WEBSOCKETS"] = '
            "False if the API takes no WebSockets, or limit messages in the proxy "
            "in front."
        )
    ]


def test_serve_on_a_unix_socket_is_seen() -> None:
    """``servers.W002`` sees ``serve --uds`` in the server's own command line."""
    assert listens_on_unix_socket([MANAGE, "serve", "--uds", "/run/api.sock"], {})
    assert listens_on_unix_socket([MANAGE, "serve", "--uds=/run/api.sock"], {})
    assert not listens_on_unix_socket([MANAGE, "serve", "0.0.0.0:8000"], {})
    assert not listens_on_unix_socket([MANAGE, "runserver", "--uds", "x"], {})


GRANIAN_OPTION: Final = (
    "--http1-header-read-timeout (GRANIAN_HTTP1_HEADER_READ_TIMEOUT), in milliseconds,"
)
UVICORN_OPTION: Final = "--timeout-keep-alive (UVICORN_TIMEOUT_KEEP_ALIVE)"
FASTAPI_OPTION: Final = "uvicorn's --timeout-keep-alive, which fastapi run cannot set"
SERVED_GRANIAN: Final = KeepAlive("Granian", 60, 'SERVE["KEEP_ALIVE"]', most=60)
SERVED_UVICORN: Final = KeepAlive("uvicorn", 5, 'SERVE["KEEP_ALIVE"]')


@pytest.mark.parametrize(
    ("argv", "environ", "found"),
    [
        (
            ["/venv/bin/granian", "--interface", "asgi"],
            {},
            KeepAlive("Granian", GRANIAN_KEEP_ALIVE, GRANIAN_OPTION, 1000, 60),
        ),
        (
            ["/venv/bin/granian", "--http1-header-read-timeout", "45500"],
            {},
            KeepAlive("Granian", 45.5, GRANIAN_OPTION, 1000, 60),
        ),
        (
            [MANAGE, "check"],
            {"GRANIAN_HTTP1_HEADER_READ_TIMEOUT": "60000"},
            KeepAlive("Granian", 60, GRANIAN_OPTION, 1000, 60),
        ),
        ([MANAGE, "check"], {"GRANIAN_HTTP1_KEEP_ALIVE": "false"}, None),
        (["/venv/bin/granian", "--no-http1-keep-alive"], {}, None),
        ([MANAGE, "check"], {"GRANIAN_HTTP1_HEADER_READ_TIMEOUT": "1m"}, None),
        (
            ["/venv/bin/uvicorn", "app:app"],
            {},
            KeepAlive("uvicorn", UVICORN_KEEP_ALIVE, UVICORN_OPTION),
        ),
        (
            ["/venv/bin/uvicorn", "--timeout-keep-alive=75"],
            {"UVICORN_TIMEOUT_KEEP_ALIVE": "3"},
            KeepAlive("uvicorn", 75, UVICORN_OPTION),
        ),
        (
            [MANAGE, "check"],
            {"UVICORN_TIMEOUT_KEEP_ALIVE": "65"},
            KeepAlive("uvicorn", 65, UVICORN_OPTION),
        ),
        (
            ["/venv/bin/gunicorn", "-k", "uvicorn.workers.UvicornWorker"],
            {},
            KeepAlive("uvicorn", 2, "gunicorn's --keep-alive"),
        ),
        (
            [
                "/venv/bin/gunicorn",
                "-k",
                "uvicorn_worker.UvicornWorker",
                "--keep-alive",
                "70",
            ],
            {"GUNICORN_CMD_ARGS": "--keep-alive 3"},
            KeepAlive("uvicorn", 70, "gunicorn's --keep-alive"),
        ),
        (
            [MANAGE, "check"],
            {"GUNICORN_CMD_ARGS": "-k uvicorn.workers.UvicornWorker --keep-alive=65"},
            KeepAlive("uvicorn", 65, "gunicorn's --keep-alive"),
        ),
        (
            ["/venv/bin/gunicorn", "-k", "uvicorn.workers.UvicornWorker"],
            {"GUNICORN_CMD_ARGS": "--keep-alive '3"},
            None,
        ),
        (
            ["/venv/bin/fastapi", "run", "main.py"],
            {},
            KeepAlive("uvicorn", 5, FASTAPI_OPTION),
        ),
        (
            [MANAGE, "serve"],
            {"GRANIAN_HTTP1_HEADER_READ_TIMEOUT": "1000"},
            SERVED_GRANIAN,
        ),
        ([MANAGE, "check"], {}, SERVED_GRANIAN),
    ],
)
def test_the_servers_keep_alive_is_found_as_the_server_is(
    argv: list[str], environ: dict[str, str], found: KeepAlive | None
) -> None:
    """From the server's options or variables; where none, ``serve``'s.

    A value that is not a whole number is unknown: nothing is said.
    Granian with keep-alive off keeps no idle connection.
    """
    assert server_keep_alive(argv, environ) == found


@pytest.mark.parametrize(
    ("configured", "found"),
    [
        ({}, SERVED_GRANIAN),
        ({"KEEP_ALIVE": 45}, KeepAlive("Granian", 45, 'SERVE["KEEP_ALIVE"]', most=60)),
        ({"SERVER": "uvicorn"}, SERVED_UVICORN),
        (
            {"SERVER": "uvicorn", "KEEP_ALIVE": 120},
            KeepAlive("uvicorn", 120, 'SERVE["KEEP_ALIVE"]'),
        ),
    ],
)
def test_serve_keeps_idle_connections_as_set(
    configured: dict[str, object], found: KeepAlive
) -> None:
    """``SERVE["KEEP_ALIVE"]``, else uvicorn's 5 seconds, or Granian's most, 60."""
    with override_settings(SERVE=configured):
        assert serve_keep_alive() == found


HINTS: Final = [
    (
        60.0,
        SERVED_GRANIAN,
        (
            "Granian keeps an idle connection 60 seconds at most: lower the "
            "proxy's idle timeout below 60 seconds."
        ),
    ),
    (
        90.0,
        KeepAlive("Granian", 30, GRANIAN_OPTION, 1000, 60),
        (
            "Granian keeps an idle connection 60 seconds at most: lower the "
            "proxy's idle timeout below 30 seconds."
        ),
    ),
    (
        50.0,
        KeepAlive("Granian", 30, GRANIAN_OPTION, 1000, 60),
        (
            f"Set {GRANIAN_OPTION} to 50001 or more, or lower the proxy's idle "
            "timeout below 30 seconds."
        ),
    ),
    (
        60.0,
        SERVED_UVICORN,
        (
            'Set SERVE["KEEP_ALIVE"] to 61 or more, or lower the proxy\'s idle '
            "timeout below 5 seconds."
        ),
    ),
    (
        4.5,
        KeepAlive("uvicorn", 2, "gunicorn's --keep-alive"),
        (
            "Set gunicorn's --keep-alive to 5 or more, or lower the proxy's idle "
            "timeout below 2 seconds."
        ),
    ),
]
"""A proxy's idle timeout, the server's keep-alive, and what the hint says."""


@pytest.mark.parametrize(("proxy", "keep_alive", "hint"), HINTS)
def test_a_proxy_that_keeps_idle_connections_as_long_as_the_server_is_warned(
    proxy: float, keep_alive: KeepAlive, hint: str
) -> None:
    """``servers.W003``, with both numbers, and what to change (user report S1).

    The server must close an idle connection after the proxy does, never
    first or at once: else the proxy may send a request on it as it
    closes, and answer 502 (AWS's guidance for its load balancers).
    """
    (found,) = keep_alive_warnings(proxy, keep_alive)

    assert found.id == "servers.W003"
    assert found.message == (
        f"The proxy keeps an idle connection to the server open {proxy:g} seconds "
        f"(PROXY_IDLE_TIMEOUT), and {keep_alive.server} closes it after "
        f"{keep_alive.seconds:g}: the proxy may send a request on a connection as "
        f"{keep_alive.server} closes it, and answer that request 502."
    )
    assert found.hint == hint


def test_a_proxy_that_closes_idle_connections_first_is_not_warned() -> None:
    """A shorter idle timeout, none set, or a server whose keep-alive is unknown."""
    assert keep_alive_warnings(59.0, SERVED_GRANIAN) == []
    assert keep_alive_warnings(4.999, SERVED_UVICORN) == []
    assert keep_alive_warnings(None, SERVED_UVICORN) == []
    assert keep_alive_warnings(600.0, None) == []
    assert settings.PROXY_IDLE_TIMEOUT is None


def test_check_compares_the_proxys_idle_timeout_with_the_servers_where_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``check`` warns in staging and production, ``check --deploy`` fails.

    With no server's variable, it judges the server ``serve`` starts:
    Granian here, an idle connection 60 seconds. An AWS load balancer
    keeps one 60 seconds unless set; nginx's upstream ``keepalive_timeout``
    too.
    """
    for name in list(os.environ):
        if name.startswith(("UVICORN_", "GRANIAN_", "GUNICORN_")):
            monkeypatch.delenv(name)

    with override_settings(**PRODUCTION, PROXY_IDLE_TIMEOUT=60):
        call_command("check", stdout=(deployed := io.StringIO()))
        with pytest.raises(CommandError, match=r"servers\.W003"):
            call_command("check", "--deploy", stdout=io.StringIO())
    with override_settings(**PRODUCTION, PROXY_IDLE_TIMEOUT=55):
        call_command("check", stdout=(shorter := io.StringIO()))
    with override_settings(**PRODUCTION):
        call_command("check", stdout=(unset := io.StringIO()))
    with override_settings(PROXY_IDLE_TIMEOUT=60):
        call_command("check", stdout=(local := io.StringIO()))

    assert (
        "  Warning: servers.W003: The proxy keeps an idle connection to the server "
        "open 60 seconds (PROXY_IDLE_TIMEOUT), and Granian closes it after 60"
    ) in deployed.getvalue()
    assert "Hint: Granian keeps an idle connection 60 seconds at most" in (
        deployed.getvalue()
    )
    for quiet in (shorter, unset, local):
        assert "servers.W003" not in quiet.getvalue()


def test_the_proxys_idle_timeout_is_bounded() -> None:
    """Positive, and a day at most."""
    for wrong in (0, -1, 86_401):
        with (
            pytest.raises(ImproperlyConfigured, match="PROXY_IDLE_TIMEOUT"),
            override_settings(PROXY_IDLE_TIMEOUT=wrong),
        ):
            pass  # pragma: no cover - refused as it loads.
