"""The admin API: staff manage users, groups, and permissions over REST."""

import asyncio
import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from domainkit import setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.contrib.audit import pseudonym_of
from domainkit.contrib.auth import (
    AnyUser,
    AuthUnitOfWork,
    BaseBackend,
    Permission,
    User,
    admin,
    get_user_model,
)
from domainkit.ddd import PermissionDenied
from domainkit.http import ErrorResponse, encode_cursor, get_asgi_application
from domainkit.management import call_command
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    SHOP,
    Api,
    UseAuth,
)
from domainkit_tests.test_auth_access import request, user
from domainkit_tests.test_auth_http import (
    PASSWORD,
    audited,
    bearer,
    create_user,
    grant,
    login,
    stored_tokens,
)

ADMIN = "/admin/auth"
NEW_PASSWORD = "Zq8!wL-d4#Kp7v"
ADMIN_PERMISSIONS = (
    "auth.view_user",
    "auth.add_user",
    "auth.change_user",
    "auth.delete_user",
    "auth.view_group",
    "auth.add_group",
    "auth.change_group",
    "auth.delete_group",
    "auth.view_permission",
)
LACK_DELETE = "you lack shop.delete_product."

type Headers = dict[str, str]


def token_for(username: str) -> Headers:
    """Headers that authenticate ``username``, without logging in."""

    async def issue() -> str:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            raw = await uow.tokens.issue(user.id, ttl=None, name="test")
            await uow.commit()
        return raw

    return bearer(run_async(issue()))


def staff(
    username: str,
    *permissions: str,
    is_staff: bool | None = True,
    **fields: Any,  # noqa: ANN401 - user fields.
) -> Headers:
    """A staff user with ``permissions``; their headers.

    ``is_staff=None`` leaves the flag out, for models without one.
    """
    if is_staff is not None:
        fields["is_staff"] = is_staff
    create_user(username, **fields)
    grant(username, *permissions)
    return token_for(username)


def make_group(name: str, *permissions: str) -> UUID:
    """Store a group with ``permissions``; return its id."""

    async def create() -> UUID:
        async with AuthUnitOfWork() as uow:
            group = await uow.groups.create(name)
            group.grant(*await uow.permissions.get_many(permissions))
            await uow.commit()
        return group.id

    return run_async(create())


def join(username: str, *groups: str) -> None:
    """Put ``username`` in ``groups``."""

    async def add() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get_by_natural_key(username)
            assert user is not None
            for group in await uow.groups.get_many_by_name(groups):
                user.join(group)
            await uow.commit()

    run_async(add())


def error(response: Any, status: int) -> tuple[str, str]:  # noqa: ANN401 - a response.
    """The ``code`` and ``detail`` of an error response with ``status``."""
    assert response.status_code == status, response.json()
    body = response.json()
    return body["code"], body["detail"]


def details(action: str) -> list[dict[str, Any]]:
    """The details of every audited ``action``, oldest first."""
    return [row["details"] for row in audited(action)]


def test_every_endpoint_needs_staff_and_its_model_permission(api: Api) -> None:
    """``is_staff`` (or a superuser) and the action's model permission."""
    client = api()
    target = str(create_user("target"))
    crew = str(make_group("crew"))
    endpoints: list[tuple[str, str, str, dict[str, Any] | None, int]] = [
        ("GET", "/users", "auth.view_user", None, 200),
        ("POST", "/users", "auth.add_user", {"username": "made"}, 201),
        ("GET", f"/users/{target}", "auth.view_user", None, 200),
        ("PATCH", f"/users/{target}", "auth.change_user", {}, 200),
        (
            "POST",
            f"/users/{target}/password",
            "auth.change_user",
            {"password": None},
            204,
        ),
        ("POST", f"/users/{target}/logout", "auth.change_user", None, 204),
        ("PUT", f"/users/{target}/groups", "auth.change_user", {"groups": []}, 200),
        (
            "PUT",
            f"/users/{target}/permissions",
            "auth.change_user",
            {"permissions": []},
            200,
        ),
        ("GET", "/groups", "auth.view_group", None, 200),
        ("POST", "/groups", "auth.add_group", {"name": "made"}, 201),
        ("GET", f"/groups/{crew}", "auth.view_group", None, 200),
        ("PATCH", f"/groups/{crew}", "auth.change_group", {"name": "crew"}, 200),
        (
            "PUT",
            f"/groups/{crew}/permissions",
            "auth.change_group",
            {"permissions": []},
            200,
        ),
        ("GET", "/permissions", "auth.view_permission", None, 200),
        ("DELETE", f"/groups/{crew}", "auth.delete_group", None, 204),
        ("DELETE", f"/users/{target}", "auth.delete_user", None, 204),
    ]
    plain = staff("plain", *ADMIN_PERMISSIONS, is_staff=False)
    for number, (method, path, permission, body, success) in enumerate(endpoints):
        others = [each for each in ADMIN_PERMISSIONS if each != permission]
        lacking = staff(f"lacking{number}", *others)
        allowed = staff(f"allowed{number}", permission)
        url = ADMIN + path

        statuses = [
            client.request(method, url, json=body, headers=headers).status_code
            for headers in (None, plain, lacking, allowed)
        ]
        assert statuses == [401, 403, 403, success], (method, path)

    refused = client.get(f"{ADMIN}/users", headers=plain)
    assert error(refused, 403)[0] == "permission_denied"
    root = staff("root", is_staff=False, is_superuser=True)
    assert client.get(f"{ADMIN}/users", headers=root).status_code == 200


def test_users_are_listed_by_username_in_pages(api: Api) -> None:
    """Keyset pages of at most ``limit`` users, and a cursor to go on."""
    client = api()
    headers = staff("admin", "auth.view_user")
    for name in ("carol", "bob", "erin", "dave"):
        create_user(name, email=f"{name}@example.com")
    url = f"{ADMIN}/users"

    first = client.get(url, params={"limit": 3}, headers=headers).json()
    assert [user["username"] for user in first["items"]] == ["admin", "bob", "carol"]
    bob = first["items"][1]
    assert set(bob) == {
        "id",
        "username",
        "is_active",
        "is_staff",
        "is_superuser",
        "last_login",
        "groups",
        "permissions",
        "fields",
    }
    assert bob["fields"].pop("date_joined")
    assert bob["fields"] == {
        "email": "bob@example.com",
        "email_verified": False,
        "first_name": "",
        "last_name": "",
    }
    assert (bob["is_active"], bob["is_staff"], bob["is_superuser"]) == (
        True,
        False,
        False,
    )
    assert first["items"][0]["permissions"] == ["auth.view_user"]
    assert (bob["last_login"], first["items"][2]["last_login"]) == (None, None)
    login(client, "bob")
    bob_again = client.get(f"{url}/{bob['id']}", headers=headers).json()
    assert bob_again["last_login"] is not None
    second = client.get(
        url, params={"limit": 3, "cursor": first["next_cursor"]}, headers=headers
    ).json()
    assert [user["username"] for user in second["items"]] == ["dave", "erin"]
    assert second["next_cursor"] is None
    exact = client.get(url, params={"limit": 5}, headers=headers).json()
    assert len(exact["items"]) == 5
    assert exact["next_cursor"] is None
    default = client.get(url, headers=headers).json()
    assert len(default["items"]) == 5

    for params in ({"limit": 0}, {"limit": 201}, {"cursor": "?"}):
        assert client.get(url, params=params, headers=headers).status_code == 422
    assert client.get(url, params={"limit": 200}, headers=headers).status_code == 200
    for cursor in (encode_cursor(["bob", "carol"]), encode_cursor([])):
        refused = client.get(url, params={"cursor": cursor}, headers=headers)
        assert error(refused, 422)[0] == "invalid_cursor"


def test_users_are_found_by_part_of_their_name(api: Api) -> None:
    """``search`` ignores case, and matches ``_`` literally."""
    client = api()
    headers = staff("admin", "auth.view_user")
    for name in ("Dan_X", "danax", "bob"):
        create_user(name)

    def found(search: str) -> set[str]:
        response = client.get(
            f"{ADMIN}/users", params={"search": search}, headers=headers
        )
        return {user["username"] for user in response.json()["items"]}

    assert found("AN_") == {"Dan_X"}
    assert found("DAN") == {"Dan_X", "danax"}
    assert found("") == {"Dan_X", "danax", "bob", "admin"}
    assert found("zz") == set()


def test_staff_create_users(api: Api) -> None:
    """Passwords are validated; staff cannot make superusers."""
    client = api()
    headers = staff("admin", "auth.add_user", "auth.view_user")
    admin_id = audited("auth.user_created")[0]["details"]["user_id"]
    url = f"{ADMIN}/users"

    response = client.post(
        url,
        json={
            "username": "newbie",
            "password": PASSWORD,
            "fields": {"email": "New@EXAMPLE.com", "first_name": "New"},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.json()
    made = response.json()
    assert made["fields"].pop("date_joined")
    assert made == {
        "id": made["id"],
        "username": "newbie",
        "is_active": True,
        "is_staff": False,
        "is_superuser": False,
        "last_login": None,
        "groups": [],
        "permissions": [],
        "fields": {
            "email": "New@example.com",
            "email_verified": False,
            "first_name": "New",
            "last_name": "",
        },
    }
    assert login(client, "newbie")["access_token"]
    created = audited("auth.user_created")[-1]
    assert created["actor"] == admin_id
    assert created["details"] == {
        "user_id": made["id"],
        "username": run_async(pseudonym_of(made["id"], "username", "newbie")),
    }

    flags = {"username": "keeper", "is_active": False, "is_staff": True}
    kept = client.post(url, json=flags, headers=headers).json()
    assert (kept["is_active"], kept["is_staff"], kept["is_superuser"]) == (
        False,
        True,
        False,
    )
    refused = client.post(
        "/auth/login", json={"username": "keeper", "password": PASSWORD}
    )
    assert refused.status_code == 401

    def refused_with(body: dict[str, Any], status: int) -> tuple[str, str]:
        return error(client.post(url, json=body, headers=headers), status)

    assert refused_with({"username": "nopass", "password": "short"}, 422)[0] == (
        "invalid_password"
    )
    similar = {"username": "kamalbasha", "password": "kamalbasha1"}
    assert refused_with(similar, 422)[0] == "invalid_password"
    named = {
        "username": "x",
        "password": "Monalisa-Gherardini",
        "fields": {"first_name": "Monalisa Gherardini"},
    }
    assert refused_with(named, 422)[0] == "invalid_password"
    assert refused_with({"username": "newbie"}, 409)[0] == "username_taken"
    assert refused_with({"username": "x", "fields": {"password": "x"}}, 422) == (
        "unknown_user_field",
        "These user fields cannot be changed here: password.",
    )
    assert refused_with(
        {"username": "x", "fields": {"is_superuser": True, "nope": 1}}, 422
    ) == (
        "unknown_user_field",
        "These user fields cannot be changed here: is_superuser, nope.",
    )
    assert refused_with({"username": "x", "fields": {"first_name": 5}}, 422) == (
        "invalid_user_field",
        "first_name: Input should be a valid string.",
    )
    assert refused_with({"username": "x", "is_superuser": True}, 403) == (
        "permission_denied",
        "Only superusers may make superusers.",
    )
    assert (
        client.post(
            url, json={"username": "x", "extra": 1}, headers=headers
        ).status_code
        == 422
    )
    assert {
        user["username"] for user in client.get(url, headers=headers).json()["items"]
    } == {
        "admin",
        "newbie",
        "keeper",
    }

    root = staff("root", is_superuser=True)
    boss = client.post(
        url, json={"username": "boss", "is_superuser": True}, headers=root
    )
    assert boss.json()["is_superuser"] is True


def test_staff_change_users(api: Api) -> None:
    """Flags and fields change, and each change is audited."""
    client = api()
    headers = staff("admin", "auth.change_user", "auth.view_user")
    admin_id = audited("auth.user_created")[0]["details"]["user_id"]
    bob = create_user("bob")
    url = f"{ADMIN}/users/{bob}"

    response = client.patch(
        url,
        json={
            "is_active": False,
            "fields": {"email": "Bob@EXAMPLE.com", "last_name": "B", "first_name": ""},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["is_active"] is False
    assert response.json()["fields"]["email"] == "Bob@example.com"
    assert response.json()["fields"]["last_name"] == "B"
    assert details("auth.user_updated") == [
        {"user_id": str(bob), "fields": ["email", "last_name"]}
    ]
    assert details("auth.user_status_changed") == [
        {"user_id": str(bob), "field": "is_active", "value": False}
    ]
    assert audited("auth.user_updated")[0]["actor"] == admin_id

    staffed = client.patch(
        url, json={"is_staff": True, "is_active": True}, headers=headers
    )
    assert (staffed.json()["is_staff"], staffed.json()["is_active"]) == (True, True)
    same = client.patch(url, json={"is_superuser": False}, headers=headers)
    assert same.status_code == 200
    assert client.patch(url, json={}, headers=headers).status_code == 200
    assert len(audited("auth.user_updated")) == 1
    assert client.get(url, headers=headers).json() == staffed.json()

    def refused_with(target: str, body: dict[str, Any], status: int) -> tuple[str, str]:
        return error(client.patch(target, json=body, headers=headers), status)

    assert refused_with(url, {"fields": {"username": "robert"}}, 422) == (
        "unknown_user_field",
        "These user fields cannot be changed here: username.",
    )
    for value in (None, 7, True):
        assert refused_with(url, {"fields": {"email": value}}, 422) == (
            "invalid_user_field",
            "email: Input should be a valid string.",
        )
    assert client.get(url, headers=headers).json()["fields"]["email"] == (
        "Bob@example.com"
    )
    assert refused_with(url, {"is_superuser": True}, 403) == (
        "permission_denied",
        "Only superusers may make or unmake superusers.",
    )
    root_id = create_user("root", is_superuser=True)
    assert refused_with(f"{ADMIN}/users/{root_id}", {}, 403) == (
        "permission_denied",
        "Only superusers may change superusers.",
    )
    assert refused_with(f"{ADMIN}/users/{uuid4()}", {}, 404) == (
        "user_not_found",
        "No such user.",
    )
    missing = client.get(f"{ADMIN}/users/{uuid4()}", headers=headers)
    assert error(missing, 404) == ("user_not_found", "No such user.")

    root = token_for("root")
    promoted = client.patch(url, json={"is_superuser": True}, headers=root)
    assert promoted.json()["is_superuser"] is True
    demoted = client.patch(
        f"{ADMIN}/users/{root_id}", json={"is_active": False}, headers=root
    )
    assert demoted.json()["is_active"] is False


def test_staff_only_change_users_who_hold_less(api: Api) -> None:
    """Setting a password is as good as becoming the user."""
    client = api()
    headers = staff("admin", *ADMIN_PERMISSIONS, "shop.view_product")
    make_group("leads", "shop.delete_product")
    boss = create_user("boss")
    grant("boss", "shop.delete_product")
    lead = create_user("lead")
    join("lead", "leads")
    idle = create_user("idle", is_active=False)
    grant("idle", "shop.delete_product")
    peer = create_user("peer")
    grant("peer", "shop.view_product")

    changes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("PATCH", "", {"is_active": False}),
        ("POST", "/password", {"password": NEW_PASSWORD}),
        ("POST", "/logout", None),
        ("PUT", "/groups", {"groups": []}),
        ("PUT", "/permissions", {"permissions": []}),
        ("DELETE", "", None),
    ]
    for target in (boss, lead, idle):
        for method, path, body in changes:
            response = client.request(
                method, f"{ADMIN}/users/{target}{path}", json=body, headers=headers
            )
            assert error(response, 403) == (
                "permission_denied",
                f"You can only change users whose permissions you have; {LACK_DELETE}",
            )
    assert login(client, "boss")["access_token"]
    response = client.patch(
        f"{ADMIN}/users/{peer}", json={"is_active": False}, headers=headers
    )
    assert response.status_code == 200
    assert client.get(f"{ADMIN}/users/{lead}", headers=headers).json()["groups"] == [
        "leads"
    ]

    root = staff("root", is_superuser=True)
    response = client.post(
        f"{ADMIN}/users/{boss}/password", json={"password": NEW_PASSWORD}, headers=root
    )
    assert response.status_code == 204


class RoleBackend(BaseBackend):
    """Grants from outside the user table: staff named ``boss...`` may delete."""

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """``shop.delete_product`` for active staff whose name starts ``boss``."""
        boss = user.get_username().startswith("boss")
        if obj is None and user.is_active and getattr(user, "is_staff", False) and boss:
            return frozenset({"shop.delete_product"})
        return frozenset()


BACKENDS = [
    "domainkit.contrib.auth.backends.ModelBackend",
    f"{__name__}.RoleBackend",
]


def test_staff_only_change_users_other_backends_grant_less(api: Api) -> None:
    """Every backend's grants count; while one is configured, inactive users wait."""
    client = api(BACKENDS=BACKENDS)
    headers = staff("helpdesk", *ADMIN_PERMISSIONS)
    cfo = create_user("boss-cfo", is_staff=True)
    intern = create_user("boss-intern")
    idle = create_user("idle", is_active=False)

    taken = client.post(
        f"{ADMIN}/users/{cfo}/password",
        json={"password": NEW_PASSWORD},
        headers=headers,
    )
    promoted = client.patch(
        f"{ADMIN}/users/{intern}", json={"is_staff": True}, headers=headers
    )
    woken = client.patch(
        f"{ADMIN}/users/{idle}", json={"is_active": True}, headers=headers
    )

    lacking = f"You can only change users whose permissions you have; {LACK_DELETE}"
    assert error(taken, 403) == ("permission_denied", lacking)
    assert error(promoted, 403) == ("permission_denied", lacking)  # Into a boss.
    inactive = (
        "Only superusers may change inactive users while other backends grant "
        "permissions."
    )
    assert error(woken, 403) == ("permission_denied", inactive)
    intern_now = client.get(f"{ADMIN}/users/{intern}", headers=headers).json()
    assert intern_now["is_staff"] is False  # Refused before the commit.
    root = staff("root", is_superuser=True)
    for path, body in ((f"{cfo}/password", {"password": NEW_PASSWORD}),):
        assert client.post(f"{ADMIN}/users/{path}", json=body, headers=root).is_success
    assert client.patch(
        f"{ADMIN}/users/{idle}", json={"is_active": True}, headers=root
    ).is_success


class DirectoryBackend(BaseBackend):
    """Grants from a directory alone: ``helpdesk`` administers users."""

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """Every admin permission for ``helpdesk``, nothing for anyone else."""
        if obj is None and user.is_active and user.get_username() == "helpdesk":
            return frozenset(ADMIN_PERMISSIONS)
        return frozenset()


def test_stored_grants_count_while_no_backend_reads_them(api: Api) -> None:
    """Without ModelBackend a user's own grants are idle, but they still count.

    Configuring ModelBackend again would bring them back.
    """
    client = api(BACKENDS=[f"{__name__}.DirectoryBackend"])
    headers = staff("helpdesk")
    plain = create_user("plain")
    clerk = create_user("clerk")
    grant("clerk", "shop.delete_product")

    allowed = client.post(
        f"{ADMIN}/users/{plain}/password",
        json={"password": NEW_PASSWORD},
        headers=headers,
    )
    taken = client.post(
        f"{ADMIN}/users/{clerk}/password",
        json={"password": NEW_PASSWORD},
        headers=headers,
    )

    assert allowed.status_code == 204
    lacking = f"You can only change users whose permissions you have; {LACK_DELETE}"
    assert error(taken, 403) == ("permission_denied", lacking)


def test_deactivating_a_user_ends_their_sessions(api: Api) -> None:
    """Tokens do not come back to life when the user is active again."""
    client = api()
    headers = staff("admin", *ADMIN_PERMISSIONS)
    worker = create_user("worker")
    token = bearer(login(client, "worker")["access_token"])

    for active in (False, True):
        response = client.patch(
            f"{ADMIN}/users/{worker}", json={"is_active": active}, headers=headers
        )
        assert response.status_code == 200
    again = client.patch(
        f"{ADMIN}/users/{worker}", json={"is_active": True}, headers=headers
    )

    assert again.status_code == 200
    assert client.get("/whoami", headers=token).status_code == 401
    assert stored_tokens(worker) == []


def test_the_last_active_superuser_stays(api: Api) -> None:
    """Demoting, deactivating, or deleting the last one is refused, not done."""
    client = api()
    root_id = create_user("root", is_superuser=True)
    root = token_for("root")
    refusal = (
        "last_superuser",
        "This is the last active superuser; make another one first.",
    )
    staff("dormant", is_superuser=True, is_active=False)  # Inactive: no help.
    create_user("clerk")  # Active, but no superuser: no help either.

    assert (
        error(
            client.patch(
                f"{ADMIN}/users/{root_id}", json={"is_superuser": False}, headers=root
            ),
            409,
        )
        == refusal
    )
    assert (
        error(
            client.patch(
                f"{ADMIN}/users/{root_id}", json={"is_active": False}, headers=root
            ),
            409,
        )
        == refusal
    )
    assert (
        error(client.delete(f"{ADMIN}/users/{root_id}", headers=root), 409) == refusal
    )
    assert client.patch(
        f"{ADMIN}/users/{root_id}", json={"is_superuser": True}, headers=root
    ).is_success  # Not a demotion.

    staff("second", is_superuser=True)
    demoted = client.patch(
        f"{ADMIN}/users/{root_id}", json={"is_superuser": False}, headers=root
    )
    assert (demoted.status_code, demoted.json()["is_superuser"]) == (200, False)


def test_staff_set_passwords_and_end_sessions(api: Api) -> None:
    """New passwords end the user's sessions; so does ``/logout``."""
    client = api()
    headers = staff("admin", "auth.change_user")
    admin_id = audited("auth.user_created")[0]["details"]["user_id"]
    bob = create_user("bob")
    url = f"{ADMIN}/users/{bob}"
    session = bearer(login(client, "bob")["access_token"])

    response = client.post(
        f"{url}/password", json={"password": NEW_PASSWORD}, headers=headers
    )
    assert response.status_code == 204
    assert client.get("/auth/me", headers=session).status_code == 401
    assert stored_tokens(bob) == []
    assert login(client, "bob", NEW_PASSWORD)["access_token"]
    changed = audited("auth.password_changed")[-1]
    assert (changed["actor"], changed["details"]) == (admin_id, {"user_id": str(bob)})

    assert (
        client.post(
            f"{url}/password", json={"password": None}, headers=headers
        ).status_code
        == 204
    )
    refused = client.post(
        "/auth/login", json={"username": "bob", "password": NEW_PASSWORD}
    )
    assert refused.status_code == 401
    similar = create_user("bartholomew")
    weak = client.post(
        f"{ADMIN}/users/{similar}/password",
        json={"password": "bartholomew1"},
        headers=headers,
    )
    assert error(weak, 422)[0] == "invalid_password"
    for body in ({}, {"password": ""}, {"password": "x", "extra": 1}):
        assert (
            client.post(f"{url}/password", json=body, headers=headers).status_code
            == 422
        )
    missing = client.post(
        f"{ADMIN}/users/{uuid4()}/password", json={"password": None}, headers=headers
    )
    assert error(missing, 404)[0] == "user_not_found"

    client.post(f"{url}/password", json={"password": PASSWORD}, headers=headers)
    first, second = login(client, "bob"), login(client, "bob")
    assert client.post(f"{url}/logout", headers=headers).status_code == 204
    for ended in (first, second):
        me = client.get("/auth/me", headers=bearer(ended["access_token"]))
        assert me.status_code == 401
    logged_out = audited("auth.user_logged_out")[-1]
    assert (logged_out["actor"], logged_out["details"]) == (
        admin_id,
        {"user_id": str(bob), "everywhere": True},
    )
    missing = client.post(f"{ADMIN}/users/{uuid4()}/logout", headers=headers)
    assert error(missing, 404)[0] == "user_not_found"


def test_staff_delete_users(api: Api) -> None:
    """With their tokens, memberships, and grants."""
    client = api()
    headers = staff("admin", "auth.delete_user", "auth.view_user", "shop.view_product")
    make_group("crew", "shop.view_product")
    bob = create_user("bob")
    join("bob", "crew")
    grant("bob", "shop.view_product")
    login(client, "bob")
    url = f"{ADMIN}/users/{bob}"

    assert client.delete(url, headers=headers).status_code == 204
    assert error(client.get(url, headers=headers), 404)[0] == "user_not_found"
    assert error(client.delete(url, headers=headers), 404)[0] == "user_not_found"
    assert stored_tokens(bob) == []
    assert details("auth.user_deleted") == [
        {
            "user_id": str(bob),
            "username": run_async(pseudonym_of(str(bob), "username", "bob")),
        }
    ]
    assert create_user("bob") != bob

    root = create_user("root", is_superuser=True)
    refused = client.delete(f"{ADMIN}/users/{root}", headers=headers)
    assert error(refused, 403)[1] == "Only superusers may change superusers."


def test_staff_set_groups_and_permissions_they_have(api: Api) -> None:
    """Exactly the groups and permissions given; never more than the actor's."""
    client = api()
    headers = staff(
        "admin", "auth.change_user", "shop.view_product", "shop.add_product"
    )
    make_group("crew", "shop.view_product")
    make_group("leads", "shop.delete_product")
    make_group("empty")
    bob = create_user("bob")
    url = f"{ADMIN}/users/{bob}"

    def put(path: str, body: dict[str, Any], by: Headers = headers) -> Any:  # noqa: ANN401 - a response.
        return client.put(f"{url}{path}", json=body, headers=by)

    joined = put("/groups", {"groups": ["empty", "crew", "crew"]})
    assert joined.json()["groups"] == ["crew", "empty"]
    assert [each["group"] for each in details("auth.user_joined_group")] == [
        "empty",
        "crew",
    ]
    assert put("/groups", {"groups": ["crew"]}).json()["groups"] == ["crew"]
    assert [each["group"] for each in details("auth.user_left_group")] == ["empty"]
    assert error(put("/groups", {"groups": ["crew", "leads"]}), 403) == (
        "permission_denied",
        f"You can only grant permissions you have; {LACK_DELETE}",
    )
    assert error(put("/groups", {"groups": ["ghost", "crew", "nobody"]}), 404) == (
        "unknown_group",
        "No such group: ghost, nobody.",
    )
    assert put("/groups", {"groups": []}).json()["groups"] == []

    granted = put(
        "/permissions", {"permissions": ["shop.view_product", "shop.add_product"]}
    )
    assert granted.json()["permissions"] == ["shop.add_product", "shop.view_product"]
    assert put("/permissions", {"permissions": ["shop.add_product"]}).json()[
        "permissions"
    ] == ["shop.add_product"]
    assert details("auth.user_permission_revoked") == [
        {"user_id": str(bob), "permission": "shop.view_product"}
    ]
    assert error(put("/permissions", {"permissions": ["shop.delete_product"]}), 403)[
        1
    ] == (f"You can only grant permissions you have; {LACK_DELETE}")
    lacking = put(
        "/permissions", {"permissions": ["shop.delete_product", "shop.change_product"]}
    )
    assert error(lacking, 403)[1] == (
        "You can only grant permissions you have; "
        "you lack shop.change_product, shop.delete_product."
    )
    assert error(put("/permissions", {"permissions": ["nope.nothing"]}), 404) == (
        "unknown_permission",
        "No such permission: nope.nothing.",
    )
    assert client.get(
        f"{ADMIN}/users/{bob}", headers=staff("viewer", "auth.view_user")
    ).json()["permissions"] == ["shop.add_product"]

    root = staff("root", is_superuser=True)
    assert put("/groups", {"groups": ["leads"]}, root).json()["groups"] == ["leads"]
    assert (
        put("/permissions", {"permissions": ["shop.delete_product"]}, root).status_code
        == 200
    )
    emptied: list[tuple[str, dict[str, Any]]] = [
        ("/groups", {"groups": []}),
        ("/permissions", {"permissions": []}),
    ]
    for path, body in emptied:
        assert put(path, body).status_code == 403


def test_staff_manage_groups(api: Api) -> None:
    """Groups are created, listed, renamed, granted, and deleted."""
    client = api()
    headers = staff(
        "admin",
        "auth.view_group",
        "auth.add_group",
        "auth.change_group",
        "auth.delete_group",
        "shop.view_product",
        "shop.add_product",
    )
    url = f"{ADMIN}/groups"

    response = client.post(
        url,
        json={"name": "crew", "permissions": ["shop.view_product"]},
        headers=headers,
    )
    assert response.status_code == 201, response.json()
    crew = response.json()
    assert crew == {
        "id": crew["id"],
        "name": "crew",
        "permissions": ["shop.view_product"],
    }
    assert details("auth.group_created") == [{"group_id": crew["id"], "name": "crew"}]
    assert details("auth.group_permission_granted") == [
        {"group_id": crew["id"], "permission": "shop.view_product"}
    ]
    for name in ("delta", "bravo", "alpha"):
        client.post(url, json={"name": name}, headers=headers)

    first = client.get(url, params={"limit": 2}, headers=headers).json()
    assert [group["name"] for group in first["items"]] == ["alpha", "bravo"]
    second = client.get(
        url, params={"cursor": first["next_cursor"]}, headers=headers
    ).json()
    assert [group["name"] for group in second["items"]] == ["crew", "delta"]
    assert second["next_cursor"] is None
    assert second["items"][0] == crew
    bad = client.get(url, params={"cursor": encode_cursor(["a", "b"])}, headers=headers)
    assert error(bad, 422)[0] == "invalid_cursor"

    def refused_with(body: dict[str, Any], status: int) -> tuple[str, str]:
        return error(client.post(url, json=body, headers=headers), status)

    assert refused_with({"name": "crew"}, 409) == (
        "group_name_taken",
        "A group called 'crew' exists.",
    )
    assert refused_with({"name": "  "}, 422)[0] == "invalid_group_name"
    assert refused_with({"name": "x", "permissions": ["shop.delete_product"]}, 403)[
        1
    ] == (f"You can only grant permissions you have; {LACK_DELETE}")
    assert refused_with({"name": "x", "permissions": ["nope.x"]}, 404)[0] == (
        "unknown_permission"
    )

    one = f"{url}/{crew['id']}"
    assert client.get(one, headers=headers).json() == crew
    renamed = client.patch(one, json={"name": " squad "}, headers=headers)
    assert renamed.json()["name"] == "squad"
    assert details("auth.group_renamed") == [{"group_id": crew["id"], "name": "squad"}]
    assert client.patch(one, json={"name": "squad"}, headers=headers).status_code == 200
    taken = client.patch(one, json={"name": "alpha"}, headers=headers)
    assert error(taken, 409) == ("group_name_taken", "A group called 'alpha' exists.")
    assert client.get(one, headers=headers).json()["name"] == "squad"
    assert len(audited("auth.group_renamed")) == 1

    granted = client.put(
        f"{one}/permissions",
        json={"permissions": ["shop.add_product"]},
        headers=headers,
    )
    assert granted.json()["permissions"] == ["shop.add_product"]
    assert details("auth.group_permission_revoked") == [
        {"group_id": crew["id"], "permission": "shop.view_product"}
    ]
    lacking = client.put(
        f"{one}/permissions",
        json={"permissions": ["shop.add_product", "shop.delete_product"]},
        headers=headers,
    )
    assert (
        error(lacking, 403)[1]
        == f"You can only grant permissions you have; {LACK_DELETE}"
    )

    create_user("bob")
    join("bob", "squad")
    assert client.delete(one, headers=headers).status_code == 204
    assert details("auth.group_deleted") == [{"group_id": crew["id"], "name": "squad"}]
    for method in ("GET", "PATCH", "DELETE"):
        response = client.request(method, one, json={"name": "x"}, headers=headers)
        assert error(response, 404) == ("group_not_found", "No such group.")
    missing = client.put(
        f"{one}/permissions", json={"permissions": []}, headers=headers
    )
    assert error(missing, 404)[0] == "group_not_found"
    viewer = staff("viewer", "auth.view_user")
    bob = client.get(f"{ADMIN}/users", params={"search": "bob"}, headers=viewer)
    assert bob.json()["items"][0]["groups"] == []


def test_staff_only_change_groups_that_grant_less(api: Api) -> None:
    """A group that grants what the actor lacks is out of reach."""
    client = api()
    headers = staff(
        "admin", "auth.change_group", "auth.delete_group", "shop.view_product"
    )
    leads = make_group("leads", "shop.delete_product", "shop.view_product")
    one = f"{ADMIN}/groups/{leads}"
    message = f"You can only change groups whose permissions you have; {LACK_DELETE}"

    for method, path, body in (
        ("PATCH", "", {"name": "bosses"}),
        ("PUT", "/permissions", {"permissions": ["shop.view_product"]}),
        ("DELETE", "", None),
    ):
        response = client.request(method, f"{one}{path}", json=body, headers=headers)
        assert error(response, 403) == ("permission_denied", message)

    root = staff("root", is_superuser=True)
    assert client.delete(one, headers=root).status_code == 204


def test_staff_list_permissions(api: Api) -> None:
    """Every permission with its content type."""
    client = api()
    headers = staff("admin", "auth.view_permission")

    listed = client.get(f"{ADMIN}/permissions", headers=headers).json()
    keys = [permission["key"] for permission in listed]
    assert "auth.add_user" in keys
    assert "shop.view_product" in keys
    assert len(keys) == len(set(keys))
    product = next(each for each in listed if each["key"] == "shop.view_product")
    assert product == {
        "key": "shop.view_product",
        "name": "Can view product",
        "content_type": "shop.product",
    }


@pytest.fixture
def swapped(
    database_url: str, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[TestClient]:
    """A project whose own user model (``request.param``) replaces the default."""
    domain, model, table = request.param
    registry.clear()
    with override_settings(
        INSTALLED_DOMAINS=[
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.audit",
            f"domainkit_tests.sample_domains.{domain}",
            SHOP,
            ALPHA,
        ],
        DATABASES={"default": {"URL": database_url}},
        MIGRATIONS_DIR=tmp_path / "migrations",
        ROOT_ROUTER="domainkit_tests.sample_auth_routes.router",
        MIDDLEWARE=[each for each in MIDDLEWARE if "idempotency" not in each],
        AUTH={
            "PASSWORD_HASHERS": [FAST_HASHER],
            "USER_MODEL": f"domainkit_tests.sample_domains.{domain}.models.{model}",
            "USER_TABLE": f"domainkit_tests.sample_domains.{domain}.tables.{table}",
        },
    ):
        setup()
        call_command("makemigrations", "-m", domain, stdout=io.StringIO())
        call_command("migrate", stdout=io.StringIO())
        with TestClient(get_asgi_application()) as client:
            yield client
    registry.clear()


MEMBERS = ("members", "Member", "members")
ACCOUNTS = ("accounts", "Account", "accounts")


@pytest.mark.parametrize("swapped", [MEMBERS], indirect=True)
def test_users_without_a_staff_flag(swapped: TestClient) -> None:
    """Superusers manage them; asking for a staff flag is refused."""
    client = swapped
    root = staff("root@example.org", is_staff=None, is_superuser=True)
    url = f"{ADMIN}/users"

    made = client.post(
        url, json={"username": "Ann@Example.org", "password": PASSWORD}, headers=root
    )
    assert made.status_code == 201, made.json()
    ann = made.json()
    assert (ann["username"], ann["is_staff"], ann["fields"]) == (
        "Ann@Example.org",
        False,
        {},
    )
    for response in (
        client.post(
            url, json={"username": "x@example.org", "is_staff": True}, headers=root
        ),
        client.patch(f"{url}/{ann['id']}", json={"is_staff": True}, headers=root),
    ):
        assert error(response, 422) == (
            "unknown_user_field",
            "This user model has no staff flag.",
        )
    unchanged = client.patch(
        f"{url}/{ann['id']}", json={"is_staff": False}, headers=root
    )
    assert error(unchanged, 422)[0] == "unknown_user_field"
    found = client.get(url, params={"search": "ANN@"}, headers=root).json()["items"]
    assert [user["id"] for user in found] == [ann["id"]]
    email = client.patch(
        f"{url}/{ann['id']}", json={"fields": {"email": "b@x.org"}}, headers=root
    )
    assert error(email, 422)[1] == "These user fields cannot be changed here: email."


@pytest.mark.parametrize("swapped", [ACCOUNTS], indirect=True)
def test_custom_user_fields(swapped: TestClient) -> None:
    """A custom model's listed fields are shown and changed; others never are."""
    client = swapped
    root = staff("root", is_superuser=True)
    url = f"{ADMIN}/users"

    made = client.post(
        url, json={"username": "carol", "fields": {"phone": "+20 100"}}, headers=root
    ).json()
    assert made["fields"]["phone"] == "+20 100"
    assert set(made["fields"]) == {
        "email",
        "email_verified",
        "first_name",
        "last_name",
        "date_joined",
        "phone",
    }
    for response in (
        client.post(
            url, json={"username": "dan", "fields": {"risk_flag": True}}, headers=root
        ),
        client.patch(
            f"{url}/{made['id']}", json={"fields": {"risk_flag": True}}, headers=root
        ),
    ):
        assert error(response, 422) == (
            "unknown_user_field",
            "These user fields cannot be changed here: risk_flag.",
        )
    changed = client.patch(
        f"{url}/{made['id']}", json={"fields": {"phone": "+20 111"}}, headers=root
    )
    assert changed.json()["fields"]["phone"] == "+20 111"
    refused = client.patch(
        f"{url}/{made['id']}", json={"fields": {"phone": None}}, headers=root
    )
    assert error(refused, 422) == (
        "invalid_user_field",
        "phone: Input should be a valid string.",
    )


def test_admin_rules_need_staff_and_the_model_permission(use_auth: UseAuth) -> None:
    """Called directly: the dependencies the endpoints declare at import."""
    use_auth()

    def allowed(dependency: Any, who: User) -> bool:  # noqa: ANN401 - Depends.
        try:
            asyncio.run(dependency.dependency(request("GET", who), None))
        except PermissionDenied:
            return False
        return True

    viewer = user("auth.view_user", is_staff=True)
    assert allowed(admin._users("view"), viewer)
    assert not allowed(admin._users("change"), viewer)
    assert not allowed(admin._users("view"), user("auth.view_user"))
    assert allowed(admin._users("delete"), user(is_superuser=True))
    assert allowed(admin._groups("change"), user("auth.change_group", is_staff=True))
    assert not allowed(admin._groups("change"), user("auth.change_user", is_staff=True))
    lister = user("auth.view_permission", is_staff=True)
    assert allowed(admin.staff("view", lambda: Permission), lister)
    assert not allowed(admin.staff("add", lambda: Permission), lister)

    rule = admin.HasModelPermission("delete", get_user_model)
    assert (rule.action, rule.model) == ("delete", get_user_model)
    assert admin._errors(401, 404) == {
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    }


def test_pages_read_one_row_more_and_track_what_they_load(auth_project: str) -> None:
    """``limit + 1`` rows tell whether more follow; changes to them commit."""
    for name in ("amy", "bob", "cat"):
        create_user(name)
        make_group(name)
    limits: list[tuple[object, ...]] = []

    def record(*args: Any) -> None:  # noqa: ANN401 - SQLAlchemy's arguments.
        if "LIMIT" in args[2]:
            limits.append(tuple(args[3]))

    async def scenario() -> list[object]:
        async with AuthUnitOfWork() as uow:
            users = await uow.users.page(limit=2)
            groups = await uow.groups.page(limit=2, after="amy")
            assert [each.get_username() for each in users.items] == ["amy", "bob"]
            assert [each.name for each in groups.items] == ["bob", "cat"]
            users.items[0].set_active(False)
            groups.items[0].rename("bea")
            await uow.commit()
        async with AuthUnitOfWork() as uow:
            amy = await uow.users.get_by_natural_key("amy")
            bea = await uow.groups.get_by_name("bea")
        return [*users.items[0].pull_events(), amy and amy.is_active, bea is None]

    event.listen(Engine, "before_cursor_execute", record)
    try:
        assert run_async(scenario()) == [False, False]
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    assert len(limits) == 2
    assert all(3 in each and 4 not in each for each in limits)


def test_pages_continue_after_a_name_and_search(auth_project: str) -> None:
    """``after`` and ``search`` narrow the users; groups page from the start."""
    for name in ("amy", "bob", "cat"):
        create_user(name)
        make_group(name)

    async def scenario() -> tuple[list[str], list[str], bool]:
        async with AuthUnitOfWork() as uow:
            users = await uow.users.page(limit=5, after="amy", search="B")
            groups = await uow.groups.page(limit=2)
        return (
            [user.get_username() for user in users.items],
            [group.name for group in groups.items],
            groups.has_more,
        )

    assert run_async(scenario()) == (["bob"], ["amy", "bob"], True)
