"""Outgoing HTTP calls, bounded by the request's deadline (``domainkit[httpclient]``).

::

    from domainkit.http.client import HttpClient

    rates = HttpClient(base_url="https://rates.example.com", timeout=3.0)

    async def euro_rate() -> Decimal:
        response = await rates.get("/v1/rates/EUR")
        response.raise_for_status()
        return Decimal(response.json()["rate"])

A client keeps one pool of connections per process (per event loop),
opened by its first call and closed with the other resources
(``close_resources``) when the application or a command ends. Make one per
service called, at module level; ``client`` serves calls that need no
options of their own. Every call:

- is bounded by ``timeout`` (``HTTP_CLIENT.TIMEOUT``), retries included,
  and by the deadline of the work it serves (``domainkit.deadlines``):
  past the deadline it raises ``DeadlineExceeded``, which a request
  answers 503, and past ``timeout``, ``CallTimeout``;
- is retried up to ``retries`` times after a network error, a timeout,
  or a 429, 502, 503, or 504 answer, with jittered exponential backoff
  (and ``Retry-After``), when its method is idempotent (GET, HEAD,
  OPTIONS, PUT, DELETE, TRACE) or it passes ``retry=True``. A call that
  could not connect is retried whatever its method, since nothing was
  sent; ``retry=False`` retries nothing;
- fails at once with ``CircuitOpen`` while its host's circuit is open:
  after ``BREAKER_FAILURES`` failures in a row (network errors, timeouts,
  5xx answers) for ``BREAKER_COOLDOWN`` seconds, after which one call at a
  time tries the host again;
- sends the request id (``X-Request-ID``) and the trace context
  (``traceparent``, ``tracestate``) of the work it serves;
- reads at most ``MAX_RESPONSE_BYTES`` of the answer, decompressed, or
  raises ``ResponseTooLarge``;
- is logged by method, origin, and path: never a query string, a header,
  or a body.

A client made with ``guard=True`` calls only public addresses
(``domainkit.http.guard``): https only (``allow_http=True`` adds http), no
credentials in the URL, every address of the host public unless in
``allowed_networks``, and the connection made to an address that was
checked, so DNS rebinding gains nothing. It follows no redirects, ignores
proxy settings from the environment, asks for uncompressed answers, logs
only the origin, and sends no request id or trace context (unless
``propagate=True``): use it for URLs that someone else chose. A refused
URL raises ``UnsafeURL``, a 422 if the request does not catch it.

In tests, ``stub(handler)`` sends every call to ``handler`` through
httpx2's ``MockTransport``; the handler takes an ``httpx2.Request`` and
returns an ``httpx2.Response``, or an awaitable of one. A client made with
``transport=`` always uses that transport.
"""

import asyncio
import logging
import random
import ssl
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from functools import cache
from pathlib import Path
from typing import Any, Final
from weakref import WeakKeyDictionary, WeakSet

import httpcore2
import httpx2

from domainkit import clock, deadlines
from domainkit.conf import settings
from domainkit.conf.global_settings import HttpClientConfig
from domainkit.context import current_request
from domainkit.http.guard import Network, UnsafeURL, checked_host, checked_url
from domainkit.tracing import trace_headers
from domainkit.utils.closing import close_each

__all__ = [
    "CallTimeout",
    "CircuitBreaker",
    "CircuitOpen",
    "HttpClient",
    "ResponseTooLarge",
    "UnsafeURL",
    "client",
    "close_all",
    "stub",
]

IDEMPOTENT: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})
"""Methods retried by default: sending them twice changes nothing more."""
RETRY_STATUSES: Final = frozenset({429, 502, 503, 504})
"""Answers worth trying again after a while."""
SERVER_ERROR: Final = 500
"""Answers from this status up count against the host's circuit."""
RETRIED: Final = (
    httpx2.NetworkError,
    httpx2.TimeoutException,
    httpx2.RemoteProtocolError,
)
"""Failures of an attempt worth another try; they count against the host too."""
BREAKER_HOSTS: Final = 1024
"""Hosts whose failures a client remembers; the oldest are forgotten first."""
REQUEST_ID_HEADER: Final = "X-Request-ID"
MAX_RETRIES: Final = 10
"""The most retries a client may make of one call."""

type Handler = (
    Callable[[httpx2.Request], httpx2.Response]
    | Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]
)
"""A stub's answer to a request: a response, sync or async."""

logger = logging.getLogger(__name__)


class CircuitOpen(httpx2.TransportError):
    """Calls to a host fail at once while its circuit is open."""


class CallTimeout(httpx2.TimeoutException):
    """A call took longer than its ``timeout``, retries included."""


class ResponseTooLarge(httpx2.RequestError):
    """An answer was longer than ``MAX_RESPONSE_BYTES``, decompressed."""


@dataclass(slots=True)
class _Circuit:
    """One host's recent failures."""

    failures: int = 0
    opened: float | None = None
    """When the circuit opened (``clock.monotonic``); None while closed."""
    trying: bool = False
    """A call is trying the host after the cooldown."""


class CircuitBreaker:
    """Circuits per host: open after failures in a row, then one trial at a time.

    A host's circuit opens after ``failures`` failures in a row and stays
    open for ``cooldown`` seconds; then one call may try the host. Its
    success closes the circuit, its failure opens it again, and a call
    that ends neither way (it was cancelled) lets the next call try. Only
    hosts with failures are kept, at most ``BREAKER_HOSTS`` of them.
    """

    def __init__(self) -> None:
        """Start with every circuit closed."""
        self._circuits: OrderedDict[str, _Circuit] = OrderedDict()

    def admits(self, host: str, cooldown: float) -> bool:
        """Whether a call to ``host`` may go now.

        While the circuit is open, no call may until ``cooldown`` seconds
        have passed; then one may, as a trial, and others wait for its end.
        """
        circuit = self._circuits.get(host)
        if circuit is None or circuit.opened is None:
            return True
        if circuit.trying or clock.monotonic() < circuit.opened + cooldown:
            return False
        circuit.trying = True
        return True

    def succeeded(self, host: str) -> None:
        """Forget the host's failures: its circuit closes."""
        self._circuits.pop(host, None)

    def failed(self, host: str, failures: int) -> bool:
        """Count a failure; return whether it opened the circuit.

        ``failures`` in a row open it, and a failed trial opens it again.
        """
        circuit = self._circuits.pop(host, None) or _Circuit()
        self._circuits[host] = circuit
        if len(self._circuits) > BREAKER_HOSTS:
            self._circuits.popitem(last=False)
        circuit.failures += 1
        circuit.trying = False
        if circuit.failures < failures:
            return False
        circuit.opened = clock.monotonic()
        return True

    def abandoned(self, host: str) -> None:
        """A call that neither succeeded nor failed: let the next one try."""
        circuit = self._circuits.get(host)
        if circuit is not None:
            circuit.trying = False


_stub: ContextVar[httpx2.AsyncBaseTransport | None] = ContextVar(
    "domainkit_http_stub", default=None
)
_clients: WeakSet["HttpClient"] = WeakSet()
"""Every client made, so ``close_all`` finds their pools."""


class HttpClient:
    """Outgoing calls, pooled, bounded, retried, and broken off per host.

    Options left as None follow ``HTTP_CLIENT``, read at each call.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        guard: bool = False,
        allow_http: bool = False,
        allowed_networks: Sequence[Network] = (),
        propagate: bool | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """Make a client; nothing is opened until its first call.

        Relative URLs are appended to ``base_url`` (absolute, without a
        query), and others must keep to its origin; ``headers`` go with
        every call; ``timeout`` and ``retries`` replace ``HTTP_CLIENT``'s.
        ``guard`` checks every URL and address (see the module);
        ``allow_http`` and ``allowed_networks`` loosen it. ``propagate``
        sends the request id and trace context (default: unless guarded).
        ``transport`` replaces the network, as httpx2's ``MockTransport``.
        """
        if timeout is not None and not timeout > 0:
            raise ValueError(f"A client's timeout must be above 0, not {timeout!r}.")
        if retries is not None and not 0 <= retries <= MAX_RETRIES:
            raise ValueError(f"A client retries 0 to 10 times, not {retries!r}.")
        self.base_url = None if base_url is None else _base(base_url)
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.retries = retries
        self.guard = guard
        self.allow_http = allow_http
        self.allowed_networks = tuple(allowed_networks)
        self.propagate = not guard if propagate is None else propagate
        self.transport = transport
        self.breaker = CircuitBreaker()
        self._pools: WeakKeyDictionary[
            asyncio.AbstractEventLoop, httpx2.AsyncClient
        ] = WeakKeyDictionary()
        _clients.add(self)

    async def get(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("GET", url, ...)``."""
        return await self.request("GET", url, **options)

    async def head(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("HEAD", url, ...)``."""
        return await self.request("HEAD", url, **options)

    async def options(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("OPTIONS", url, ...)``."""
        return await self.request("OPTIONS", url, **options)

    async def post(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("POST", url, ...)``."""
        return await self.request("POST", url, **options)

    async def put(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("PUT", url, ...)``."""
        return await self.request("PUT", url, **options)

    async def patch(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("PATCH", url, ...)``."""
        return await self.request("PATCH", url, **options)

    async def delete(self, url: str, **options: Any) -> httpx2.Response:  # noqa: ANN401
        """``request("DELETE", url, ...)``."""
        return await self.request("DELETE", url, **options)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any = None,  # noqa: ANN401 - any JSON value.
        timeout: float | None = None,  # noqa: ASYNC109 - bounds the call, retries too.
        retry: bool | None = None,
    ) -> httpx2.Response:
        """Send one call: ``method`` to ``url``, retried as the module says.

        ``timeout`` replaces the client's for this call; ``retry`` forces
        retries (True) or forbids them (False).

        Raises:
            DeadlineExceeded: The deadline of the work passed.
            CallTimeout: ``timeout`` passed, retries included.
            CircuitOpen: The host's circuit is open.
            ResponseTooLarge: The answer was over ``MAX_RESPONSE_BYTES``.
            UnsafeURL: A guarded client was asked for a URL it may not call.
            httpx2.HTTPError: The call failed on its last try.
        """
        if timeout is not None and not timeout > 0:
            raise ValueError(f"A call's timeout must be above 0, not {timeout!r}.")
        config = settings.HTTP_CLIENT
        method = method.upper()
        target = self._target(url)
        origin = _origin(target)
        seconds = timeout or self.timeout or config.TIMEOUT
        call = _Call(
            method=method,
            shown=_shown(target, path=not self.guard),
            origin=origin,
            seconds=seconds,
            retries=config.RETRIES if self.retries is None else self.retries,
            retry=retry,
        )
        budget = deadlines.timeout(seconds)
        pool = self._pool(config)
        try:
            async with budget:
                request = pool.build_request(
                    method,
                    target,
                    params=params,
                    headers=self._headers(config, headers),
                    content=content,
                    data=data,
                    json=json,
                )
                return await self._send(call, pool, request, config)
        except TimeoutError as exc:
            if isinstance(exc, deadlines.DeadlineExceeded) or not budget.expired():
                raise
            if self.breaker.failed(origin, config.BREAKER_FAILURES):
                _opened(origin, config)
            logger.warning(
                "%s %s took longer than its %gs", method, call.shown, seconds
            )
            raise CallTimeout(
                f"{method} {call.shown} took longer than {seconds:g}s."
            ) from exc

    async def aclose(self) -> None:
        """Close this client's connections on the running event loop."""
        pool = self._pools.pop(asyncio.get_running_loop(), None)
        if pool is not None:
            await pool.aclose()

    async def _send(
        self,
        call: "_Call",
        pool: httpx2.AsyncClient,
        request: httpx2.Request,
        config: HttpClientConfig,
    ) -> httpx2.Response:
        """Try the call until it succeeds, may not be retried, or runs out of time."""
        while True:
            if not self.breaker.admits(call.origin, config.BREAKER_COOLDOWN):
                raise CircuitOpen(
                    f"Calls to {call.origin} fail at once: its circuit is open.",
                    request=request,
                )
            started = clock.monotonic()
            try:
                response = await self._attempt(call, pool, request, config)
            except RETRIED as exc:
                self._failed(call, config)
                delay = call.wait_before_retry(exc, config)
                if delay is None:
                    logger.warning(
                        "%s %s failed (%s) after %d attempt(s)",
                        call.method,
                        call.shown,
                        type(exc).__name__,
                        call.attempt + 1,
                    )
                    raise
                outcome = type(exc).__name__
            except BaseException:
                self.breaker.abandoned(call.origin)
                raise
            else:
                if response.status_code >= SERVER_ERROR:
                    self._failed(call, config)
                else:
                    self.breaker.succeeded(call.origin)
                delay = call.wait_before_retry(response, config)
                if delay is None:
                    logger.debug(
                        "%s %s %d in %.1fms",
                        call.method,
                        call.shown,
                        response.status_code,
                        (clock.monotonic() - started) * 1000,
                    )
                    return response
                outcome = str(response.status_code)
            logger.info(
                "Retrying %s %s in %.2fs after %s (attempt %d of %d)",
                call.method,
                call.shown,
                delay,
                outcome,
                call.attempt + 1,
                call.retries + 1,
            )
            await _sleep(delay)
            call.attempt += 1

    async def _attempt(
        self,
        call: "_Call",
        pool: httpx2.AsyncClient,
        request: httpx2.Request,
        config: HttpClientConfig,
    ) -> httpx2.Response:
        """Send ``request`` once and read its answer, at most ``MAX_RESPONSE_BYTES``.

        httpx2's own timeouts get the time the call has left, so its errors
        say which step was slow.
        """
        left = deadlines.remaining(call.seconds)
        request.extensions["timeout"] = httpx2.Timeout(
            left, connect=min(config.CONNECT_TIMEOUT, left)
        ).as_dict()
        response = await pool.send(request, stream=True)
        try:
            body = await _read(response, config.MAX_RESPONSE_BYTES)
        finally:
            await response.aclose()
        response._content = body
        return response

    def _failed(self, call: "_Call", config: HttpClientConfig) -> None:
        """Count a failure against the host; warn once when its circuit opens."""
        if self.breaker.failed(call.origin, config.BREAKER_FAILURES):
            _opened(call.origin, config)

    def _target(self, url: str) -> httpx2.URL:
        """``url`` under ``base_url``, if the client has one; checked if guarded.

        A relative URL is appended to the base URL's path, as httpx2 does. A
        URL naming a host must keep to the base URL's origin, so a path built
        from someone's input cannot send the call elsewhere.

        Raises:
            UnsafeURL: The URL leaves the base URL's origin, or the guard
                refuses it.
        """
        target = httpx2.URL(url)
        base = self.base_url
        if base is not None:
            if target.scheme or target.host:
                if (target.scheme, target.host, target.port) != (
                    base.scheme,
                    base.host,
                    base.port,
                ):
                    raise UnsafeURL("The URL leaves the client's base URL.")
            else:
                path = base.raw_path.rstrip(b"/") + b"/" + target.raw_path.lstrip(b"/")
                target = base.copy_with(raw_path=path)
        if self.guard:
            target = checked_url(
                str(target), allow_http=self.allow_http, allowed=self.allowed_networks
            )
        return target

    def _headers(
        self, config: HttpClientConfig, extra: Mapping[str, str] | None
    ) -> httpx2.Headers:
        """The call's headers: what it carries along, the client's, then its own.

        Each replaces the one before it of the same name, whatever its case.
        """
        headers = httpx2.Headers({"User-Agent": config.USER_AGENT})
        if self.guard:
            headers["Accept-Encoding"] = "identity"
        if self.propagate:
            request = current_request()
            if request is not None:
                headers[REQUEST_ID_HEADER] = request.request_id
            headers.update(trace_headers())
        headers.update(self.headers)
        headers.update(extra or {})
        return headers

    def _pool(self, config: HttpClientConfig) -> httpx2.AsyncClient:
        """The running loop's pool (a stub's, inside ``stub``), opened on first use."""
        stubbed = _stub.get()
        if stubbed is not None:
            return httpx2.AsyncClient(transport=stubbed, follow_redirects=False)
        loop = asyncio.get_running_loop()
        pool = self._pools.get(loop)
        if pool is None:
            pool = self._pools[loop] = self._open(config)
        return pool

    def _open(self, config: HttpClientConfig) -> httpx2.AsyncClient:
        """A pool of connections, with the guard's transport if guarded."""
        verify = tls_context(config.CA_BUNDLE)
        limits = httpx2.Limits(
            max_connections=config.MAX_CONNECTIONS,
            max_keepalive_connections=config.MAX_KEEPALIVE_CONNECTIONS,
        )
        transport = self.transport
        if transport is None and self.guard:
            transport = GuardedTransport(self.allowed_networks, verify, limits)
        return httpx2.AsyncClient(
            transport=transport,
            verify=verify,
            limits=limits,
            trust_env=not self.guard,
            follow_redirects=False,
        )


@dataclass(slots=True)
class _Call:
    """One call's progress through its attempts."""

    method: str
    shown: str
    """How logs name the URL: no query string, no credentials."""
    origin: str
    seconds: float
    """The call's timeout, retries included."""
    retries: int
    retry: bool | None
    attempt: int = 0

    def wait_before_retry(
        self, outcome: httpx2.Response | Exception, config: HttpClientConfig
    ) -> float | None:
        """Seconds to wait before trying again; None when it should not.

        A call is retried while it has retries left, its kind of failure
        is worth another try, the wait is at most ``MAX_BACKOFF``, and
        enough time is left to wait.
        """
        if self.attempt >= self.retries or self.retry is False:
            return None
        delay = backoff(self.attempt, config.BACKOFF, config.MAX_BACKOFF)
        if isinstance(outcome, Exception):
            if not (self.retry or self.method in IDEMPOTENT or _not_sent(outcome)):
                return None
        elif outcome.status_code not in RETRY_STATUSES or not (
            self.retry or self.method in IDEMPOTENT
        ):
            return None
        else:
            asked = retry_after(outcome.headers.get("retry-after"))
            if asked is not None:
                if asked > config.MAX_BACKOFF:
                    return None
                delay = max(delay, asked)
        left = deadlines.remaining()
        if left is not None and delay >= left:
            return None
        return delay


def _base(url: str) -> httpx2.URL:
    """A client's base URL: absolute, with no query or fragment to append to."""
    base = httpx2.URL(url)
    if not base.is_absolute_url or base.query or base.fragment:
        raise ValueError(
            "A client's base URL names a scheme and a host, and has no query "
            "or fragment."
        )
    return base


def backoff(attempt: int, base: float, most: float) -> float:
    """A random wait before retry ``attempt + 1``: up to ``base`` times 2^attempt.

    "Full jitter": the wait is spread evenly from 0 to that bound, capped at
    ``most``, so clients that failed together do not retry together.
    """
    return random.random() * min(most, base * 2.0**attempt)  # noqa: S311 - jitter only.


def retry_after(value: str | None) -> float | None:
    """Seconds a ``Retry-After`` value asks for: a number, or an HTTP date."""
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - clock.now()).total_seconds(), 0.0)


def _not_sent(error: Exception) -> bool:
    """Whether the call failed before anything was sent: safe to send again."""
    return isinstance(
        error, httpx2.ConnectError | httpx2.ConnectTimeout | httpx2.PoolTimeout
    )


def _origin(url: httpx2.URL) -> str:
    """``https://host:443``: whom a call goes to, which its circuit is kept for."""
    port = url.port or {"https": 443, "http": 80}.get(url.scheme)
    return f"{url.scheme}://{url.raw_host.decode('ascii')}:{port}"


def _shown(url: httpx2.URL, *, path: bool) -> str:
    """How logs name ``url``: origin and path, never the query or credentials."""
    shown = f"{url.scheme}://{url.netloc.decode('ascii')}"
    if path:
        shown += url.raw_path.split(b"?", 1)[0].decode("ascii")
    return shown


def _opened(origin: str, config: HttpClientConfig) -> None:
    """Warn that a host's circuit opened."""
    logger.warning(
        "The circuit to %s is open for %gs after %d failures in a row",
        origin,
        config.BREAKER_COOLDOWN,
        config.BREAKER_FAILURES,
    )


async def _read(response: httpx2.Response, limit: int) -> bytes:
    """The decompressed body of ``response``, unless longer than ``limit``."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ResponseTooLarge(
                f"The answer is longer than {limit} bytes.", request=response.request
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _sleep(seconds: float) -> None:
    """Wait before a retry (tests replace it)."""
    await asyncio.sleep(seconds)


@cache
def tls_context(ca_bundle: Path | None) -> ssl.SSLContext:
    """A client TLS context for ``CA_BUNDLE`` (default: httpx2's), built once."""
    if ca_bundle is None:
        context = httpx2.create_ssl_context()
    else:
        context = ssl.create_default_context(cafile=ca_bundle)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class GuardedBackend(httpcore2.AsyncNetworkBackend):
    """Connects only to addresses the guard passed (``domainkit.http.guard``).

    The host is resolved, and every address checked, as each connection is
    opened; the connection goes to the first address. TLS still checks the
    certificate against the host name, and the pool keeps connections per
    host name, never per address.
    """

    def __init__(self, allowed: Sequence[Network]) -> None:
        """Let ``allowed`` private networks through."""
        self.allowed = tuple(allowed)
        self._backend = httpcore2.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface.
        local_address: str | None = None,
        socket_options: Any = None,  # noqa: ANN401 - httpcore2's option type.
    ) -> httpcore2.AsyncNetworkStream:
        """Check the host's addresses, then connect to the first.

        Raises:
            UnsafeURL: The host has an address the guard refuses.
            httpcore2.ConnectError: The host could not be resolved.
            httpcore2.ConnectTimeout: Resolving took longer than ``timeout``.
        """
        try:
            async with asyncio.timeout(timeout):
                address = await checked_host(host, port, self.allowed)
        except TimeoutError as exc:
            raise httpcore2.ConnectTimeout("Resolving the host took too long.") from exc
        except OSError as exc:
            raise httpcore2.ConnectError(
                f"The host could not be resolved: {exc}"
            ) from exc
        return await self._backend.connect_tcp(
            str(address),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore2's interface.
        socket_options: Any = None,  # noqa: ANN401 - httpcore2's option type.
    ) -> httpcore2.AsyncNetworkStream:
        """Refuse: a guarded client calls hosts, never local sockets."""
        raise UnsafeURL("The URL names a local socket.")

    async def sleep(self, seconds: float) -> None:
        """Sleep, as the backend underneath does."""
        await self._backend.sleep(seconds)


class GuardedTransport(httpx2.AsyncHTTPTransport):
    """httpx2's transport, connecting through ``GuardedBackend``, without proxies."""

    def __init__(
        self,
        allowed: Sequence[Network],
        verify: ssl.SSLContext,
        limits: httpx2.Limits,
    ) -> None:
        """Pool connections as httpx2 does, opened by the guarded backend."""
        super().__init__(verify=verify, limits=limits, trust_env=False)
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=verify,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=GuardedBackend(allowed),
        )


@contextmanager
def stub(handler: Handler | httpx2.AsyncBaseTransport) -> Iterator[None]:
    """Send every call made in the block (and tasks it starts) to ``handler``.

    ``handler`` takes an ``httpx2.Request`` and returns an
    ``httpx2.Response`` (or an awaitable of one), through httpx2's
    ``MockTransport``; a transport is used as it is. The URL checks of
    guarded clients still apply; address checks need the network, so a
    stub skips them.
    """
    transport = (
        handler
        if isinstance(handler, httpx2.AsyncBaseTransport)
        else httpx2.MockTransport(handler)
    )
    token = _stub.set(transport)
    try:
        yield
    finally:
        _stub.reset(token)


async def close_all() -> None:
    """Close every client's connections on the running event loop."""
    await close_each(list(_clients), HttpClient.aclose)


client: Final = HttpClient()
"""A client with ``HTTP_CLIENT``'s options, for calls that need no others."""
