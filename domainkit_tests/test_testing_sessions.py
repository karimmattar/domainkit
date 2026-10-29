"""Clients signed in with a browser session, without posting to /auth/session.

Litestar's ``set_session_data`` for domainkit's browser sessions: the
session ``POST /auth/session`` would start, in cookies, made without the
password. Until 0.13 a test logged in through ``/auth/session`` as a
browser does, which needs a password and, for a user with a second factor,
a code.
"""

from collections.abc import Iterator

import pytest

from domainkit.conf import override_settings
from domainkit.contrib.auth.testing import create_user
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.testing import AsyncTestClient, TestClient, browser_session_for
from domainkit_tests.conftest import IDENTITY, Migrated


@pytest.fixture
def identity_project(migrated: Migrated) -> Iterator[None]:
    """Auth with browser sessions (``CookieAuthentication``), migrated."""
    with migrated(IDENTITY):
        yield


@pytest.mark.usefixtures("identity_project")
def test_a_sync_client_is_signed_in_with_a_browser_session() -> None:
    """Reads and writes go through the session cookie, its CSRF token sent too."""
    with TestClient(get_asgi_application()) as client:
        ann = client.run(create_user, username="ann")
        browser = client.browser_session_as(ann)

        assert browser.get("/whoami").json() == {"user": "ann", "scheme": "cookie"}
        assert browser.get("/auth/session").status_code == 200
        written = browser.post("/notes")
        assert written.json() == {"user": "ann", "scheme": "cookie"}
        forged = client.another({"Cookie": browser.headers["Cookie"]})
        assert forged.post("/notes").json()["code"] == "csrf_failed"
        assert client.get("/whoami").json()["user"] != "ann"
        assert browser.portal is client.portal


@pytest.mark.anyio
@pytest.mark.usefixtures("identity_project")
async def test_an_async_client_is_signed_in_by_id() -> None:
    """``browser_session_as`` takes the user or its id; logging out ends it."""
    ann = await create_user(username="ann")
    async with AsyncTestClient(get_asgi_application()) as client:
        browser = await client.browser_session_as(ann.id)
        try:
            seen = await browser.get("/whoami")
            assert seen.json() == {"user": "ann", "scheme": "cookie"}
            assert (await browser.delete("/auth/session")).status_code == 204
            assert (await browser.get("/whoami")).json()["user"] != "ann"
        finally:
            await browser.aclose()


@pytest.mark.anyio
@pytest.mark.usefixtures("identity_project")
async def test_the_headers_are_a_browser_s() -> None:
    """The session cookie, the CSRF cookie, and the token in its header."""
    ann = await create_user(username="ann")
    headers = await browser_session_for(ann)

    assert set(headers) == {"Cookie", "X-CSRF-Token"}
    names = [part.split("=")[0] for part in headers["Cookie"].split("; ")]
    assert names == ["__Host-dk_session", "__Host-dk_csrf"]
    assert headers["Cookie"].endswith(f"__Host-dk_csrf={headers['X-CSRF-Token']}")


@pytest.mark.anyio
@pytest.mark.usefixtures("identity_project")
async def test_sessions_need_cookie_authentication() -> None:
    """Without ``CookieAuthentication`` no cookie would be read: refused."""
    ann = await create_user(username="ann")
    tokens_only = ["domainkit.contrib.auth.authentication.TokenAuthentication"]
    with (
        override_settings(
            AUTH={**IDENTITY.settings["AUTH"], "AUTHENTICATION_CLASSES": tokens_only}
        ),
        pytest.raises(ImproperlyConfigured, match=r"needs .*CookieAuthentication"),
    ):
        await browser_session_for(ann)


@pytest.mark.anyio
@pytest.mark.usefixtures("identity_project")
async def test_no_session_outside_the_test_settings() -> None:
    """As the test tokens (SEC-12a-2): no credentials, so only where tests run.

    Nothing else reaches ``issue_test_session``: it is no route's.
    """
    ann = await create_user(username="ann", superuser=True)
    with (
        override_settings(ENVIRONMENT="local"),
        pytest.raises(ImproperlyConfigured, match='needs ENVIRONMENT = "test"'),
    ):
        await browser_session_for(ann)


@pytest.mark.anyio
async def test_sessions_need_a_domain_that_starts_them() -> None:
    """Without ``domainkit.contrib.auth`` installed, nothing can start one."""
    with pytest.raises(ImproperlyConfigured, match="starts browser sessions"):
        await browser_session_for("someone")
