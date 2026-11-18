"""The outgoing client and its guard in ``domainkit.outbound``, and at their 2.8 paths.

ADR 0195 moved them below the adapters that call through them (audit
A28-ARCH-04). ``domainkit.http.client`` and ``domainkit.http.guard`` still
import every name they had, the same objects, so ``isinstance`` and
``except`` hold either way, and the process keeps one set of clients,
circuits, metrics, and lookup threads.
"""

import socket
import sys

import httpx2
import pytest

from domainkit import lifecycle
from domainkit.conf import settings
from domainkit.http import client as old_client
from domainkit.http import guard as old_guard
from domainkit.outbound import client, guard

pytestmark = pytest.mark.anyio


def test_the_client_path_of_2_8_lists_and_imports_the_same_objects() -> None:
    """Every public name, the same object, in the same ``__all__``."""
    assert old_client.__all__ == client.__all__
    assert [
        name
        for name in client.__all__
        if vars(old_client)[name] is not getattr(client, name)
    ] == []
    assert old_client.HttpClient.__module__ == "domainkit.outbound.client"


def test_the_client_path_of_2_8_reads_every_other_name_through() -> None:
    """What 2.8's module also held: its helpers, its libraries, its internals."""
    assert old_client.backoff is client.backoff
    assert old_client.every_client is client.every_client
    assert old_client.httpx2 is httpx2
    assert old_client.LOGGER == "domainkit.http.client"  # Logging settings name it.
    with pytest.raises(ImportError, match="cannot import name 'nothing_here'"):
        from domainkit.http.client import nothing_here  # noqa: F401


def test_the_guard_path_of_2_8_imports_the_same_objects() -> None:
    """Every name the moved module holds, and the one resolver of the process."""
    held = [name for name in vars(guard) if not name.startswith("_")]

    assert [
        name for name in held if getattr(old_guard, name) is not vars(guard)[name]
    ] == []
    assert set(guard.__all__) | {"GuardResolver", "resolver"} <= set(vars(old_guard))
    assert old_guard.resolver is guard.resolver
    assert old_guard.socket is socket  # Read through: not set here, still there.


def test_a_name_set_on_the_old_path_is_not_the_one_the_client_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch where the name is looked up: ``domainkit.outbound.client``."""

    def never(attempt: int, base: float, most: float) -> float:
        raise AssertionError("not read")  # pragma: no cover - never called.

    monkeypatch.setattr(old_client, "backoff", never)

    assert client.backoff is not never
    assert client.backoff(1, 0.0, 0.0) == 0.0


async def test_a_client_made_by_either_name_is_one_of_the_process_clients() -> None:
    """One registry: ``close_all``, the circuits' reset, and metrics find it."""
    made = old_client.HttpClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200))
    )

    assert isinstance(made, client.HttpClient)
    assert made in client.every_client()
    with pytest.raises(old_client.UnsafeURL):
        raise guard.UnsafeURL("The URL may not be called.")
    await made.aclose()


async def test_shutdown_closes_the_pools_of_a_process_that_never_imported_2_8s_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapters import ``domainkit.outbound.client``; the lifespan looks there."""
    api = client.HttpClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200))
    )
    pool = await api._pool(settings.HTTP_CLIENT)
    monkeypatch.delitem(sys.modules, "domainkit.http.client")

    await lifecycle.close_http_clients()

    assert pool.is_closed
    await api.aclose()
