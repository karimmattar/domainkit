"""Clients that are never entered run on one event loop per test.

Starlette's ``TestClient`` gives a client that was never entered a new
event loop for each request. domainkit keeps a loop's engines for that
loop, so each request opened a connection that nothing closed, and the
collector found it during a later test (``conftest.SharedLoop``).
"""

import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from domainkit_tests.conftest import Api, SharedLoop
from domainkit_tests.engine_events import OPENED
from domainkit_tests.support.auth import PASSWORD, create_user


def looping() -> tuple[FastAPI, list[asyncio.AbstractEventLoop]]:
    """An application that notes the loop each request runs on."""
    app = FastAPI()
    loops: list[asyncio.AbstractEventLoop] = []

    @app.get("/loop")
    async def note() -> dict[str, str]:
        loops.append(asyncio.get_running_loop())
        return {}

    return app, loops


def test_clients_never_entered_share_one_loop() -> None:
    """Two requests of one client, and another client's: the same loop."""
    app, loops = looping()
    client = TestClient(app)

    client.get("/loop")
    client.get("/loop")
    TestClient(app, base_url="https://testserver").get("/loop")

    assert loops[0] is loops[1] is loops[2]
    assert not loops[0].is_closed()


def test_an_entered_client_keeps_a_loop_of_its_own() -> None:
    """Its lifespan's loop serves it, not the shared one.

    Once it has been exited, Starlette sets its ``portal`` to None, which
    hides the shared loop from it: it is back to a loop for each request.
    Enter a client again, or make a new one, to send more.
    """
    app, loops = looping()
    client = TestClient(app)
    client.get("/loop")

    with client:
        client.get("/loop")
        client.get("/loop")
    client.get("/loop")

    shared, entered, again, after = loops
    assert entered is again
    assert entered is not shared
    assert not shared.is_closed()
    assert after not in (shared, entered)
    assert after.is_closed()


def test_the_shared_loops_connections_are_closed_with_it(api: Api) -> None:
    """What a client never entered opened is disposed when its loop ends."""
    entered = api()
    create_user("alice")
    patched: Any = TestClient
    loop, fixtures = SharedLoop(), patched.portal
    patched.portal = loop
    known = set(OPENED)
    try:
        answer = TestClient(entered.app).post(
            "/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        opened = set(OPENED) - known
    finally:
        patched.portal = fixtures
        loop.close()

    assert answer.status_code == 200
    assert opened  # The request ran on the shared loop, with engines of its own.
    assert not opened & set(OPENED)


def test_a_loop_no_client_used_is_never_started() -> None:
    """Most tests send no request: closing costs them nothing."""
    loop = SharedLoop()

    loop.close()
    loop.close()

    assert loop.__get__(None, TestClient) is loop
