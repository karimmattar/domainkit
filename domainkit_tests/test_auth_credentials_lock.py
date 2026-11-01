"""What issues a credential on an earlier check holds the user's credentials lock.

A password change, turning a second factor on, and logging out end a
user's tokens under the lock; a login, a refresh, and a personal token
issue theirs under it too, after checking that what was checked still
holds (SEC-16a-6).
"""

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
