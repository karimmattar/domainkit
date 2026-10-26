"""Personal tokens: scopes, self-service, and when tokens were last used."""

import io
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from domainkit.clock import freeze_time
from domainkit.contrib.auth import AuthUnitOfWork, Credentials, User
from domainkit.contrib.auth.tables import tokens
from domainkit.contrib.auth.tokens import LAST_USED_EVERY
from domainkit.contrib.idempotency.tables import idempotency_keys
from domainkit.db import connections
from domainkit.management import call_command
from domainkit.management.base import CommandError, run_async
from domainkit.testing import TestClient as ClockedClient
from domainkit_tests.conftest import Api
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    grant,
    login,
    stored_tokens,
)

VIEW = "shop.view_product"
DELETE = "shop.delete_product"


def make(
    client: TestClient,
    headers: dict[str, str],
    **body: Any,  # noqa: ANN401 - fields.
) -> Any:  # noqa: ANN401 - a response.
    """Post a personal token."""
    return client.post("/auth/tokens", json={"name": "ci", **body}, headers=headers)


def user_session(client: TestClient, username: str = "alice") -> dict[str, str]:
    """Log in; the bearer header."""
    return bearer(login(client, username)["access_token"])


def test_tokens_are_shown_once_and_listed_without_their_value(identity: Api) -> None:
    """The value comes back once; the list has digests, names, and dates."""
    alice = create_user("alice")
    client = identity()
    headers = user_session(client)

    made = make(client, headers, expires_in_days=30)

    assert made.status_code == 201
    body = made.json()
    assert body["token"].startswith("dkt_")
    assert (body["name"], body["kind"], body["scopes"], body["current"]) == (
        "ci",
        "access",
        None,
        False,
    )
    assert body["last_used_at"] is None
    listed = client.get("/auth/tokens", headers=headers).json()
    assert listed["next_cursor"] is None
    assert [(item["name"], item["current"]) for item in listed["items"]] == [
        ("ci", False),
        ("login", True),
    ]
    assert listed["items"][0]["expires_at"] == body["expires_at"]
    assert listed["items"][0]["expires_at"] is not None
    assert body["token"] not in str(listed)
    assert {item["id"] for item in listed["items"]} == {
        row["id"] for row in stored_tokens(alice)
    }
    (created,) = audited("auth.token_created")
    assert created["details"] == {"user_id": str(alice), "name": "ci", "scopes": None}
    assert client.get("/whoami", headers=bearer(body["token"])).json()["user"] == (
        "alice"
    )


def test_new_tokens_are_kept_by_no_cache_and_no_idempotency_key(
    identity: Api,
) -> None:
    """``no-store``: ``IdempotencyMiddleware`` keeps no copy to replay.

    A retry with the same key gets 409, not the token a second time, and
    the idempotency table never holds a working token.
    """
    create_user("alice")
    client = identity()
    headers = {**user_session(client), "Idempotency-Key": "k-1"}

    first = make(client, headers)
    again = make(client, headers)

    assert first.status_code == 201
    assert first.headers["Cache-Control"] == "no-store"
    raw = first.json()["token"]
    assert (again.status_code, again.json()["code"]) == (
        409,
        "idempotency_response_unavailable",
    )
    assert raw not in again.text
    assert [raw.encode() in (body or b"") for body in idempotency_bodies()] == [False]


def idempotency_bodies() -> list[bytes | None]:
    """The response bodies the idempotency table keeps."""

    async def read() -> list[bytes | None]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(idempotency_keys.c.body))
            return [row.body for row in rows]

    return run_async(read())


def test_scopes_narrow_what_a_token_may_do(identity: Api) -> None:
    """Only the permissions that the user has and the scopes name."""
    create_user("alice")
    grant("alice", VIEW, DELETE)
    client = identity()
    headers = user_session(client)
    scoped = bearer(make(client, headers, scopes=[VIEW]).json()["token"])

    assert client.get("/scoped/view", headers=scoped).status_code == 200
    assert client.get("/scoped/model", headers=scoped).status_code == 200
    assert client.delete("/scoped/model", headers=scoped).status_code == 403
    assert client.delete("/scoped/model", headers=headers).status_code == 200
    assert client.get("/scoped/scope", headers=scoped).status_code == 200
    assert client.get("/scoped/scope", headers=headers).status_code == 200
    assert client.get("/scoped/scope").status_code == 401
    narrow = bearer(make(client, headers, scopes=[DELETE]).json()["token"])
    assert client.get("/scoped/scope", headers=narrow).status_code == 403
    assert client.get("/scoped/view", headers=narrow).status_code == 403
    empty = bearer(make(client, headers, scopes=[]).json()["token"])
    assert client.get("/scoped/model", headers=empty).status_code == 403
    make(client, headers, scopes=[VIEW, DELETE, VIEW])
    listed = client.get("/auth/tokens", headers=headers).json()["items"]
    assert sorted(tuple(item["scopes"]) for item in listed if item["name"] == "ci") == [
        (),
        (DELETE,),
        (DELETE, VIEW),
        (VIEW,),
    ]


def test_tokens_never_carry_more_than_their_user(identity: Api) -> None:
    """Scopes the user lacks are refused; superusers may name existing ones."""
    create_user("alice")
    create_user("root", is_superuser=True)
    grant("alice", VIEW)
    client = identity()

    refused = make(client, user_session(client), scopes=[VIEW, DELETE, "x.y"])
    rooted = make(client, user_session(client, "root"), scopes=[DELETE])
    unknown = make(client, user_session(client, "root"), scopes=["x.y"])

    assert (refused.status_code, refused.json()["code"]) == (403, "scope_not_held")
    assert refused.json()["detail"].endswith("shop.delete_product, x.y.")
    assert rooted.status_code == 201
    assert unknown.status_code == 403


def test_scoped_tokens_manage_nothing(identity: Api) -> None:
    """No tokens, no admin API, no superuser routes: scopes cannot bound those."""
    create_user("root", is_superuser=True, is_staff=True)
    client = identity()
    headers = user_session(client, "root")
    scoped = bearer(make(client, headers, scopes=[VIEW]).json()["token"])

    for response in (
        client.get("/auth/tokens", headers=scoped),
        make(client, scoped),
        client.get("/scoped/root", headers=scoped),
    ):
        assert response.status_code == 403
    assert client.get("/auth/tokens", headers=scoped).json()["detail"] == (
        "Credentials limited to scopes cannot do this."
    )
    assert client.get("/scoped/root", headers=headers).status_code == 200
    assert client.get("/scoped/view", headers=scoped).status_code == 200


def test_the_admin_api_refuses_scoped_tokens(api: Api) -> None:
    """Staff grant what they hold; a key's scopes would not bound it."""
    create_user("root", is_superuser=True, is_staff=True)
    client = api()

    async def scoped_token() -> str:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key("root")
            assert user is not None
            raw = await uow.tokens.issue(
                user.id, ttl=None, scopes=frozenset({"auth.view_user"})
            )
            await uow.commit()
        return raw

    scoped = bearer(run_async(scoped_token()))

    assert client.get("/admin/auth/users", headers=scoped).status_code == 403
    assert (
        client.get(
            "/admin/auth/users", headers=user_session(client, "root")
        ).status_code
        == 200
    )


def test_users_revoke_their_own_tokens_only(identity: Api) -> None:
    """Another user's token is not found; a revoked one stops working."""
    alice = create_user("alice")
    create_user("bob")
    client = identity()
    headers = user_session(client)
    made = make(client, headers).json()
    bobs = make(client, user_session(client, "bob")).json()

    others = client.delete(f"/auth/tokens/{bobs['id']}", headers=headers)
    missing = client.delete(f"/auth/tokens/{'0' * 64}", headers=headers)
    malformed = client.delete("/auth/tokens/not-a-digest", headers=headers)
    revoked = client.delete(f"/auth/tokens/{made['id']}", headers=headers)

    assert (others.status_code, others.json()["code"]) == (404, "token_not_found")
    assert missing.status_code == 404
    assert malformed.status_code == 422
    assert revoked.status_code == 204
    assert client.get("/whoami", headers=bearer(made["token"])).status_code == 401
    assert client.get("/whoami", headers=bearer(bobs["token"])).status_code == 200
    (event,) = audited("auth.token_revoked")
    assert event["details"] == {"user_id": str(alice), "name": "ci"}


def test_revoking_a_refresh_token_ends_its_login(identity: Api) -> None:
    """A JWT login is its refresh tokens: revoking one ends the whole login."""
    create_user("alice")
    client = identity(
        AUTHENTICATION_CLASSES=[
            "domainkit.contrib.auth.authentication.JWTAuthentication",
            "domainkit.contrib.auth.authentication.TokenAuthentication",
            "domainkit.contrib.auth.cookies.CookieAuthentication",
        ],
        JWT={"SIGNING_KEY": "k" * 32},
    )
    first = login(client, "alice")
    second = bearer(login(client, "alice")["access_token"])
    listed = client.get("/auth/tokens", headers=second).json()["items"]
    refresh = [item for item in listed if item["kind"] == "refresh"]

    assert [item["current"] for item in refresh] == [True, False]
    (other,) = [item for item in refresh if not item["current"]]
    assert (
        client.delete(f"/auth/tokens/{other['id']}", headers=second).status_code == 204
    )
    assert (
        client.get("/whoami", headers=bearer(first["access_token"])).status_code == 401
    )
    assert client.get("/whoami", headers=second).status_code == 200


def test_tokens_come_in_pages(identity: Api) -> None:
    """Newest first, ``limit`` at a time, with a cursor to the next page."""
    create_user("alice")
    client = identity()
    headers = user_session(client)
    names = []
    with freeze_time("2026-01-01T00:00:00Z") as clock:
        for index in range(3):
            clock.tick(1)
            names.append(f"t{index}")
            run_async(_issue("alice", f"t{index}"))
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/auth/tokens", params=params, headers=headers).json()
        seen += [item["name"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == ["login", "t2", "t1", "t0"]
    for bad in ("e30", "WyJub3QtYS10aW1lIiwiaWQiXQ", "!"):
        response = client.get("/auth/tokens", params={"cursor": bad}, headers=headers)
        assert response.status_code == 422


async def _issue(username: str, name: str) -> None:
    """Store a token for ``username``."""
    async with AuthUnitOfWork() as uow:
        user = await uow.users.get_by_natural_key(username)
        assert user is not None
        await uow.tokens.issue(user.id, ttl=None, name=name)
        await uow.commit()


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": ""}, "name"),
        ({"name": "n" * 101}, "name"),
        ({"name": "ci\nadmin"}, "name"),
        ({"name": "ci\x00"}, "name"),
        ({"expires_in_days": 0}, "expires_in_days"),
        ({"expires_in_days": 3651}, "expires_in_days"),
        ({"scopes": [f"a.b{n}" for n in range(101)]}, "scopes"),
        ({"scopes": [""]}, "scopes"),
        ({"token": "chosen"}, "token"),
    ],
)
def test_token_requests_are_bounded(
    identity: Api, body: dict[str, Any], field: str
) -> None:
    """Names, lifetimes, and scopes have limits; clients choose no values."""
    create_user("alice")
    client = identity()

    response = make(client, user_session(client), **body)

    assert response.status_code == 422
    assert field in str(response.json()["errors"])


def test_tokens_may_work_until_revoked(identity: Api) -> None:
    """``expires_in_days: null``: no end."""
    create_user("alice")
    client = identity()

    body = make(client, user_session(client), expires_in_days=None).json()

    assert body["expires_at"] is None


def test_last_use_is_written_at_most_every_five_minutes(identity: Api) -> None:
    """Enough to tell a dead key, without a write per request."""
    create_user("alice")
    client = identity()
    raw = make(client, user_session(client)).json()["token"]
    clocked = ClockedClient(client.app)

    def last_used() -> Any:  # noqa: ANN401 - a datetime.
        (item,) = [
            item
            for item in client.get("/auth/tokens", headers=user_session(client)).json()[
                "items"
            ]
            if item["name"] == "ci"
        ]
        return item["last_used_at"]

    with freeze_time("2026-03-01T12:00:00Z") as clock:
        clocked.get("/whoami", headers=bearer(raw))
        first = last_used()
        clock.tick(LAST_USED_EVERY - timedelta(seconds=1))
        clocked.get("/whoami", headers=bearer(raw))
        assert last_used() == first
        clock.tick(timedelta(seconds=1))
        clocked.get("/whoami", headers=bearer(raw))
        later = last_used()

    assert first.startswith("2026-03-01T12:00:00")
    assert later.startswith("2026-03-01T12:05:00")


def test_touching_twice_at_once_writes_once(identity: Api) -> None:
    """The stored time decides: a second write in the same five minutes is none."""
    alice = create_user("alice")
    identity()
    run_async(_issue("alice", "k"))

    async def touch_twice() -> tuple[bool, bool, bool]:
        async with AuthUnitOfWork() as uow:
            page = await uow.tokens.of_user(alice, limit=10)
            (token,) = page.items
            first = await uow.tokens.touch(token)
            # As another request, read earlier, sees it: its own copy, not a
            # change of this session's (which would be flushed first).
            set_committed_value(token, "last_used_at", None)
            second = await uow.tokens.touch(token)
            third = await uow.tokens.touch(token)
            await uow.commit()
        return first, second, third

    assert run_async(touch_twice()) == (True, False, False)


def test_browser_sessions_record_their_last_use(identity: Api) -> None:
    """Cookie sessions are tokens too, and show when they were used."""
    create_user("alice")
    client = identity()
    browser = TestClient(client.app, base_url="https://testserver")
    browser.post(
        "/auth/session",
        json={"username": "alice", "password": PASSWORD},
        headers={"Origin": "https://testserver"},
    )

    browser.get("/whoami")

    items = client.get("/auth/tokens", headers=user_session(client)).json()["items"]
    (session,) = [item for item in items if item["kind"] == "session"]
    assert session["last_used_at"] is not None


def test_createtoken_takes_scopes(identity: Api) -> None:
    """``--scope`` limits the token; the user must hold each one."""
    alice = create_user("alice")
    grant("alice", VIEW)
    client = identity()
    out = io.StringIO()

    call_command("createtoken", "alice", "--scope", VIEW, stdout=out)

    raw = out.getvalue().strip()
    assert client.get("/scoped/view", headers=bearer(raw)).status_code == 200
    assert client.delete("/scoped/model", headers=bearer(raw)).status_code == 403
    (created,) = audited("auth.token_created")
    assert created["details"]["scopes"] == [VIEW]
    with pytest.raises(CommandError, match=r"lacks these permissions: shop\.delete"):
        call_command("createtoken", "alice", "--scope", DELETE, stdout=out)
    assert len(stored_tokens(alice)) == 1


def test_credentials_allow_their_scopes() -> None:
    """No scopes: anything; scopes: only those."""
    user = User.create(password=_unusable(), username="a")

    assert Credentials(user, scheme="token").allows("a.b", "c.d")
    limited = Credentials(user, scheme="token", scopes=frozenset({"a.b"}))
    assert limited.allows("a.b")
    assert limited.allows()
    assert not limited.allows("a.b", "c.d")


def _unusable() -> Any:  # noqa: ANN401 - a PasswordHash.
    """A password that never matches."""
    from domainkit.contrib.auth import PasswordHash

    return PasswordHash.unusable()


def test_scopes_are_stored_as_sorted_keys(identity: Api) -> None:
    """Space-separated, in order; no scopes is NULL, an empty set is ''."""
    from sqlalchemy import select

    from domainkit.db import connections

    alice = create_user("alice")
    grant("alice", VIEW, DELETE)
    client = identity()
    headers = user_session(client)
    make(client, headers, name="two", scopes=[VIEW, DELETE])
    make(client, headers, name="none", scopes=[])

    async def read() -> dict[str, Any]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(tokens.c.name, tokens.c.scopes).where(tokens.c.user_id == alice)
            )
            return {row.name: row.scopes for row in rows}

    stored = run_async(read())
    assert stored == {
        "login": None,
        "two": frozenset({VIEW, DELETE}),
        "none": frozenset(),
    }
    assert isinstance(alice, UUID)


def test_rules_need_something_to_check() -> None:
    """``HasScope()`` without a scope is a mistake, caught at import."""
    from domainkit.exceptions import ImproperlyConfigured
    from domainkit.http.access import HasScope

    with pytest.raises(ImproperlyConfigured, match="at least one scope"):
        HasScope()


def test_scoped_tokens_cannot_change_the_account(identity: Api) -> None:
    """Passwords, second factors, staff routes, and logging out everywhere."""
    create_user("alice", is_staff=True)
    grant("alice", VIEW)
    client = identity()
    headers = user_session(client)
    scoped = bearer(make(client, headers, scopes=[VIEW]).json()["token"])

    refused = [
        client.post(
            "/auth/password",
            json={"old_password": PASSWORD, "new_password": "n3w-Strong-pass"},
            headers=scoped,
        ),
        client.post("/auth/logout", json={"everywhere": True}, headers=scoped),
        client.get("/auth/mfa", headers=scoped),
        client.post("/auth/mfa/totp", json={"password": PASSWORD}, headers=scoped),
        client.get("/scoped/staff", headers=scoped),
        client.post("/auth/email-verification", headers=scoped),
    ]

    assert [response.status_code for response in refused] == [403] * 6
    assert client.post("/auth/email-verification", headers=headers).status_code == 202
    assert client.get("/scoped/staff", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers=scoped).status_code == 204
    assert client.get("/whoami", headers=scoped).status_code == 401
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_me_lists_what_the_credentials_may_do(identity: Api) -> None:
    """With scopes, only the permissions both the user and the scopes allow."""
    create_user("alice")
    create_user("root", is_superuser=True)
    grant("alice", VIEW, DELETE)
    client = identity()
    alice = user_session(client)
    root = user_session(client, "root")

    def permissions(headers: dict[str, str], **body: Any) -> list[str]:  # noqa: ANN401
        if body:
            headers = bearer(make(client, headers, **body).json()["token"])
        listed: list[str] = client.get("/auth/me", headers=headers).json()[
            "permissions"
        ]
        return listed

    assert permissions(alice) == [DELETE, VIEW]
    assert permissions(alice, scopes=[VIEW]) == [VIEW]
    assert permissions(alice, scopes=[]) == []
    assert {VIEW, DELETE} < set(permissions(root))  # Every permission there is.
    assert permissions(root, scopes=[DELETE]) == [DELETE]
