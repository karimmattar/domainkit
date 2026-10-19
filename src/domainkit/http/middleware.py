"""Pure-ASGI middleware enforcing the HTTP security baseline.

List them in ``settings.MIDDLEWARE``, outermost first (Django's order). Each
class takes only the wrapped app and reads its options from settings. The
default order:

1. ``ProxyHeadersMiddleware``: the real client address behind a load balancer.
2. ``SecurityHeadersMiddleware``: headers on every response, including errors.
3. ``RequestContextMiddleware``: request id, access log, safe 500 responses,
   for HTTP requests and WebSocket connections.
4. ``AllowedHostsMiddleware``: rejects untrusted ``Host`` headers (logged).
5. ``RequestBodyLimitMiddleware``: rejects oversized bodies (logged).

Add ``CORSMiddleware`` right after ``SecurityHeadersMiddleware`` when browsers
on other origins call the API. Pure ASGI (not ``BaseHTTPMiddleware``) keeps
streaming responses intact and context variables visible to endpoints.
"""

import ipaddress
import logging
import re
import time
from collections.abc import Sequence
from typing import Final
from uuid import uuid4

from fastapi import status
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import cors
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit.conf import settings
from domainkit.context import RequestContext, bind_request
from domainkit.http.errors import (
    APIError,
    database_refusal,
    error_response,
    refusal_reason,
)
from domainkit.log import ACCESS_LOGGER, SECURITY_LOGGER
from domainkit.reporting import report_error

REQUEST_ID_HEADER: Final = "X-Request-ID"
# Accept caller-supplied ids only in a safe, bounded format; otherwise mint one.
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_HOST: Final = re.compile(r"([a-z0-9.-]+|\[[a-f0-9]*:[a-f0-9.:]+\])(?::[0-9]+)?")
"""A valid Host header (lowercased): the host, then an optional port."""
WEBSOCKET_METHOD: Final = "WS"
"""The method a WebSocket connection's request context and access log show."""
_STARTS: Final = frozenset(
    {"http.response.start", "websocket.http.response.start", "websocket.accept"}
)
"""Messages that answer a request (a WebSocket's accept, or its denial)."""
MESSAGE_TOO_BIG: Final = 1009
"""WebSocket close code for a message over ``MAX_REQUEST_BODY_SIZE``."""

access_logger = logging.getLogger(ACCESS_LOGGER)
security_logger = logging.getLogger(SECURITY_LOGGER)
logger = logging.getLogger(__name__)

TRY_AGAIN_LATER: Final = 1013
"""WebSocket close code: the server is busy."""
INVALID_DATA: Final = 1007
"""WebSocket close code: a value the server cannot take."""


type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class ProxyHeadersMiddleware:
    """Take the client address and scheme from trusted proxies' headers.

    Only connections from ``TRUSTED_PROXIES`` are believed. The client is the
    right-most ``X-Forwarded-For`` address that is not itself a trusted proxy,
    so a client cannot spoof its address by sending the header itself. Every
    line of a header counts (some proxies add a line instead of extending
    one), in order. The scheme is the ``X-Forwarded-Proto`` value at the
    client's place, counted from the right as its address was, or the last
    value when there are fewer. Without trusted proxies this middleware does
    nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the configured proxy networks."""
        self.app = app
        self.networks: tuple[_Network, ...] = tuple(settings.TRUSTED_PROXIES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Rewrite ``client`` and ``scheme`` for requests from a trusted proxy."""
        client = scope.get("client")
        if (
            self.networks
            and scope["type"] in {"http", "websocket"}
            and client
            and self.trusted(client[0])
        ):
            scope = dict(scope)
            headers = Headers(scope=scope)
            place = 1  # From the right: the hop the scheme describes.
            if found := self.client_hop(_values(headers, "x-forwarded-for")):
                address, place = found
                scope["client"] = (address, 0)
            protos = _values(headers, "x-forwarded-proto")
            proto = protos[-min(place, len(protos))].lower() if protos else ""
            if proto in {"http", "https"}:
                secure = proto == "https"
                if scope["type"] == "websocket":
                    scope["scheme"] = "wss" if secure else "ws"
                else:
                    scope["scheme"] = proto
        await self.app(scope, receive, send)

    def trusted(self, address: str) -> bool:
        """Report whether ``address`` belongs to a trusted proxy."""
        ip = _ip(address)
        return ip is not None and self._covers(ip)

    def _covers(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Whether a trusted network holds ``ip``."""
        return any(ip in network for network in self.networks)

    def client_hop(self, hops: list[str]) -> tuple[str, int] | None:
        """The first untrusted hop from the right, and its place; None if malformed.

        Hops may carry a port (``203.0.113.9:4711``, ``[2001:db8::1]:443``),
        and IPv4 addresses mapped into IPv6 count as IPv4.
        """
        addresses = []
        for hop in hops:
            if (ip := _ip(hop)) is None:
                return None
            addresses.append(ip)
        for place, ip in enumerate(reversed(addresses), start=1):
            if not self._covers(ip):
                return str(ip), place
        return (str(addresses[0]), len(addresses)) if addresses else None


def _values(headers: Headers, name: str) -> list[str]:
    """Every comma-separated value of every ``name`` line, in order."""
    return [
        value.strip()
        for line in headers.getlist(name)
        for value in line.split(",")
        if value.strip()
    ]


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``text`` as an address, after any port; IPv4-mapped IPv6 as IPv4."""
    host, port = text, None
    if text.startswith("["):  # [2001:db8::1]:443
        host, bracket, rest = text[1:].partition("]")
        if not bracket or rest[:1] not in {"", ":"}:
            return None
        port = rest[1:] if rest else None
    elif text.count(":") == 1:  # 203.0.113.9:4711
        host, _, port = text.partition(":")
    if port is not None and not port.isdecimal():
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def security_headers(
    *,
    hsts_seconds: int,
    hsts_include_subdomains: bool,
    content_type_nosniff: bool,
    referrer_policy: str | None,
    frame_options: str,
) -> list[tuple[str, str]]:
    """Translate Django-style ``SECURE_*`` settings into response headers."""
    headers = [("Cache-Control", "no-store"), ("X-Frame-Options", frame_options)]
    if content_type_nosniff:
        headers.append(("X-Content-Type-Options", "nosniff"))
    if referrer_policy:
        headers.append(("Referrer-Policy", referrer_policy))
    if hsts_seconds > 0:
        value = f"max-age={hsts_seconds}"
        if hsts_include_subdomains:
            value += "; includeSubDomains"
        headers.append(("Strict-Transport-Security", value))
    return headers


class SecurityHeadersMiddleware:
    """Add security headers unless the endpoint already set them."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the headers derived from ``SECURE_*`` settings."""
        self.app = app
        self.headers = security_headers(
            hsts_seconds=settings.SECURE_HSTS_SECONDS,
            hsts_include_subdomains=settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
            content_type_nosniff=settings.SECURE_CONTENT_TYPE_NOSNIFF,
            referrer_policy=settings.SECURE_REFERRER_POLICY,
            frame_options=settings.X_FRAME_OPTIONS,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add headers to the response start message."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    if name not in response_headers:
                        response_headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CORSMiddleware(cors.CORSMiddleware):
    """Starlette's CORS middleware configured from ``CORS_*`` settings."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the configured origins, methods, and headers."""
        super().__init__(
            app,
            allow_origins=settings.CORS_ALLOWED_ORIGINS,
            allow_methods=settings.CORS_ALLOWED_METHODS,
            allow_headers=settings.CORS_ALLOWED_HEADERS,
            allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
            expose_headers=[REQUEST_ID_HEADER],
        )


class RequestContextMiddleware:
    """Bind request context, echo the request id, and write the access log.

    Unhandled exceptions are logged once with the request id. Outside debug
    mode the client gets a generic JSON 500 with no internal details.

    A WebSocket connection gets a context too (method ``WS``), so the actor,
    the audit trail, and logs name its user. The request id is sent with the
    accept, and the access log line, written when the connection ends, shows
    101 if it was accepted and 403 if it was closed first. An error before
    the accept closes it with 1011, the WebSocket equivalent of a 500.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``; in debug mode errors reach the framework debug page."""
        self.app = app
        self.debug = settings.DEBUG
        self.quiet_paths = frozenset(settings.ACCESS_LOG_QUIET_PATHS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request, or one WebSocket connection, inside its context."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        websocket = scope["type"] == "websocket"
        supplied = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = (
            supplied if supplied and _REQUEST_ID.fullmatch(supplied) else uuid4().hex
        )
        client = scope.get("client")
        context = RequestContext(
            request_id=request_id,
            client_ip=client[0] if client else None,
            method=WEBSOCKET_METHOD if websocket else scope["method"],
            path=scope["path"],
        )
        response_status: int | None = None
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal response_status
            if message["type"] in _STARTS:
                accepted = message["type"] == "websocket.accept"
                response_status = (
                    status.HTTP_101_SWITCHING_PROTOCOLS
                    if accepted
                    else message["status"]
                )
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "websocket.close" and response_status is None:
                response_status = status.HTTP_403_FORBIDDEN  # Closed before accept.
            await send(message)

        with bind_request(context):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                refusal = database_refusal(exc)
                if refusal is not None and response_status is None:
                    logger.warning(
                        "Database refused %s %s: %s (%s)",
                        context.method,
                        context.path,
                        refusal.code,
                        refusal_reason(exc),
                    )
                    await self._refuse(refusal, scope, receive, send_with_id)
                    return
                logger.exception(
                    "Unhandled error in %s %s", context.method, context.path
                )
                report_error(
                    exc,
                    request_id=context.request_id,
                    method=context.method,
                    path=context.path,
                )
                if self.debug or response_status is not None:
                    raise
                if websocket:
                    await send({"type": "websocket.close", "code": 1011})
                    return
                response = error_response(
                    500, "Internal server error.", code="internal_error"
                )
                await response(scope, receive, send_with_id)
            finally:
                self._log_access(context, response_status, started)

    async def _refuse(
        self, refusal: APIError, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Answer ``refusal``; a WebSocket is closed as busy (1013) or invalid."""
        if scope["type"] == "websocket":
            busy = refusal.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
            code = TRY_AGAIN_LATER if busy else INVALID_DATA
            await send({"type": "websocket.close", "code": code})
            return
        response = error_response(
            refusal.status_code,
            str(refusal.detail),
            code=refusal.code,
            errors=refusal.errors,
        )
        response.headers.update(refusal.headers or {})
        await response(scope, receive, send)

    def _log_access(
        self, context: RequestContext, response_status: int | None, started: float
    ) -> None:
        """Log method, path (never the query string), status, and duration."""
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if context.path in self.quiet_paths and response_status == status.HTTP_200_OK:
            level = logging.DEBUG
        elif response_status is None or response_status >= 500:
            level = logging.ERROR
        elif response_status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        access_logger.log(
            level,
            "%s %s %s %.1fms",
            context.method,
            context.path,
            response_status if response_status is not None else "-",
            duration_ms,
            extra={
                "access": {
                    "method": context.method,
                    "path": context.path,
                    "status": response_status,
                    "duration_ms": duration_ms,
                }
            },
        )


def _hostname(host_header: str) -> str:
    """The lowercase host name of a Host header, without its port; "" if malformed.

    As in Django, a host is a name (letters, digits, dots, hyphens), an IPv4
    address, or a bracketed IPv6 address, with an optional numeric port.
    Anything else, such as ``evil.com/.example.com``, is no host at all, so
    a wildcard pattern cannot be matched by what follows a path or query.
    """
    match = _HOST.fullmatch(host_header.lower())
    return "" if match is None else match[1].removesuffix(".")


def host_allowed(host_header: str, allowed: Sequence[str]) -> bool:
    """Match exact names, ``*.example.com`` subdomains, or ``*`` (any host)."""
    name = _hostname(host_header)
    if not name:
        return False
    for pattern in allowed:
        pattern = pattern.lower()
        if pattern == "*" or name == pattern:
            return True
        if pattern.startswith("*.") and name.endswith(pattern[1:]):
            return True
    return False


class AllowedHostsMiddleware:
    """Reject requests whose Host header is not in ``ALLOWED_HOSTS``."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the configured host patterns."""
        self.app = app
        self.allowed_hosts = tuple(settings.ALLOWED_HOSTS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass trusted hosts through; answer 400 and log everything else."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "")
        if host_allowed(host, self.allowed_hosts):
            await self.app(scope, receive, send)
            return
        security_logger.warning("Rejected untrusted Host header %r", host[:255])
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await error_response(400, "Invalid host header.", code="invalid_host")(
            scope, receive, send
        )


class RequestBodyTooLarge(APIError):
    """The request body exceeded ``MAX_REQUEST_BODY_SIZE``.

    It is an ``HTTPException`` so FastAPI's body parsing re-raises it unchanged
    instead of turning it into a generic 400.
    """

    def __init__(self) -> None:
        """Use status 413."""
        super().__init__(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "Request body too large.",
            code="content_too_large",
        )


class RequestBodyLimitMiddleware:
    """Refuse bodies over the limit, by declared length and while streaming.

    A WebSocket message over the limit closes the connection with 1009
    (message too big); the application sees the client disconnect.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the ``MAX_REQUEST_BODY_SIZE`` byte limit."""
        self.app = app
        self.max_body_size = settings.MAX_REQUEST_BODY_SIZE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check ``Content-Length`` first, then count streamed bytes."""
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            # isdigit() alone accepts "²", which int() then rejects.
            if not (declared.isascii() and declared.isdigit()):
                await error_response(
                    400, "Invalid Content-Length header.", code="invalid_content_length"
                )(scope, receive, send)
                return
            if int(declared) > self.max_body_size:
                self._reject(int(declared))
                await error_response(
                    413, "Request body too large.", code="content_too_large"
                )(scope, receive, send)
                return
        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_size:
                    self._reject(received)
                    raise RequestBodyTooLarge
            return message

        async def send_tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, send_tracked)
        except RequestBodyTooLarge:
            # Middleware that reads the body itself (IdempotencyMiddleware)
            # raises outside FastAPI's error handling: answer for it.
            if started:
                raise
            await error_response(
                413, "Request body too large.", code="content_too_large"
            )(scope, receive, send)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Close the connection on a message over the limit, text or bytes."""
        closed = False

        async def limited_receive() -> Message:
            nonlocal closed
            message = await receive()
            if message["type"] == "websocket.receive":
                size = len(message.get("bytes") or b"") + len(
                    (message.get("text") or "").encode()
                )
                if size > self.max_body_size:
                    self._reject(size)
                    closed = True
                    await send({"type": "websocket.close", "code": MESSAGE_TOO_BIG})
                    return {"type": "websocket.disconnect", "code": MESSAGE_TOO_BIG}
            return message

        async def open_send(message: Message) -> None:
            if not closed:  # After the close, the application talks to no one.
                await send(message)

        await self.app(scope, limited_receive, open_send)

    def _reject(self, size: int) -> None:
        """Log an oversized request as a security event."""
        security_logger.warning(
            "Rejected request body of %d bytes (limit %d)", size, self.max_body_size
        )
