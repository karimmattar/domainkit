"""The outgoing client and its guard in ``domainkit.outbound``, and at their 2.8 paths.

ADR 0195 moved them below the adapters that call through them (audit
A28-ARCH-04). ``domainkit.http.client`` and ``domainkit.http.guard`` are
views of the new modules: every name reads from them, the same object, so
``isinstance`` and ``except`` hold either way, and the process keeps one
set of clients, circuits, metrics, and lookup threads. A name a 2.8 test
sets or deletes on an old path is set or deleted where the code reads it.
"""

import importlib
import socket
import sys
from types import ModuleType
from unittest import mock

import httpx2
import pytest

from domainkit import lifecycle
from domainkit.conf import settings
from domainkit.http import _moved
from domainkit.http import client as old_client
from domainkit.http import guard as old_guard
from domainkit.outbound import client, guard
from domainkit.search._http import shared_client

pytestmark = pytest.mark.anyio


class FakeClient:
    """What a 2.8 test put in place of ``HttpClient`` to fake an adapter's calls."""

    def __init__(self, **options: object) -> None:
        """Keep the options the adapter made the client with."""
        self.options = options


def test_the_client_path_of_2_8_lists_and_reads_the_same_objects() -> None:
    """Every public name, the same object, in the same ``__all__``; none kept."""
    assert old_client.__all__ == client.__all__
    assert [
        name
        for name in client.__all__
        if getattr(old_client, name) is not getattr(client, name)
    ] == []
    assert set(client.__all__).isdisjoint(vars(old_client))
    assert old_client.HttpClient.__module__ == "domainkit.outbound.client"


def test_the_client_path_of_2_8_reads_every_other_name_through() -> None:
    """What 2.8's module also held: its helpers, its libraries, its internals."""
    assert old_client.backoff is client.backoff
    assert old_client.every_client is client.every_client
    assert old_client.httpx2 is httpx2
    assert old_client.LOGGER == "domainkit.http.client"  # Logging settings name it.
    with pytest.raises(ImportError, match="cannot import name 'nothing_here'"):
        from domainkit.http.client import nothing_here  # noqa: F401


def test_the_guard_path_of_2_8_reads_the_same_objects() -> None:
    """Every name the moved module holds, and the one resolver of the process."""
    held = [name for name in vars(guard) if not name.startswith("_")]

    assert [
        name for name in held if getattr(old_guard, name) is not vars(guard)[name]
    ] == []
    assert old_guard.resolver is guard.resolver
    assert old_guard.socket is socket


def test_dir_of_an_old_path_lists_the_moved_names() -> None:
    """``dir()`` shows what the old path reads through, and its own names."""
    assert {"HttpClient", "backoff", "client", "__all__"} <= set(dir(old_client))
    assert {"checked_url", "resolver", "GuardResolver"} <= set(dir(old_guard))


def test_monkeypatching_the_old_path_reaches_the_adapters_and_undoes_cleanly() -> None:
    """A 2.8 test's fake ``HttpClient`` is the one an adapter makes (review B1)."""
    real = client.HttpClient
    patch = pytest.MonkeyPatch()
    patch.setattr("domainkit.http.client.HttpClient", FakeClient)
    try:
        made = shared_client("https://search.example.test", 3.0)
    finally:
        patch.undo()

    assert isinstance(made, FakeClient)
    assert made.options["base_url"] == "https://search.example.test"
    assert client.HttpClient is real
    assert old_client.HttpClient is real
    assert "HttpClient" not in vars(old_client)


def test_mock_patch_on_the_old_path_reaches_the_code_and_undoes_cleanly() -> None:
    """``mock.patch`` sets, then deletes and puts back, on the module moved to."""
    real = client.backoff
    with mock.patch("domainkit.http.client.backoff", return_value=0.5) as backoff:
        assert client.backoff is backoff
    with mock.patch("domainkit.http.guard.lookup") as lookup:
        assert guard.lookup is lookup

    assert client.backoff is real
    assert guard.lookup.__module__ == "domainkit.outbound.guard"
    assert "backoff" not in vars(old_client)
    assert "lookup" not in vars(old_guard)


def test_a_stub_s_shared_transport_is_read_live_through_the_old_path() -> None:
    """A monkeypatch undone leaves no copy that hides the client's own value."""
    patch = pytest.MonkeyPatch()
    patch.setattr("domainkit.http.client._shared_stub", None)
    patch.undo()

    with client.stub(lambda _: httpx2.Response(200), everywhere=True):
        assert old_client._shared_stub is client._shared_stub is not None
    assert "_shared_stub" not in vars(old_client)


def test_reloading_an_old_path_leaves_the_new_module_as_it_is() -> None:
    """The view is made again; the module it shows stays a module (review)."""
    reloaded = importlib.reload(old_client)

    assert reloaded is old_client
    assert type(client) is ModuleType
    assert isinstance(old_client, _moved.MovedModule)
    assert old_client.HttpClient is client.HttpClient
    assert "backoff" in dir(old_client)


def test_the_old_path_s_own_names_stay_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its docstring and the like are set and deleted on the old module itself."""
    monkeypatch.setattr(old_client, "__doc__", "Patched.")
    assert old_client.__doc__ == "Patched."
    assert client.__doc__ != "Patched."
    monkeypatch.delattr(old_guard, "__doc__")
    assert "__doc__" not in vars(old_guard)
    assert guard.__doc__
    assert isinstance(old_client, _moved.MovedModule)


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
