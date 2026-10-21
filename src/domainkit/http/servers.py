"""What the ASGI server reads before the application sees it.

``MAX_REQUEST_BODY_SIZE`` closes a WebSocket message that is too large, but
only once the server has read it whole: uvicorn reads up to 16 MiB unless
``--ws-max-size`` says less, and Granian has no such limit. ``server_limits``
finds which server runs, and its WebSocket limit, from its command line
(the server's own process) or its environment variables (``UVICORN_*``,
``GRANIAN_*``, which both read); ``limit_warnings`` says what to change.
``check`` lists them in staging and production, from the environment, and
a deployed application logs them when it starts. A value that cannot be
read is treated as unknown, never as an error: warning code must not stop
an application starting.

The request line and headers have the server's own default limits (h11's
16 KiB under uvicorn), already below any body limit, so they are not
checked against it.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

UVICORN_WS_MAX_SIZE: Final = 16 * 1024 * 1024
"""uvicorn's default ``--ws-max-size``, in bytes."""
_OFF: Final = frozenset({"0", "false", "no", "off", "f", "n"})
"""How a ``GRANIAN_*`` switch is turned off."""


@dataclass(frozen=True, slots=True)
class ServerLimits:
    """The server that runs the application, and its WebSocket limit."""

    server: str
    """``uvicorn`` or ``granian``."""
    websockets: bool
    """Whether the server accepts WebSockets at all."""
    ws_max_size: int | None
    """The largest WebSocket message it reads, in bytes; None: no limit to set."""


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
    if program == "uvicorn" or _configures(environ, "UVICORN_"):
        size = _option(argv, "--ws-max-size") or environ.get("UVICORN_WS_MAX_SIZE")
        ws = _option(argv, "--ws") or environ.get("UVICORN_WS", "auto")
        max_size = _bytes(size) if size else UVICORN_WS_MAX_SIZE
        if max_size is None:
            return None
        return ServerLimits("uvicorn", websockets=ws != "none", ws_max_size=max_size)
    if program == "granian" or _configures(environ, "GRANIAN_"):
        off = "--no-ws" in argv or (
            environ.get("GRANIAN_WEBSOCKETS", "").lower() in _OFF
        )
        return ServerLimits("granian", websockets=not off, ws_max_size=None)
    return None


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
                f"({max_body_size}) refuses them; start it with --ws-max-size "
                f"{max_body_size} (UVICORN_WS_MAX_SIZE)."
            )
        ]
    return []


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
