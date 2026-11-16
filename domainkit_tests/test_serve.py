"""``serve``: the production server, from the settings, and the proxy's idle timeout.

``serve`` starts Granian, or uvicorn, with the options "Server limits"
gives, read from ``SERVE`` (user report S1). ``check`` warns
(``servers.W003``) when the proxy in front keeps idle connections as
long as the server, or longer.
"""

import asyncio
import io
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, MutableMapping
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

    def serve(self, **serving: Any) -> None:  # noqa: ANN401 - Granian's.
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
    writes it, and it never reloads. It speaks HTTP/1.1 and h2c, as
    Granian does unless told, and bounds a request's headers to 16 KiB over
    both (user report S1).
    """
    from granian.constants import HTTPModes, Interfaces, Loops
    from granian.http import HTTP1Settings, HTTP2Settings

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
        "http": HTTPModes.auto,
        "http1_settings": HTTP1Settings(
            header_read_timeout=60_000, max_buffer_size=16_384
        ),
        "http2_settings": HTTP2Settings(max_headers_size=16_384),
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


@pytest.mark.parametrize("http", ["auto", "1", "2"])
def test_serve_keeps_granian_to_the_http_versions_set(
    granian: list[dict[str, Any]], http: str
) -> None:
    """``SERVE["HTTP"]`` is Granian's ``--http``: ``"1"`` keeps it to HTTP/1.1.

    Granian's own default, ``auto``, also takes HTTP/2 without TLS (h2c)
    from a client that opens with it. Over HTTP/2 too, a request's
    headers are bounded by ``MAX_HEADER_SIZE``, where Granian's HTTP/2
    takes 16 MiB unless told (user report S1).
    """
    from granian.constants import HTTPModes
    from granian.http import HTTP2Settings

    with override_settings(
        ASGI_APPLICATION=APPLICATION,
        SERVE={"HTTP": http, "WEBSOCKETS": False, "MAX_HEADER_SIZE": 32_768},
    ):
        call_command("serve")

    options = granian[0]
    assert options["http"] is HTTPModes(http)
    assert options["http2_settings"] == HTTP2Settings(max_headers_size=32_768)
    assert HTTP2Settings().max_headers_size == 16 * 1024 * 1024


def test_http_two_alone_is_refused_where_it_cannot_be_served(
    monkeypatch: pytest.MonkeyPatch, uvicorn: list[dict[str, Any]]
) -> None:
    """HTTP/2 alone: uvicorn speaks HTTP/1.1 only, Granian no WebSocket over it.

    Named, ``SERVE`` refuses either as it loads; with ``auto`` choosing
    uvicorn, ``serve`` refuses as it starts. ``"1"`` and ``"auto"`` are
    uvicorn's HTTP/1.1.
    """
    for refused, wrong in [
        ({"SERVER": "uvicorn", "HTTP": "2"}, "uvicorn speaks HTTP/1.1 only"),
        ({"HTTP": "2"}, "Granian serves no WebSocket: set WEBSOCKETS to False"),
    ]:
        with (
            pytest.raises(ImproperlyConfigured, match=wrong),
            override_settings(SERVE=refused),
        ):
            pass  # pragma: no cover - refused as it loads.
    monkeypatch.setattr(servers, "find_spec", lambda name: None)
    with (
        override_settings(
            ASGI_APPLICATION=APPLICATION, SERVE={"HTTP": "2", "WEBSOCKETS": False}
        ),
        pytest.raises(CommandError) as refused_at_start,
    ):
        call_command("serve")
    for http in ("1", "auto"):
        with override_settings(
            ASGI_APPLICATION=APPLICATION, SERVE={"SERVER": "uvicorn", "HTTP": http}
        ):
            call_command("serve")

    assert str(refused_at_start.value) == (
        'SERVE["HTTP"] is "2", but uvicorn speaks HTTP/1.1 only: install the '
        'granian extra, or set it to "1" or "auto".'
    )
    assert len(uvicorn) == 2
    assert ServeConfig().HTTP == "auto"
    assert ServeConfig().EXIT_ON_FAILED_START is False


class Lifespan:
    """An ASGI application whose lifespan startup sends ``outcome``."""

    def __init__(self, outcome: str) -> None:
        """Answer the startup with ``lifespan.startup.<outcome>``."""
        self.outcome = outcome
        self.served: list[str] = []

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Any,  # noqa: ANN401 - the server's.
        send: Any,  # noqa: ANN401 - the server's.
    ) -> None:
        """Answer the startup, or note a request."""
        if scope["type"] != "lifespan":
            self.served.append(scope["type"])
            return
        await receive()
        await send({"type": f"lifespan.startup.{self.outcome}"})


FAILING: Final = Lifespan("failed")
"""A module-level application that ``load_application`` can import."""


@pytest.fixture
def signalled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """The signals a worker sends (``os.kill``), recorded, never sent."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


@pytest.mark.anyio
async def test_a_worker_whose_startup_fails_tells_the_main_process(
    signalled: list[tuple[int, int]],
) -> None:
    """The worker's application signals ``SIGUSR1`` once its startup fails.

    A startup that completes signals nothing; a request reaches the
    application in one plain call, its own awaitable returned.
    """
    failing = serve.load_application(4321, f"{__name__}:FAILING")
    working = serve._Starting(Lifespan("complete"), 4321)
    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "lifespan.startup"}

    await failing({"type": "lifespan"}, receive, send)
    await working({"type": "lifespan"}, receive, send)
    answer = working({"type": "http"}, receive, send)
    await answer

    assert failing.application is FAILING
    assert signalled == [(4321, signal.SIGUSR1)]
    assert [message["type"] for message in sent] == [
        "lifespan.startup.failed",
        "lifespan.startup.complete",
    ]
    assert answer.cr_code is Lifespan.__call__.__code__  # type: ignore[attr-defined]
    assert working.application.served == ["http"]  # type: ignore[attr-defined]


def test_a_worker_that_cannot_import_the_application_tells_the_main_process(
    signalled: list[tuple[int, int]],
) -> None:
    """An import that fails signals, then fails as it would have."""
    with pytest.raises(ModuleNotFoundError):
        serve.load_application(4321, "domainkit_tests.no_such_module:application")
    with pytest.raises(AttributeError):
        serve.load_application(4321, f"{__name__}:FAILING.missing")

    assert signalled == [(4321, signal.SIGUSR1)] * 2


class Starting:
    """A stand-in for ``granian.Granian`` whose two workers fail as they start.

    Its ``serve`` handles ``SIGTERM`` as Granian does (stop serving), loads
    the application as each worker would, then exits 1, as Granian does
    when a worker died, unless ``exits`` is 0.
    """

    made: list[dict[str, Any]]
    exits: int = 1

    def __init__(self, **options: Any) -> None:  # noqa: ANN401 - Granian's options.
        """Record ``options``."""
        type(self).made.append(options)

    def serve(self, target_loader: Any) -> None:  # noqa: ANN401 - Granian's loader.
        """Load the application as a worker does; stop on ``SIGTERM``."""
        stopped: list[int] = []
        previous = signal.signal(signal.SIGTERM, lambda sig, frame: stopped.append(sig))
        try:
            for _ in range(2):
                loaded = target_loader(f"{__name__}:FAILING")
                asyncio.run(loaded({"type": "lifespan"}, _startup, _ignore))
        finally:
            signal.signal(signal.SIGTERM, previous)
        type(self).made.append({"stopped": stopped, "loader": target_loader})
        if self.exits:
            raise SystemExit(self.exits)


async def _startup() -> MutableMapping[str, Any]:
    """The lifespan's first message."""
    return {"type": "lifespan.startup"}


async def _ignore(message: MutableMapping[str, Any]) -> None:
    """Take a message the server would read."""


@pytest.mark.parametrize("exits", [1, 0])
def test_serve_exits_when_a_worker_fails_as_it_starts(
    monkeypatch: pytest.MonkeyPatch, exits: int
) -> None:
    """``EXIT_ON_FAILED_START``: the signal stops Granian; ``serve`` exits 3.

    The first worker's signal stops Granian, once. Granian may exit 1 (a
    worker died) or return: either way ``serve`` says why, with uvicorn's
    and gunicorn's status for a worker that could not start (user report
    S1).
    """
    made: list[dict[str, Any]] = []
    monkeypatch.setattr(Starting, "made", made, raising=False)
    monkeypatch.setattr(Starting, "exits", exits)
    monkeypatch.setattr("granian.Granian", Starting)
    before = signal.getsignal(signal.SIGUSR1)

    with (
        override_settings(
            ASGI_APPLICATION=APPLICATION, SERVE={"EXIT_ON_FAILED_START": True}
        ),
        pytest.raises(CommandError) as stopped,
    ):
        call_command("serve")

    (options, served) = made
    assert options["respawn_failed_workers"] is True
    assert served["stopped"] == [signal.SIGTERM]
    assert served["loader"].func is serve.load_application
    assert served["loader"].args == (os.getpid(),)
    assert stopped.value.returncode == serve.FAILED_START == 3
    assert str(stopped.value) == (
        "A worker failed as it started, so the server stopped "
        '(SERVE["EXIT_ON_FAILED_START"]); its log above says why.'
    )
    assert signal.getsignal(signal.SIGUSR1) is before


def test_serve_lets_granian_stop_as_it_would_when_no_start_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that died once it served: Granian's own exit, and status, stand."""

    class Died(Served):
        def serve(self, **serving: Any) -> None:  # noqa: ANN401 - Granian's.
            raise SystemExit(1)

    made: list[dict[str, Any]] = []
    monkeypatch.setattr(Served, "made", made, raising=False)
    for granian, ends in [(Served, None), (Died, SystemExit)]:
        monkeypatch.setattr("granian.Granian", granian)
        with override_settings(
            ASGI_APPLICATION=APPLICATION, SERVE={"EXIT_ON_FAILED_START": True}
        ):
            if ends is None:
                call_command("serve")
            else:
                with pytest.raises(ends) as exited:
                    call_command("serve")

    assert exited.value.code == 1
    assert made[1] == {"served": True}


def test_exit_on_failed_start_needs_posix_signals(
    monkeypatch: pytest.MonkeyPatch, granian: list[dict[str, Any]]
) -> None:
    """Without ``SIGUSR1`` (Windows), the option is refused before Granian starts."""
    monkeypatch.delattr(signal, "SIGUSR1")

    with (
        override_settings(
            ASGI_APPLICATION=APPLICATION, SERVE={"EXIT_ON_FAILED_START": True}
        ),
        pytest.raises(CommandError, match="needs POSIX signals"),
    ):
        call_command("serve")

    assert granian == []


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
    }
    environment.pop("WEB_CONCURRENCY", None)
    lines: list[str] = []
    try:
        with subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments.
            [sys.executable, "-m", "domainkit", "serve", "--uds", str(path)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            try:
                assert process.stdout is not None
                for line in process.stdout:  # Waits on the server's log, not a clock.
                    lines.append(line)
                    if STARTED[server] in line:
                        break
                else:  # pragma: no cover - only when the server fails to start.
                    pytest.fail(f"serve ended before {server} listened: {lines}")
                yield server, path, lines
                process.send_signal(signal.SIGTERM)
                lines.extend(process.stdout)
                assert process.wait(30) == STOPPED[server], lines
            finally:
                if process.poll() is None:  # pragma: no cover - a stuck shutdown.
                    process.kill()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


FAILING_SETTINGS: Final = """\
from domainkit_tests.settings import *  # noqa: F403

ASGI_APPLICATION = "failing_asgi:application"
SERVE = {{"SERVER": "granian", "EXIT_ON_FAILED_START": {exits}}}
"""
FAILING_MODULE: Final = """\
async def application(scope, receive, send):
    await receive()
    await send({"type": "lifespan.startup.failed", "message": "no database"})
"""


@pytest.mark.parametrize(("exits", "status"), [(True, 3), (False, 1)])
def test_granian_under_serve_stops_at_a_failed_start_only_when_set(
    exits: bool, status: int
) -> None:
    """A worker whose startup fails: with the option, ``serve`` exits 3 at once.

    Without it, Granian starts the worker again, and stops only because
    the second failure came within 5.5 seconds of the restart (its crash
    loop). A startup that takes longer to fail is started again for
    ever, and the container stays up, serving nothing (user report S1).
    """
    pytest.importorskip("granian")
    directory = Path(tempfile.mkdtemp(prefix="dk-"))
    (directory / "failing_settings.py").write_text(FAILING_SETTINGS.format(exits=exits))
    (directory / "failing_asgi.py").write_text(FAILING_MODULE)
    environment = {
        **os.environ,
        f"{project_prefix()}SETTINGS_MODULE": "failing_settings",
        f"{project_prefix()}ENV_FILE": "",
        "PYTHONPATH": os.pathsep.join([str(directory), str(ROOT), *sys.path]),
    }
    environment.pop("WEB_CONCURRENCY", None)
    try:
        finished = subprocess.run(  # noqa: S603 - this interpreter, fixed arguments.
            [
                sys.executable,
                "-m",
                "domainkit",
                "serve",
                "--uds",
                str(directory / "serve.sock"),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    output = finished.stdout + finished.stderr
    assert finished.returncode == status, output
    assert "no database" in output
    assert ("Respawning worker-1" in output) is not exits, output
    assert ('SERVE["EXIT_ON_FAILED_START"]' in output) is exits, output


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
