"""Request deadlines over HTTP: ``REQUEST_TIMEOUT``, and ``RequestTimeout`` per route.

``DeadlineMiddleware`` gives each HTTP request a deadline (see
``domainkit.deadlines``) of ``REQUEST_TIMEOUT`` seconds from when it reaches
the middleware. The deadline covers everything until the response starts:
authentication, reading the body, the handler, and what they wait on (the
database, outgoing calls). When it passes, that work is cancelled, the
request is answered 503 ``deadline_exceeded`` with ``Retry-After``, and one
warning is logged.

Once the response starts, the deadline is lifted: a streamed response (an
export, Server-Sent Events) and background tasks that run after the
response are not cut off. WebSocket connections have no request deadline.

A route gives itself another deadline, counted from when the request
arrived, with a dependency; ``RequestTimeout(None)`` removes it::

    @router.post(
        "/exports", dependencies=[Require(...), Depends(RequestTimeout(300))]
    )
    async def export(...): ...

The body is read before route dependencies run, under ``REQUEST_TIMEOUT``.
List the middleware right after ``RequestContextMiddleware``, so the
deadline starts before any work and its 503 carries the request id.
"""

import logging
import math
from typing import Final

from fastapi import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from domainkit import deadlines
from domainkit.conf import settings
from domainkit.http.errors import error_response

try:
    from domainkit.http.metrics import observe_deadline_exceeded
except ImportError:  # pragma: no cover - only without the [metrics] extra.

    def observe_deadline_exceeded() -> None:
        """Record nothing: ``prometheus_client`` is not installed."""


__all__ = [
    "DeadlineMiddleware",
    "RequestTimeout",
]


DEADLINE_EXCEEDED: Final = "deadline_exceeded"
"""The error code of a request answered 503 because its deadline passed."""
RETRY_AFTER: Final = "1"
"""Seconds a client should wait before trying a cut-off request again."""

logger = logging.getLogger(__name__)


class DeadlineMiddleware:
    """Cancel HTTP requests that run past their deadline, and answer 503."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app`` with the ``REQUEST_TIMEOUT`` deadline."""
        self.app = app
        self.seconds = settings.REQUEST_TIMEOUT

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the request within its deadline; answer 503 when it passes."""
        if scope["type"] != "http" or self.seconds is None:
            await self.app(scope, receive, send)
            return
        bound = deadlines.timeout(self.seconds)
        started = False

        async def send_lifting(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start" and not started:
                started = True
                lift(bound)
            await send(message)

        try:
            async with bound:
                await self.app(scope, receive, send_lifting)
        except TimeoutError as exc:
            # Once the response has started, it cannot become a 503: the
            # error goes on, and RequestContextMiddleware logs it.
            ours = bound.expired() or isinstance(exc, deadlines.DeadlineExceeded)
            if started or not ours:
                raise
            observe_deadline_exceeded()
            logger.warning(
                "%s %s ran past its deadline%s",
                scope["method"],
                scope["path"],
                _given(bound),
            )
            await _answer(scope, receive, send)


def lift(bound: deadlines.Timeout) -> None:
    """Remove the deadline of a request whose response has started."""
    if bound.deadline is not None:
        bound.deadline.clear()


def _given(bound: deadlines.Timeout) -> str:
    """`` of 30s``: the seconds the request was given, if it had a deadline."""
    seconds = None if bound.deadline is None else bound.deadline.seconds
    return "" if seconds is None else f" of {seconds:g}s"


async def _answer(scope: Scope, receive: Receive, send: Send) -> None:
    """503 ``deadline_exceeded``, with ``Retry-After``."""
    response = error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The request took too long; try again shortly.",
        code=DEADLINE_EXCEEDED,
    )
    response.headers["Retry-After"] = RETRY_AFTER
    await response(scope, receive, send)


class RequestTimeout:
    """A dependency that gives a route its own deadline (``Depends``).

    The deadline is ``seconds`` from when the request arrived, longer or
    shorter than ``REQUEST_TIMEOUT``; None removes it. Outside
    ``DeadlineMiddleware`` it does nothing. It is not an access rule: the
    route still needs ``Require(...)`` or ``Public``.
    """

    def __init__(self, seconds: float | None) -> None:
        """Allow the route ``seconds`` (above 0), or no deadline with None."""
        if seconds is not None and not 0 < seconds < math.inf:
            raise ValueError(
                f"A route's timeout is a number of seconds above 0, not {seconds!r}."
            )
        self.seconds = seconds

    async def __call__(self) -> None:
        """Move the request's deadline."""
        deadline = deadlines.current_deadline()
        if deadline is not None:
            deadline.reset(self.seconds)
