"""What issues a credential on an earlier check holds the user's credentials lock.

A password change, turning a second factor on, and logging out end a
user's tokens under the lock; a login, a refresh, and a personal token
issue theirs under it too, after checking that what was checked still
holds (SEC-16a-6).
"""

import os
from typing import Any

import pytest

from domainkit.contrib.auth import AuthUnitOfWork, UserId, api, personal_tokens
from domainkit.contrib.auth.authentication import AuthenticationFailed, Credentials
from domainkit.contrib.auth.repositories import TokenStore
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import (
    JWT,
    PASSWORD,
    audited,
    bearer,
    create_user,
    login,
)
from domainkit_tests.test_auth_mfa import clock as clock
from domainkit_tests.test_auth_mfa import enrol

NEW_PASSWORD = "Zq8!wL-d4#Kp7v"


def loaded(username: str) -> Any:  # noqa: ANN401 - a user.
    """The user as a login's backend loaded them."""

    async def find() -> Any:  # noqa: ANN401 - a user.
        async with AuthUnitOfWork() as uow:
            return await uow.users.get_by_natural_key(username)

    return run_async(find())


def change_password(username: str) -> None:
    """Store a new password for ``username``, ending their tokens."""

    async def change() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            await uow.users.set_password(user, NEW_PASSWORD)
            await uow.tokens.revoke_user(user.id)
            await uow.commit()

    run_async(change())


def test_a_login_checked_before_a_password_change_is_not_issued(
    identity: Api,
) -> None:
    """The hash checked is no longer stored: 401, and no token."""
    create_user("alice")
    identity()
    checked = loaded("alice")
    change_password("alice")

    with pytest.raises(AuthenticationFailed) as refused:
        run_async(api.issue_login(checked, factor_proven=False))

    assert refused.value.detail == api.CREDENTIALS_CHANGED
    (row,) = audited("auth.login")
    assert row["details"] == {"reason": "credentials_changed"}
    assert run_async(api.issue_login(loaded("alice"), factor_proven=False))


def test_a_login_that_found_no_factor_is_not_issued_once_one_is_on(
    identity: Api,
    clock: list[float],
) -> None:
    """Without the factor proven, a factor turned on since refuses the login."""
    create_user("alice")
    client = identity()
    checked = loaded("alice")
    enrol(client, bearer(login(client, "alice")["access_token"]), clock)

    with pytest.raises(AuthenticationFailed):
        run_async(api.issue_login(loaded("alice"), factor_proven=False))
    assert run_async(api.issue_login(checked))  # The factor was proven.


def test_a_refresh_ended_while_it_waited_issues_nothing(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login ends after the refresh token is read, before its lock is held."""
    create_user("alice")
    client = identity(**JWT)
    tokens = login(client, "alice")
    original = TokenStore.lock_user
    ended: list[bool] = []

    async def ended_meanwhile(self: TokenStore, user_id: Any) -> None:  # noqa: ANN401
        if not ended:
            ended.append(True)
            async with AuthUnitOfWork() as other:
                await other.tokens.revoke_user(user_id)
                await other.commit()
        await original(self, user_id)

    monkeypatch.setattr(TokenStore, "lock_user", ended_meanwhile)
    refused = client.post(
        "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert (refused.status_code, refused.json()["detail"]) == (
        401,
        "Invalid refresh token.",
    )


def test_a_jwt_login_ended_meanwhile_makes_no_personal_token(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login (its family) is checked under the lock, as a token is."""
    create_user("alice")
    client = identity(**JWT)
    session = bearer(login(client, "alice")["access_token"])
    other = bearer(login(client, "alice")["access_token"])
    original = personal_tokens.check_password_again  # type: ignore[attr-defined]

    async def logged_out_meanwhile(*args: Any) -> None:  # noqa: ANN401 - as given.
        await original(*args)
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("alice")
            assert user is not None
            await uow.tokens.revoke_user(user.id)
            await uow.commit()

    monkeypatch.setattr(personal_tokens, "check_password_again", logged_out_meanwhile)
    made = client.post(
        "/auth/tokens", json={"name": "ci", "password": PASSWORD}, headers=session
    )

    assert (made.status_code, made.json()["detail"]) == (
        401,
        "Session ended; log in again.",
    )
    assert client.get("/auth/me", headers=other).status_code == 401


def test_credentials_no_token_holds_are_live(identity: Api) -> None:
    """A gateway's credentials hold no token row: nothing to have ended."""
    alice = create_user("alice")
    identity()

    async def held() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(UserId(alice))
            await personal_tokens.held_credentials(
                uow, Credentials(user, scheme="gateway")
            )

    run_async(held())


def test_a_bare_token_store_takes_no_lock_and_ends_unknown_logins(
    identity: Api,
) -> None:
    """Without a unit of work's lock, none is taken; a family nobody holds ends none."""
    from uuid import uuid4

    alice = create_user("alice")
    identity()

    async def bare() -> int:
        async with AuthUnitOfWork() as uow:
            store = TokenStore(uow.session)
            await store.lock_user(alice)
            return await uow.tokens.revoke_family(uuid4())

    assert run_async(bare()) == 0


ON_POSTGRESQL = bool(os.environ.get("DOMAINKIT_TEST_POSTGRES_URL"))


@pytest.mark.skipif(ON_POSTGRESQL, reason="SQLite's lock is the unit of work's")
def test_every_change_and_issue_holds_the_users_credentials_lock(
    identity: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login, refresh, a personal token, logout, a password change: each locks."""
    from domainkit.contrib.auth.repositories import CREDENTIALS_LOCK

    alice = create_user("alice")
    client = identity(**JWT)
    taken: list[str] = []
    original = AuthUnitOfWork.lock

    async def spied(self: AuthUnitOfWork, name: str, **options: Any) -> None:  # noqa: ANN401
        taken.append(name)
        await original(self, name, **options)

    monkeypatch.setattr(AuthUnitOfWork, "lock", spied)
    mine = CREDENTIALS_LOCK.format(user_id=alice)

    def locked(action: Any) -> list[str]:  # noqa: ANN401 - a call.
        taken.clear()
        action()
        return [name for name in taken if name == mine]

    tokens: dict[str, Any] = {}
    assert locked(lambda: tokens.update(login(client, "alice")))
    headers = bearer(tokens["access_token"])
    assert locked(
        lambda: client.post(
            "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
    )
    made = client.post(
        "/auth/tokens", json={"name": "ci", "password": PASSWORD}, headers=headers
    )
    assert made.status_code == 201  # A live JSON Web Token login makes one.
    assert locked(
        lambda: client.post(
            "/auth/tokens", json={"name": "ci2", "password": PASSWORD}, headers=headers
        )
    )
    assert locked(lambda: client.post("/auth/logout", headers=headers))
    other = bearer(login(client, "alice")["access_token"])
    change = {"old_password": PASSWORD, "new_password": NEW_PASSWORD}
    assert locked(lambda: client.post("/auth/password", json=change, headers=other))


@pytest.mark.skipif(
    not ON_POSTGRESQL, reason="set DOMAINKIT_TEST_POSTGRES_URL: row locks are its"
)
def test_the_lock_is_the_users_row_on_postgresql(identity: Api) -> None:
    """The user's row, and no other, until the transaction ends."""
    from sqlalchemy import select

    from domainkit.contrib.auth.users import get_user_table
    from domainkit.db import connections

    alice = create_user("alice")
    bob = create_user("bob")
    identity()
    table = get_user_table()

    async def contended() -> tuple[bool, bool]:
        async with AuthUnitOfWork() as uow:
            await uow.tokens.lock_user(alice)
            results = []
            for user_id in (alice, bob):
                async with connections.engine().connect() as other:
                    try:
                        await other.execute(
                            select(table.c.id)
                            .where(table.c.id == user_id)
                            .with_for_update(nowait=True)
                        )
                    except Exception:  # noqa: BLE001 - the driver's lock error.
                        results.append(True)
                    else:
                        results.append(False)
            return results[0], results[1]

    assert run_async(contended()) == (True, False)


def test_a_rehashed_password_still_logs_in(identity: Api) -> None:
    """The login is held to the hash it stored, not the one it read."""
    from domainkit_tests.conftest import FAST_HASHER

    create_user("alice")
    client = identity(
        PASSWORD_HASHERS=[
            "domainkit.contrib.auth.hashers.ScryptPasswordHasher",
            FAST_HASHER,
        ]
    )
    before = loaded("alice").password.value

    answer = client.post(
        "/auth/login", json={"username": "alice", "password": PASSWORD}
    )

    assert answer.status_code == 200, answer.json()
    assert loaded("alice").password.value != before  # Moved to the preferred one.


def test_a_live_jwt_login_makes_a_personal_token(identity: Api) -> None:
    """Its login (family) is the one looked for: no other token stands in for it."""
    create_user("alice")
    client = identity(**JWT, DEVICE_TTL=None)
    session = bearer(login(client, "alice")["access_token"])

    made = client.post(
        "/auth/tokens", json={"name": "ci", "password": PASSWORD}, headers=session
    )

    assert made.status_code == 201, made.json()
