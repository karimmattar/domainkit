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
at most ``GUARD_LOOKUP_THREADS`` at once, ``LOOKUPS_PER_HOST`` of one host,
and a quarter of the threads (``OWNER_SHARE``) for one owner: the caller of
the request (its user, or its address), or whom ``lookups_for`` names
(``domainkit.contrib.webhooks`` names each delivery's subscription).
Never on the event loop's default executor: a name whose DNS never answers
holds its thread until the system's resolver gives up, long after the
caller stopped waiting, and would otherwise hold up ``asyncio.to_thread``
and new database connections. A lookup past those bounds fails at once, as
does one of a host whose lookup failed in the last
``FAILED_LOOKUP_SECONDS``, or in the last ``HUNG_LOOKUP_BACKOFF`` if it
failed after ``HUNG_LOOKUP_SECONDS`` or more: one caller's hosts that
never answer cannot hold every thread, nor take them again at once. A
lookup that succeeds after ``HUNG_LOOKUP_SECONDS`` or more has its answer
kept for ``HUNG_LOOKUP_BACKOFF`` (at most ``SLOW_ANSWERS`` hosts), so a
host whose DNS answers slowly does not take the threads again either: see
``GuardResolver``. A kept answer is checked again by its caller, as every
lookup's is.

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
from collections.abc import Awaitable, Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any, Final

import httpx2

from domainkit import clock
from domainkit.conf import settings
from domainkit.context import current_request
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
LOOKUP_THREADS: Final = 16
"""Threads that look host names up for the guard, in each process, by default.

``GUARD_LOOKUP_THREADS`` sets them."""
LOOKUPS_PER_HOST: Final = 2
"""Lookups of one host name under way at once; another fails at once."""
OWNER_SHARE: Final = 4
"""One owner's lookups may take this part of the threads (a quarter), at least 1."""
FAILED_LOOKUP_SECONDS: Final = 5.0
"""How long a failed lookup is remembered: its host fails at once meanwhile."""
HUNG_LOOKUP_SECONDS: Final = 5.0
"""A lookup that failed after this long hung: the resolver timed out on it."""
HUNG_LOOKUP_BACKOFF: Final = 60.0
"""How long a host whose lookup hung fails at once, instead of a retry's taking
a thread for as long again."""
FAILED_LOOKUPS: Final = 1024
"""Hosts whose failed lookups are remembered at most; the oldest go first."""
SLOW_ANSWERS: Final = 1024
"""Hosts whose slow answers are kept at most; the least recently used go first."""
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

    At most ``threads`` lookups run at once (default:
    ``GUARD_LOOKUP_THREADS``, read when the threads start), ``per_host``
    of them for one host, and a quarter of the threads (``OWNER_SHARE``)
    for one owner (``lookup_owner``); one past a bound fails at once. A
    lookup counts until its thread is done, even when its caller stopped
    waiting: the system's resolver cannot be interrupted. A failed lookup
    is remembered for ``remember`` seconds, and its error raised again at
    once meanwhile; one that failed after ``hung_after`` seconds or more
    hung, and is remembered for ``back_off`` seconds. Event loops on
    several threads share one resolver, under its lock. The threads start
    with the first lookup and stop at shutdown (``close``), registered
    with ``domainkit.lifecycle``.

    A lookup that succeeds after ``hung_after`` seconds or more has its
    answer kept for ``back_off`` seconds, and lookups of the host and port
    meanwhile get it at once, without a thread. The host is not refused:
    the system's resolver waits 5 seconds for a name server before it
    asks the next (``timeout`` in resolv.conf), so a slow success is what
    one name server down looks like, and the host resolves. Keeping the
    answer is what keeps a host whose DNS answers slowly, which its owner
    may control, from taking the threads again on every retry. The answer
    was the system resolver's; callers check its addresses on every use
    (``checked_host``), so a kept answer passes the same checks a fresh
    one would. A quick success is not kept, and a host that fails in
    under ``hung_after`` takes a thread on each new lookup; ``per_host``
    and the owner's share bound how many it holds at once.
    """

    def __init__(
        self,
        threads: int | None = None,
        per_host: int = LOOKUPS_PER_HOST,
        remember: float = FAILED_LOOKUP_SECONDS,
        *,
        hung_after: float = HUNG_LOOKUP_SECONDS,
        back_off: float = HUNG_LOOKUP_BACKOFF,
    ) -> None:
        """Run ``threads`` lookups at once, ``per_host`` per host."""
        self.threads = threads
        self.per_host = per_host
        self.remember = remember
        self.hung_after = hung_after
        self.back_off = back_off
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._size = 0
        """Threads of the running executor; 0 while there is none."""
        self._running: dict[str, int] = {}
        self._owners: dict[str, int] = {}
        """Lookups under way per owner; owners with none are forgotten."""
        self._failed: OrderedDict[str, tuple[float, int, str]] = OrderedDict()
        self._answered: OrderedDict[tuple[str, int], tuple[float, list[Any]]] = (
            OrderedDict()
        )
        """Slow answers by host and port: until when (``monotonic()``), and what."""

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
        infos = await asyncio.wrap_future(self._start(host, port, lookup_owner()))
        return list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))

    async def close(self) -> None:
        """Stop the threads once their lookups end; the next lookup starts others."""
        with self._lock:
            executor, self._executor = self._executor, None
            self._size = 0
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _start(
        self, host: str, port: int, owner: str | None = None
    ) -> "Future[list[Any]]":
        """Hand the lookup to a thread, or refuse it at once."""
        with self._lock:
            now = clock.monotonic()
            failed = self._failed.get(host)
            if failed is not None:
                until, number, reason = failed
                if now < until:
                    raise socket.gaierror(number, reason)
                del self._failed[host]
            if (kept := self._kept(host, port, now)) is not None:
                return kept
            size = self._size or self.threads or _configured_threads()
            if (
                sum(self._running.values()) >= size
                or self._running.get(host, 0) >= self.per_host
            ):
                raise socket.gaierror(
                    socket.EAI_AGAIN, "Too many host name lookups are under way."
                )
            if owner is not None and self._owners.get(owner, 0) >= max(
                1, size // OWNER_SHARE
            ):
                raise socket.gaierror(
                    socket.EAI_AGAIN,
                    "Too many host name lookups of one caller are under way.",
                )
            executor = self._executor
            starting = executor is None
            if executor is None:
                executor = self._executor = ThreadPoolExecutor(
                    size, thread_name_prefix="domainkit-lookup"
                )
                self._size = size
            future = executor.submit(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
            self._running[host] = self._running.get(host, 0) + 1
            if owner is not None:
                self._owners[owner] = self._owners.get(owner, 0) + 1
        if starting:
            _register(self)
        # Its clock is the caller's, frozen in tests, whichever thread ends it.
        context = contextvars.copy_context()
        future.add_done_callback(
            lambda done: context.run(self._finished, host, done, owner, now, port)
        )
        return future

    def _kept(self, host: str, port: int, now: float) -> "Future[list[Any]] | None":
        """A done lookup with ``host``'s kept slow answer, if it has not run out.

        Called under the lock.
        """
        answered = self._answered.get((host, port))
        if answered is None:
            return None
        until, infos = answered
        if now >= until:
            del self._answered[(host, port)]
            return None
        self._answered.move_to_end((host, port))
        kept: Future[list[Any]] = Future()
        kept.set_result(list(infos))
        return kept

    def _finished(
        self,
        host: str,
        future: "Future[list[Any]]",
        owner: str | None = None,
        started: float | None = None,
        port: int | None = None,
    ) -> None:
        """Count the lookup's thread free; remember its host if it failed.

        A failure after ``hung_after`` seconds or more, since ``started``,
        is remembered for ``back_off`` seconds, not ``remember``; a success
        that slow is kept for ``back_off`` seconds, for ``host`` and ``port``.
        """
        cancelled = future.cancelled()
        error = None if cancelled else future.exception()
        with self._lock:
            _count_off(self._running, host)
            if owner is not None:
                _count_off(self._owners, owner)
            now = clock.monotonic()
            hung = started is not None and now - started >= self.hung_after
            if hung and port is not None and not cancelled and error is None:
                self._answered[(host, port)] = (now + self.back_off, future.result())
                self._answered.move_to_end((host, port))
                if len(self._answered) > SLOW_ANSWERS:
                    self._answered.popitem(last=False)
            if isinstance(error, OSError):
                until = now + (self.back_off if hung else self.remember)
                reason = error.strerror or str(error)
                self._failed[host] = (until, error.errno or 0, reason)
                if len(self._failed) > FAILED_LOOKUPS:
                    self._failed.popitem(last=False)


def _configured_threads() -> int:
    """``GUARD_LOOKUP_THREADS``; ``LOOKUP_THREADS`` where no settings are loaded.

    The guard serves scripts that load no settings too, as it always has.
    """
    try:
        threads: int = settings.GUARD_LOOKUP_THREADS
    except ImproperlyConfigured:
        return LOOKUP_THREADS
    return threads


def _count_off(counts: dict[str, int], key: str) -> None:
    """One fewer of ``key`` under way; a key with none left is forgotten."""
    if counts[key] > 1:
        counts[key] -= 1
    else:
        del counts[key]


_owner: Final[contextvars.ContextVar[str | None]] = contextvars.ContextVar(
    "domainkit_lookup_owner", default=None
)


@contextmanager
def lookups_for(owner: str) -> Iterator[None]:
    """Count the guard's lookups in the block, and tasks it starts, as ``owner``'s.

    One owner's lookups may take a quarter of the threads (``OWNER_SHARE``),
    so hosts one owner chose that never answer leave the rest to others.
    """
    token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(token)


def lookup_owner() -> str | None:
    """Whose lookups these are: as ``lookups_for`` says, else the request's caller.

    The caller is the request's user (``actor:<id>``), or its address
    (``ip:<address>``, an IPv6 address's network, as rate limits count
    it). Outside a request, and unnamed, nobody's: only the other bounds
    hold.
    """
    owner = _owner.get()
    if owner is not None:
        return owner
    request = current_request()
    if request is None:
        return None
    if request.actor:
        return f"actor:{request.actor}"
    if request.client_ip is None:
        return "ip:unknown"
    from domainkit.http.ratelimit import client_group  # It imports the HTTP stack.

    return f"ip:{client_group(request.client_ip)}"


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
