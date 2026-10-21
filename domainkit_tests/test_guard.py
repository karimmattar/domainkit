"""The guard's own lookups and TLS: a few threads, and no CA from the environment.

Lookups run on threads of the guard's own, bounded in all and per host, so
names whose DNS never answers cannot hold up the event loop's default
executor; failures are remembered for a few seconds. Calls to URLs someone
else chose trust the system's CAs or a bundle, never ``SSL_CERT_FILE``.
"""

import asyncio
import ipaddress
import socket
import ssl
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import truststore

from domainkit import lifecycle
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import guard
from domainkit.http.client import HttpClient
from domainkit.http.guard import (
    FAILED_LOOKUP_SECONDS,
    LOOKUPS,
    GuardResolver,
    system_trust,
    tls_context,
)
from domainkit.lifecycle import Phase
from domainkit_tests.deadline_routes import Counter
from domainkit_tests.test_webhooks import certificates  # noqa: F401 - fixture.

pytestmark = pytest.mark.anyio

LOCAL = (ipaddress.ip_network("127.0.0.0/8"),)


class Lookups:
    """``socket.getaddrinfo`` for ``*.test`` names: each waits to be let go.

    The guard's threads call it; ``started`` counts them on the loop's side.
    """

    def __init__(self, *, blocking: bool = True) -> None:
        """Hold every lookup until ``release`` is set, if ``blocking``."""
        self.loop = asyncio.get_running_loop()
        self.started = Counter()
        self.release = threading.Event()
        if not blocking:
            self.release.set()
        self.threads: list[str] = []
        self.hosts: list[str] = []
        self.answers: dict[str, list[str] | OSError] = {}
        self.real = socket.getaddrinfo

    def __call__(self, host: str, port: int, **options: Any) -> list[Any]:  # noqa: ANN401
        """Answer from ``answers`` (default: one documentation address)."""
        if not host.endswith(".test"):
            return self.real(host, port, **options)  # pragma: no cover - others.
        self.threads.append(threading.current_thread().name)
        self.hosts.append(host)
        self.loop.call_soon_threadsafe(self.started.add)
        assert self.release.wait(30)
        answer = self.answers.get(host, ["192.0.2.1"])
        if isinstance(answer, OSError):
            raise answer
        family = socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (a, port)) for a in answer]


@pytest.fixture
async def lookups(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Lookups]:
    """The system resolver, replaced; held lookups are let go at the end."""
    fake = Lookups()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    yield fake
    fake.release.set()


@pytest.fixture
def registered() -> Iterator[None]:
    """Forget the closers a test's resolvers register."""
    saved = dict(lifecycle._registered[Phase.RESOURCES])
    yield
    lifecycle._registered[Phase.RESOURCES].clear()
    lifecycle._registered[Phase.RESOURCES].update(saved)


async def drained(resolver: GuardResolver) -> None:
    """Wait until a one-thread resolver's thread has finished its lookup."""
    assert resolver._executor is not None
    await asyncio.wrap_future(resolver._executor.submit(lambda: None))


def refused_at_once(resolver: GuardResolver, host: str) -> None:
    """Check that a lookup of ``host`` is refused before any thread takes it."""
    with pytest.raises(
        socket.gaierror, match=r"^\[Errno -3\] Too many host name lookups"
    ):
        resolver._start(host, 443)  # What lookup() hands a thread, or refuses.


@pytest.mark.usefixtures("registered")
async def test_lookups_run_on_the_guards_threads(lookups: Lookups) -> None:
    """Never on the loop's default executor, which other work needs."""
    lookups.release.set()
    lookups.answers["service.test"] = ["192.0.2.7", "192.0.2.8", "192.0.2.7"]

    found = await guard.lookup("service.test", 443)

    assert found == [
        ipaddress.ip_address("192.0.2.7"),
        ipaddress.ip_address("192.0.2.8"),
    ]
    assert lookups.threads[0].startswith("domainkit-lookup")
    await guard.resolver.close()


@pytest.mark.usefixtures("registered")
async def test_lookups_past_the_bounds_fail_at_once(lookups: Lookups) -> None:
    """At most ``threads`` at once, ``per_host`` per host; the others fail fast.

    The loop's default executor stays free while they wait.
    """
    resolver = GuardResolver(threads=2, per_host=1)
    first = asyncio.create_task(resolver.lookup("a.test", 443))
    await lookups.started.reach(1)
    refused_at_once(resolver, "a.test")  # One of a host at once.
    second = asyncio.create_task(resolver.lookup("b.test", 443))
    await lookups.started.reach(2)
    refused_at_once(resolver, "c.test")  # Two in all.
    assert resolver.running == 2
    assert await asyncio.to_thread(lambda: "free") == "free"

    lookups.release.set()
    assert await first == [ipaddress.ip_address("192.0.2.1")]
    assert await second == [ipaddress.ip_address("192.0.2.1")]
    assert resolver.running == 0
    assert await resolver.lookup("c.test", 443) == [ipaddress.ip_address("192.0.2.1")]
    assert lookups.hosts == ["a.test", "b.test", "c.test"]
    await resolver.close()


@pytest.mark.usefixtures("registered")
async def test_a_lookup_counts_until_its_thread_ends(lookups: Lookups) -> None:
    """Its caller gave up, the thread still waits for the system's resolver."""
    resolver = GuardResolver(threads=1)
    gone = asyncio.create_task(resolver.lookup("slow.test", 443))
    await lookups.started.reach(1)
    gone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gone
    assert resolver.running == 1
    refused_at_once(resolver, "other.test")

    lookups.release.set()
    await drained(resolver)
    assert resolver.running == 0
    assert await resolver.lookup("other.test", 443)
    await resolver.close()


@pytest.mark.usefixtures("registered")
async def test_a_failed_lookup_is_remembered_for_a_while(lookups: Lookups) -> None:
    """Its error comes back at once, without a thread, until the time is up."""
    lookups.release.set()
    lookups.answers["gone.test"] = socket.gaierror(
        socket.EAI_NONAME, "Name or service not known"
    )
    resolver = GuardResolver()
    with freeze_time("2026-01-01") as clock:
        clock.tick(3600)  # Far from the real clock: the failure is timed on this one.
        for _ in range(2):
            with pytest.raises(socket.gaierror) as failed:
                await resolver.lookup("gone.test", 443)
            assert (failed.value.errno, failed.value.strerror) == (
                socket.EAI_NONAME,
                "Name or service not known",
            )
        assert lookups.hosts == ["gone.test"]
        lookups.answers["odd.test"] = OSError("No route to the resolver")
        for _ in range(2):
            with pytest.raises(OSError, match="No route to the resolver"):
                await resolver.lookup("odd.test", 443)
        assert lookups.hosts == ["gone.test", "odd.test"]
        assert await resolver.lookup("other.test", 443)
        clock.tick(FAILED_LOOKUP_SECONDS - 0.5)
        with pytest.raises(socket.gaierror):
            await resolver.lookup("gone.test", 443)
        assert lookups.hosts == ["gone.test", "odd.test", "other.test"]
        clock.tick(0.5)
        lookups.answers["gone.test"] = ["192.0.2.9"]
        assert await resolver.lookup("gone.test", 443) == [
            ipaddress.ip_address("192.0.2.9")
        ]
    assert list(resolver._failed) == ["odd.test"]
    await resolver.close()


@pytest.mark.usefixtures("registered")
async def test_remembered_failures_are_bounded(
    lookups: Lookups, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oldest is forgotten first, and its host is looked up again."""
    monkeypatch.setattr(guard, "FAILED_LOOKUPS", 2)
    lookups.release.set()
    resolver = GuardResolver()
    for host in ("a.test", "b.test", "c.test", "a.test", "c.test"):
        lookups.answers[host] = socket.gaierror(socket.EAI_AGAIN, "Try again")
        with pytest.raises(socket.gaierror, match="Try again"):
            await resolver.lookup(host, 443)

    assert lookups.hosts == ["a.test", "b.test", "c.test", "a.test"]
    assert list(resolver._failed) == ["c.test", "a.test"]
    await resolver.close()


async def test_a_cancelled_lookup_is_neither_counted_nor_remembered() -> None:
    """A lookup the threads never started (cancelled at shutdown) frees its place."""
    resolver = GuardResolver()
    resolver._running["queued.test"] = 2
    cancelled: Future[list[Any]] = Future()
    assert cancelled.cancel()
    resolver._finished("queued.test", cancelled)
    resolver._finished("queued.test", cancelled)

    assert (resolver._running, resolver._failed) == ({}, {})


async def test_the_threads_stop_at_shutdown_and_start_again(
    lookups: Lookups,
) -> None:
    """The first lookup registers the resolver; closing it lets a later one start."""
    lookups.release.set()
    resolver = GuardResolver()
    saved = dict(lifecycle._registered[Phase.RESOURCES])
    try:
        await resolver.close()  # Nothing started: nothing to stop.
        await resolver.lookup("a.test", 443)
        first = resolver._executor
        assert first is not None
        assert lifecycle._registered[Phase.RESOURCES][LOOKUPS] == resolver.close

        await lifecycle.close_resources()
        assert resolver._executor is None
        assert first._shutdown

        await resolver.lookup("a.test", 443)
        assert resolver._executor not in {None, first}
        await resolver.close()
    finally:
        lifecycle._registered[Phase.RESOURCES].clear()
        lifecycle._registered[Phase.RESOURCES].update(saved)


# TLS --------------------------------------------------------------------------


@asynccontextmanager
async def serving_tls(chain: Path) -> AsyncIterator[int]:
    """Serve HTTPS on 127.0.0.1 with ``chain``, answering 200; yield the port."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass  # The client refused the certificate.
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def test_a_guarded_client_trusts_no_ca_the_environment_names(
    certificates: dict[str, Path],  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSL_CERT_FILE and SSL_CERT_DIR are ignored, as proxies are; CA_BUNDLE is not."""

    async def local(host: str, port: int) -> list[guard.Address]:
        return [ipaddress.ip_address("127.0.0.1")]

    monkeypatch.setattr(guard, "lookup", local)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificates["ca"]))
    monkeypatch.setenv("SSL_CERT_DIR", str(certificates["ca"].parent))
    async with serving_tls(certificates["hooks.example.test"]) as port:
        url = f"https://hooks.example.test:{port}/"
        untrusting = HttpClient(guard=True, allowed_networks=LOCAL, retries=0)
        with pytest.raises(httpx2.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            await untrusting.get(url)
        await untrusting.aclose()
        with override_settings(HTTP_CLIENT={"CA_BUNDLE": str(certificates["ca"])}):
            bundled = HttpClient(guard=True, allowed_networks=LOCAL, retries=0)
            assert (await bundled.get(url)).text == "ok"
            await bundled.aclose()


def test_tls_contexts_for_guarded_calls_are_built_once_and_strict(
    certificates: dict[str, Path],  # noqa: F811 - the fixture.
) -> None:
    """One per CA bundle; TLS 1.2 at least; certificates and names checked."""
    system = tls_context(None)
    bundle = tls_context(certificates["ca"])

    assert tls_context(None) is system
    assert tls_context(certificates["ca"]) is bundle
    assert bundle is not system
    for context in (system, bundle):
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED


def fake_paths(cafile: str, capath: str) -> ssl.DefaultVerifyPaths:
    """OpenSSL's default paths, as if it were built to look at these."""
    return ssl.DefaultVerifyPaths(
        cafile="",
        capath="",
        openssl_cafile_env="SSL_CERT_FILE",
        openssl_cafile=cafile,
        openssl_capath_env="SSL_CERT_DIR",
        openssl_capath=capath,
    )


def subjects(context: ssl.SSLContext) -> list[str]:
    """The common names of the CAs ``context`` loaded from a file."""
    names: list[str] = []
    for cert in context.get_ca_certs():
        subject: Any = cert["subject"]
        names += [value for rdn in subject for key, value in rdn if key == "commonName"]
    return names


def test_system_trust_loads_where_openssl_looks_or_the_system_bundle(
    certificates: dict[str, Path],  # noqa: F811 - the fixture.
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OpenSSL's built-in file, else the first system bundle found; never the env."""
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    ca = str(certificates["ca"])
    missing = str(tmp_path / "missing.pem")
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: fake_paths(ca, missing)
    )
    assert subjects(system_trust("linux")) == ["Webhook test CA"]

    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: fake_paths(missing, missing)
    )
    monkeypatch.setattr(guard, "SYSTEM_BUNDLES", (missing, ca))
    assert subjects(system_trust("linux")) == ["Webhook test CA"]

    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: fake_paths(missing, str(tmp_path))
    )
    monkeypatch.setattr(guard, "SYSTEM_BUNDLES", (missing,))
    assert subjects(system_trust("linux")) == []  # A directory, read as needed.

    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: fake_paths(missing, missing)
    )
    with pytest.raises(ImproperlyConfigured, match="No CA certificates were found"):
        system_trust("linux")


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_the_operating_system_keeps_the_trust_store_elsewhere(platform: str) -> None:
    """On macOS and Windows, truststore asks it, and reads no variable."""
    assert isinstance(system_trust(platform), truststore.SSLContext)
