"""Which URLs a server may call: the guard against server-side request forgery.

A URL chosen by someone else (a webhook subscription, a callback, a link to
fetch) is checked before it is called:

- ``https`` only (``http`` too where allowed), with a host, and without a
  user name, password, or fragment;
- not ``localhost`` (or ``*.localhost``) or a cloud metadata host name;
- every address the host resolves to must be public: no private, loopback,
  link-local, shared (CGNAT), reserved, multicast, or unspecified address,
  in IPv4 or IPv6. An IPv4 address inside an IPv6 one (``::ffff:10.0.0.1``,
  NAT64's ``64:ff9b::/96``) is judged as the IPv4 address it holds.
  ``allowed`` networks let named private networks through; cloud metadata
  addresses never are.

``checked_url`` checks what the URL shows; ``checked_host`` (and
``checked_address`` for a URL) resolves the host and checks every address.
Callers connect to the address that was checked, never to a second lookup,
so a host that resolves differently a moment later (DNS rebinding) gains
nothing. ``domainkit.contrib.webhooks`` checks subscriptions with it, and
``domainkit.http.client`` the calls of a client made with ``guard=True``.

Host names are looked up on threads of the guard's own (``GuardResolver``),
at most ``LOOKUP_THREADS`` at once and ``LOOKUPS_PER_HOST`` of one host,
never on the event loop's default executor: a name whose DNS never answers
holds its thread until the system's resolver gives up, long after the
caller stopped waiting, and would otherwise hold up ``asyncio.to_thread``
and new database connections. A lookup past those bounds fails at once, as
does one of a host whose lookup failed in the last
``FAILED_LOOKUP_SECONDS``.

TLS for those calls (``tls_context``) trusts the system's CAs, or a bundle
of the project's, and nothing the environment adds: ``SSL_CERT_FILE`` and
``SSL_CERT_DIR`` are ignored, as proxy settings are.

Error messages never quote the URL, which may hold a secret token.
"""

import asyncio
import contextvars
import ipaddress
import socket
import ssl
import sys
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any, Final

import httpx2

from domainkit import clock
from domainkit.ddd import BusinessRuleViolation
from domainkit.exceptions import ImproperlyConfigured

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type Resolver = Callable[[str, int], Awaitable[list[Address]]]

MAX_URL_LENGTH: Final = 2048
METADATA_ADDRESSES: Final = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "169.254.169.254",  # AWS, Azure, Google Cloud, OpenStack, DigitalOcean
        "169.254.170.2",  # AWS ECS task metadata
        "169.254.170.23",  # AWS EKS Pod Identity
        "169.254.169.253",  # AWS DNS resolver
        "fd00:ec2::254",  # AWS over IPv6
        "fd00:ec2::23",  # AWS EKS Pod Identity over IPv6
        "fd00:ec2::253",  # AWS DNS resolver over IPv6
        "100.100.100.200",  # Alibaba Cloud
        "168.63.129.16",  # Azure platform (a public address)
        "192.0.0.192",  # Oracle Cloud
    )
)
METADATA_HOSTS: Final = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")
SYSTEM_BUNDLES: Final = (
    "/etc/ssl/cert.pem",  # Alpine, Arch, Fedora, RHEL 9-10, the BSDs
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora 44+, RHEL 11+
    "/etc/pki/tls/cert.pem",  # older Fedora, RHEL, CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/ssl/ca-bundle.pem",  # SUSE
)
"""Where systems keep their CA bundle, when OpenSSL was built to look elsewhere."""
OS_TRUST_STORES: Final = frozenset({"darwin", "win32"})
"""Platforms whose trust store the operating system keeps, not OpenSSL."""
LOOKUP_THREADS: Final = 4
"""Threads that look host names up for the guard, in each process."""
LOOKUPS_PER_HOST: Final = 2
"""Lookups of one host name under way at once; another fails at once."""
FAILED_LOOKUP_SECONDS: Final = 5.0
"""How long a failed lookup is remembered: its host fails at once meanwhile."""
FAILED_LOOKUPS: Final = 1024
"""Hosts whose failed lookups are remembered at most; the oldest go first."""
LOOKUPS: Final = "guard lookups"
"""The resolver's name among the resources closed at shutdown."""
SUBJECT: Final = "The URL"
"""How messages name what was refused, unless the caller names it otherwise."""


class UnsafeURL(BusinessRuleViolation):
    """A URL that may not be called; the message never quotes it."""

    code = "unsafe_url"


def judged(address: Address) -> Address:
    """The address whose kind decides: an IPv4 address inside an IPv6 one."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def refusal(address: Address, allowed: Sequence[Network] = ()) -> str | None:
    """Why ``address`` may not be called, or None if it may."""
    inner = judged(address)
    if address in METADATA_ADDRESSES or inner in METADATA_ADDRESSES:
        return "a cloud metadata address"
    if inner.is_unspecified or inner.is_multicast:
        return "an unspecified or multicast address"
    if any(inner in network for network in allowed):
        return None
    if (
        not inner.is_global
        or inner.is_reserved
        or (isinstance(inner, ipaddress.IPv6Address) and inner.is_site_local)
    ):
        return "a private, loopback, link-local, or reserved address"
    return None


def _literal(host: str) -> Address | None:
    """The address ``host`` spells, if it is one."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def checked_url(
    url: str,
    *,
    allow_http: bool,
    allowed: Sequence[Network] = (),
    subject: str = SUBJECT,
    error: type[UnsafeURL] = UnsafeURL,
    https_only: str = ".",
) -> httpx2.URL:
    """Parse ``url`` and check everything that does not need a lookup.

    Messages start with ``subject``; ``https_only`` ends the one that asks
    for https when http is not allowed.

    Raises:
        UnsafeURL: The URL may not be called (an ``error``).
    """
    if len(url) > MAX_URL_LENGTH:
        raise error(f"{subject} is longer than {MAX_URL_LENGTH} characters.")
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL:
        raise error(f"{subject} is not a valid URL.") from None
    schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in schemes:
        raise error(
            f"{subject} must use https" + (" or http." if allow_http else https_only)
        )
    if parsed.userinfo:
        raise error(f"{subject} may not hold a user name or password.")
    if parsed.port is not None and not 0 < parsed.port < 65536:
        raise error(f"{subject}'s port is not valid.")
    if parsed.fragment:
        raise error(f"{subject} may not have a fragment (#...).")
    host = parsed.raw_host.decode("ascii").rstrip(".").lower()
    if not host:
        raise error(f"{subject} has no host.")
    if host in METADATA_HOSTS or host.endswith(".localhost"):
        raise error(f"{subject} names a local or metadata host.")
    literal = _literal(host)
    if literal is not None and (reason := refusal(literal, allowed)):
        raise error(f"{subject}'s host is {reason}.")
    return parsed


class GuardResolver:
    """Looks host names up on a few threads of its own, a few at a time.

    At most ``threads`` lookups run at once, ``per_host`` of them for one
    host; one past either bound fails at once. A lookup counts until its
    thread is done, even when its caller stopped waiting: the system's
    resolver cannot be interrupted. A failed lookup is remembered for
    ``remember`` seconds, and its error raised again at once meanwhile.
    Event loops on several threads share one resolver, under its lock.
    The threads start with the first lookup and stop at shutdown
    (``close``), registered with ``domainkit.lifecycle``.
    """

    def __init__(
        self,
        threads: int = LOOKUP_THREADS,
        per_host: int = LOOKUPS_PER_HOST,
        remember: float = FAILED_LOOKUP_SECONDS,
    ) -> None:
        """Run ``threads`` lookups at once, ``per_host`` per host."""
        self.threads = threads
        self.per_host = per_host
        self.remember = remember
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._running: dict[str, int] = {}
        self._failed: OrderedDict[str, tuple[float, int, str]] = OrderedDict()

    @property
    def running(self) -> int:
        """Lookups whose thread has not finished, waited for or not."""
        with self._lock:
            return sum(self._running.values())

    async def lookup(self, host: str, port: int) -> list[Address]:
        """Every address ``host`` resolves to, in the system resolver's order.

        Raises:
            OSError: The host could not be resolved, failed to be a moment
                ago, or too many lookups are under way (``EAI_AGAIN``).
        """
        infos = await asyncio.wrap_future(self._start(host, port))
        return list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))

    async def close(self) -> None:
        """Stop the threads once their lookups end; the next lookup starts others."""
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _start(self, host: str, port: int) -> "Future[list[Any]]":
        """Hand the lookup to a thread, or refuse it at once."""
        with self._lock:
            failed = self._failed.get(host)
            if failed is not None:
                until, number, reason = failed
                if clock.monotonic() < until:
                    raise socket.gaierror(number, reason)
                del self._failed[host]
            if (
                sum(self._running.values()) >= self.threads
                or self._running.get(host, 0) >= self.per_host
            ):
                raise socket.gaierror(
                    socket.EAI_AGAIN, "Too many host name lookups are under way."
                )
            executor = self._executor
            starting = executor is None
            if executor is None:
                executor = self._executor = ThreadPoolExecutor(
                    self.threads, thread_name_prefix="domainkit-lookup"
                )
            future = executor.submit(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
            self._running[host] = self._running.get(host, 0) + 1
        if starting:
            _register(self)
        # Its clock is the caller's, frozen in tests, whichever thread ends it.
        context = contextvars.copy_context()
        future.add_done_callback(lambda done: context.run(self._finished, host, done))
        return future

    def _finished(self, host: str, future: "Future[list[Any]]") -> None:
        """Count the lookup's thread free; remember its host if it failed."""
        error = None if future.cancelled() else future.exception()
        with self._lock:
            if self._running[host] > 1:
                self._running[host] -= 1
            else:
                del self._running[host]
            if isinstance(error, OSError):
                until = clock.monotonic() + self.remember
                reason = error.strerror or str(error)
                self._failed[host] = (until, error.errno or 0, reason)
                if len(self._failed) > FAILED_LOOKUPS:
                    self._failed.popitem(last=False)


def _register(resolver: GuardResolver) -> None:
    """Close ``resolver``'s threads with the other resources at shutdown."""
    from domainkit import lifecycle  # It imports the databases: only when used.

    lifecycle.register(LOOKUPS, resolver.close)


resolver: Final = GuardResolver()
"""The resolver of every guarded lookup in the process."""


async def lookup(host: str, port: int) -> list[Address]:
    """Every address ``host`` resolves to, on the guard's threads (``resolver``).

    Raises:
        OSError: The host could not be resolved, failed to be a moment ago,
            or too many lookups are under way.
    """
    return await resolver.lookup(host, port)


async def checked_host(
    host: str,
    port: int,
    allowed: Sequence[Network] = (),
    *,
    resolve: Resolver | None = None,
    subject: str = SUBJECT,
    error: type[UnsafeURL] = UnsafeURL,
) -> Address:
    """The address to connect to for ``host``, once all of its addresses pass.

    ``resolve`` looks the host up (default: ``lookup``, the system resolver).

    Raises:
        UnsafeURL: One of the addresses may not be called, or there is none
            (an ``error``).
        OSError: The host could not be resolved.
    """
    literal = _literal(host)
    if literal is not None:
        addresses = [literal]
    else:
        addresses = await (resolve or lookup)(host, port)
    if not addresses:
        raise error(f"{subject}'s host has no address.")
    for address in addresses:
        if reason := refusal(address, allowed):
            raise error(f"{subject}'s host resolves to {reason}.")
    return addresses[0]


async def checked_address(
    url: httpx2.URL,
    allowed: Sequence[Network] = (),
    *,
    resolve: Resolver | None = None,
    subject: str = SUBJECT,
    error: type[UnsafeURL] = UnsafeURL,
) -> Address:
    """The address to connect to for ``url`` (see ``checked_host``)."""
    port = url.port or (443 if url.scheme == "https" else 80)
    return await checked_host(
        url.raw_host.decode("ascii"),
        port,
        allowed,
        resolve=resolve,
        subject=subject,
        error=error,
    )


@cache
def tls_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """A client TLS context for calls to URLs someone else chose, built once.

    It trusts ``ca_bundle``, or the system's CAs (``system_trust``), never
    a CA the environment names, and needs TLS 1.2 at least.
    """
    if ca_bundle is None:
        context = system_trust()
    else:
        context = ssl.create_default_context(cafile=ca_bundle)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def system_trust(platform: str = sys.platform) -> ssl.SSLContext:
    """A context trusting the system's CAs, and none the environment adds.

    On macOS and Windows the operating system keeps them (through
    ``truststore``), and reads no environment variable. Elsewhere OpenSSL's
    default paths follow ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``, so the CAs
    are loaded from where OpenSSL was built to look, or from the system's
    usual bundle.

    Raises:
        ImproperlyConfigured: No CA certificates were found: set a bundle.
    """
    if platform in OS_TRUST_STORES:
        return httpx2.create_ssl_context(trust_env=False)
    paths = ssl.get_default_verify_paths()
    bundles = (paths.openssl_cafile, *SYSTEM_BUNDLES)
    cafile = next((path for path in bundles if Path(path).is_file()), None)
    capath = paths.openssl_capath if Path(paths.openssl_capath).is_dir() else None
    if cafile is None and capath is None:
        raise ImproperlyConfigured(
            "No CA certificates were found for calls to URLs someone else "
            "chose; set CA_BUNDLE (HTTP_CLIENT, or WEBHOOKS) to a bundle."
        )
    return ssl.create_default_context(cafile=cafile, capath=capath)
