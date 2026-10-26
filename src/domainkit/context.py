"""Per-request context shared by logging and error responses.

The context lives in a ``ContextVar``, so concurrent requests never see each
other's values, and tasks started inside a request inherit them.
"""

from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Final

SYSTEM_ACTOR: Final = "system"
"""The actor of work no caller asked for: periodic tasks, commands' messages."""
UNMATCHED_PATH: Final = "unmatched"
"""The path logs show for a request no route matched (``ACCESS_LOG_PATH``)."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Facts about the request or job being served; no personal data.

    ``actor`` names who is acting. Authentication sets it; None means an
    anonymous caller, and background work carries the actor that queued it,
    or ``SYSTEM_ACTOR`` when no request did.
    """

    request_id: str
    client_ip: str | None
    method: str
    path: str
    """What logs and error reports show as the path: see ``RoutedRequestContext``."""
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class RoutedRequestContext(RequestContext):
    """A request's context whose ``path`` is its route's template (``ACCESS_LOG_PATH``).

    ``path`` is ``/accounts/{account_id}`` once the router has matched the
    request, read from ``scope`` when asked (``route_template``), and
    ``UNMATCHED_PATH`` before, or when no route matches: an id, a token,
    or an email in the path never reaches a log line or an error report
    through it. ``request_path`` is the path the caller sent, for answers
    to that caller only.
    """

    scope: MutableMapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    """The request's ASGI scope, which the router adds the route to."""

    @property
    def request_path(self) -> str:
        """The path the caller sent: for answers to it, never for logs."""
        path: str = self.scope["path"]
        return path


def _routed_path(context: RoutedRequestContext) -> str:
    """``RoutedRequestContext.path``: the template of the route matched so far.

    A flattened route's is read here, without calling ``route_template``.
    """
    scope = context.scope
    if "fastapi" not in scope and "route" in scope:
        path: str = getattr(scope["route"], "path", UNMATCHED_PATH)
        return path
    return route_template(scope)


# The dataclass takes inherited fields' names out of the class it builds, so
# the property goes in after: it reads the template, and the slot keeps what
# __init__ and dataclasses.replace() store.
RoutedRequestContext.path = property(  # type: ignore[assignment,misc]
    _routed_path, RequestContext.__dict__["path"].__set__
)


def route_template(scope: MutableMapping[str, Any]) -> str:
    """The matched route's full path template, or ``UNMATCHED_PATH``.

    A flattened route (``domainkit.http.routing``) is ``scope["route"]``,
    with its full path. For a route of a router FastAPI keeps nested, the
    effective, prefixed route is in ``scope["fastapi"]``: ``scope["route"]``
    alone would drop the prefixes. No call is made for a flattened route
    but the one ``getattr``: every logged request asks.
    """
    if "fastapi" in scope:
        fastapi_scope = scope["fastapi"]
        effective = (
            fastapi_scope.get("effective_route_context")
            if isinstance(fastapi_scope, dict)
            else None
        )
        nested = getattr(effective, "path", None)
        if isinstance(nested, str):
            return nested
    route = scope["route"] if "route" in scope else None  # noqa: SIM401 - no call.
    if route is None:
        return UNMATCHED_PATH
    path: str = getattr(route, "path", UNMATCHED_PATH)  # A Host route has none.
    return path


_current: ContextVar[RequestContext | None] = ContextVar("request", default=None)


def current_request() -> RequestContext | None:
    """Return the request being served, or None outside a request."""
    return _current.get()


@contextmanager
def bind_request(context: RequestContext) -> Iterator[None]:
    """Make ``context`` current for the duration of the block."""
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def enter_request(context: RequestContext) -> Token[RequestContext | None]:
    """Make ``context`` current until ``leave_request(token)``.

    ``bind_request`` without a context manager, for the one place every
    request passes (``RequestContextMiddleware``), where its calls count.
    """
    return _current.set(context)


def leave_request(token: Token[RequestContext | None]) -> None:
    """Make current again what was before ``enter_request`` gave ``token``."""
    _current.reset(token)
