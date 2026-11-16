"""HTTP over a server's unix socket, for tests that start a real server.

A unix socket in a new directory belongs to its test alone. A TCP port
found free and closed again can be taken by another process (another
pytest-xdist worker's server) before the server binds it, and a request
would then reach that process.
"""

import http.client
import socket
import time
from pathlib import Path


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


def get(path: Path, target: str) -> tuple[int, bytes]:
    """GET ``target`` through the socket at ``path``, once the worker accepts.

    A server's worker listens a moment after it says it started; until then
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
