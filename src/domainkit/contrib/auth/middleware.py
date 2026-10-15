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
"""

import logging
from dataclasses import replace

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.context import bind_request, current_request
from domainkit.http.errors import error_response
from domainkit.log import SECURITY_LOGGER

from .authentication import (
    AuthenticationFailed,
    BaseAuthentication,
    Credentials,
    get_authenticators,
)
from .models import AnonymousUser
from .settings import auth_settings

security_logger = logging.getLogger(SECURITY_LOGGER)


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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate, then serve the request as its user."""
        if scope["type"] not in {"http", "websocket"} or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        try:
            credentials = await self._authenticate(connection)
        except _Refused as refused:
            await self._refuse(scope, receive, send, refused)
            return
        scope["user"] = AnonymousUser() if credentials is None else credentials.user
        scope["auth"] = credentials
        context = current_request()
        if credentials is None or context is None:
            await self.app(scope, receive, send)
            return
        with bind_request(replace(context, actor=str(credentials.user.id))):
            await self.app(scope, receive, send)

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
        """Answer 401 with the class's challenge (a WebSocket is closed)."""
        security_logger.warning("Refused credentials: %s", refused.error.detail)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = error_response(
            refused.error.status_code,
            str(refused.error.detail),
            code=refused.error.code,
        )
        response.headers["WWW-Authenticate"] = refused.authenticator.challenge(
            "invalid_token"
        )
        await response(scope, receive, send)


class _Refused(Exception):
    """An authentication class refused the request's credentials."""

    def __init__(
        self, authenticator: BaseAuthentication, error: AuthenticationFailed
    ) -> None:
        """Keep who refused, and why."""
        super().__init__()
        self.authenticator = authenticator
        self.error = error
