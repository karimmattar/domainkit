"""``AuthenticationMiddleware``: who sent each request, before anything counts it.

Add it to ``MIDDLEWARE`` after ``RequestContextMiddleware`` and before
``RateLimitMiddleware`` and ``IdempotencyMiddleware``, so rate limits,
idempotency keys, and the audit trail all see the user::

    MIDDLEWARE = [
        ...,
        "domainkit.http.middleware.RequestContextMiddleware",
        "domainkit.http.middleware.AllowedHostsMiddleware",
        "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
        "domainkit.http.ratelimit.RateLimitMiddleware",
        "domainkit.http.middleware.RequestBodyLimitMiddleware",
    ]

It tries ``AUTH.AUTHENTICATION_CLASSES`` in order. Credentials that one
recognizes and that do not hold get a 401 at once, even on public routes,
so a client never mistakes a bad token for anonymity. Requests without
credentials go on as ``AnonymousUser``: routes decide what anonymous
callers may do (``domainkit.contrib.auth.access``).

Refused credentials never reach ``RateLimitMiddleware``, so they are
counted here, per client address, against ``RATE_LIMIT``. Past it, the
address gets 429, and its requests with an ``Authorization`` header are
refused before any lookup until ``Retry-After`` has passed. Credentials in
other headers, which only a project's own class reads, are refused too,
once read, even when they hold: a guess that works gets the same 429.
Only credentials that do not hold (401) count: a refusal of credentials
that do, such as a cookie request that fails its CSRF checks (403), is a
request another site can make a browser send, and counting it would let
that site lock everyone behind the browser's address out.

An open WebSocket or event stream has its credentials checked again while
it lasts (``reauthenticate``, called by ``domainkit.http.live``), so a
revoked or expired token, or a deactivated user, does not keep it open.
"""

import logging
import threading
from collections import OrderedDict
from dataclasses import replace
from typing import Final

from fastapi import status
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.clock import monotonic
from domainkit.conf import settings
from domainkit.context import bind_request, current_request
from domainkit.http.errors import error_response
from domainkit.http.live import REAUTHENTICATE
from domainkit.http.ratelimit import Backend, Decision, address_key, refuse
from domainkit.log import SECURITY_LOGGER
from domainkit.utils.module_loading import import_string
from domainkit.utils.rates import parse_rate

from .authentication import (
    AuthenticationFailed,
    BaseAuthentication,
    Credentials,
    get_authenticators,
)
from .models import AnonymousUser
from .settings import auth_settings

security_logger = logging.getLogger(SECURITY_LOGGER)

MAX_HELD: Final = 10_000
"""Addresses held back at once; the longest held is let go first."""


class AuthenticationMiddleware:
    """Set ``scope["user"]``, ``scope["auth"]``, and the request's actor.

    ``request.user`` is the user, or ``AnonymousUser``; ``request.auth`` is
    the ``Credentials`` used, or None. The actor, which rate limits,
    idempotency keys, logs, and the audit trail use, is the user's id.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the configured authentication classes."""
        self.app = app
        self.authenticators = get_authenticators()
        self.exempt = frozenset(auth_settings().AUTHENTICATION_EXEMPT_PATHS)
        self.rate = parse_rate(settings.RATE_LIMIT) if settings.RATE_LIMIT else None
        self.limiter: Backend | None = (
            import_string(settings.RATE_LIMIT_BACKEND)() if self.rate else None
        )
        self.held: OrderedDict[str, float] = OrderedDict()
        """Addresses over the limit, and until when (``monotonic()``)."""
        # Event loops on several threads (free-threaded Python) share it.
        self._holding = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate, then serve the request as its user."""
        if scope["type"] not in {"http", "websocket"} or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        # Nothing held, nothing to look up: most requests skip the address.
        # The emptiness is read without the lock (a dict's length is atomic,
        # free-threaded too): a hold set meanwhile applies from the next
        # request, as if this one had come first.
        held = self._held(address_key(scope)) if self.held else None
        if held is not None and "authorization" in connection.headers:
            await refuse(scope, receive, send, *held)
            return
        try:
            credentials = await self._authenticate(connection)
        except _Refused as refused:
            await self._refuse(scope, receive, send, refused)
            return
        if held is not None and credentials is not None:
            # Credentials in another header, read by a project's own class,
            # are refused once read: a guess that works must not stand out.
            await refuse(scope, receive, send, *held)
            return
        scope["user"] = AnonymousUser() if credentials is None else credentials.user
        scope["auth"] = credentials
        scope[REAUTHENTICATE] = self.reauthenticate
        context = current_request()
        if credentials is None or context is None:
            await self.app(scope, receive, send)
            return
        with bind_request(replace(context, actor=str(credentials.user.id))):
            await self.app(scope, receive, send)

    async def reauthenticate(self, scope: Scope) -> bool:
        """Whether the credentials of ``scope`` still hold, for the same user.

        On success the user, with its groups and permissions, is loaded
        afresh into the scope, for the access rules checked next. Why
        credentials failed is logged, never told to the client.
        """
        before: Credentials | None = scope.get("auth")
        try:
            credentials = await self._authenticate(HTTPConnection(scope))
        except _Refused as refused:
            security_logger.warning(
                "Credentials of an open connection no longer hold: %s",
                refused.error.detail,
            )
            return False
        if before is None and credentials is None:
            return True
        if (
            before is None
            or credentials is None
            or credentials.user.id != before.user.id
        ):
            security_logger.warning(
                "An open connection's credentials no longer name its user"
            )
            return False
        scope["user"] = credentials.user
        scope["auth"] = credentials
        return True

    async def _authenticate(self, connection: HTTPConnection) -> Credentials | None:
        """The first class's verdict on the request's credentials."""
        for authenticator in self.authenticators:
            try:
                credentials = await authenticator.authenticate(connection)
            except AuthenticationFailed as exc:
                raise _Refused(authenticator, exc) from None
            if credentials is not None:
                return credentials
        return None

    async def _refuse(
        self, scope: Scope, receive: Receive, send: Send, refused: "_Refused"
    ) -> None:
        """Answer 401 with the class's challenge (a WebSocket is closed).

        A 401 counts against ``RATE_LIMIT`` for the address, and past it is
        a 429 instead; other refusals (a 403 for CSRF) are not counted.
        """
        security_logger.warning("Refused credentials: %s", refused.error.detail)
        unauthorized = refused.error.status_code == status.HTTP_401_UNAUTHORIZED
        if unauthorized and self.rate is not None and self.limiter is not None:
            limit, period = self.rate
            address = address_key(scope)
            decision = await self.limiter.hit(f"refused:{address}", limit, period)
            if not decision.allowed:
                self._hold(address, decision.retry_after)
                await refuse(scope, receive, send, limit, decision)
                return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = error_response(
            refused.error.status_code,
            str(refused.error.detail),
            code=refused.error.code,
        )
        if unauthorized:
            response.headers["WWW-Authenticate"] = refused.authenticator.challenge(
                "invalid_token"
            )
        await response(scope, receive, send)

    def _hold(self, address: str, seconds: float) -> None:
        """Refuse ``address``'s credentials unread for ``seconds``."""
        with self._holding:
            self.held[address] = monotonic() + seconds
            self.held.move_to_end(address)
            if len(self.held) > MAX_HELD:
                self.held.popitem(last=False)

    def _held(self, address: str) -> tuple[int, Decision] | None:
        """The limit and the refusal ``address`` is still held to, if any."""
        with self._holding:
            until = self.held.get(address)
            if until is None or self.rate is None:
                return None
            left = until - monotonic()
            if left <= 0:
                del self.held[address]
                return None
        refusal = Decision(
            allowed=False, remaining=0, reset_after=left, retry_after=left
        )
        return self.rate[0], refusal


class _Refused(Exception):
    """An authentication class refused the request's credentials."""

    def __init__(
        self, authenticator: BaseAuthentication, error: AuthenticationFailed
    ) -> None:
        """Keep who refused, and why."""
        super().__init__()
        self.authenticator = authenticator
        self.error = error
