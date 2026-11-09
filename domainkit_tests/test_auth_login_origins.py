"""Where each login came from, logging out the others, and who manages logins.

ADR 0130: a login's row keeps how it signed in, the browser or app, and
the address; ``logout`` ends the other logins; ``on_manage_logins`` checks
who may list and end them.
"""

from typing import Any
from uuid import UUID

from fastapi import Request, status
from sqlalchemy import select

from domainkit.apps.hooks import on_export_subject
from domainkit.contrib.auth import AbstractBaseUser
from domainkit.contrib.auth.gates import on_manage_logins
from domainkit.contrib.auth.tables import tokens
from domainkit.contrib.auth.tokens import AGENT_MAX_LENGTH
from domainkit.db import connections
from domainkit.http.errors import APIError
from domainkit.management.base import run_async
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import (
    JWT,
    audited,
    bearer,
    create_user,
    login,
    refresh,
    stored_tokens,
)
from domainkit_tests.test_auth_personal_tokens import make

ADDRESS = ("203.0.113.7", 50000)
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0"
REFUSED = "not_this_login"


def origins() -> list[tuple[str | None, UUID | None]]:
    """Each stored token's sign-in method and device, oldest first."""

    async def read() -> list[tuple[str | None, UUID | None]]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(tokens.c.method, tokens.c.device_id).order_by(
                    tokens.c.created_at, tokens.c.id
                )
            )
            return [(method, device) for method, device in rows]

    return run_async(read())


def listed(client: Any, headers: dict[str, str]) -> list[dict[str, Any]]:  # noqa: ANN401
    """The user's tokens as ``GET /auth/tokens`` answers them."""
    answer = client.get("/auth/tokens", headers=headers)
    assert answer.status_code == 200, answer.json()
    items: list[dict[str, Any]] = answer.json()["items"]
    return items


def test_a_login_lists_how_it_signed_in_with_what_and_from_where(
    identity: Api,
) -> None:
    """The method, the agent, and the address of the request that logged in."""
    alice = create_user("alice")
    client = identity(client=ADDRESS)
    client.headers["User-Agent"] = AGENT
    headers = bearer(login(client, "alice")["access_token"])

    [item] = listed(client, headers)

    assert {key: item[key] for key in ("method", "agent", "ip", "device_id")} == {
        "method": "password",
        "agent": AGENT,
        "ip": "203.0.113.7",
        "device_id": None,
    }
    assert item["current"] is True
    [row] = stored_tokens(alice)
    assert (row["method"], row["agent"], row["ip"]) == ("password", AGENT, ADDRESS[0])


def test_a_long_agent_is_cut_and_a_personal_token_records_nothing(
    identity: Api,
) -> None:
    """The agent keeps its first 200 characters; a personal token is not a login."""
    create_user("alice")
    client = identity()
    client.headers["User-Agent"] = "x" * 500
    headers = bearer(login(client, "alice")["access_token"])
    assert make(client, headers).status_code == 201

    personal, session = listed(client, headers)

    assert session["agent"] == "x" * AGENT_MAX_LENGTH
    assert personal["name"] == "ci"
    assert [personal[key] for key in ("method", "agent", "ip", "device_id")] == [
        None,
        None,
        None,
        None,
    ]


def test_a_refreshed_login_keeps_where_it_came_from(api: Api) -> None:
    """The new refresh token copies the origin, not the refreshing request's."""
    alice = create_user("alice")
    client = api(client=ADDRESS, **JWT)
    client.headers["User-Agent"] = AGENT
    first = login(client, "alice")["refresh_token"]

    elsewhere = api(client=("198.51.100.9", 50000))
    elsewhere.headers["User-Agent"] = "curl/8"
    answer = refresh(elsewhere, first)
    assert answer.status_code == 200, answer.json()

    rows = stored_tokens(alice)
    assert len(rows) == 2
    assert {(row["method"], row["agent"], row["ip"]) for row in rows} == {
        ("password", AGENT, ADDRESS[0])
    }


def test_logging_out_the_others_keeps_this_login_and_personal_tokens(
    identity: Api,
) -> None:
    """Every other login ends; this one and the personal tokens stay."""
    create_user("alice")
    client = identity()
    phone, web, tablet = (
        bearer(login(client, "alice")["access_token"]) for _ in range(3)
    )
    personal = bearer(make(client, phone).json()["token"])

    answer = client.post("/auth/logout", headers=phone, json={"others": True})

    assert answer.status_code == 204
    assert client.get("/auth/me", headers=web).status_code == 401
    assert client.get("/auth/me", headers=tablet).status_code == 401
    assert client.get("/auth/me", headers=phone).status_code == 200
    assert client.get("/auth/me", headers=personal).status_code == 200
    assert [item["name"] for item in listed(client, phone)] == ["ci", "login"]
    [logged_out] = audited("auth.user_logged_out")
    assert (logged_out["details"]["everywhere"], logged_out["details"]["others"]) == (
        False,
        True,
    )


def test_a_refresh_login_logging_out_the_others_keeps_its_family(api: Api) -> None:
    """A JSON Web Token login keeps its refresh token; the other logins end."""
    alice = create_user("alice")
    client = api(**JWT)
    kept = login(client, "alice")
    ended = login(client, "alice")

    answer = client.post(
        "/auth/logout", headers=bearer(kept["access_token"]), json={"others": True}
    )

    assert answer.status_code == 204
    assert refresh(client, ended["refresh_token"]).status_code == 401
    assert refresh(client, kept["refresh_token"]).status_code == 200
    assert len(stored_tokens(alice)) == 2  # The used refresh token, and its next.


def test_everywhere_and_others_together_or_from_a_scoped_token_are_refused(
    identity: Api,
) -> None:
    """One or the other; a token limited to scopes ends only itself."""
    create_user("alice")
    client = identity()
    headers = bearer(login(client, "alice")["access_token"])
    scoped = bearer(make(client, headers, scopes=[]).json()["token"])

    both = client.post(
        "/auth/logout", headers=headers, json={"everywhere": True, "others": True}
    )
    narrow = client.post("/auth/logout", headers=scoped, json={"others": True})

    assert both.status_code == 422
    assert narrow.status_code == 403
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_a_manage_logins_check_guards_listing_and_ending_other_logins(
    identity: Api,
) -> None:
    """Listing, ending another login, everywhere, and others are checked."""
    create_user("alice")
    client = identity()
    other = login(client, "alice")["access_token"]
    headers = bearer(login(client, "alice")["access_token"])
    ids = {item["current"]: item["id"] for item in listed(client, headers)}
    seen: list[tuple[str, str]] = []

    async def refuse(request: Request, user: AbstractBaseUser) -> None:
        seen.append((request.method, user.get_username()))
        raise APIError(status.HTTP_403_FORBIDDEN, "Not here.", code=REFUSED)

    on_manage_logins(refuse, domain="auth")

    answers = [
        client.get("/auth/tokens", headers=headers),
        client.delete(f"/auth/tokens/{ids[False]}", headers=headers),
        client.post("/auth/logout", headers=headers, json={"everywhere": True}),
        client.post("/auth/logout", headers=headers, json={"others": True}),
    ]

    assert [answer.status_code for answer in answers] == [403] * 4
    assert {answer.json()["code"] for answer in answers} == {REFUSED}
    assert seen == [
        ("GET", "alice"),
        ("DELETE", "alice"),
        ("POST", "alice"),
        ("POST", "alice"),
    ]
    assert client.get("/auth/me", headers=bearer(other)).status_code == 200


def test_a_manage_logins_check_never_stops_a_user_ending_their_own_login(
    identity: Api,
) -> None:
    """Logging out, or ending the request's own login by id, is not checked."""
    create_user("alice")
    client = identity()
    first = bearer(login(client, "alice")["access_token"])
    second = bearer(login(client, "alice")["access_token"])
    [own] = [item["id"] for item in listed(client, second) if item["current"]]
    seen: list[str] = []

    async def refuse(request: Request, user: AbstractBaseUser) -> None:
        seen.append(request.url.path)
        raise APIError(status.HTTP_403_FORBIDDEN, "Not here.", code=REFUSED)

    on_manage_logins(refuse, domain="auth")

    assert client.post("/auth/logout", headers=first).status_code == 204
    assert client.delete(f"/auth/tokens/{own}", headers=second).status_code == 204
    assert seen == []
    assert client.get("/auth/me", headers=first).status_code == 401
    assert client.get("/auth/me", headers=second).status_code == 401


def test_privacy_exports_each_logins_origin_but_never_its_digest(
    identity: Api,
) -> None:
    """Auth's ``privacy export`` hook lists the user's tokens and their origins."""
    alice = create_user("alice")
    create_user("bob")
    client = identity(client=ADDRESS)
    client.headers["User-Agent"] = AGENT
    headers = bearer(login(client, "alice")["access_token"])
    login(client, "bob")
    [hook] = [each for each in on_export_subject if each.domain == "auth"]

    exported = run_async(hook.call(str(alice)))

    # The login, and the device it remembers for the second factor.
    entry, remembered = exported["tokens"]
    assert (remembered["kind"], remembered["method"]) == ("device", None)
    assert {key: entry[key] for key in ("kind", "name", "method", "agent", "ip")} == {
        "kind": "access",
        "name": "login",
        "method": "password",
        "agent": AGENT,
        "ip": ADDRESS[0],
    }
    assert entry["device_id"] is None
    assert entry["created_at"] is not None
    assert headers["Authorization"].removeprefix("Bearer ") not in str(exported)
    assert set(entry) == {
        "kind",
        "name",
        "method",
        "agent",
        "ip",
        "device_id",
        "created_at",
        "last_used_at",
        "expires_at",
    }
    assert run_async(hook.call("not-a-user")) == {}
    assert run_async(hook.call(str(UUID(int=1)))) == {}
