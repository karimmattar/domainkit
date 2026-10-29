"""``live_server``: the application served on a port of the test process.

For browsers and other processes, which need a real socket: ``LiveServer``
runs uvicorn in a thread of the test process, on ``127.0.0.1`` and a port
the system picks, with the application's lifespan, and stops it when the
block ends. The ``live_server`` fixture serves the project's application
on the test databases (``db``), with ``127.0.0.1`` added to
``ALLOWED_HOSTS``, for one test.

The server's thread is a daemon, so an interrupted run (Ctrl-C) never
waits for it, and stopping cancels requests still open after
``STOP_SECONDS``. It starts with the context of the code that starts it, as
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
STOP_SECONDS: Final = 5
"""How long stopping waits for requests still open (a stream, a WebSocket)
before uvicorn cancels them."""


class LiveServer:
    """An ASGI application served by uvicorn in a thread, on a free port.

    ``with LiveServer(app) as server:`` starts it, lifespan included, and
    ``server.url`` (``http://127.0.0.1:<port>``) is where it answers. The
    block's end stops it, shutting the lifespan down. The application's
    ``ALLOWED_HOSTS`` must hold ``127.0.0.1``, as ``live_server``'s does.
    """

    __test__ = False

    def __init__(
        self, app: ASGIApp, *, host: str = LIVE_HOST, stop_seconds: int = STOP_SECONDS
    ) -> None:
        """Serve ``app`` on ``host``, on a port the system picks when entered.

        Stopping waits ``stop_seconds`` for requests still open, then
        cancels them, so an open stream never holds the test's teardown.
        """
        self.app = app
        self.host = host
        self.stop_seconds = stop_seconds
        self.port = 0
        """The port it listens on, once entered."""
        self._running: tuple[uvicorn.Server, threading.Thread, socket.socket] | None
        self._running = None

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
        self.port = listening.getsockname()[1]
        config = uvicorn.Config(
            self._application,
            factory=True,
            interface="asgi3",
            lifespan="on",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=self.stop_seconds,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=copy_context().run,
            args=(_serve, server, listening),
            name=f"live-server-{self.port}",
            daemon=True,
        )
        self._running = (server, thread, listening)
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

    def _application(self) -> ASGIApp:
        """The application, for uvicorn: named as a factory, and as ASGI 3.

        So uvicorn guesses neither: it calls an application with no
        arguments to find out whether it is a factory, which an application
        whose ``__call__`` takes any arguments answers with a coroutine.
        Proxy headers are the application's to read (``TRUSTED_PROXIES``),
        as ``runserver`` leaves them.
        """
        return self.app

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop serving, shut the application down, and free the port.

        Once stopped, or before it started, there is nothing to do.
        """
        if self._running is None:
            return
        server, thread, listening = self._running
        self._running = None
        server.should_exit = True
        thread.join()
        listening.close()


def _serve(server: uvicorn.Server, listening: socket.socket) -> None:
    """Serve until told to stop; a failed startup ends the thread quietly.

    uvicorn exits (``SystemExit``) when the application's startup fails,
    having logged why; entering raises instead.
    """
    with suppress(SystemExit):
        server.run(sockets=[listening])
