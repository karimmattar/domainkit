"""Outgoing HTTP calls: ``domainkit.outbound.client``, under its 2.8 path.

The client moved to ``domainkit.outbound`` in 2.9, below the adapters
that call through it (ADR 0195). This module is a view of that one: each
name is read from it, the same object, so ``isinstance`` and ``except``
hold either way, and there is one set of clients, circuits, and metrics.
A name set here (a 2.8 test's ``monkeypatch.setattr``) is set there,
where the client and the adapters read it, and deleting it puts back a
name the client defined. New code imports and patches
``domainkit.outbound.client``.
"""

from typing import TYPE_CHECKING, Any

from domainkit.http._moved import keep_path
from domainkit.outbound import client as _new

if TYPE_CHECKING:
    from domainkit.outbound.client import CallTimeout as CallTimeout
    from domainkit.outbound.client import CircuitBreaker as CircuitBreaker
    from domainkit.outbound.client import CircuitOpen as CircuitOpen
    from domainkit.outbound.client import HttpClient as HttpClient
    from domainkit.outbound.client import ResponseTooLarge as ResponseTooLarge
    from domainkit.outbound.client import UnsafeURL as UnsafeURL
    from domainkit.outbound.client import client as client
    from domainkit.outbound.client import close_all as close_all
    from domainkit.outbound.client import reset_circuits as reset_circuits
    from domainkit.outbound.client import stub as stub

__all__ = [
    "CallTimeout",
    "CircuitBreaker",
    "CircuitOpen",
    "HttpClient",
    "ResponseTooLarge",
    "UnsafeURL",
    "client",
    "close_all",
    "reset_circuits",
    "stub",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401 - whichever name 2.8 had.
    """A name of ``domainkit.outbound.client``, the same object."""
    return getattr(_new, name)


keep_path(__name__, _new)
