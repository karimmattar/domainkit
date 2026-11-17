"""Each change of the admin API is an act, authorized once (user report A6).

With ``AUTH.ADMIN_ACTION`` on (ADR 0163), every route of the admin API
that changes a user or a group asks for an authorization of that change,
``auth.admin_change``, in place of a step-up window: staff ask for one at
the route's twin (``POST <its path>/authorization``, or ``.../patch`` and
``.../delete`` after it where those share a path), confirm it, and send
it with the change, which spends it once, with its commit. The act binds
the user or group and what changes; the screen says both. Off (the
default), the admin API is as it was, and the twins answer ``404``.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from domainkit.clock import FrozenClock, freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.approvals import ApprovalsUnitOfWork
from domainkit.contrib.approvals.api import requests as approval_requests
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.auth import AuthSettings, GroupNotFound, StepUp, UserNotFound
from domainkit.contrib.auth.actions import (
    Action,
    Shown,
    action_for,
    register_action,
    spent_in_code,
)
from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.admin_actions import AdminAct, fit, fitted, listed, show
from domainkit.contrib.auth.apps import action_problems
from domainkit.contrib.auth.tables import authorizations
from domainkit.db import connections
from domainkit.ddd import Command, PermissionDenied, fingerprint
from domainkit.http import get_asgi_application
from domainkit.http.access import Require
from domainkit.http.routers import select_routes
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    Api,
    Migrated,
    Shape,
    UseAuth,
)
from domainkit_tests.support.auth import audited, create_user, grant
from domainkit_tests.test_approvals import APPROVALS, AUDIT, FAST, MESSAGING
from domainkit_tests.test_auth_admin import NEW_PASSWORD, join, make_group, token_for
from domainkit_tests.test_auth_stepup import enrol, now_code

ROUTES: Final = "domainkit_tests.sample_admin_action_routes"
SOURCE: Final = Path(__file__).parents[1] / "src"
ACTS: Final = Shape(
    "admin_acts",
    "approvals",
    {
        "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, MESSAGING, AUDIT, APPROVALS, ALPHA],
        "ROOT_ROUTER": f"{ROUTES}.router",
        "MIDDLEWARE": ["domainkit.http.locale.LocaleMiddleware", *MIDDLEWARE[:-1]],
        "AUTH": {
            "PASSWORD_HASHERS": [FAST_HASHER],
            "ADMIN_ACTION": True,
            "IMPERSONATION_TTL": timedelta(minutes=30),
        },
        "ERROR_FORMAT": "problem+json",
        "MESSAGING": FAST,
        "APPROVALS": {
            "SUBMIT_ACTION": True,
            "REJECT_ACTION": True,
            "CANCEL_ACTION": True,
            "ADMIN_APPROVALS": {"auth.set_staff": 1},
        },
    },
)
"""Every change of the admin API, and approvals' submit, reject and cancel,
an act; making staff waits for a superuser's approval too."""
ADMIN: Final = "/admin/auth"
START: Final = "2027-01-04T10:00:00Z"
PURPOSE: Final = "auth.admin_change"
STAFF: Final = (
    "auth.view_user",
    "auth.add_user",
    "auth.change_user",
    "auth.delete_user",
    "auth.view_group",
    "auth.add_group",
    "auth.change_group",
    "auth.delete_group",
    "approvals.view_approvalrequest",
)
type Headers = dict[str, str]


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """One clock for codes and authorizations."""
    with freeze_time(START) as clock:
        yield clock


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API, each admin change an act."""
    with migrated(ACTS), TestClient(get_asgi_application()) as test_client:
        yield test_client


class Staff:
    """A staff member with an authenticator app, who asks, confirms, and acts."""

    def __init__(
        self,
        http: TestClient,
        clock: FrozenClock,
        name: str,
        *permissions: str,
        superuser: bool = False,
    ) -> None:
        """Create ``name``, staff with ``STAFF`` and ``permissions``; enrol a factor."""
        self.id = create_user(name, is_staff=True, is_superuser=superuser)
        grant(name, *STAFF, *permissions)
        self.client = http
        self.clock = clock
        self.headers = token_for(name)
        self.secret = enrol(http, self.headers, clock)

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,  # noqa: ANN401 - JSON.
        authorization: str | None = None,
    ) -> Any:  # noqa: ANN401 - a response.
        """``method path`` with ``body``, sending ``authorization`` if given."""
        headers = dict(self.headers)
        if authorization is not None:
            headers["Action-Authorization"] = authorization
        return self.client.request(method, ADMIN + path, json=body, headers=headers)

    def ask(self, path: str, body: Any = None) -> Any:  # noqa: ANN401 - JSON.
        """Ask the twin at ``path`` for an authorization of ``body``'s change."""
        return self.call("POST", path, body)

    def confirm(self, authorization_id: str) -> str:
        """Confirm it with now's code; ``<id>.<code>``."""
        confirmed = self.client.post(
            f"/auth/authorizations/{authorization_id}/confirm",
            json={"code": now_code(self.secret, self.clock)},
            headers=self.headers,
        )
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def authorized(self, path: str, body: Any = None) -> str:  # noqa: ANN401 - JSON.
        """A confirmed authorization of the change the twin at ``path`` asks for."""
        asked = self.ask(path, body)
        assert asked.status_code == 201, asked.json()
        return self.confirm(asked.json()["id"])


def problem(response: Any, status: int, code: str) -> None:  # noqa: ANN401 - a response.
    """``response`` is the problem ``code`` with ``status``."""
    assert response.status_code == status, response.json()
    assert response.json()["code"] == code, response.json()


def row(authorization: str) -> dict[str, Any]:
    """The stored authorization that ``<id>.<code>`` names."""

    async def read() -> dict[str, Any]:
        async with connections.engine().connect() as connection:
            found = await connection.execute(
                select(authorizations).where(
                    authorizations.c.id == UUID(authorization.partition(".")[0])
                )
            )
            return dict(found.one()._mapping)

    return run_async(read())


def held_requests() -> int:
    """How many approval requests are stored."""

    async def count() -> int:
        async with ApprovalsUnitOfWork() as uow:
            found = await uow.session.execute(select(requests.c.id))
            return len(found.all())

    return run_async(count())


def test_off_by_default_changes_ask_nothing_and_the_twins_are_not_there(
    api: Api,
) -> None:
    """Off, a change needs no authorization, and every twin answers 404."""
    client = api()
    headers = token_for_staff()
    target = str(create_user("target"))

    twin = client.post(
        f"{ADMIN}/users/{target}/authorization/patch", json={}, headers=headers
    )
    changed = client.patch(
        f"{ADMIN}/users/{target}",
        json={"fields": {"first_name": "Ada"}},
        headers=headers,
    )

    assert AuthSettings().ADMIN_ACTION is False
    assert action_for(PURPOSE) is None
    problem(twin, 404, "not_found")
    assert changed.status_code == 200, changed.json()


def token_for_staff() -> Headers:
    """A superuser who is staff, on the ``api`` fixture's project."""
    create_user("root", is_staff=True, is_superuser=True)
    return token_for("root")


def test_a_change_without_an_authorization_asks_for_one(
    client: TestClient, frozen: FrozenClock
) -> None:
    """401 ``authorization_required``, naming the action; staff rules answer first."""
    ann = Staff(client, frozen, "ann")
    target = str(create_user("target"))
    create_user("stranger")
    stranger = token_for("stranger")

    refused = ann.call("PATCH", f"/users/{target}", {"fields": {"first_name": "X"}})
    plain = client.patch(f"{ADMIN}/users/{target}", json={}, headers=stranger)
    plain_twin = client.post(
        f"{ADMIN}/users/{target}/authorization/patch", json={}, headers=stranger
    )

    problem(refused, 401, "authorization_required")
    assert f'purpose="{PURPOSE}"' in refused.headers["WWW-Authenticate"]
    problem(plain, 403, "permission_denied")
    problem(plain_twin, 403, "permission_denied")
    shown = ann.call("GET", f"/users/{target}").json()
    assert shown["fields"]["first_name"] == ""
    assert action_for(PURPOSE) is not None


def test_staff_ask_confirm_and_change_once(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Asked, shown, confirmed, spent by the change with its commit; again, refused.

    The authorization is of this user (its subject) and of this change (its
    digest: what it adds and takes away, sorted).
    """
    ann = Staff(client, frozen, "ann")
    target = create_user("target")
    make_group("auditors")
    make_group("clerks")
    body = {"groups": ["clerks", "auditors"]}
    path = f"/users/{target}/groups"

    asked = ann.ask(f"{path}/authorization", body)
    authorization = ann.confirm(asked.json()["id"])
    changed = ann.call("PUT", path, body, authorization)
    again = ann.call("PUT", path, {"groups": []}, authorization)

    assert asked.status_code == 201, asked.json()
    assert asked.headers["Cache-Control"] == "no-store"
    assert asked.json()["purpose"] == PURPOSE
    assert asked.json()["shown"] == {
        "title": "Change a user's groups",
        "details": [
            {"label": "User", "value": "target"},
            {"label": "Adds", "value": "auditors, clerks"},
        ],
    }
    assert changed.status_code == 200, changed.json()
    assert changed.json()["groups"] == ["auditors", "clerks"]
    problem(again, 403, "action_not_authorized")
    stored = row(authorization)
    assert stored["subject"] == f"auth.user:{target}"
    assert stored["consumed_at"] is not None
    assert stored["digest"] == fingerprint(
        PURPOSE,
        {
            "kind": "groups",
            "user_id": str(target),
            "group_id": None,
            "name": None,
            "values": {},
            "added": ["auditors", "clerks"],
            "removed": [],
            "password": None,
        },
    )
    assert len(audited("auth.action_authorization_used")) == 1


def test_another_change_voids_the_authorization(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Sent with another change, or for another user, it is refused and voided."""
    ann = Staff(client, frozen, "ann")
    target = create_user("target")
    other = create_user("other")
    make_group("auditors")
    make_group("clerks")
    path = f"/users/{target}/groups"

    authorization = ann.authorized(f"{path}/authorization", {"groups": ["auditors"]})
    changed = ann.call("PUT", path, {"groups": ["clerks"]}, authorization)
    voided = ann.call("PUT", path, {"groups": ["auditors"]}, authorization)
    elsewhere = ann.authorized(f"{path}/authorization", {"groups": ["auditors"]})
    moved = ann.call(
        "PUT", f"/users/{other}/groups", {"groups": ["auditors"]}, elsewhere
    )

    problem(changed, 403, "action_not_authorized")
    problem(voided, 403, "action_not_authorized")
    problem(moved, 403, "action_not_authorized")
    assert ann.call("GET", f"/users/{target}").json()["groups"] == []
    assert ann.call("GET", f"/users/{other}").json()["groups"] == []
    assert row(authorization)["consumed_at"] is not None  # Voided.


def test_every_change_asks_and_its_twin_answers(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Each route that changes a user or a group: 401 bare, made once authorized."""
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target", email="target@example.com")
    victim = create_user("victim")
    crew = make_group("crew")
    gone = make_group("gone")
    changes: list[tuple[str, str, str, Any, int]] = [
        ("POST", "/users", "/users/authorization", {"username": "made"}, 201),
        (
            "PATCH",
            f"/users/{target}",
            f"/users/{target}/authorization/patch",
            {"fields": {"first_name": "Ada"}},
            200,
        ),
        (
            "POST",
            f"/users/{target}/password",
            f"/users/{target}/password/authorization",
            {"password": "Zq8!wL-d4#Kp7v"},
            204,
        ),
        (
            "POST",
            f"/users/{target}/logout",
            f"/users/{target}/logout/authorization",
            None,
            204,
        ),
        (
            "POST",
            f"/users/{target}/impersonate",
            f"/users/{target}/impersonate/authorization",
            {"reason": "Ticket 42"},
            201,
        ),
        (
            "PUT",
            f"/users/{target}/groups",
            f"/users/{target}/groups/authorization",
            {"groups": ["crew"]},
            200,
        ),
        (
            "PUT",
            f"/users/{target}/permissions",
            f"/users/{target}/permissions/authorization",
            {"permissions": ["auth.view_user"]},
            200,
        ),
        (
            "DELETE",
            f"/users/{victim}",
            f"/users/{victim}/authorization/delete",
            None,
            204,
        ),
        ("POST", "/groups", "/groups/authorization", {"name": "made"}, 201),
        (
            "PATCH",
            f"/groups/{crew}",
            f"/groups/{crew}/authorization/patch",
            {"name": "team"},
            200,
        ),
        (
            "PUT",
            f"/groups/{crew}/permissions",
            f"/groups/{crew}/permissions/authorization",
            {"permissions": ["auth.view_group"]},
            200,
        ),
        (
            "DELETE",
            f"/groups/{gone}",
            f"/groups/{gone}/authorization/delete",
            None,
            204,
        ),
    ]

    for method, path, twin, body, success in changes:
        bare = root.call(method, path, body)
        authorization = root.authorized(twin, body)
        made = root.call(method, path, body, authorization)

        problem(bare, 401, "authorization_required")
        assert made.status_code == success, (method, path, made.json())
        assert row(authorization)["consumed_at"] is not None, (method, path)
    assert len(audited("auth.action_authorization_used")) == len(changes)


def test_the_screen_names_the_user_or_group_and_the_change(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Which user or group, by name; what changes; six values at most."""
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target", email="target@example.com")
    crew = make_group("crew")
    join("target", "crew")

    def shown(twin: str, body: Any = None) -> dict[str, Any]:  # noqa: ANN401 - JSON.
        asked = root.ask(twin, body)
        assert asked.status_code == 201, asked.json()
        found: dict[str, Any] = asked.json()["shown"]
        return found

    def pairs(found: dict[str, Any]) -> list[tuple[str, str]]:
        return [(each["label"], each["value"]) for each in found["details"]]

    unusable = shown(f"/users/{target}/password/authorization", {"password": None})
    reason = shown(
        f"/users/{target}/impersonate/authorization", {"reason": "Ticket\n42"}
    )
    many = shown(
        f"/users/{target}/authorization/patch",
        {
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
            "fields": {
                "email": "ada@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
            },
        },
    )
    created = shown(
        "/users/authorization",
        {"username": "made", "password": "Zq8!wL-d4#Kp7v", "is_staff": False},
    )
    renamed = shown(f"/groups/{crew}/authorization/patch", {"name": "team"})
    deleted = shown(f"/groups/{crew}/authorization/delete")
    logged_out = shown(f"/users/{target}/logout/authorization")
    left = shown(f"/users/{target}/groups/authorization", {"groups": []})
    granting = shown(
        "/groups/authorization", {"name": "made", "permissions": ["auth.view_user"]}
    )

    assert unusable["title"] == "Make a user's password unusable"
    assert pairs(unusable) == [("User", "target")]
    assert reason["title"] == "Act as a user"
    assert pairs(reason) == [("User", "target"), ("Reason", "Ticket\\n42")]
    assert many["title"] == "Change a user"
    assert pairs(many) == [
        ("User", "target"),
        ("is_superuser", "false"),
        ("is_staff", "false"),
        ("is_active", "true"),
        ("email", "ada@example.com"),
        ("…", "2 more values"),
    ]
    assert created["title"] == "Create a user"
    assert pairs(created) == [
        ("Username", "made"),
        ("is_active", "true"),
        ("Password", "set"),
    ]
    assert renamed["title"] == "Rename a group"
    assert pairs(renamed) == [("Group", "crew"), ("New name", "team")]
    assert (deleted["title"], pairs(deleted)) == ("Delete a group", [("Group", "crew")])
    assert logged_out["title"] == "End a user's sessions"
    assert pairs(left) == [("User", "target"), ("Takes away", "crew")]
    assert granting["title"] == "Create a group"
    assert pairs(granting) == [("Group", "made"), ("Adds", "auth.view_user")]


def test_a_user_or_group_gone_is_not_shown(client: TestClient) -> None:
    """Asking about one deleted meanwhile is refused as the change would be."""
    nobody = UUID(int=1)

    with pytest.raises(UserNotFound):
        run_async(show(AdminAct(kind="logout", user_id=nobody)))
    with pytest.raises(GroupNotFound):
        run_async(show(AdminAct(kind="group_delete", group_id=nobody)))


def test_flags_are_never_hidden_and_a_cut_list_says_how_many_more(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Making a superuser shows, whatever else changes; so does every group added.

    The flags that grant or take access come first, before the fields,
    which are counted past what fits; a list too long for a line shows its
    first items and how many more, so the last one added is never dropped
    unsaid.
    """
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target", email="target@example.com")
    names = [f"aaa_harmless_{each}" for each in range(5)] + ["zz_admins"]
    for name in names:
        make_group(name)
    body = {
        "is_active": True,
        "is_staff": False,
        "is_superuser": True,
        "fields": {"email": "a@example.com", "first_name": "A", "last_name": "B"},
    }

    making = root.ask(f"/users/{target}/authorization/patch", body)
    joining = root.ask(f"/users/{target}/groups/authorization", {"groups": names})

    assert making.status_code == 201, making.json()
    shown = {each["label"]: each["value"] for each in making.json()["shown"]["details"]}
    assert shown["is_superuser"] == "true"
    assert joining.status_code == 201, joining.json()
    assert joining.json()["shown"]["details"][1] == {
        "label": "Adds",
        "value": "aaa_harmless_0, aaa_harmless_1, aaa_harmless_2 and 3 more",
    }


def test_a_list_is_cut_to_fit_with_its_count() -> None:
    """All that fit, then how many more; one item too long alone is cut."""
    long = "g" * 100

    assert listed(("a", "b")) == "a, b"
    assert listed((long,)) == "g" * 63 + "…"
    cut = listed((long, "b", "c"))
    assert cut.endswith("… and 2 more")
    assert len(cut) == 64
    assert listed(tuple(f"group_{each:02}" for each in range(9))) == (
        "group_00, group_01, group_02, group_03, group_04 and 4 more"
    )


def test_creating_binds_whether_the_user_is_active(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Authorized to create an inactive user, staff cannot create an active one."""
    root = Staff(client, frozen, "root", superuser=True)
    inactive = {"username": "made", "is_active": False}

    asked = root.ask("/users/authorization", inactive)
    authorization = root.confirm(asked.json()["id"])
    active = root.call("POST", "/users", {"username": "made"}, authorization)

    assert asked.json()["shown"]["details"][1] == {
        "label": "is_active",
        "value": "false",
    }
    problem(active, 403, "action_not_authorized")
    assert root.call("GET", "/users", None).json()["items"][0]["username"] == "root"


def test_every_twin_is_marked_and_named_by_its_route(client: TestClient) -> None:
    """Each hand-written twin carries the twins' marker; shared paths name methods.

    ``domainkit_twin`` is the attribute by which
    ``domainkit.resources.asking_authorization`` (user report A17) tells a twin's
    request apart. A ``PATCH`` and a ``DELETE`` of one path name their
    method; a route alone on its path keeps ``<path>/authorization``.
    """
    twins = {
        route.path: route.endpoint
        for router in (admin_router, approval_requests)
        for route in router.routes
        if isinstance(route, APIRoute) and "/authorization" in route.path
    }

    assert set(twins) >= {
        "/users/authorization",
        "/users/{user_id}/authorization/patch",
        "/users/{user_id}/authorization/delete",
        "/users/{user_id}/password/authorization",
        "/groups/{group_id}/authorization/patch",
        "/groups/{group_id}/authorization/delete",
        "/requests/authorization",
    }
    assert "/users/{user_id}/authorization" not in twins
    assert "/users/{user_id}/delete/authorization" not in twins
    hand_written = {
        path: endpoint
        for path, endpoint in twins.items()
        if not path.startswith("/requests/{request_id}")
    }
    assert len(hand_written) == 13
    assert all(
        getattr(each, "domainkit_twin", None) is True for each in hand_written.values()
    )


def test_the_admin_api_loads_no_act_while_the_setting_is_off() -> None:
    """Importing the admin API leaves ``admin_actions`` unloaded.

    A project that keeps ``ADMIN_ACTION`` off loads no module more than
    before.
    """
    probe = (
        "import sys, domainkit.contrib.auth.admin;"
        "print('domainkit.contrib.auth.admin_actions' in sys.modules)"
    )
    found = subprocess.run(  # noqa: S603 - the test interpreter and a fixed script.
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
    )

    assert found.stdout.strip() == "False", found.stderr


def test_details_fit_a_screen() -> None:
    """Printable, cut to 64 characters, six at most; a taken label is counted."""
    long = "x" * 100

    assert fitted("a\nb") == "a\\nb"
    assert fitted(long) == "x" * 63 + "…"
    assert fit([("User", "ann")], [("User", "again"), ("b", "2")]) == [
        ("User", "ann"),
        ("b", "2"),
        ("…", "1 more value"),
    ]
    assert len(fit([], [(f"k{each}", long) for each in range(9)])) == 6
    shown = Shown("t", fit([("User", "ann")], [(f"k{each}", "v") for each in range(5)]))
    assert shown.details == (
        ("User", "ann"),
        *((f"k{each}", "v") for each in range(5)),
    )


def test_the_twin_refuses_what_the_change_would(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Staff ask only for a change they could make: their bounds, the user, the reason.

    A permission they lack, a superuser they may not change, themselves to
    impersonate, a password the validators refuse, a user who is not there,
    the last superuser deleted or deactivated, a user created staff while
    making staff waits for approval.
    """
    ann = Staff(client, frozen, "ann")
    target = create_user("target")
    boss = create_user("boss", is_superuser=True)

    lacking = ann.ask(
        f"/users/{target}/permissions/authorization",
        {"permissions": ["approvals.add_approvalrequest"]},
    )
    superior = ann.ask(f"/users/{boss}/logout/authorization")
    themselves = ann.ask(
        f"/users/{ann.id}/impersonate/authorization", {"reason": "Ticket 42"}
    )
    weak = ann.ask(f"/users/{target}/password/authorization", {"password": "123"})
    missing = ann.ask(f"/users/{UUID(int=1)}/authorization/delete")

    problem(lacking, 403, "permission_denied")
    problem(superior, 403, "permission_denied")
    problem(themselves, 403, "permission_denied")
    assert weak.status_code == 422, weak.json()
    problem(missing, 404, "user_not_found")


def test_the_twins_keep_the_last_superuser_and_refuse_creating_staff_held(
    client: TestClient, frozen: FrozenClock
) -> None:
    """What the change refuses after the project's checks, its twin refuses too.

    Deleting or deactivating the last active superuser; creating a user who
    is staff while making staff waits for approval (``ADMIN_APPROVALS``).
    """
    root = Staff(client, frozen, "root", superuser=True)

    last = root.ask(f"/users/{root.id}/authorization/delete")
    demoted = root.ask(f"/users/{root.id}/authorization/patch", {"is_active": False})
    kept = root.ask(f"/users/{root.id}/authorization/patch", {"is_active": True})
    staffed = root.ask("/users/authorization", {"username": "made", "is_staff": True})

    problem(last, 409, "last_superuser")
    problem(demoted, 409, "last_superuser")
    assert kept.status_code == 201, kept.json()
    problem(staffed, 403, "approval_required")


def test_a_change_refused_after_its_checks_spends_nothing(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Spent with the change's commit: a refused change leaves it unspent.

    A username taken is found by the change alone, after the spend, in its
    transaction: the change is refused, and the authorization holds still.
    """
    root = Staff(client, frozen, "root", superuser=True)
    create_user("made")
    body = {"username": "made"}

    authorization = root.authorized("/users/authorization", body)
    refused = root.call("POST", "/users", body, authorization)

    assert refused.status_code == 409, refused.json()
    assert row(authorization)["consumed_at"] is None


def test_a_held_change_is_checked_before_it_waits_and_spent_once_approved(
    client: TestClient, frozen: FrozenClock
) -> None:
    """With ``ADMIN_APPROVALS``: the authorization first, the approval, then the change.

    An authorization of another change is refused before the change is
    submitted for approval. A change that waits spends nothing; once
    approved, the same call with the same authorization makes it, once.
    """
    ann = Staff(client, frozen, "ann")
    root = Staff(client, frozen, "root", superuser=True)
    target = create_user("target")
    path = f"/users/{target}"
    making = {"is_staff": True}

    other = ann.authorized(f"{path}/authorization/patch", {"is_active": True})
    mismatched = ann.call("PATCH", path, making, other)
    held = held_requests()
    authorization = ann.authorized(f"{path}/authorization/patch", making)
    waiting = ann.call("PATCH", path, making, authorization)
    request_id = waiting.json()["approval_request_id"]
    read = client.get(f"/approvals/requests/{request_id}", headers=root.headers)
    approval = root.client.post(
        f"/approvals/requests/{request_id}/approve",
        json={"fingerprint": read.json()["fingerprint"]},
        headers=root.headers,
    )
    unspent = row(authorization)["consumed_at"]
    made = ann.call("PATCH", path, making, authorization)

    problem(mismatched, 403, "action_not_authorized")
    assert held == 0
    problem(waiting, 403, "approval_pending")
    assert approval.status_code == 200, approval.json()
    assert unspent is None
    assert made.status_code == 200, made.json()
    assert made.json()["is_staff"] is True
    assert row(authorization)["consumed_at"] is not None


def test_the_twin_runs_the_projects_checks_and_holds_nothing(
    client: TestClient, frozen: FrozenClock
) -> None:
    """A check that refuses the change refuses its twin, before the confirmation.

    Before 2.7 the twins ran no ``on_admin_change`` check: the phone
    confirmed a change the project then refused (user report A17). The
    checks see ``asking``: approvals' hold submits nothing at a twin, and
    the change held is submitted when it is made. A group's twin runs the
    ``on_admin_group_change`` checks the same way.
    """
    from domainkit.contrib.auth.gates import (
        AdminChange,
        GroupChange,
        on_admin_change,
        on_admin_group_change,
    )

    ann = Staff(client, frozen, "ann")
    target = create_user("target")
    group = make_group("clerks")
    seen: list[bool] = []

    async def not_yourself(actor: Any, user: Any, change: AdminChange) -> None:  # noqa: ANN401
        seen.append(change.asking)
        if user is not None and user.id == actor.id:
            raise PermissionDenied("Nobody changes their own account here.")

    async def no_admins(actor: Any, group: Any, change: GroupChange) -> None:  # noqa: ANN401
        seen.append(change.asking)
        if change.name == "admins":
            raise PermissionDenied("No group is called admins here.")

    on_admin_change(not_yourself, domain="alpha")
    on_admin_group_change(no_admins, domain="alpha")
    own = ann.ask(
        f"/users/{ann.id}/authorization/patch", {"fields": {"last_name": "A"}}
    )
    renamed = ann.ask(f"/groups/{group}/authorization/patch", {"name": "admins"})
    making = ann.ask(f"/users/{target}/authorization/patch", {"is_staff": True})
    held = held_requests()
    authorization = ann.confirm(making.json()["id"])
    waiting = ann.call("PATCH", f"/users/{target}", {"is_staff": True}, authorization)

    problem(own, 403, "permission_denied")
    assert own.json()["detail"] == "Nobody changes their own account here."
    problem(renamed, 403, "permission_denied")
    assert making.status_code == 201, making.json()
    assert held == 0
    problem(waiting, 403, "approval_pending")
    assert seen == [True, True, True, False]


def test_a_reused_password_spends_its_authorization(
    client: TestClient, frozen: FrozenClock
) -> None:
    """One confirmation, one guess at the user's old passwords.

    The authorization binds only that a password is set. Before 2.7 a
    reused one was refused and spent nothing, so one confirmation tested
    password after password against the user's history (user report A17).
    """
    ann = Staff(client, frozen, "ann")
    target = create_user("target", password=NEW_PASSWORD)
    path = f"/users/{target}/password"
    history = {**ACTS.settings["AUTH"], "PASSWORD_HISTORY": 2}

    with override_settings(AUTH=history):
        authorization = ann.authorized(
            f"{path}/authorization", {"password": NEW_PASSWORD}
        )
        reused = ann.call("POST", path, {"password": NEW_PASSWORD}, authorization)
        again = ann.call("POST", path, {"password": "Other-pass-42"}, authorization)

    problem(reused, 422, "invalid_password")
    assert row(authorization)["consumed_at"] is not None
    problem(again, 403, "action_not_authorized")


PASSWORDS_HELD: Final = Shape(
    "admin_acts_passwords_held",
    "approvals",
    {
        **ACTS.settings,
        "AUTH": {**ACTS.settings["AUTH"], "PASSWORD_HISTORY": 3},
        "APPROVALS": {
            **ACTS.settings["APPROVALS"],
            "ADMIN_APPROVALS": {"auth.set_password": 1},
        },
    },
)
"""Each change an act; setting a password waits for a superuser's approval."""


def test_nothing_is_compared_while_a_password_waits_for_approval(
    migrated: Migrated, frozen: FrozenClock
) -> None:
    """Every password answers ``approval_pending`` while the change waits.

    The history is compared when the change is about to be made: once
    the approval is found, before it is spent. A comparison before the
    hold answered guess after guess on one authorization, misses free,
    while the change waited (found in review of user report A17). Once
    approved, a reused password is refused, spends the authorization, and
    leaves the approval for a password that will do.
    """
    with migrated(PASSWORDS_HELD), TestClient(get_asgi_application()) as client:
        ann = Staff(client, frozen, "ann")
        root = Staff(client, frozen, "root", superuser=True)
        target = create_user("target", password=NEW_PASSWORD)
        path = f"/users/{target}/password"
        authorization = ann.authorized(
            f"{path}/authorization", {"password": "x-Guess-0000"}
        )
        guesses = [f"x-Guess-{each:04d}" for each in range(5)] + [NEW_PASSWORD]
        answers = {
            (answer.status_code, answer.json()["code"])
            for answer in (
                ann.call("POST", path, {"password": guess}, authorization)
                for guess in guesses
            )
        }
        unspent = row(authorization)["consumed_at"]
        request_id = (
            ann.call("POST", path, {"password": NEW_PASSWORD}, authorization)
        ).json()["approval_request_id"]
        shown = client.get(f"/approvals/requests/{request_id}", headers=root.headers)
        approved = client.post(
            f"/approvals/requests/{request_id}/approve",
            json={"fingerprint": shown.json()["fingerprint"]},
            headers=root.headers,
        )
        reused = ann.call("POST", path, {"password": NEW_PASSWORD}, authorization)
        spent = row(authorization)["consumed_at"]
        still = client.get(f"/approvals/requests/{request_id}", headers=ann.headers)
        fresh = ann.authorized(f"{path}/authorization", {"password": "Fresh-pass-42"})
        made = ann.call("POST", path, {"password": "Fresh-pass-42"}, fresh)

    assert answers == {(403, "approval_pending")}
    assert unspent is None
    assert approved.status_code == 200, approved.json()
    problem(reused, 422, "invalid_password")
    assert spent is not None
    assert still.json()["status"] == "approved"
    assert made.status_code == 204


def test_the_act_digest_is_the_change_sorted_never_a_password() -> None:
    """Groups and permissions sorted; values as JSON; a password only as set or not."""
    from domainkit.contrib.auth.gates import AdminChange

    change = AdminChange(
        "groups", added=frozenset({"b", "a", "c"}), removed=frozenset({"z", "y"})
    )
    act = AdminAct.of(None, change)
    password = AdminAct.of(None, AdminChange("password"), password=False)

    assert (act.added, act.removed) == (("a", "b", "c"), ("y", "z"))
    assert password.model_dump(mode="json") == {
        "kind": "password",
        "user_id": None,
        "group_id": None,
        "name": None,
        "values": {},
        "added": [],
        "removed": [],
        "password": False,
    }


class Retitle(Command):
    """Give an account a new title."""

    account_id: UUID
    title: str


RETITLE: Final = Action(
    "accounts.retitle",
    command=Retitle,
    subject=lambda command: f"accounts.account:{command.account_id}",
    show=lambda command: Shown("Retitle an account", {"Title": command.title}),
)
"""An act a project spends in code of its own, with ``spend_authorization``."""


def test_check_accepts_an_act_spent_in_code(use_auth: UseAuth) -> None:
    """``auth.E110`` passes a route whose act code spends, once it says so.

    Before, it took only a service decorated ``requires_authorization``: a
    route of the admin API, whose acts are spent in its own code, was
    refused, and so was a project's act spent with ``spend_authorization``.
    """
    use_auth(ADMIN_ACTION=True)
    register_action(RETITLE, domain="alpha")
    app = FastAPI()
    asking = [Depends(Require(StepUp(for_=PURPOSE)))]
    app.include_router(
        select_routes(admin_router, dependencies={"DELETE /users/{user_id}": asking})
    )
    project = APIRouter()

    @project.post("/accounts/{account_id}/title")
    async def give_title(account_id: UUID) -> None:
        """A route of the project's that asks for the act it spends in code."""

    app.include_router(project, dependencies=[Depends(Require(StepUp(for_=RETITLE)))])

    before = [problem.id for problem in action_problems(app)]
    spent_in_code(RETITLE)
    after = action_problems(app)

    assert before == ["auth.E110"]
    assert after == []


def test_the_screen_speaks_the_request_language(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The title and labels in the request's language: Arabic here."""
    ann = Staff(client, frozen, "ann")
    target = create_user("target")

    asked = client.post(
        f"{ADMIN}/users/{target}/logout/authorization",
        headers={**ann.headers, "Accept-Language": "ar"},
    )

    assert asked.status_code == 201, asked.json()
    assert asked.json()["shown"]["title"] == "إنهاء جلسات مستخدم"
    assert asked.json()["shown"]["details"][0]["label"] == "المستخدم"


def test_override_keeps_the_twins_closed_until_the_setting(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Turned off at run time, the routes ask nothing and the twins answer 404."""
    ann = Staff(client, frozen, "ann")
    target = create_user("target")

    with override_settings(
        AUTH={**ACTS.settings["AUTH"], "ADMIN_ACTION": False},
    ):
        twin = ann.ask(f"/users/{target}/logout/authorization")
        made = ann.call("POST", f"/users/{target}/logout")

    problem(twin, 404, "not_found")
    assert made.status_code == 204
