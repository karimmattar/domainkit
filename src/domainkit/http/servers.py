"""What the ASGI server reads before the application sees it.

``MAX_REQUEST_BODY_SIZE`` closes a WebSocket message that is too large, but
only once the server has read it whole: uvicorn reads up to 16 MiB unless
``--ws-max-size`` says less, and Granian has no such limit. ``server_limits``
finds which server runs, and its WebSocket limit, from its command line
(the server's own process) or its environment variables (``UVICORN_*``,
``GRANIAN_*``, which both read, and ``GUNICORN_CMD_ARGS``);
``limit_warnings`` says what to change. ``check`` lists them in staging and
production, from the environment, and a deployed application logs them
when it starts. ``check`` runs apart from the server, often in another
container (``check --deploy`` in a deployment job): there it sees the
variables set where it runs, never the server's arguments.

uvicorn is found however it is started: ``uvicorn`` itself, gunicorn with
its worker class (``-k uvicorn.workers.UvicornWorker``, or
``uvicorn_worker.UvicornWorker``), and ``fastapi run`` or ``fastapi dev``.
Under gunicorn or ``fastapi``, uvicorn reads neither ``--ws-max-size`` nor
``UVICORN_WS_MAX_SIZE``, so it always reads up to 16 MiB. Hypercorn,
gunicorn with another worker class (a subclass that sets ``ws_max_size``
included) or one named only in its configuration file, and other servers
are not recognised: nothing is said about them. A value that cannot be
read is treated as unknown too, never as an error: warning code must not
stop an application starting.

The request line and headers have the server's own limits, not checked
against the body limit: h11's 16 KiB under uvicorn, and Granian's
``--http1-buffer-size``, over 500 KB unless set, which "Server limits"
sets to 16 KiB.

A server that listens on a unix socket (``uvicorn --uds``, ``granian
--uds``, ``gunicorn --bind unix:...``, or their ``UVICORN_UDS``,
``GRANIAN_UDS`` and ``GUNICORN_CMD_ARGS``) gives no caller an address:
``unix_socket_warnings`` says so unless ``TRUSTED_PROXIES`` holds
``"unix"``, which believes the proxy on the other end of the socket.

``server_processes`` counts the worker processes the server starts, each
with an application and live connections of its own (``live.W003``).

``manage.py serve`` starts the server from the settings (``SERVE``), in
its own process: its command line names no server's option, and its
limits are read from ``SERVE`` instead. Elsewhere, ``check`` cannot see
which server runs; where no server's variable says, it judges the server
``serve`` would start.

A proxy in front keeps idle connections to the server open for the next
requests. When the server closes one first, the proxy can send a request
on it as it closes, and answers that request 502. ``keep_alive_warnings``
says so when the proxy's idle timeout (``PROXY_IDLE_TIMEOUT``) is not
shorter than the server's keep-alive (``server_keep_alive``).
"""

import math
import os
import shlex
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Final

from domainkit.checks import CheckMessage
from domainkit.conf import settings
from domainkit.conf.global_settings import GRANIAN_MAX_KEEP_ALIVE

UVICORN_WS_MAX_SIZE: Final = 16 * 1024 * 1024
"""uvicorn's default ``--ws-max-size``, in bytes."""
UVICORN_KEEP_ALIVE: Final = 5
"""uvicorn's default ``--timeout-keep-alive``, in seconds; ``serve``'s too."""
GRANIAN_KEEP_ALIVE: Final = 30
"""Granian's default ``--http1-header-read-timeout``, in seconds (30,000 ms)."""
GUNICORN_KEEP_ALIVE: Final = 2
"""gunicorn's default ``--keep-alive``, which its uvicorn worker keeps, in seconds."""
SERVE: Final = "serve"
"""The command that starts the server from the settings (``manage.py serve``)."""
_SERVERS: Final = frozenset({"uvicorn", "granian", "gunicorn", "fastapi"})
"""The programs that are servers, or start one, themselves."""
_OFF: Final = frozenset({"0", "false", "no", "off", "f", "n"})
"""How a ``GRANIAN_*`` switch is turned off."""
UVICORN_WORKERS: Final = frozenset(
    {
        "uvicorn.workers.UvicornWorker",
        "uvicorn.workers.UvicornH11Worker",
        "uvicorn_worker.UvicornWorker",
        "uvicorn_worker.UvicornH11Worker",
    }
)
"""gunicorn worker classes that serve with uvicorn, at its default limits."""


@dataclass(frozen=True, slots=True)
class ServerLimits:
    """The server that runs the application, and its WebSocket limit."""

    server: str
    """``uvicorn`` or ``granian``."""
    websockets: bool
    """Whether the server accepts WebSockets at all."""
    ws_max_size: int | None
    """The largest WebSocket message it reads, in bytes; None: no limit to set."""
    launcher: str | None = None
    """What started the server, if not the server itself: ``gunicorn`` or
    ``fastapi``, neither of which passes uvicorn a WebSocket limit, or
    ``serve``, which passes ``MAX_REQUEST_BODY_SIZE``."""


def server_limits(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> ServerLimits | None:
    """The server and its limits, from ``argv`` and ``environ``; None if unknown.

    They default to this process's. A value that is not a number is treated
    as unknown, and nothing is said about it.
    """
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    program = _program(argv)
    if program == "gunicorn":
        return _gunicorn(argv[1:], environ)
    if program == "fastapi":
        return _fastapi(argv[1:])
    if program == "uvicorn":
        return _uvicorn(argv, environ)
    if program == "granian":
        return _granian(argv, environ)
    if _serves(argv):
        return serve_limits()
    # Not the server's own process (``check``): its variables tell. gunicorn's
    # come first, as its uvicorn worker reads none of uvicorn's.
    if (under_gunicorn := _gunicorn((), environ)) is not None:
        return under_gunicorn
    if _configures(environ, "UVICORN_"):
        return _uvicorn(argv, environ)
    if _configures(environ, "GRANIAN_"):
        return _granian(argv, environ)
    return None


def _uvicorn(argv: Sequence[str], environ: Mapping[str, str]) -> ServerLimits | None:
    """Under uvicorn itself: ``--ws-max-size``, or ``UVICORN_WS_MAX_SIZE``."""
    size = _option(argv, "--ws-max-size") or environ.get("UVICORN_WS_MAX_SIZE")
    ws = _option(argv, "--ws") or environ.get("UVICORN_WS", "auto")
    max_size = _whole(size) if size else UVICORN_WS_MAX_SIZE
    if max_size is None:
        return None
    return ServerLimits("uvicorn", websockets=ws != "none", ws_max_size=max_size)


def _gunicorn(args: Sequence[str], environ: Mapping[str, str]) -> ServerLimits | None:
    """Under gunicorn: uvicorn, if the worker class (``-k``) is uvicorn's.

    The worker builds uvicorn's configuration itself, from gunicorn's
    settings: ``--ws-max-size`` is no gunicorn option, and the worker reads
    no ``UVICORN_*`` variable, so the limit is uvicorn's default. The
    command line wins over ``GUNICORN_CMD_ARGS``, as in gunicorn.
    """
    worker = _worker_class(args)
    if worker is None:
        try:
            worker = _worker_class(shlex.split(environ.get("GUNICORN_CMD_ARGS", "")))
        except ValueError:  # Unbalanced quotes: gunicorn would refuse it too.
            return None
    if worker not in UVICORN_WORKERS:
        return None
    return ServerLimits("uvicorn", True, UVICORN_WS_MAX_SIZE, launcher="gunicorn")


def _fastapi(args: Sequence[str]) -> ServerLimits | None:
    """``fastapi run`` or ``fastapi dev``: uvicorn, with no WebSocket options."""
    command = next((arg for arg in args if not arg.startswith("-")), None)
    if command not in {"run", "dev"}:
        return None
    return ServerLimits("uvicorn", True, UVICORN_WS_MAX_SIZE, launcher="fastapi")


def _granian(argv: Sequence[str], environ: Mapping[str, str]) -> ServerLimits:
    """Granian: WebSockets on unless ``--no-ws`` or ``GRANIAN_WEBSOCKETS``."""
    off = "--no-ws" in argv or environ.get("GRANIAN_WEBSOCKETS", "").lower() in _OFF
    return ServerLimits("granian", websockets=not off, ws_max_size=None)


def serve_server(choice: str) -> str:
    """The server ``serve`` starts for ``SERVE["SERVER"]`` (``choice``).

    ``auto`` is Granian where it is installed, else uvicorn.
    """
    if choice != "auto":
        return choice
    return "granian" if find_spec("granian") is not None else "uvicorn"


def serve_limits() -> ServerLimits:
    """The server ``serve`` starts, and its WebSocket limit, from ``SERVE``."""
    server = serve_server(settings.SERVE.SERVER)
    size = None if server == "granian" else settings.MAX_REQUEST_BODY_SIZE
    return ServerLimits(server, settings.SERVE.WEBSOCKETS, size, launcher=SERVE)


def _serves(argv: Sequence[str]) -> bool:
    """Whether ``argv`` runs ``serve``: ``manage.py serve``, ``domainkit serve``."""
    return len(argv) > 1 and argv[1] == SERVE and _program(argv) not in _SERVERS


def limit_warnings(max_body_size: int, limits: ServerLimits | None) -> list[str]:
    """What the server reads past ``MAX_REQUEST_BODY_SIZE``; empty if nothing."""
    if limits is None or not limits.websockets:
        return []
    if limits.ws_max_size is None:
        off = (
            'set SERVE["WEBSOCKETS"] = False'
            if limits.launcher == SERVE
            else "start it with --no-ws (GRANIAN_WEBSOCKETS=false)"
        )
        return [
            (
                f"{limits.server} reads each WebSocket message whole, at any "
                f"size, before MAX_REQUEST_BODY_SIZE ({max_body_size}) refuses "
                f"it; {off} if the API takes no WebSockets, or limit messages "
                "in the proxy in front."
            )
        ]
    if limits.ws_max_size > max_body_size:
        return [
            (
                f"{limits.server} reads WebSocket messages of up to "
                f"{limits.ws_max_size} bytes before MAX_REQUEST_BODY_SIZE "
                f"({max_body_size}) refuses them; {_how(limits, max_body_size)}"
            )
        ]
    return []


def _how(limits: ServerLimits, size: int) -> str:
    """How to give uvicorn a WebSocket limit of ``size``, as it was started."""
    if limits.launcher == "gunicorn":
        return (
            "gunicorn's uvicorn worker reads neither --ws-max-size nor "
            "UVICORN_WS_MAX_SIZE: use a subclass of it whose CONFIG_KWARGS set "
            f'"ws_max_size": {size}.'
        )
    if limits.launcher == "fastapi":
        return f"fastapi run cannot set it: run uvicorn with --ws-max-size {size}."
    return f"start it with --ws-max-size {size} (UVICORN_WS_MAX_SIZE)."


UNIX_SOCKET_WARNING: Final = (
    "The server listens on a unix socket, where callers have no address: they "
    "all share one rate limit and one hold on refused credentials, and the "
    'audit trail records no address. Add "unix" to TRUSTED_PROXIES, so the '
    "X-Forwarded-For of the proxy on the other end names each caller."
)


def unix_socket_warnings(
    trusted: Collection[object],
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """What to change when the server listens on a unix socket; empty if nothing.

    ``trusted`` is ``TRUSTED_PROXIES``: with ``"unix"`` in it, the proxy
    on the socket's other end is believed, and nothing needs saying.
    """
    if "unix" in trusted or not listens_on_unix_socket(argv, environ):
        return []
    return [UNIX_SOCKET_WARNING]


def listens_on_unix_socket(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether the server listens on a unix socket, by ``argv`` and ``environ``.

    They default to this process's: ``--uds`` (uvicorn, Granian), a
    ``unix:`` bind (gunicorn), or the variables that set them.
    """
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    program = _program(argv)
    if (program in {"uvicorn", "granian"} or _serves(argv)) and _option(argv, "--uds"):
        return True
    if program == "gunicorn" and _binds_unix(argv[1:]):
        return True
    if environ.get("UVICORN_UDS") or environ.get("GRANIAN_UDS"):
        return True
    try:
        return _binds_unix(shlex.split(environ.get("GUNICORN_CMD_ARGS", "")))
    except ValueError:  # Unbalanced quotes: gunicorn would refuse it too.
        return False


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """How long the server keeps an idle connection open, and how to change it."""

    server: str
    """``uvicorn`` or ``Granian``."""
    seconds: float
    """Seconds an idle connection stays open."""
    setting: str
    """What sets it: an option, a variable, or ``SERVE["KEEP_ALIVE"]``."""
    per_second: int = 1
    """The setting's units in a second: 1,000 for milliseconds."""
    most: int | None = None
    """The most seconds the server takes, if it has a most."""


def serve_keep_alive() -> KeepAlive:
    """The keep-alive ``serve`` gives the server: ``SERVE["KEEP_ALIVE"]``.

    Unless set: uvicorn's own, 5 seconds, and Granian's most, 60.
    """
    server = serve_server(settings.SERVE.SERVER)
    granian = server == "granian"
    configured = settings.SERVE.KEEP_ALIVE
    default = GRANIAN_MAX_KEEP_ALIVE if granian else UVICORN_KEEP_ALIVE
    return KeepAlive(
        "Granian" if granian else "uvicorn",
        configured or default,
        'SERVE["KEEP_ALIVE"]',
        most=GRANIAN_MAX_KEEP_ALIVE if granian else None,
    )


def server_keep_alive(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> KeepAlive | None:
    """The server's keep-alive, from ``argv`` and ``environ``; None if unknown.

    They default to this process's. The server is found as
    ``server_limits`` finds it; where nothing names one, it is the server
    ``serve`` starts. Granian with keep-alive off keeps no idle
    connection: None.
    """
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    limits = server_limits(argv, environ)
    if limits is None or limits.launcher == SERVE:
        return serve_keep_alive()
    if limits.launcher == "gunicorn":
        given = (
            _option(argv[1:], "--keep-alive") if _program(argv) == "gunicorn" else None
        )
        if given is None:
            try:
                args = shlex.split(environ.get("GUNICORN_CMD_ARGS", ""))
            except ValueError:  # Unbalanced quotes: gunicorn would refuse it too.
                return None
            given = _option(args, "--keep-alive")
        return _keep_alive(
            "uvicorn", given, GUNICORN_KEEP_ALIVE, "gunicorn's --keep-alive"
        )
    if limits.launcher == "fastapi":
        return KeepAlive(
            "uvicorn",
            UVICORN_KEEP_ALIVE,
            "uvicorn's --timeout-keep-alive, which fastapi run cannot set",
        )
    if limits.server == "uvicorn":
        given = _option(argv, "--timeout-keep-alive") or environ.get(
            "UVICORN_TIMEOUT_KEEP_ALIVE"
        )
        return _keep_alive(
            "uvicorn",
            given,
            UVICORN_KEEP_ALIVE,
            "--timeout-keep-alive (UVICORN_TIMEOUT_KEEP_ALIVE)",
        )
    switch = environ.get("GRANIAN_HTTP1_KEEP_ALIVE", "").lower()
    if "--no-http1-keep-alive" in argv or switch in _OFF:
        return None
    given = _option(argv, "--http1-header-read-timeout") or environ.get(
        "GRANIAN_HTTP1_HEADER_READ_TIMEOUT"
    )
    return _keep_alive(
        "Granian",
        given,
        GRANIAN_KEEP_ALIVE,
        "--http1-header-read-timeout (GRANIAN_HTTP1_HEADER_READ_TIMEOUT), in "
        "milliseconds,",
        per_second=1000,
        most=GRANIAN_MAX_KEEP_ALIVE,
    )


def _keep_alive(
    server: str,
    given: str | None,
    default: int,
    setting: str,
    *,
    per_second: int = 1,
    most: int | None = None,
) -> KeepAlive | None:
    """A keep-alive of ``given`` units (``default`` seconds if not); None if unread."""
    if given is None:
        return KeepAlive(server, default, setting, per_second, most)
    if (units := _whole(given)) is None:
        return None
    return KeepAlive(server, units / per_second, setting, per_second, most)


def keep_alive_warnings(
    proxy_idle_timeout: float | None, keep_alive: KeepAlive | None
) -> list[CheckMessage]:
    """``servers.W003`` when the proxy keeps idle connections as long as the server.

    ``proxy_idle_timeout`` is ``PROXY_IDLE_TIMEOUT``: None, or a server
    whose keep-alive is unknown, says nothing.
    """
    if (
        proxy_idle_timeout is None
        or keep_alive is None
        or keep_alive.seconds > proxy_idle_timeout
    ):
        return []
    server, seconds = keep_alive.server, f"{keep_alive.seconds:g}"
    wanted = math.floor(proxy_idle_timeout * keep_alive.per_second) + 1
    lower = f"lower the proxy's idle timeout below {seconds} seconds"
    if keep_alive.most is not None and wanted > keep_alive.most * keep_alive.per_second:
        most = keep_alive.most
        hint = f"{server} keeps an idle connection {most} seconds at most: {lower}."
    else:
        hint = f"Set {keep_alive.setting} to {wanted} or more, or {lower}."
    return [
        CheckMessage(
            "servers.W003",
            f"The proxy keeps an idle connection to the server open "
            f"{proxy_idle_timeout:g} seconds (PROXY_IDLE_TIMEOUT), and {server} "
            f"closes it after {seconds}: the proxy may send a request on a "
            f"connection as {server} closes it, and answer that request 502.",
            hint,
        )
    ]


def _binds_unix(args: Sequence[str]) -> bool:
    """Whether gunicorn's ``args`` bind a unix socket (``-b unix:PATH``)."""
    for index, argument in enumerate(args):
        if argument in {"-b", "--bind"}:
            bound = args[index + 1] if index + 1 < len(args) else ""
        elif argument.startswith("--bind="):
            bound = argument.partition("=")[2]
        elif argument.startswith("-b"):
            bound = argument[2:]
        else:
            continue
        if bound.startswith("unix:"):
            return True
    return False


def server_processes(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int | None:
    """How many worker processes the server starts; None if no server is found.

    From the server's command line in its own process, else from the
    variables each server reads in place of its option: uvicorn's
    ``--workers``, ``UVICORN_WORKERS``, or ``WEB_CONCURRENCY`` (``fastapi
    run`` passes ``--workers``, and reads ``WEB_CONCURRENCY`` too);
    gunicorn's ``-w``/``--workers``, ``GUNICORN_CMD_ARGS``, or
    ``WEB_CONCURRENCY``; Granian's ``--workers``, or ``GRANIAN_WORKERS``;
    ``serve``'s ``SERVE["WORKERS"]``, else ``WEB_CONCURRENCY``. Elsewhere
    (``check``), the most any of them says. A value that is not a whole
    number counts as unknown, and gunicorn's configuration file is not
    read.
    """
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    program = _program(argv)
    concurrency = environ.get("WEB_CONCURRENCY")
    workers = settings.SERVE.WORKERS
    if _serves(argv):
        return workers if workers is not None else _count(concurrency)
    try:
        gunicorn = _gunicorn_option(
            shlex.split(environ.get("GUNICORN_CMD_ARGS", "")), "-w", "--workers"
        )
    except ValueError:  # Unbalanced quotes: gunicorn would refuse it too.
        gunicorn = None
    if program == "uvicorn":
        given = _option(argv, "--workers") or environ.get("UVICORN_WORKERS")
        return _count(given or concurrency)
    if program == "fastapi":
        return _count(_option(argv, "--workers") or concurrency)
    if program == "gunicorn":
        given = _gunicorn_option(argv[1:], "-w", "--workers") or gunicorn
        return _count(given or concurrency)
    if program == "granian":
        return _count(_option(argv, "--workers") or environ.get("GRANIAN_WORKERS"))
    said = [
        count
        for value in (
            environ.get("UVICORN_WORKERS"),
            environ.get("GRANIAN_WORKERS"),
            gunicorn,
            concurrency,
        )
        if value and (count := _whole(value)) is not None
    ]
    if workers is not None:
        said.append(workers)
    return max(said, default=None)


def _count(text: str | None) -> int | None:
    """A server's number of processes: 1 unless ``text`` says another."""
    return 1 if not text else _whole(text)


def _program(argv: Sequence[str]) -> str:
    """The program ``argv`` runs: ``uvicorn`` for ``python -m uvicorn`` too."""
    if not argv:
        return ""
    path = Path(argv[0])
    return path.parent.name if path.name == "__main__.py" else path.name


def _whole(text: str) -> int | None:
    """``text`` as a whole number (bytes, seconds, processes), or None if it is not one.

    ``isdigit`` takes superscripts, which ``int`` refuses, so ``isdecimal``
    decides; ``int`` also refuses more digits than Python's limit (4,300).
    """
    if not text.isdecimal():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _configures(environ: Mapping[str, str], prefix: str) -> bool:
    """Whether variables with ``prefix`` configure a server here."""
    return any(name.startswith(prefix) for name in environ)


def _option(argv: Sequence[str], name: str) -> str | None:
    """The value of ``--name value`` or ``--name=value`` in ``argv``, if given."""
    for index, argument in enumerate(argv):
        if argument == name and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith(f"{name}="):
            return argument.partition("=")[2]
    return None


def _worker_class(args: Sequence[str]) -> str | None:
    """The worker class gunicorn's ``args`` name, the last if several do."""
    return _gunicorn_option(args, "-k", "--worker-class")


def _gunicorn_option(args: Sequence[str], short: str, long: str) -> str | None:
    """The value gunicorn's ``args`` give an option, the last if several do.

    ``-k NAME``, ``-kNAME``, ``--worker-class NAME`` and
    ``--worker-class=NAME``, for ``short`` ``-k`` and ``long``
    ``--worker-class``.
    """
    found = None
    for index, argument in enumerate(args):
        if argument in {short, long}:
            if index + 1 < len(args):
                found = args[index + 1]
        elif argument.startswith(f"{long}="):
            found = argument.partition("=")[2]
        elif argument.startswith(short):
            found = argument[len(short) :]
    return found
