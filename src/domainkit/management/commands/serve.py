"""``serve``: serve ``ASGI_APPLICATION`` in production, with Granian or uvicorn.

``runserver`` is for development: it restarts when the code changes, and
takes an address and nothing else. ``serve`` starts the server with the
options of docs/reference/servers.md ("Server limits"), read from
``SERVE``:

- the server: Granian where its extra is installed, else uvicorn, unless
  ``SERVE["SERVER"]`` names one;
- its worker processes: ``SERVE["WORKERS"]``, else ``WEB_CONCURRENCY``,
  else one;
- each worker's connections, the backlog, how long an idle connection
  stays open, how long requests in flight get once the server stops, the
  size of a request's headers, and whether it takes WebSockets;
- under Granian, the HTTP versions it speaks (``SERVE["HTTP"]``), and
  whether a worker that fails as it starts stops the server
  (``SERVE["EXIT_ON_FAILED_START"]``).

Granian starts again a worker that died, unless it died within 5.5
seconds of being started again: a startup that fails slower is started
again for ever, and the container stays up, serving nothing. With
``EXIT_ON_FAILED_START``, each worker loads the application through
``load_application``, which signals the main process (``SIGUSR1``) when
the import or the application's startup fails; the main process then
stops Granian, as on ``SIGTERM``, and ``serve`` exits 3, as uvicorn and
gunicorn do when a worker cannot start. A worker that dies once it served
is started again (ADR 0169).

It never reloads. It reads no proxy header (``TRUSTED_PROXIES`` does),
writes no access log (``domainkit.access`` does), and, under uvicorn, no
``Server`` header. Its own lines (it starts, a worker died) go through
``LOGGING``, given to it as ``logconfig`` writes it: its main process,
which never imports the application, writes them as the application does.

Only the settings are loaded here: each worker imports the application,
as under the server's own command line. ``check --deploy`` reads the same
settings, so it judges the server this command starts (ADR 0160).
"""

import math
import os
import signal
from argparse import ArgumentParser
from collections.abc import Awaitable, Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from functools import partial
from importlib import import_module
from pathlib import Path
from types import FrameType
from typing import Any, Final

from domainkit.conf import settings
from domainkit.conf.global_settings import GRANIAN_MAX_KEEP_ALIVE, MAX_SERVE_WORKERS
from domainkit.http.servers import serve_keep_alive, serve_server
from domainkit.management.base import BaseCommand, CommandError
from domainkit.management.commands.logconfig import portable
from domainkit.management.commands.runserver import parse_address
from domainkit.utils.loops import loop_factory

DEFAULT_HOST: Final = "0.0.0.0"  # noqa: S104 - in a container, behind its proxy.
"""Every address of the host, as a container behind its proxy needs."""
DEFAULT_PORT: Final = 8000
FAILED_START: Final = 3
"""``serve``'s exit status when a worker failed as it started (uvicorn's too)."""

type _Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type _Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
type _Application = Callable[
    [MutableMapping[str, Any], _Receive, _Send], Awaitable[None]
]


class Command(BaseCommand):
    """Start Granian or uvicorn with the limits of ``SERVE``; never reload."""

    help = (
        "Serve ASGI_APPLICATION in production: Granian where installed, else "
        "uvicorn, with the limits of SERVE, and LOGGING for the server's own lines."
    )
    requires_setup = False

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept an optional address, or a unix socket."""
        parser.add_argument(
            "address",
            nargs="?",
            default=f"{DEFAULT_HOST}:{DEFAULT_PORT}",
            help="port or address:port (default: %(default)s)",
        )
        parser.add_argument(
            "--uds",
            type=Path,
            help="listen on this unix socket instead, for a proxy on the same host",
        )

    def handle(
        self,
        *,
        address: str = f"{DEFAULT_HOST}:{DEFAULT_PORT}",
        uds: Path | None = None,
        **options: object,
    ) -> None:
        """Start the server ``SERVE`` names, until it is stopped."""
        target = settings.ASGI_APPLICATION
        if not target:
            raise CommandError("Set ASGI_APPLICATION, e.g. 'config.asgi:application'.")
        host, port = parse_address(address, host=DEFAULT_HOST)
        workers = worker_count(os.environ)
        if serve_server(settings.SERVE.SERVER) == "granian":
            given = granian_options(target, host, port, uds, workers)
            if settings.SERVE.EXIT_ON_FAILED_START:
                serve_until_a_start_fails(given)
                return
            serve_with_granian(given)
            return
        if settings.SERVE.HTTP == "2":
            raise CommandError(
                'SERVE["HTTP"] is "2", but uvicorn speaks HTTP/1.1 only: install '
                'the granian extra, or set it to "1" or "auto".'
            )
        import uvicorn

        uvicorn.run(**uvicorn_options(target, host, port, uds, workers))


def worker_count(environ: Mapping[str, str]) -> int:
    """``SERVE["WORKERS"]``, else ``WEB_CONCURRENCY`` (as uvicorn and gunicorn), else 1.

    Raises:
        CommandError: ``WEB_CONCURRENCY`` is not a whole number from 1 to
            ``MAX_SERVE_WORKERS``.
    """
    if settings.SERVE.WORKERS is not None:
        return settings.SERVE.WORKERS
    value = environ.get("WEB_CONCURRENCY", "").strip()
    if not value:
        return 1
    if not (value.isdecimal() and len(value) <= 4) or not (
        1 <= int(value) <= MAX_SERVE_WORKERS
    ):
        raise CommandError(
            f"WEB_CONCURRENCY is {value!r}: give the worker processes as a whole "
            f'number from 1 to {MAX_SERVE_WORKERS}, or set SERVE["WORKERS"].'
        )
    return int(value)


def granian_options(
    target: str, host: str, port: int, uds: Path | None, workers: int
) -> dict[str, Any]:
    """What ``serve`` gives ``granian.Granian``: the options "Server limits" gives.

    Its loop is ``EVENT_LOOP``'s, resolved here: Granian's own ``auto``
    would take rloop before uvloop. It speaks the HTTP versions of
    ``SERVE["HTTP"]``, and bounds a request's headers by
    ``MAX_HEADER_SIZE`` over HTTP/2 as over HTTP/1.1 (its HTTP/2 default
    is 16 MiB). It keeps an idle connection as long as it waits for the
    next request's headers. A worker that dies is started again. Workers
    still running ``SERVE["GRACEFUL_TIMEOUT"]`` plus ``SHUTDOWN_TIMEOUT``
    seconds after the stop are killed: the application's shutdown counts
    in Granian's time.

    Raises:
        CommandError: Granian is not installed, or ``SERVE["KEEP_ALIVE"]``
            is longer than it takes.
    """
    try:
        from granian.constants import HTTPModes, Interfaces, Loops
        from granian.http import HTTP1Settings, HTTP2Settings
    except ImportError:
        raise CommandError(
            'SERVE["SERVER"] is "granian", which needs the granian extra: '
            "pip install 'domainkit[granian]'."
        ) from None
    serve = settings.SERVE
    keep_alive = serve_keep_alive().seconds
    if keep_alive > GRANIAN_MAX_KEEP_ALIVE:
        raise CommandError(
            f'SERVE["KEEP_ALIVE"] is {keep_alive:g}, but Granian keeps an idle '
            f"connection {GRANIAN_MAX_KEEP_ALIVE} seconds at most: set it to "
            f"{GRANIAN_MAX_KEEP_ALIVE} or less, or serve with uvicorn "
            '(SERVE["SERVER"] = "uvicorn").'
        )
    uvloop = loop_factory(settings.EVENT_LOOP) is not None
    return {
        "target": target,
        "address": host,
        "port": port,
        "uds": uds,
        "interface": Interfaces.ASGI,
        "workers": workers,
        "loop": Loops.uvloop if uvloop else Loops.asyncio,
        "backlog": serve.BACKLOG,
        "backpressure": serve.MAX_CONNECTIONS,
        "http": HTTPModes(serve.HTTP),
        "http1_settings": HTTP1Settings(
            header_read_timeout=int(keep_alive * 1000),
            max_buffer_size=serve.MAX_HEADER_SIZE,
        ),
        "http2_settings": HTTP2Settings(max_headers_size=serve.MAX_HEADER_SIZE),
        "websockets": serve.WEBSOCKETS,
        "respawn_failed_workers": True,
        "workers_kill_timeout": serve.GRACEFUL_TIMEOUT
        + math.ceil(settings.SHUTDOWN_TIMEOUT),
        "log_dictconfig": portable(settings.LOGGING, "LOGGING"),
        "log_access": False,
        "reload": False,
    }


def serve_with_granian(options: dict[str, Any], **serving: Any) -> None:  # noqa: ANN401 - Granian's serve() arguments.
    """Run Granian with ``options`` until it stops; ``serving`` goes to its ``serve``.

    Raises:
        CommandError: Granian refused its configuration, as its log says.
    """
    from granian import Granian
    from granian.errors import FatalError

    try:
        Granian(**options).serve(**serving)
    except FatalError as exc:
        raise CommandError(
            f"Granian could not start ({exc}); its log above says why."
        ) from None


def serve_until_a_start_fails(options: dict[str, Any]) -> None:
    """Run Granian until it stops, or until a worker fails as it starts.

    Each worker loads the application with ``load_application``, which
    signals this process (``SIGUSR1``) as its start fails; this process
    then stops Granian as ``SIGTERM`` does.

    Raises:
        CommandError: A worker failed as it started (exit status 3), or
            Granian refused its configuration.
    """
    with _failed_starts() as failed:
        try:
            serve_with_granian(
                options, target_loader=partial(load_application, os.getpid())
            )
        except SystemExit:
            if not failed:
                raise
        if failed:
            raise CommandError(
                "A worker failed as it started, so the server stopped "
                '(SERVE["EXIT_ON_FAILED_START"]); its log above says why.',
                returncode=FAILED_START,
            )


@contextmanager
def _failed_starts() -> Iterator[list[int]]:
    """The signals of workers that failed as they started, while Granian serves.

    The first one stops Granian, as ``SIGTERM`` does: its own handler of
    ``SIGTERM`` stops the workers, then returns from ``serve``.

    Raises:
        CommandError: This platform has no ``SIGUSR1`` (Windows).
    """
    started = getattr(signal, "SIGUSR1", None)
    if started is None:
        raise CommandError(
            'SERVE["EXIT_ON_FAILED_START"] needs POSIX signals, which this '
            "platform lacks."
        )
    failed: list[int] = []

    def stop(signum: int, frame: FrameType | None) -> None:
        if not failed:
            signal.raise_signal(signal.SIGTERM)
        failed.append(signum)

    previous = signal.signal(started, stop)
    try:
        yield failed
    finally:
        signal.signal(started, previous)


def load_application(main: int, target: str) -> "_Starting":
    """The application at ``target`` (``module:attribute``), for a Granian worker.

    It tells the main process, ``main``, when the import fails, or once
    the application's startup fails (``serve_until_a_start_fails``).
    """
    try:
        module, _, name = target.partition(":")
        found: Any = import_module(module)
        for part in (name or "app").split("."):
            found = getattr(found, part)
    except BaseException:
        _failed(main)
        raise
    return _Starting(found, main)


class _Starting:
    """An application that signals the main process when its startup fails.

    Requests reach the application as they would without it: their call
    returns the application's own awaitable, in one plain call.
    """

    def __init__(self, application: _Application, main: int) -> None:
        """Serve ``application``; signal ``main`` if its startup fails."""
        self.application = application
        self.main = main

    def __call__(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send
    ) -> Awaitable[None]:
        """The application's answer; for the lifespan, its startup watched."""
        if scope["type"] != "lifespan":
            return self.application(scope, receive, send)
        return self._lifespan(scope, receive, send)

    async def _lifespan(
        self, scope: MutableMapping[str, Any], receive: _Receive, send: _Send
    ) -> None:
        """Run the lifespan, signalling ``main`` once its startup failed."""

        async def sending(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "lifespan.startup.failed":
                _failed(self.main)
            await send(message)

        await self.application(scope, receive, sending)


def _failed(main: int) -> None:
    """Tell the main process, ``main``, that this worker failed as it started."""
    os.kill(main, signal.SIGUSR1)


def uvicorn_options(
    target: str, host: str, port: int, uds: Path | None, workers: int
) -> dict[str, Any]:
    """What ``serve`` gives ``uvicorn.run``: the options "Server limits" gives.

    A WebSocket message is read up to ``MAX_REQUEST_BODY_SIZE``, which
    refuses a larger one anyway. Its loop is ``EVENT_LOOP``'s, resolved
    here as for Granian.
    """
    serve = settings.SERVE
    uvloop = loop_factory(settings.EVENT_LOOP) is not None
    return {
        "app": target,
        "host": host,
        "port": port,
        "uds": None if uds is None else str(uds),
        "workers": workers,
        "loop": "uvloop" if uvloop else "asyncio",
        "limit_concurrency": serve.MAX_CONNECTIONS,
        "backlog": serve.BACKLOG,
        "timeout_keep_alive": int(serve_keep_alive().seconds),
        "timeout_graceful_shutdown": serve.GRACEFUL_TIMEOUT,
        "h11_max_incomplete_event_size": serve.MAX_HEADER_SIZE,
        "ws": "auto" if serve.WEBSOCKETS else "none",
        "ws_max_size": settings.MAX_REQUEST_BODY_SIZE,
        # ProxyHeadersMiddleware and TRUSTED_PROXIES handle forwarding.
        "proxy_headers": False,
        "server_header": False,
        "log_config": portable(settings.LOGGING, "LOGGING"),
        "access_log": False,
        "reload": False,
    }
