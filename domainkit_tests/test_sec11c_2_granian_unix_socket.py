"""SEC-11c-2: under ``granian --uds``, ``TRUSTED_PROXIES = ["unix"]`` trusts nobody.

WEB-02 (0.9.1) made ``"unix"`` in ``TRUSTED_PROXIES`` trust the proxy on the
other end of a unix socket, and the reference names ``granian --uds`` among
the servers it is for. ``ProxyHeadersMiddleware`` recognises that peer only
by a missing ``client`` (uvicorn's ``None``). Granian 2.8.4, the floor of
the ``granian`` extra, reports a unix socket's peer as ``("", "0")``
instead: an empty host, which is no address and no trusted network. So
behind Granian on a unix socket the proxy is never trusted, whatever the
settings say:

- every caller is the client ``""``, one rate-limit key (``ip:``) and one
  login throttle per address for all of them: one caller's refused
  passwords hold back everyone's logins, and one caller spends everyone's
  ``RATE_LIMIT`` (WEB-02's attack, back);
- the proxy's ``X-Request-ID`` is dropped (WEB-13 fails safe here, but the
  ids no longer match the proxy's logs);
- ``servers.W002`` stays quiet, since ``"unix"`` is listed.

The first test gives the middleware Granian's scope, as Granian 2.8.4 sends
it on a unix socket (seen by serving an ASGI app that prints ``client``);
the second serves the framework's application under Granian itself.
"""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from starlette.types import Message, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.conf.env import project_prefix
from domainkit.context import current_request
from domainkit.http.middleware import ProxyHeadersMiddleware, RequestContextMiddleware
from domainkit.http.ratelimit import address_key
from domainkit_tests.test_servers import UnixHTTPConnection

ROOT: Final = Path(__file__).parents[1]
SOCKET: Final = "/run/api.sock"
GRANIAN_UNIX_PEER: Final = ("", "0")
"""``scope["client"]`` from Granian 2.8.4 for a connection on a unix socket."""
UVICORN_UNIX_PEER: Final = None
"""``scope["client"]`` from uvicorn for the same connection."""
ALICE: Final = "203.0.113.9"
MALLORY: Final = "198.51.100.4"
PROXYS_ID: Final = "nginx-4f1c2a"


def serve(client: object, forwarded_for: str) -> tuple[object, str, str]:
    """The client, request id and rate-limit key one proxied request is served with."""
    seen: list[tuple[object, str, str]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = current_request()
        assert request is not None
        seen.append((scope["client"], request.request_id, address_key(scope)))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/ok",
        "headers": [
            (b"host", b"testserver"),
            (b"x-forwarded-for", forwarded_for.encode()),  # nginx's $remote_addr.
            (b"x-request-id", PROXYS_ID.encode()),  # nginx's $request_id.
        ],
        "client": client,
        "server": (SOCKET, "0"),
    }
    with override_settings(TRUSTED_PROXIES=["unix"]):
        stack = ProxyHeadersMiddleware(RequestContextMiddleware(app))
        asyncio.run(stack(scope, receive, send))
    (found,) = seen
    return found


def test_uvicorns_unix_socket_peer_is_trusted() -> None:
    """The control: uvicorn's peerless connection is the trusted proxy."""
    client, request_id, key = serve(UVICORN_UNIX_PEER, ALICE)

    assert client == (ALICE, 0)
    assert request_id == PROXYS_ID
    assert key == f"ip:{ALICE}"


def test_granians_unix_socket_peer_is_trusted_too() -> None:
    """Granian's peer of a unix socket must be the trusted proxy as well.

    Otherwise Alice and Mallory, two callers behind the proxy, are one
    caller, ``""``, to every rate limit and login throttle.
    """
    alice = serve(GRANIAN_UNIX_PEER, ALICE)
    mallory = serve(GRANIAN_UNIX_PEER, MALLORY)

    assert alice[2] != mallory[2], (
        f"behind granian --uds with TRUSTED_PROXIES = ['unix'], two callers "
        f"share the rate-limit key {alice[2]!r}"
    )
    assert alice[0] == (ALICE, 0)
    assert alice[1] == PROXYS_ID


SETTINGS: Final = """\
from domainkit.conf import global_settings as _defaults
from domainkit_tests.settings import *  # noqa: F403

TRUSTED_PROXIES = ["unix"]  # nginx: proxy_pass http://unix:/run/api.sock;
RATE_LIMIT = "2/minute"
MIDDLEWARE = (
    *_defaults.DEFAULT_MIDDLEWARE,
    "domainkit.http.ratelimit.RateLimitMiddleware",
)
"""


@pytest.fixture
def granian_behind_a_proxy() -> Iterator[Path]:
    """Granian serving the application on a unix socket, ``"unix"`` trusted."""
    pytest.importorskip("granian")
    directory = Path(tempfile.mkdtemp(prefix="dk-"))
    path = directory / "granian.sock"
    (directory / "sec11c2_settings.py").write_text(SETTINGS)
    prefix = project_prefix()
    environment = {
        **os.environ,
        f"{prefix}SETTINGS_MODULE": "sec11c2_settings",
        f"{prefix}ENV_FILE": "",
        "PYTHONPATH": os.pathsep.join([str(directory), str(ROOT), *sys.path]),
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
                yield path
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(30)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()
                    raise
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def proxied_get(path: Path, client: str) -> tuple[int, str | None, str | None]:
    """GET /ok as nginx sends it for ``client``: status, request id, quota left.

    Granian's worker listens a moment after it says it started; until then
    the socket refuses connections, which is retried (as ``test_servers``).
    """
    deadline = time.monotonic() + 30
    while True:
        connection = UnixHTTPConnection(path)
        try:
            connection.request(
                "GET",
                "/ok",
                headers={
                    "Host": "testserver",
                    "X-Forwarded-For": client,
                    "X-Request-ID": PROXYS_ID,
                },
            )
            response = connection.getresponse()
            response.read()
            return (
                response.status,
                response.getheader("X-Request-ID"),
                response.getheader("RateLimit-Remaining"),
            )
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() > deadline:  # pragma: no cover - a dead worker.
                raise
            time.sleep(0.05)
        finally:
            connection.close()


def test_callers_behind_granian_on_a_unix_socket_are_told_apart(
    granian_behind_a_proxy: Path,
) -> None:
    """Under Granian itself: Mallory spends Alice's quota, and the id is dropped."""
    path = granian_behind_a_proxy

    first = proxied_get(path, MALLORY)
    second = proxied_get(path, MALLORY)
    alice = proxied_get(path, ALICE)

    assert (first[0], second[0]) == (200, 200)
    assert alice[0] == 200, (
        f"Alice, a caller of her own, was counted with Mallory: {alice!r}"
    )
    assert alice == (200, PROXYS_ID, "1")
