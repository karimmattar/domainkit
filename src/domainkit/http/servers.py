"""What the ASGI server reads before the application sees it.

``MAX_REQUEST_BODY_SIZE`` closes a WebSocket message that is too large, but
only once the server has read it whole: uvicorn reads up to 16 MiB unless
``--ws-max-size`` says less, and Granian has no such limit. ``server_limits``
finds which server runs, and its WebSocket limit, from its command line
(the server's own process) or its environment variables (``UVICORN_*``,
``GRANIAN_*``, which both read, and ``GUNICORN_CMD_ARGS``);
``limit_warnings`` says what to change. ``check`` lists them in staging and
production, from the environment, and a deployed application logs them
when it starts.

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

The request line and headers have the server's own default limits (h11's
16 KiB under uvicorn), already below any body limit, so they are not
checked against it.
"""

import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

UVICORN_WS_MAX_SIZE: Final = 16 * 1024 * 1024
"""uvicorn's default ``--ws-max-size``, in bytes."""
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
    ``fastapi``. Neither passes uvicorn a WebSocket limit."""


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
    max_size = _bytes(size) if size else UVICORN_WS_MAX_SIZE
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


def limit_warnings(max_body_size: int, limits: ServerLimits | None) -> list[str]:
    """What the server reads past ``MAX_REQUEST_BODY_SIZE``; empty if nothing."""
    if limits is None or not limits.websockets:
        return []
    if limits.ws_max_size is None:
        return [
            (
                f"{limits.server} reads each WebSocket message whole, at any "
                f"size, before MAX_REQUEST_BODY_SIZE ({max_body_size}) refuses "
                "it; start it with --no-ws (GRANIAN_WEBSOCKETS=false) if the "
                "API takes no WebSockets, or limit messages in the proxy in front."
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


def _program(argv: Sequence[str]) -> str:
    """The program ``argv`` runs: ``uvicorn`` for ``python -m uvicorn`` too."""
    if not argv:
        return ""
    path = Path(argv[0])
    return path.parent.name if path.name == "__main__.py" else path.name


def _bytes(text: str) -> int | None:
    """``text`` as a whole number of bytes, or None if it is not one.

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
    """The worker class gunicorn's ``args`` name, the last if several do.

    ``-k NAME``, ``-kNAME``, ``--worker-class NAME`` and
    ``--worker-class=NAME``.
    """
    found = None
    for index, argument in enumerate(args):
        if argument in {"-k", "--worker-class"}:
            if index + 1 < len(args):
                found = args[index + 1]
        elif argument.startswith("--worker-class="):
            found = argument.partition("=")[2]
        elif argument.startswith("-k"):
            found = argument[2:]
    return found
