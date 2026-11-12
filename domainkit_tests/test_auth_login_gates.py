"""Login gates: every login route runs them, and a login that skips them fails."""

from typing import Any

import pytest
from fastapi import status
from starlette.requests import Request

from domainkit.contrib.auth import AbstractBaseUser, AuthUnitOfWork
from domainkit.contrib.auth.api import issue_login
from domainkit.contrib.auth.gates import (
    LoginNotGated,
    gates_passed,
    on_login_gate,
)
from domainkit.contrib.auth.models import UserId
from domainkit.http.errors import APIError
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import PASSWORD, create_user, stored_tokens
from domainkit_tests.test_auth_cookies import TRUSTED, browser
from domainkit_tests.test_auth_mfa import clock, code_at, enrol, finish, log_in

__all__ = ["clock"]  # The fixture, used here.

REFUSED = "gate_refused"


def refusing(seen: list[tuple[str, str]]) -> Any:  # noqa: ANN401 - a gate.
    """A gate that records each login it judges, then refuses it."""

    async def gate(request: Request, user: AbstractBaseUser) -> None:
        seen.append((request.url.path, user.get_username()))
        raise APIError(status.HTTP_403_FORBIDDEN, "Not from here.", code=REFUSED)

    return gate


def passing(seen: list[tuple[str, str]]) -> Any:  # noqa: ANN401 - a gate.
    """A gate that records each login it judges, and lets it through."""

    async def gate(request: Request, user: AbstractBaseUser) -> None:
        seen.append((request.url.path, user.get_username()))

    return gate


def test_a_gate_refuses_password_and_session_logins(identity: Api) -> None:
    """Refused before tokens, a session, or a challenge; nothing is stored."""
    alice = create_user("alice")
    client = browser(identity)
    seen: list[tuple[str, str]] = []
    on_login_gate(refusing(seen), domain="auth")

    answer = client.post(
        "/auth/login", json={"identifier": "alice", "password": PASSWORD}
    )
    assert answer.status_code == 403
    assert answer.json()["code"] == REFUSED
    session = client.post(
        "/auth/session",
        json={"identifier": "alice", "password": PASSWORD},
        headers=TRUSTED,
    )
    assert session.status_code == 403
    assert seen == [("/auth/login", "alice"), ("/auth/session", "alice")]
    assert stored_tokens(alice) == []

    wrong = client.post("/auth/login", json={"identifier": "alice", "password": "x"})
    assert wrong.status_code == 401  # The password is checked first.
    assert len(seen) == 2


def test_a_gate_judges_the_second_factor_step_too(
    identity: Api, clock: list[float]
) -> None:
    """A user with a factor is judged at /login and again at /mfa/login."""
    alice = create_user("alice")
    client = identity()
    headers = {
        "Authorization": "Bearer "
        + client.post(
            "/auth/login", json={"identifier": "alice", "password": PASSWORD}
        ).json()["access_token"]
    }
    secret, _ = enrol(client, headers, clock)
    seen: list[tuple[str, str]] = []
    on_login_gate(passing(seen), domain="auth")

    challenge = log_in(client)
    assert challenge.status_code == 202
    clock[0] += 30
    answer = finish(client, challenge.json()["mfa_token"], code_at(secret, clock[0]))
    assert answer.status_code == 200, answer.json()
    assert seen == [("/auth/login", "alice"), ("/auth/mfa/login", "alice")]
    assert len(stored_tokens(alice)) == 2  # The first login, and this one.


def test_a_passing_gate_lets_logins_and_sessions_through(identity: Api) -> None:
    """Both routes issue once the gate returns."""
    create_user("alice")
    client = browser(identity)
    seen: list[tuple[str, str]] = []
    on_login_gate(passing(seen), domain="auth")

    tokens = client.post(
        "/auth/login", json={"identifier": "alice", "password": PASSWORD}
    )
    assert tokens.status_code == 200
    session = client.post(
        "/auth/session",
        json={"identifier": "alice", "password": PASSWORD},
        headers=TRUSTED,
    )
    assert session.status_code == 200
    assert [path for path, _ in seen] == ["/auth/login", "/auth/session"]


async def _stored(user_id: Any) -> AbstractBaseUser:  # noqa: ANN401 - any id.
    """The user as stored."""
    async with AuthUnitOfWork() as uow:
        return await uow.users.get(UserId(user_id))


def test_a_login_that_skips_the_gates_fails_closed(identity: Api) -> None:
    """With a gate registered, issuing without passing it raises; the block allows."""
    identity()
    alice = run_async(_stored(create_user("alice")))
    assert run_async(issue_login(alice)).access_token  # No gate: as before.

    on_login_gate(passing([]), domain="auth")
    with pytest.raises(LoginNotGated, match="pass_login_gates"):
        run_async(issue_login(alice))
    with gates_passed(alice.id):
        assert run_async(issue_login(alice)).access_token
    with gates_passed(create_user("bob")), pytest.raises(LoginNotGated):
        run_async(issue_login(alice))  # Another user's pass is not hers.


def test_a_test_session_needs_no_gate(identity: Api) -> None:
    """``issue_test_session`` is a server-side login, outside the gates."""
    from domainkit.apps import registry
    from domainkit.contrib.auth.apps import AuthConfig

    identity()
    alice = create_user("alice")
    on_login_gate(refusing([]), domain="auth")
    config = registry.get_domain_config("auth")
    assert isinstance(config, AuthConfig)
    headers = run_async(config.issue_test_session(alice))
    assert "Cookie" in headers


def test_gates_take_a_request_and_a_user(identity: Api) -> None:
    """A gate of the wrong shape is refused when it registers."""
    from domainkit.exceptions import ImproperlyConfigured

    identity()

    async def one(request: Request) -> None:
        """Takes too few arguments."""

    with pytest.raises(ImproperlyConfigured, match="2 positional"):
        on_login_gate(one, domain="auth")  # type: ignore[arg-type]
