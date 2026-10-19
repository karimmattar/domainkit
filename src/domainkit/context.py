"""Per-request context shared by logging and error responses.

The context lives in a ``ContextVar``, so concurrent requests never see each
other's values, and tasks started inside a request inherit them.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

SYSTEM_ACTOR: Final = "system"
"""The actor of work no caller asked for: periodic tasks, commands' messages."""


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
    actor: str | None = None


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
