"""Outgoing HTTP calls: ``domainkit.outbound.client``, under its 2.8 path.

The client moved to ``domainkit.outbound`` in 2.9, below the adapters
that call through it (ADR 0195). Every name of that module imports from
here too, and is the same object: ``isinstance`` and ``except`` hold
either way, and there is one set of clients, circuits, and metrics. A
name set on this module (a test's ``monkeypatch.setattr``) is not one the
client reads: patch ``domainkit.outbound.client`` instead.
"""

from typing import Any

from domainkit.outbound import client as moved
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
    """Any other name of ``domainkit.outbound.client``, the same object."""
    return getattr(moved, name)
