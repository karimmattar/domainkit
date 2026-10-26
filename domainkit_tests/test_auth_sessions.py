"""Session hygiene: re-logins, bounds, the session list, and idle logins.

A new login ends the browser's last session (AUTH-05); logins are bounded
(AUTH-08); users list and end their sessions; idle logins end.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from domainkit.clock import freeze_time
from domainkit.contrib.auth import AuthUnitOfWork
from domainkit.contrib.auth.authentication import (
    TOKEN_SCHEME,
    stored_credential,
    unless_idle,
)
from domainkit.contrib.auth.principals import Entry
from domainkit.contrib.auth.settings import auth_settings
from domainkit.contrib.auth.tokens import (
    TokenKind,
    idle_since,
    is_login,
    last_used_every,
    token_digest,
)
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.test_auth_cookies import (
    SESSION,
    SITE,
    TRUSTED,
    browser,
    start,
)
from domainkit_tests.test_auth_http import PASSWORD, bearer, create_user, login
from domainkit_tests.test_auth_personal_tokens import make, user_session
from domainkit_tests.test_auth_principals import issue, stored_last_used

JWT: Final = {
    "AUTHENTICATION_CLASSES": [
        "domainkit.contrib.auth.authentication.JWTAuthentication",
        "domainkit.contrib.auth.authentication.TokenAuthentication",
        "domainkit.contrib.auth.cookies.CookieAuthentication",
    ],
    "JWT": {"SIGNING_KEY": "k" * 32},
}


def kinds(client: TestClient, headers: dict[str, str] | None = None) -> list[str]:
    """The kinds of the user's tokens, as ``GET /auth/tokens`` lists them."""
    items = client.get("/auth/tokens", headers=headers or {}).json()["items"]
    return sorted(item["kind"] for item in items)


def who(client: TestClient, session: str) -> Any:  # noqa: ANN401 - a user or None.
    """Who a browser holding only the session cookie ``session`` is ("": nobody)."""
    other = TestClient(client.app, base_url=SITE, cookies={SESSION: session})
    return other.get("/whoami").json()["user"]


def test_a_new_login_ends_the_browsers_previous_session(identity: Api) -> None:
    """The PoC of AUTH-05: two logins in one browser used to leave two sessions.

    The browser replaces its cookie; a copy of the first went on working for
    14 days. Now the first ends, whoever logs in the second time.
    """
    create_user("alice")
    create_user("bob")
    client = browser(identity)
    csrf = start(client)
    first = client.cookies[SESSION]

    again = client.post(
        "/auth/session",
        json={"username": "bob", "password": PASSWORD},
        headers={**TRUSTED, "X-CSRF-Token": csrf},
    )

    assert again.status_code == 200
    assert who(client, first) == ""
    assert who(client, client.cookies[SESSION]) == "bob"
    assert kinds(client) == ["session"]


def test_the_previous_session_can_be_kept(identity: Api) -> None:
    """``COOKIE.END_PREVIOUS_SESSION = False``: as before 0.10."""
    create_user("alice")
    client = browser(identity, COOKIE={"END_PREVIOUS_SESSION": False})
    csrf = start(client)
    first = client.cookies[SESSION]
    client.post(
        "/auth/session",
        json={"username": "alice", "password": PASSWORD},
        headers={**TRUSTED, "X-CSRF-Token": csrf},
    )

    assert who(client, first) == "alice"
    assert kinds(client) == ["session", "session"]


def test_logins_past_the_bound_end_the_oldest(identity: Api) -> None:
    """The PoC of AUTH-08: logins used to pile up, one row each, for 14 days.

    ``MAX_LOGINS`` logins at most, of every kind; personal tokens neither
    count nor end.
    """
    create_user("alice")
    client = identity(MAX_LOGINS=3)
    script = make(client, user_session(client)).json()["token"]
    tokens = [login(client, "alice")["access_token"] for _ in range(4)]
    session = TestClient(client.app, base_url=SITE)
    start(session)

    live = [client.get("/whoami", headers=bearer(each)).status_code for each in tokens]
    assert live == [401, 401, 200, 200]
    assert client.get("/whoami", headers=bearer(script)).json()["user"] == "alice"
    assert kinds(client, bearer(script)) == ["access", "access", "access", "session"]


def test_jwt_logins_count_as_one_each_and_end_whole(identity: Api) -> None:
    """A JSON Web Token login is its refresh tokens: refreshing adds none."""
    create_user("alice")
    client = identity(MAX_LOGINS=2, **JWT)
    first = login(client, "alice")
    refreshed = client.post(
        "/auth/refresh", json={"refresh_token": first["refresh_token"]}
    ).json()
    second = login(client, "alice")
    third = login(client, "alice")

    ended = client.post(
        "/auth/refresh", json={"refresh_token": refreshed["refresh_token"]}
    )
    assert ended.status_code == 401
    gone = client.get("/whoami", headers=bearer(refreshed["access_token"]))
    assert gone.status_code == 401
    for kept in (second, third):
        seen = client.get("/whoami", headers=bearer(kept["access_token"]))
        assert seen.json()["user"] == "alice"


def test_users_list_their_sessions_and_end_one(identity: Api) -> None:
    """``GET /auth/tokens`` is the session list; ``DELETE`` ends one, elsewhere too."""
    create_user("alice")
    laptop = browser(identity)
    start(laptop)
    phone = TestClient(laptop.app, base_url=SITE)
    start(phone)
    listed = laptop.get("/auth/tokens").json()["items"]
    other = next(item for item in listed if not item["current"])

    assert sorted((item["kind"], item["current"]) for item in listed) == [
        ("session", False),
        ("session", True),
    ]
    ended = laptop.delete(f"/auth/tokens/{other['id']}", headers={**TRUSTED})
    assert ended.status_code == 403  # A cookie request needs its CSRF token.
    csrf = laptop.get("/auth/session").json()["csrf_token"]
    ended = laptop.delete(
        f"/auth/tokens/{other['id']}", headers={**TRUSTED, "X-CSRF-Token": csrf}
    )
    assert ended.status_code == 204
    assert phone.get("/whoami").json()["user"] == ""
    assert laptop.get("/whoami").json()["user"] == "alice"


def test_idle_logins_end_and_active_ones_last(identity: Api) -> None:
    """``IDLE_TIMEOUT``: a login unused that long ends, within a tenth after it.

    Its use is written every tenth of the timeout, so one used just before
    lasts; personal tokens have no idle timeout.
    """
    create_user("alice")
    client = identity(IDLE_TIMEOUT=timedelta(minutes=15))
    assert last_used_every() == timedelta(seconds=90)
    with freeze_time("2026-10-26T10:00:00Z") as clock:
        active = user_session(client)
        idle = user_session(client)
        script = make(client, active).json()["token"]
        for _ in range(20):  # Ten minutes a step: well past the timeout in all.
            assert client.get("/whoami", headers=active).json()["user"] == "alice"
            clock.tick(timedelta(minutes=10))
        refused = client.get("/whoami", headers=idle)
        kept = client.get("/whoami", headers=bearer(script))

    assert (refused.status_code, refused.json()["detail"]) == (
        401,
        "Session ended after inactivity; log in again.",
    )
    assert kept.json()["user"] == "alice"


def test_a_login_ends_no_sooner_than_its_timeout(identity: Api) -> None:
    """Never before ``IDLE_TIMEOUT``, and by a tenth of it after."""
    create_user("alice")
    client = identity(IDLE_TIMEOUT=timedelta(minutes=15))
    with freeze_time("2026-10-26T10:00:00Z") as clock:
        early = user_session(client)
        late = user_session(client)
        client.get("/whoami", headers=early)
        client.get("/whoami", headers=late)
        clock.tick(timedelta(minutes=15))
        assert client.get("/whoami", headers=early).json()["user"] == "alice"
        clock.tick(timedelta(minutes=16, seconds=30))
        assert client.get("/whoami", headers=late).status_code == 401


def test_another_processes_use_keeps_a_kept_login_alive(identity: Api) -> None:
    """A login this process last saw long ago is read again before it ends."""
    create_user("alice")
    client = identity(
        IDLE_TIMEOUT=timedelta(minutes=15),
        PRINCIPAL_CACHE={"TTL": timedelta(minutes=5)},
    )
    with freeze_time("2026-10-26T10:00:00Z") as clock:
        headers = user_session(client)
        raw = headers["Authorization"].removeprefix("Bearer ")
        assert client.get("/whoami", headers=headers).json()["user"] == "alice"
        clock.tick(timedelta(minutes=17))

        async def used_elsewhere() -> None:
            async with AuthUnitOfWork() as uow:
                token = await uow.tokens.find(raw)
                assert token is not None
                await uow.tokens.touch_digest(token.id, clock.now())
                await uow.commit()

        run_async(used_elsewhere())
        assert stored_last_used(raw) == clock.now()
        clock.tick(timedelta(seconds=1))
        assert client.get("/whoami", headers=headers).json()["user"] == "alice"


def test_idle_browser_sessions_and_jwt_logins_end(identity: Api) -> None:
    """An idle cookie leaves the request anonymous; an idle refresh is refused."""
    create_user("alice")
    client = identity(IDLE_TIMEOUT=timedelta(minutes=15), **JWT)
    with freeze_time("2026-10-26T10:00:00Z") as clock:
        session = TestClient(client.app, base_url=SITE)
        start(session)
        tokens = login(client, "alice")
        assert session.get("/whoami").json()["user"] == "alice"  # Not idle yet.
        clock.tick(timedelta(minutes=17))
        assert session.get("/whoami").json()["user"] == ""
        refused = client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

    assert refused.status_code == 401
    assert refused.json()["detail"] == "Session ended after inactivity; log in again."


@pytest.mark.parametrize("timeout", [timedelta(minutes=4), timedelta(days=31)])
def test_the_idle_timeout_is_bounded(identity: Api, timeout: timedelta) -> None:
    """From 5 minutes to 30 days."""
    with pytest.raises(Exception, match="IDLE_TIMEOUT"):
        identity(IDLE_TIMEOUT=timeout)


def test_a_kept_login_that_looks_idle_is_read_again(identity: Api) -> None:
    """``unless_idle``: what this process saw is used when it shows a recent use.

    A kept entry that looks idle is read afresh, and ends only if the
    stored login is idle too; one that does not look idle is used as is.
    """
    create_user("alice")
    identity(IDLE_TIMEOUT=timedelta(minutes=15))
    config = auth_settings().PRINCIPAL_CACHE
    with freeze_time("2026-10-26T10:00:00Z") as clock:
        raw = run_async(issue("alice", kind=TokenKind.SESSION))  # A login's.
        digest = token_digest(raw)
        entry = run_async(stored_credential(TOKEN_SCHEME, digest, config))
        assert entry is not None
        assert entry.credential is not None
        clock.tick(timedelta(hours=1))
        stale = replace(entry.credential, last_used_at=clock.now())
        recent = Entry(entry.key, None, credential=stale, last_used=None)
        assert run_async(unless_idle(recent, TOKEN_SCHEME, digest, config)) is recent
        written = Entry(entry.key, None, credential=entry.credential)
        written.last_used = clock.now()
        assert run_async(unless_idle(written, TOKEN_SCHEME, digest, config)) is written

        assert run_async(unless_idle(entry, TOKEN_SCHEME, digest, config)) is None
        run_async(touch_now(digest, clock.now()))
        fresh = run_async(unless_idle(entry, TOKEN_SCHEME, digest, config))
        assert fresh is not None
        assert fresh is not entry
        assert fresh.credential is not None
        assert fresh.credential.last_used_at == clock.now()
        assert run_async(unless_idle(Entry(entry.key, None), "token", digest, config))


def test_idle_ends_exactly_a_tenth_after_the_timeout(use_auth: UseAuth) -> None:
    """At ``IDLE_TIMEOUT`` plus the interval of writes, not a microsecond before."""
    use_auth(IDLE_TIMEOUT=timedelta(minutes=15))
    last = datetime(2026, 10, 26, 10, tzinfo=UTC)
    limit = last + timedelta(minutes=16, seconds=30)
    assert idle_since(last, limit)
    assert not idle_since(last, limit - timedelta(microseconds=1))
    assert is_login(TokenKind.SESSION, "browser")
    assert is_login(TokenKind.ACCESS, "login")
    assert is_login(TokenKind.REFRESH, "login")
    assert not is_login(TokenKind.ACCESS, "ci")
    assert not is_login(TokenKind.CERTIFICATE, "login")


async def touch_now(digest: str, now: datetime) -> None:
    """Write ``now`` as the token's last use, as another process would."""
    async with AuthUnitOfWork() as uow:
        await uow.tokens.touch_digest(digest, now)
        await uow.commit()
