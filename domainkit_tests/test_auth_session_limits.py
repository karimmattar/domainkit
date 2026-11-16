"""Shorter sessions for some logins: an idle timeout and a lifetime of their own.

ADR 0146 (user report A11): ``IDLE_TIMEOUT``, ``COOKIE.TTL`` and
``TOKEN_TTL`` were one value for everyone, so a project could not end its
staff's sessions sooner without forking auth. An ``on_session_limits``
hook gives a login shorter limits when it is made, and the login keeps
them; everyone else keeps the settings'.
"""

from datetime import timedelta
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from domainkit.clock import freeze_time
from domainkit.contrib.auth import AbstractBaseUser, AuthUnitOfWork
from domainkit.contrib.auth.gates import LoginOrigin, SessionLimits, on_session_limits
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import (
    JWT,
    PASSWORD,
    bearer,
    create_user,
    login,
    stored_tokens,
)
from domainkit_tests.test_auth_cookies import SESSION, SITE, TRUSTED, set_cookies
from domainkit_tests.test_auth_http import refresh

MADE: Final = "2026-11-16T09:00:00Z"
STAFF: Final = SessionLimits(
    idle_timeout=timedelta(minutes=15), lifetime=timedelta(hours=8)
)
IDLE: Final = "Session ended after inactivity; log in again."


async def staff_sessions(
    user: AbstractBaseUser, origin: LoginOrigin
) -> SessionLimits | None:
    """Staff get 15 idle minutes and 8 hours; everyone else the settings'."""
    return STAFF if getattr(user, "is_staff", False) else None


def users() -> None:
    """Alice, staff; and Bob, who is not."""
    create_user("alice", is_staff=True)
    create_user("bob")


def whoami(client: TestClient, headers: dict[str, str] | None = None) -> Any:  # noqa: ANN401
    """``/whoami``'s answer."""
    return client.get("/whoami", headers=headers or {})


def stored_tokens_of(username: str) -> list[dict[str, Any]]:
    """The stored tokens of ``username``."""

    async def find() -> Any:  # noqa: ANN401 - a user id.
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            return user.id

    return stored_tokens(run_async(find()))


def test_staff_login_tokens_end_sooner_and_others_keep_the_settings(
    identity: Api,
) -> None:
    """Staff: 8 hours, and 15 minutes idle; Bob: ``TOKEN_TTL``, no idle timeout."""
    users()
    client = identity()
    on_session_limits(staff_sessions, domain="auth")
    with freeze_time(MADE) as clock:
        alice = login(client, "alice")
        bob = login(client, "bob")
        clock.tick(timedelta(minutes=17))
        idle = whoami(client, bearer(alice["access_token"]))
        kept = whoami(client, bearer(bob["access_token"]))

    assert (alice["expires_in"], bob["expires_in"]) == (8 * 3600, 14 * 86400)
    assert (idle.status_code, idle.json()["detail"]) == (401, IDLE)
    assert kept.json()["user"] == "bob"


def test_a_staff_login_in_use_ends_with_its_lifetime(identity: Api) -> None:
    """Used every ten minutes, it lasts its 8 hours, and not a minute more."""
    users()
    client = identity()
    on_session_limits(staff_sessions, domain="auth")
    with freeze_time(MADE) as clock:
        headers = bearer(login(client, "alice")["access_token"])
        for _ in range(47):
            assert whoami(client, headers).json()["user"] == "alice"
            clock.tick(timedelta(minutes=10))
        assert whoami(client, headers).json()["user"] == "alice"  # 7 h 50.
        clock.tick(timedelta(minutes=10))
        ended = whoami(client, headers)

    assert (ended.status_code, ended.json()["detail"]) == (401, "Token expired.")


@pytest.mark.parametrize("by", ["token", "cookie"])
def test_a_staff_login_writes_its_use_every_tenth_of_its_own_timeout(
    identity: Api, by: str
) -> None:
    """Without ``IDLE_TIMEOUT``: used 4.9 minutes in, it lasts 16.5 minutes more."""
    users()
    client = identity()
    on_session_limits(staff_sessions, domain="auth")
    with freeze_time(MADE) as clock:
        browsing = TestClient(client.app, base_url=SITE)
        if by == "token":
            headers = bearer(login(browsing, "alice")["access_token"])
        else:
            headers = {}
            started = browsing.post(
                "/auth/session",
                json={"identifier": "alice", "password": PASSWORD},
                headers=TRUSTED,
            )
            assert started.status_code == 200, started.json()
        for step in (timedelta(0), timedelta(minutes=1), timedelta(minutes=3.9)):
            clock.tick(step)
            assert whoami(browsing, headers).json()["user"] == "alice"
        clock.tick(timedelta(minutes=16, seconds=20))
        assert whoami(browsing, headers).json()["user"] == "alice"
        clock.tick(timedelta(minutes=16, seconds=40))
        ended = whoami(browsing, headers)

    if by == "token":
        assert (ended.status_code, ended.json()["detail"]) == (401, IDLE)
    else:  # An idle cookie leaves the request anonymous.
        assert ended.json()["user"] == ""


def test_a_staff_browser_sessions_cookies_last_as_long_as_it_does(
    identity: Api,
) -> None:
    """``Max-Age`` is the login's 8 hours, not ``COOKIE.TTL``'s 14 days."""
    users()
    client = identity()
    on_session_limits(staff_sessions, domain="auth")
    ages = {}
    for username in ("alice", "bob"):
        browsing = TestClient(client.app, base_url=SITE)
        started = browsing.post(
            "/auth/session",
            json={"identifier": username, "password": PASSWORD},
            headers=TRUSTED,
        )
        assert started.status_code == 200, started.json()
        cookies = set_cookies(started)
        ages[username] = [
            part.strip()
            for name in (SESSION, "__Host-dk_csrf")
            for part in cookies[name].split(";")
            if part.strip().lower().startswith("max-age")
        ]

    assert ages == {
        "alice": ["Max-Age=28800", "Max-Age=28800"],
        "bob": ["Max-Age=1209600", "Max-Age=1209600"],
    }


def test_a_staff_jwt_login_refreshes_within_its_limits(api: Api) -> None:
    """Its refresh tokens end with its 8 hours; idle 15 minutes, it cannot refresh.

    An access token issued longer ago than its idle timeout no longer works,
    whatever ``ACCESS_TOKEN_TTL`` says; Bob's still does.
    """
    users()
    hour = {**JWT["JWT"], "ACCESS_TOKEN_TTL": timedelta(hours=1)}
    client = api(**{**JWT, "JWT": hour})
    on_session_limits(staff_sessions, domain="auth")
    with freeze_time(MADE) as clock:
        alice = login(client, "alice")
        bob = login(client, "bob")
        clock.tick(timedelta(minutes=10))
        renewed = refresh(client, alice["refresh_token"])
        assert renewed.status_code == 200, renewed.json()
        clock.tick(timedelta(minutes=17))
        stale = whoami(client, bearer(renewed.json()["access_token"]))
        kept = whoami(client, bearer(bob["access_token"]))
        idle = refresh(client, renewed.json()["refresh_token"])

    assert (stale.status_code, stale.json()["detail"]) == (401, IDLE)
    assert kept.json()["user"] == "bob"
    assert (idle.status_code, idle.json()["detail"]) == (401, IDLE)


def test_a_refreshed_staff_login_keeps_its_end_and_idle_timeout(api: Api) -> None:
    """The new refresh token ends when the first would have, and idles as fast."""
    users()
    client = api(**JWT)
    on_session_limits(staff_sessions, domain="auth")
    with freeze_time(MADE) as clock:
        first = login(client, "alice")
        clock.tick(timedelta(minutes=10))
        refresh(client, first["refresh_token"])
        rows = sorted(stored_tokens_of("alice"), key=lambda row: row["created_at"])
        made = clock.now() - timedelta(minutes=10)

    assert [(row["expires_at"], row["idle_timeout"]) for row in rows] == [
        (made + timedelta(hours=8), 900),
        (made + timedelta(hours=8), 900),
    ]


def test_hooks_see_the_user_and_the_login_and_the_shortest_limit_holds(
    identity: Api,
) -> None:
    """Each hook is asked; the shortest of each limit holds; None asks nothing."""
    users()
    client = identity()
    asked: list[tuple[str, LoginOrigin]] = []

    async def short_idle(
        user: AbstractBaseUser, origin: LoginOrigin
    ) -> SessionLimits | None:
        asked.append((user.get_username(), origin))
        return SessionLimits(idle_timeout=timedelta(minutes=10))

    async def short_life(
        user: AbstractBaseUser, origin: LoginOrigin
    ) -> SessionLimits | None:
        return SessionLimits(
            idle_timeout=timedelta(hours=1), lifetime=timedelta(hours=2)
        )

    async def none(user: AbstractBaseUser, origin: LoginOrigin) -> None:
        return None

    for hook in (short_idle, short_life, none):
        on_session_limits(hook, domain="auth")
    with freeze_time(MADE) as clock:
        tokens = login(client, "bob")
        made = clock.now()

    assert tokens["expires_in"] == 2 * 3600
    [row] = stored_tokens_of("bob")
    assert row["idle_timeout"] == 600
    [(who, origin)] = asked
    assert who == "bob"
    assert origin == LoginOrigin("password", None, frozenset({"pwd"}), made)


def test_a_limit_longer_than_the_setting_changes_nothing(identity: Api) -> None:
    """``IDLE_TIMEOUT`` of 15 minutes holds against a hook's hour."""
    users()
    client = identity(IDLE_TIMEOUT=timedelta(minutes=15), TOKEN_TTL=timedelta(hours=1))

    async def generous(
        user: AbstractBaseUser, origin: LoginOrigin
    ) -> SessionLimits | None:
        return SessionLimits(
            idle_timeout=timedelta(hours=1), lifetime=timedelta(days=2)
        )

    on_session_limits(generous, domain="auth")
    with freeze_time(MADE) as clock:
        tokens = login(client, "bob")
        clock.tick(timedelta(minutes=17))
        idle = whoami(client, bearer(tokens["access_token"]))

    assert tokens["expires_in"] == 3600
    assert (idle.status_code, idle.json()["detail"]) == (401, IDLE)


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"idle_timeout": timedelta(minutes=4)}, "idle_timeout"),
        ({"idle_timeout": timedelta(days=31)}, "idle_timeout"),
        ({"lifetime": timedelta(0)}, "lifetime"),
    ],
)
def test_limits_are_bounded(limits: dict[str, timedelta], message: str) -> None:
    """Idle from 5 minutes to 30 days, as ``IDLE_TIMEOUT``; a positive lifetime."""
    with pytest.raises(ValueError, match=message):
        SessionLimits(**limits)


def test_limits_combine_to_the_shortest_of_each() -> None:
    """``within``: the shorter idle timeout, the shorter lifetime; unset is none."""
    hour, day = timedelta(hours=1), timedelta(days=1)
    assert SessionLimits(hour, None).within(SessionLimits(day, day)) == SessionLimits(
        hour, day
    )
    assert SessionLimits().within(SessionLimits()) == SessionLimits()
