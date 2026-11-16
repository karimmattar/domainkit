"""How a login was made: what its gates see, and what its credentials say.

ADR 0146 (user report A11): a gate was ``(request, user)`` and could not
tell a password from a passkey, nor ``Credentials`` a login's method, age,
or device. The gates keep their two arguments and read
``login_origin(request)``; credentials carry ``origin``.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import HTTPConnection, Request

from domainkit.clock import freeze_time
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.gates import LoginOrigin, login_origin, on_login_gate
from domainkit.contrib.auth.tables import tokens
from domainkit.contrib.auth.tokens import TokenKind
from domainkit.db import connections
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.support.auth import JWT, PASSWORD, bearer, create_user, login
from domainkit_tests.test_auth_cookies import SESSION, TRUSTED, browser, start
from domainkit_tests.test_auth_http import refresh
from domainkit_tests.test_auth_mfa import clock, code_at, enrol, finish, log_in
from domainkit_tests.test_auth_personal_tokens import make
from domainkit_tests.test_auth_principals import credentials_of

__all__ = ["clock"]  # The fixture, used here.

MADE = "2026-11-16T09:00:00Z"
ENROLLED = "2026-11-16T08:00:00Z"
"""Before ``MADE``: a login made then is older than those made at ``MADE``,
whatever the time is now."""


def recording(seen: list[tuple[str, LoginOrigin | None]]) -> Any:  # noqa: ANN401 - a gate.
    """A gate of two arguments, as every gate is, that records what it sees."""

    async def gate(request: Request, user: AbstractBaseUser) -> None:
        seen.append((request.url.path, login_origin(request)))

    return gate


def stored_amr() -> list[frozenset[str] | None]:
    """What each stored login says it proved, oldest first (not device proofs)."""

    async def read() -> list[frozenset[str] | None]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(tokens.c.amr)
                .where(tokens.c.kind != TokenKind.DEVICE)
                .order_by(tokens.c.created_at, tokens.c.id)
            )
            return [amr for (amr,) in rows]

    return run_async(read())


def at(moment: str) -> datetime:
    """``moment`` as the clock gives it."""
    with freeze_time(moment) as frozen:
        return frozen.now()


def test_a_gate_sees_how_each_password_login_is_made(identity: Api) -> None:
    """The password, when, and no device: at ``/login`` and ``/session`` alike."""
    create_user("alice")
    client = browser(identity)
    seen: list[tuple[str, LoginOrigin | None]] = []
    on_login_gate(recording(seen), domain="auth")

    with freeze_time(MADE):
        login(client, "alice")
        start(client)

    password = LoginOrigin("password", None, frozenset({"pwd"}), at(MADE))
    assert seen == [("/auth/login", password), ("/auth/session", password)]
    assert stored_amr() == [frozenset({"pwd"}), frozenset({"pwd"})]


def test_a_gate_sees_the_second_factor_once_its_code_holds(
    identity: Api, clock: list[float]
) -> None:
    """``/login`` proves the password; ``/mfa/login``, ``/session/mfa`` a code too."""
    create_user("alice")
    client = browser(identity)
    with freeze_time(ENROLLED):  # Not now: the oldest login whenever it runs.
        headers = bearer(login(client, "alice")["access_token"])
        secret, _ = enrol(client, headers, clock)
    seen: list[tuple[str, LoginOrigin | None]] = []
    on_login_gate(recording(seen), domain="auth")

    with freeze_time(MADE):
        challenge = log_in(client)
        clock[0] += 30
        tokens_answer = finish(
            client, challenge.json()["mfa_token"], code_at(secret, clock[0])
        )
        session_challenge = client.post(
            "/auth/session",
            json={"identifier": "alice", "password": PASSWORD},
            headers=TRUSTED,
        )
        clock[0] += 30
        session = client.post(
            "/auth/session/mfa",
            json={
                "mfa_token": session_challenge.json()["mfa_token"],
                "code": code_at(secret, clock[0]),
            },
            headers=TRUSTED,
        )

    assert tokens_answer.status_code == 200, tokens_answer.json()
    assert session.status_code == 200, session.json()
    two = frozenset({"pwd", "otp"})
    assert [(path, each and each.amr) for path, each in seen] == [
        ("/auth/login", frozenset({"pwd"})),
        ("/auth/mfa/login", two),
        ("/auth/session", frozenset({"pwd"})),
        ("/auth/session/mfa", two),
    ]
    assert stored_amr()[-2:] == [two, two]


def test_a_request_that_makes_no_login_has_no_origin() -> None:
    """Outside the gates there is nothing to see."""
    assert login_origin(HTTPConnection({"type": "http", "headers": []})) is None


def test_credentials_say_how_and_when_their_login_was_made(identity: Api) -> None:
    """A login token and a browser session: method, proof, and age; a script none."""
    create_user("alice")
    client = browser(identity)
    with freeze_time(MADE) as frozen:
        made = frozen.now()
        token = login(client, "alice")["access_token"]
        start(client)
        cookie = client.cookies[SESSION]
        script = make(client, bearer(token)).json()["token"]
        frozen.tick(timedelta(hours=3))

        by_token = run_async(credentials_of(headers=bearer(token)))
        by_cookie = run_async(credentials_of(headers={"Cookie": f"{SESSION}={cookie}"}))
        by_script = run_async(credentials_of(headers=bearer(script)))

    password = LoginOrigin("password", None, frozenset({"pwd"}), made)
    assert by_token is not None
    assert (by_token.scheme, by_token.origin) == ("token", password)
    assert by_cookie is not None
    assert (by_cookie.scheme, by_cookie.origin) == ("cookie", password)
    assert by_script is not None
    assert by_script.origin is None  # A personal token is no login.


def test_a_jwt_login_keeps_how_and_when_it_was_made_across_refreshes(
    api: Api,
) -> None:
    """Its access tokens name the login, whose first token says how and when."""
    create_user("alice")
    client: TestClient = api(**JWT)
    with freeze_time(MADE) as frozen:
        made = frozen.now()
        first = login(client, "alice")
        frozen.tick(timedelta(minutes=4))
        renewed = refresh(client, first["refresh_token"]).json()
        frozen.tick(timedelta(minutes=1))
        found = run_async(credentials_of(headers=bearer(renewed["access_token"])))

    assert found is not None
    assert found.scheme == "jwt"
    assert found.origin == LoginOrigin("password", None, frozenset({"pwd"}), made)
