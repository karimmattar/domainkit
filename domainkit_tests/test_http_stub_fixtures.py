"""A stub set in a fixture reaches the test, whatever its context (user report U5).

A plain ``stub`` lives in a context variable: AnyIO runs async fixtures and
async tests in one task, so they share it, but a sync test, or an async
one after a sync fixture set it, runs in another context and never sees
it. ``everywhere`` holds for every call of the process until the block
ends, as respx and pytest-httpx patch the transport for a test.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack

import httpx2
import pytest
from fastapi import FastAPI

from domainkit.http.access import Public
from domainkit.outbound import client as client_module
from domainkit.outbound.client import HttpClient, stub
from domainkit.testing import AsyncTestClient, TestClient

RATES = HttpClient(base_url="https://rates.invalid", retries=0)
"""Never reached: every test here stubs it."""

app = FastAPI()


@app.get("/rate", dependencies=[Public])
async def rate() -> dict[str, str]:
    """What the rates service answers, as the application sees it."""
    return dict((await RATES.get("/eur")).json())


def answer(text: str) -> httpx2.MockTransport:
    """A stub that answers ``{"rate": text}``."""
    return httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={"rate": text})
    )


@pytest.fixture
async def stubbed_async() -> AsyncIterator[None]:
    """An async fixture's stub: its context is the async runner's."""
    with stub(answer("async fixture"), everywhere=True):
        yield


@pytest.fixture
def stubbed_sync() -> Iterator[None]:
    """A sync fixture's stub: its context is the test thread's."""
    with stub(answer("sync fixture"), everywhere=True):
        yield


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncTestClient]:
    """The application in the async runner's task, entered before the stub."""
    async with AsyncTestClient(app) as client:
        yield client


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
def test_an_async_fixtures_stub_reaches_a_sync_tests_application(
    anyio_backend: str, stubbed_async: None
) -> None:
    """The test thread holds no stub of its own; the application's call is stubbed."""
    assert client_module._stub.get() is None
    with TestClient(app) as client:
        assert client.get("/rate").json() == {"rate": "async fixture"}


@pytest.mark.anyio
async def test_a_sync_fixtures_stub_reaches_a_runner_started_before_it(
    async_client: AsyncTestClient, stubbed_sync: None
) -> None:
    """The async runner's task was made before the sync fixture set the stub."""
    assert client_module._stub.get() is None
    assert (await async_client.get("/rate")).json() == {"rate": "sync fixture"}


def test_a_plain_stub_wins_and_blocks_end_in_any_order() -> None:
    """A context's own stub first; then the last ``everywhere`` still open."""

    def called() -> str:
        return str(asyncio.run(RATES.get("/eur")).json()["rate"])

    seen: list[str] = []
    with ExitStack() as first:
        first.enter_context(stub(answer("first"), everywhere=True))
        second = stub(answer("second"), everywhere=True)
        second.__enter__()
        with stub(answer("own")):
            seen.append(called())
        thread = threading.Thread(target=lambda: seen.append(called()))
        thread.start()
        thread.join()
        first.close()  # The first ends while the second is open.
        seen.append(called())
        second.__exit__(None, None, None)

    assert seen == ["own", "second", "second"]
    assert client_module._shared_stub is None
