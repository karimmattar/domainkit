"""``live_server``: the application served on a port of the test process.

For browsers and other processes, which need a real socket: ``LiveServer``
runs uvicorn in a thread of the test process, on ``127.0.0.1`` and a port
the system picks, with the application's lifespan, and stops it when the
block ends. The ``live_server`` fixture serves the project's application
on the test databases (``db``), with ``127.0.0.1`` added to
``ALLOWED_HOSTS``, for one test.

The server's thread starts with the context of the code that starts it, as
the test clients carry the caller's: a clock frozen before the server
starts is the application's clock too, and moving it moves both.
"""

import socket
import threading
from contextlib import suppress
from contextvars import copy_context
from types import TracebackType
from typing import Final, Self

import uvicorn
from starlette.types import ASGIApp

LIVE_HOST: Final = "127.0.0.1"
"""Where the live server listens: this machine only."""
WAIT_SECONDS: Final = 0.005
"""How often entering checks whether the startup is done."""


class LiveServer:
    """An ASGI application served by uvicorn in a thread, on a free port.

    ``with LiveServer(app) as server:`` starts it, lifespan included, and
    ``server.url`` (``http://127.0.0.1:<port>``) is where it answers. The
    block's end stops it, shutting the lifespan down. The application's
    ``ALLOWED_HOSTS`` must hold ``127.0.0.1``, as ``live_server``'s does.
    """

    __test__ = False

    def __init__(self, app: ASGIApp, *, host: str = LIVE_HOST) -> None:
        """Serve ``app`` on ``host``, on a port the system picks when entered."""
        self.app = app
        self.host = host
        self.port = 0
        """The port it listens on, once entered."""
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """The server's address: ``http://127.0.0.1:<port>``."""
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> Self:
        """Bind a free port and start serving; wait until the startup is done.

        Raises:
            RuntimeError: The application's startup failed; uvicorn logged why.
        """
        listening = socket.socket()
        listening.bind((self.host, 0))
        self._socket = listening
        self.port = listening.getsockname()[1]
        config = uvicorn.Config(
            self.app, lifespan="on", log_config=None, access_log=False
        )
        self._server = server = uvicorn.Server(config)
        self._thread = thread = threading.Thread(
            target=copy_context().run,
            args=(_serve, server, listening),
            name=f"live-server-{self.port}",
        )
        thread.start()
        while not server.started and thread.is_alive():
            thread.join(WAIT_SECONDS)
        if not server.started:
            self.__exit__(None, None, None)
            raise RuntimeError(
                "The live server did not start: its application's startup "
                "failed; uvicorn's log says why."
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop serving, shut the application down, and free the port."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join()
        if self._socket is not None:
            self._socket.close()
        self._server = self._thread = self._socket = None


def _serve(server: uvicorn.Server, listening: socket.socket) -> None:
    """Serve until told to stop; a failed startup ends the thread quietly.

    uvicorn exits (``SystemExit``) when the application's startup fails,
    having logged why; entering raises instead.
    """
    with suppress(SystemExit):
        server.run(sockets=[listening])
