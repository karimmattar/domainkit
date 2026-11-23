"""Changes of the admin API held for approval (user report A9).

``APPROVALS.ADMIN_APPROVALS`` names the changes of auth's admin API that
wait for other people: making staff, adding groups or permissions,
setting a password, impersonating. The admin API submits the change as the
staff member's request and answers ``403 approval_pending`` with its id;
once approved, the same call, made again by the same staff member, makes
exactly that change, once.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.checks import CheckMessage
from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.approvals import (
    IMPERSONATE,
    REACTIVATE,
    SET_GROUP_PERMISSIONS,
    SET_GROUPS,
    SET_STAFF,
    ApprovalRequest,
    ApprovalsSettings,
    ApprovalsUnitOfWork,
    HeldGroup,
    Reactivate,
    SetGroupPermissions,
    SetGroups,
    SetStaff,
    approvers_of,
    fingerprint,
    guards,
)
from domainkit.contrib.approvals.authorizing import details_of, shown_of
from domainkit.contrib.approvals.builtin import (
    _updated,
    check_admin_change,
    check_group_change,
)
from domainkit.contrib.approvals.services import _spend
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.auth import (
    AbstractBaseUser,
    AuthUnitOfWork,
    BaseBackend,
    Group,
    User,
)
from domainkit.contrib.auth.backends import ObjectGrant
from domainkit.contrib.auth.gates import (
    AdminChange,
    GroupChange,
    on_admin_change,
)
from domainkit.contrib.auth.screens import TooMuchToShow, laid_out
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.i18n import gettext, override
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA, Migrated, Shape
from domainkit_tests.support.auth import create_user, grant
from domainkit_tests.test_admin_actions import ACTS, Staff, end_of
from domainkit_tests.test_admin_actions import frozen as frozen  # The fixture.
from domainkit_tests.test_approvals import APPROVALS, AUDIT, MESSAGING
from domainkit_tests.test_approvals_api import APPROVALS_API, problem
from domainkit_tests.test_auth_admin import NEW_PASSWORD, join, make_group, token_for

ADMIN: Final = "/admin/auth"
API: Final = "/approvals/requests"
APPROVALS_ADMIN: Final = Shape(
    "approvals_admin",
    "approvals",
    {
        **APPROVALS_API.settings,
        "AUTH": {
            **APPROVALS_API.settings["AUTH"],
            "IMPERSONATION_TTL": timedelta(minutes=30),
        },
        "APPROVALS": {
            "ADMIN_APPROVALS": {
                "auth.set_staff": 1,
                "auth.set_groups": 1,
                "auth.set_permissions": 1,
                "auth.set_password": 1,
                "auth.impersonate": 2,
                "auth.reactivate": 1,
                "auth.set_group_permissions": 1,
            },
        },
    },
)
"""Every admin change held: one superuser approves each, two an impersonation."""
STAFF: Final = (
    "auth.view_user",
    "auth.add_user",
    "auth.change_user",
    "approvals.view_approvalrequest",
)
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API, every admin change held for approval."""
    with migrated(APPROVALS_ADMIN), TestClient(get_asgi_application()) as test_client:
        yield test_client


def staff(name: str, *permissions: str, superuser: bool = False) -> Headers:
    """A staff member granted ``permissions`` (a superuser too, if asked)."""
    create_user(name, is_staff=True, is_superuser=superuser)
    grant(name, *STAFF, *permissions)
    return token_for(name)


def pending(response: Any) -> str:  # noqa: ANN401 - a response.
    """The id of the approval request ``response`` says the change waits for."""
    problem(response, 403, "approval_pending")
    return str(response.json()["approval_request_id"])


def approve(client: TestClient, request_id: str, headers: Headers) -> Any:  # noqa: ANN401 - JSON.
    """Approve the request ``request_id``, as read by its approver; the answer."""
    shown = client.get(f"{API}/{request_id}", headers=headers).json()
    return client.post(
        f"{API}/{request_id}/approve",
        json={"fingerprint": shown["fingerprint"]},
        headers=headers,
    )


def read(client: TestClient, request_id: str, headers: Headers) -> dict[str, Any]:
    """The request ``request_id`` as ``headers``' caller reads it."""
    found = client.get(f"{API}/{request_id}", headers=headers)
    assert found.status_code == 200, found.json()
    body: dict[str, Any] = found.json()
    return body


def user_of(user_id: UUID) -> User:
    """The user ``user_id``, as stored now."""

    async def load() -> User:
        async with AuthUnitOfWork() as uow:
            return await uow.users.get(user_id)  # type: ignore[arg-type,return-value]

    return run_async(load())


def test_making_staff_waits_then_the_same_call_makes_it_once(
    client: TestClient,
) -> None:
    """Submitted, found again, approved, made by the maker's call; unmaking is free.

    The worker runs nothing: the change is the admin API's, made by the
    maker's own call once the request is approved.
    """
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob")

    first = pending(
        client.patch(f"{ADMIN}/users/{bob}", json={"is_staff": True}, headers=ann)
    )
    again = pending(
        client.patch(f"{ADMIN}/users/{bob}", json={"is_staff": True}, headers=ann)
    )
    request = read(client, first, ann)
    approved = approve(client, first, root)
    run_async(drain())
    still = user_of(bob).is_staff
    made = client.patch(f"{ADMIN}/users/{bob}", json={"is_staff": True}, headers=ann)
    done = read(client, first, ann)
    unmade = client.patch(f"{ADMIN}/users/{bob}", json={"is_staff": False}, headers=ann)

    assert again == first
    assert (request["action"], request["policy"], request["status"]) == (
        SET_STAFF,
        "auth.staff",
        "pending",
    )
    assert request["arguments"] == {
        "user_id": str(bob),
        "is_superuser": False,
        "groups": [],
        "permissions": [],
        "object_grants": 0,
        "object_grants_digest": "",
    }
    assert request["maker"] == str(user_of_name("ann"))
    assert approved.json()["status"] == "approved", approved.json()
    assert still is False
    assert made.status_code == 200, made.json()
    assert made.json()["is_staff"] is True
    assert done["status"] == "executed"
    assert unmade.status_code == 200, unmade.json()
    assert unmade.json()["is_staff"] is False


def user_of_name(username: str) -> UUID:
    """The id of ``username``."""

    async def load() -> UUID:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(username)
            assert found is not None
            return found.id

    return run_async(load())


def test_an_approval_is_the_makers_and_the_user_it_is_about_does_not_decide(
    client: TestClient,
) -> None:
    """Another staff member's call submits its own; the user made staff may not approve.

    Approvers are superusers by default (``ADMIN_APPROVERS`` unset): staff
    who may change users are not.
    """
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    sue = create_user("sue", is_superuser=True)
    grant("sue", "approvals.view_approvalrequest")
    carol = staff("carol")

    made = pending(
        client.patch(f"{ADMIN}/users/{sue}", json={"is_staff": True}, headers=root)
    )
    by_sue = approve(client, made, token_for("sue"))
    by_carol = client.post(
        f"{API}/{made}/approve", json={"fingerprint": "0" * 64}, headers=carol
    )
    assert approve(client, made, other).json()["status"] == "approved"
    by_other = pending(
        client.patch(f"{ADMIN}/users/{sue}", json={"is_staff": True}, headers=other)
    )

    problem(by_sue, 403, "interested_party")
    problem(by_carol, 403, "permission_denied")
    assert by_other != made
    assert user_of(sue).is_staff is False


def test_a_change_of_groups_is_bound_to_what_it_adds_and_removes(
    client: TestClient,
) -> None:
    """The approved change is the one made; another needs its own approval.

    A change that only takes groups away waits for no one. Each group
    added is held by its id and the permissions it grants (user report A21).
    """
    ann = staff("ann", "accounting.view_account", "accounting.view_entry")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    viewers = make_group("viewers", "accounting.view_account")
    readers = make_group("readers", "accounting.view_entry")
    path = f"{ADMIN}/users/{bob}/groups"

    first = pending(client.put(path, json={"groups": ["viewers"]}, headers=ann))
    shown = read(client, first, ann)
    approve(client, first, root)
    wider = pending(
        client.put(path, json={"groups": ["viewers", "readers"]}, headers=ann)
    )
    joined = client.put(path, json={"groups": ["viewers"]}, headers=ann)
    swapped = pending(client.put(path, json={"groups": ["readers"]}, headers=ann))
    swap = read(client, swapped, ann)
    left = client.put(path, json={"groups": []}, headers=ann)

    assert shown["arguments"] == {
        "user_id": str(bob),
        "added": ["viewers"],
        "removed": [],
        "groups": [bound_group("viewers", viewers, "accounting.view_account")],
    }
    assert wider != first
    assert joined.status_code == 200, joined.json()
    assert joined.json()["groups"] == ["viewers"]
    assert read(client, first, ann)["status"] == "executed"
    assert read(client, wider, ann)["status"] == "pending"
    assert swap["arguments"] == {
        "user_id": str(bob),
        "added": ["readers"],
        "removed": ["viewers"],
        "groups": [bound_group("readers", readers, "accounting.view_entry")],
    }
    assert left.status_code == 200, left.json()
    assert left.json()["groups"] == []


def bound_group(name: str, group_id: UUID, *permissions: str) -> dict[str, Any]:
    """A group as ``SetGroups`` holds it: id, name, permissions, no object grants."""
    return {
        "id": str(group_id),
        "name": name,
        "permissions": sorted(permissions),
        "object_grants": 0,
        "object_grants_digest": "",
    }


def test_the_bounds_of_staff_are_checked_before_anything_is_submitted(
    client: TestClient,
) -> None:
    """A group or a permission the maker lacks is refused; no request is made."""
    ann = staff("ann")
    bob = create_user("bob")
    make_group("viewers", "accounting.view_account")

    grouped = client.put(
        f"{ADMIN}/users/{bob}/groups", json={"groups": ["viewers"]}, headers=ann
    )
    granted = client.put(
        f"{ADMIN}/users/{bob}/permissions",
        json={"permissions": ["accounting.view_account"]},
        headers=ann,
    )
    weak = client.post(
        f"{ADMIN}/users/{bob}/password", json={"password": "x"}, headers=ann
    )

    problem(grouped, 403, "permission_denied")
    problem(granted, 403, "permission_denied")
    assert weak.status_code == 422, weak.json()
    assert client.get(API, headers=ann).json()["items"] == []


def test_permissions_wait_for_their_approval(client: TestClient) -> None:
    """Granting a permission is held; taking one away is not."""
    ann = staff("ann", "accounting.view_account")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    path = f"{ADMIN}/users/{bob}/permissions"
    body = {"permissions": ["accounting.view_account"]}

    held = pending(client.put(path, json=body, headers=ann))
    arguments = read(client, held, ann)["arguments"]
    approve(client, held, root)
    granted = client.put(path, json=body, headers=ann)
    revoked = client.put(path, json={"permissions": []}, headers=ann)

    assert arguments == {
        "user_id": str(bob),
        "added": ["accounting.view_account"],
        "removed": [],
    }
    assert granted.json()["permissions"] == ["accounting.view_account"]
    assert revoked.status_code == 200, revoked.json()
    assert revoked.json()["permissions"] == []


def test_a_password_is_never_stored_with_its_request(client: TestClient) -> None:
    """The request names the user alone; once approved, one password is set.

    Approvers could not judge a password, so it is not bound: the call made
    again may set another. The approval is spent: a third call waits again.
    """
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    path = f"{ADMIN}/users/{bob}/password"

    async def password_hash() -> object:
        async with AuthUnitOfWork() as uow:
            return await uow.users.password_hash(bob)

    async def stored() -> list[str]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(select(requests.c.arguments))
            return [str(row.arguments) for row in rows]

    before = run_async(password_hash())
    held = pending(client.post(path, json={"password": NEW_PASSWORD}, headers=ann))
    arguments = read(client, held, ann)["arguments"]
    unchanged = run_async(password_hash())
    approve(client, held, root)
    set_ = client.post(path, json={"password": NEW_PASSWORD + "!"}, headers=ann)
    third = pending(client.post(path, json={"password": NEW_PASSWORD}, headers=ann))

    assert arguments == {"user_id": str(bob)}
    assert unchanged == before
    assert set_.status_code == 204
    assert run_async(password_hash()) != before
    assert third != held
    assert not any(NEW_PASSWORD in each for each in run_async(stored()))


def test_an_impersonation_waits_for_two_and_its_token_goes_to_the_maker(
    client: TestClient,
) -> None:
    """The reason is bound; the token is issued to the maker's call, once."""
    ann = staff("ann")
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    bob = create_user("bob")
    path = f"{ADMIN}/users/{bob}/impersonate"
    reason = {"reason": "Ticket 42: invoices"}

    held = pending(client.post(path, json=reason, headers=ann))
    shown = read(client, held, ann)
    once = approve(client, held, root).json()
    elsewhere = pending(client.post(path, json={"reason": "Another"}, headers=ann))
    early = pending(client.post(path, json=reason, headers=ann))
    approve(client, held, other)
    issued = client.post(path, json=reason, headers=ann)
    later = pending(client.post(path, json=reason, headers=ann))

    assert (shown["action"], shown["required"]) == (IMPERSONATE, 2)
    assert shown["arguments"] == {"user_id": str(bob), "reason": "Ticket 42: invoices"}
    assert once["status"] == "pending"
    assert elsewhere != held
    assert early == held
    assert issued.status_code == 201, issued.json()
    assert issued.json()["access_token"]
    assert later not in (held, elsewhere)


def test_creating_staff_waits_for_making_them_staff(client: TestClient) -> None:
    """A user is created plain, then made staff with an approval."""
    ann = staff("ann")

    refused = client.post(
        f"{ADMIN}/users", json={"username": "new", "is_staff": True}, headers=ann
    )
    plain = client.post(f"{ADMIN}/users", json={"username": "new"}, headers=ann)

    problem(refused, 403, "approval_required")
    assert refused.json()["detail"] == (
        "Making staff needs approval: create the user, then make them staff."
    )
    with override("ar"):
        assert gettext(refused.json()["detail"]) != refused.json()["detail"]
    assert plain.status_code == 201, plain.json()


def test_an_approval_not_used_before_it_expires_is_never_used(
    client: TestClient,
) -> None:
    """Past the request's ``expires_at``, the call submits a new request."""
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    body = {"is_staff": True}

    held = pending(client.patch(f"{ADMIN}/users/{bob}", json=body, headers=ann))
    approve(client, held, root)
    with clock.freeze_time(clock.now() + timedelta(days=4)):
        late = pending(client.patch(f"{ADMIN}/users/{bob}", json=body, headers=ann))

    assert late != held
    assert read(client, held, ann)["status"] == "approved"
    assert user_of(bob).is_staff is False


def test_an_approved_request_changed_around_the_application_fails(
    client: TestClient,
) -> None:
    """Its seal does not hold: it fails, and the call waits for a new approval."""
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    body = {"is_staff": True}
    held = pending(client.patch(f"{ADMIN}/users/{bob}", json=body, headers=ann))
    approve(client, held, root)

    async def forge() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests).where(requests.c.id == UUID(held)).values(seal="x")
            )

    run_async(forge())
    fresh = pending(client.patch(f"{ADMIN}/users/{bob}", json=body, headers=ann))

    assert fresh != held
    failed = read(client, held, ann)
    assert (failed["status"], failed["reason"]) == ("failed", "request_tampered")
    assert user_of(bob).is_staff is False


def test_a_request_found_by_a_forged_fingerprint_runs_nothing_else(
    client: TestClient,
) -> None:
    """Its fingerprint column names another change: the call does not make it.

    The column is how the call finds the request, and the seal does not
    cover it: what the request runs must read as what the call asks.
    """
    ann = staff("ann", "accounting.view_account", "accounting.view_entry")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    make_group("viewers", "accounting.view_account")
    readers = make_group("readers", "accounting.view_entry")
    path = f"{ADMIN}/users/{bob}/groups"
    held = pending(client.put(path, json={"groups": ["viewers"]}, headers=ann))
    other = {
        "user_id": str(bob),
        "added": ["readers"],
        "removed": [],
        "groups": [bound_group("readers", readers, "accounting.view_entry")],
    }

    async def forge() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests)
                .where(requests.c.id == UUID(held))
                .values(fingerprint=fingerprint("auth.set_groups", other))
            )

    run_async(forge())
    approve(client, held, root)
    fresh = pending(client.put(path, json={"groups": ["readers"]}, headers=ann))

    assert fresh != held
    failed = read(client, held, ann)
    assert (failed["status"], failed["reason"]) == ("failed", "fingerprint_mismatch")
    assert user_of(bob).groups == set()


def test_only_the_staff_member_acting_in_their_own_name_submits(
    client: TestClient,
) -> None:
    """An impersonation, or a call with no request, is refused; others pass."""
    actor = user_of(create_user("root", is_staff=True, is_superuser=True))
    target = user_of(create_user("bob"))
    elsewhere = RequestContext(
        request_id=uuid.uuid4().hex,
        client_ip=None,
        method="POST",
        path="/x",
        actor="someone-else",
    )
    free = [
        (target, AdminChange("logout")),
        (target, AdminChange("update", values={"is_staff": False})),
        (None, AdminChange("create", values={"is_staff": False})),
        (target, AdminChange("groups", removed=frozenset({"viewers"}))),
    ]

    with bind_request(elsewhere), pytest.raises(PermissionDenied, match="own name"):
        run_async(check_admin_change(actor, target, AdminChange("password")))
    with pytest.raises(PermissionDenied, match="own name"):
        run_async(check_admin_change(actor, target, AdminChange("password")))
    for user, change in free:
        run_async(check_admin_change(actor, user, change))


def test_the_guards_are_by_maker_off_http_and_about_the_user(
    client: TestClient,
) -> None:
    """Each guard is run by its maker, never submitted at ``POST /requests``."""
    found = guards()
    held = found[SET_STAFF]
    promoted = uuid.uuid4()
    ann = staff("ann", "approvals.add_approvalrequest")

    submitted = client.post(
        API,
        json={"action": SET_STAFF, "arguments": {"user_id": str(promoted)}},
        headers=ann,
    )

    assert {action for action in found if action.startswith("auth.set_")} == {
        "auth.set_staff",
        "auth.set_groups",
        "auth.set_permissions",
        "auth.set_password",
        "auth.set_group_permissions",
    }
    assert (held.by_maker, held.http) == (True, False)
    assert held.parties_of(SetStaff(user_id=promoted)) == {str(promoted)}
    assert type(held.policies[0].approver_rule).__name__ == "IsSuperuser"
    problem(submitted, 404, "unknown_action")


def test_the_settings_name_known_changes_and_bounded_approvals() -> None:
    """Another change, or approvals out of 1 to 10, is refused; ``""`` is None."""
    for wrong in (
        {"auth.delete_user": 1},
        {"auth.set_staff": 0},
        {"auth.set_staff": 11},
    ):
        with pytest.raises(ValidationError):
            ApprovalsSettings(ADMIN_APPROVALS=wrong)
    assert ApprovalsSettings(ADMIN_APPROVERS="").ADMIN_APPROVERS is None
    assert ApprovalsSettings().ADMIN_APPROVALS == {}


def test_holding_admin_changes_and_finding_approvers_need_auth() -> None:
    """Without ``domainkit.contrib.auth``, ``ADMIN_APPROVALS`` fails ``setup()``.

    So does ``approvers_of``, which finds users.
    """
    registry.clear()
    try:
        with (
            override_settings(
                INSTALLED_DOMAINS=[MESSAGING, AUDIT, APPROVALS, ALPHA],
                APPROVALS={"ADMIN_APPROVALS": {"auth.set_staff": 1}},
            ),
            pytest.raises(ImproperlyConfigured, match="ADMIN_APPROVALS"),
        ):
            setup()
        registry.clear()
        with override_settings(INSTALLED_DOMAINS=[MESSAGING, AUDIT, APPROVALS, ALPHA]):
            setup()
            with pytest.raises(ImproperlyConfigured, match="approvers_of"):
                run_async(approvers_of(None))  # type: ignore[arg-type]
    finally:
        registry.clear()


def test_a_request_no_longer_approved_is_not_spent(client: TestClient) -> None:
    """Two calls at once: the second finds the request spent, and spends nothing."""
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    held = pending(
        client.patch(f"{ADMIN}/users/{bob}", json={"is_staff": True}, headers=ann)
    )
    approve(client, held, root)
    registered = guards()[SET_STAFF]

    arguments = read(client, held, root)["arguments"]
    first = run_async(_spend(registered, UUID(held), arguments))
    second = run_async(_spend(registered, UUID(held), arguments))

    assert first is not None
    assert first.status.value == "executed"
    assert second is None


ADMIN_APPROVERS: Final = Shape(
    "approvals_admin_approvers",
    "approvals",
    {
        **APPROVALS_ADMIN.settings,
        "APPROVALS": {
            "ADMIN_APPROVALS": {"auth.set_permissions": 1},
            "ADMIN_APPROVERS": "auth.change_user",
        },
    },
)
"""Granting permissions held, approved by whoever holds ``auth.change_user``."""


def test_with_admin_approvers_their_holders_approve(migrated: Migrated) -> None:
    """Staff holding the permission approve; the changes not named are not held."""
    with migrated(ADMIN_APPROVERS), TestClient(get_asgi_application()) as client:
        ann = staff("ann", "accounting.view_account")
        carol = staff("carol")
        bob = create_user("bob")
        path = f"{ADMIN}/users/{bob}/permissions"
        body = {"permissions": ["accounting.view_account"]}

        held = pending(client.put(path, json=body, headers=ann))
        approved = approve(client, held, carol)
        granted = client.put(path, json=body, headers=ann)
        made = client.patch(
            f"{ADMIN}/users/{bob}", json={"is_staff": True}, headers=ann
        )
        created = client.post(
            f"{ADMIN}/users", json={"username": "new", "is_staff": True}, headers=ann
        )
        password = client.post(
            f"{ADMIN}/users/{bob}/password",
            json={"password": NEW_PASSWORD},
            headers=ann,
        )

        assert approved.json()["status"] == "approved", approved.json()
        assert granted.json()["permissions"] == ["accounting.view_account"]
        assert made.json()["is_staff"] is True
        assert created.status_code == 201, created.json()
        assert password.status_code == 204
        assert set(guards()) & {SET_STAFF, IMPERSONATE} == set()
        policy = guards()["auth.set_permissions"].policies[0]
        assert policy.permission == "auth.change_user"


def rename(client: TestClient, group: UUID, name: str, headers: Headers) -> None:
    """Rename ``group`` to ``name`` through the admin API."""
    renamed = client.patch(
        f"{ADMIN}/groups/{group}", json={"name": name}, headers=headers
    )
    assert renamed.status_code == 200, renamed.json()


def test_an_approval_grants_the_groups_it_held_not_their_names(
    client: TestClient,
) -> None:
    """A group renamed to the name approved, or made again, needs its own approval.

    Approved for "readers", the maker renamed readers away and admins to
    "readers", repeated the call, and the user joined admins (user report
    A21). The approval holds each group's id and permissions: the same call
    now submits a request of its own, which shows what admins grant. A
    group granted more since is another group too.
    """
    ann = staff("ann", "accounting.view_entry", "accounting.add_entry")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    readers = make_group("readers", "accounting.view_entry")
    admins = make_group("admins", "accounting.view_entry", "accounting.add_entry")
    path = f"{ADMIN}/users/{bob}/groups"
    body = {"groups": ["readers"]}

    approved = pending(client.put(path, json=body, headers=ann))
    approve(client, approved, root)
    rename(client, readers, "old", root)
    rename(client, admins, "readers", root)
    swapped = pending(client.put(path, json=body, headers=ann))
    swap = read(client, swapped, ann)
    rename(client, admins, "admins", root)
    rename(client, readers, "readers", root)
    widen(readers, "accounting.add_entry")
    wider = pending(client.put(path, json=body, headers=ann))

    assert swapped not in (approved, wider)
    assert swap["arguments"]["groups"] == [
        bound_group("readers", admins, "accounting.view_entry", "accounting.add_entry")
    ]
    assert read(client, approved, ann)["status"] == "approved"
    assert user_of(bob).groups == set()


def widen(group_id: UUID, *permissions: str) -> None:
    """Grant the group ``group_id`` more, as a way round the admin API would."""

    async def grant_more() -> None:
        async with AuthUnitOfWork() as uow:
            group = await uow.groups.get(group_id)  # type: ignore[arg-type]
            group.grant(*await uow.permissions.get_many(permissions))
            await uow.commit()

    run_async(grant_more())


def test_an_approval_does_not_reach_a_group_made_again(client: TestClient) -> None:
    """A group deleted and made again with the same name is another group."""
    ann = staff("ann", "accounting.view_entry")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    first = make_group("readers", "accounting.view_entry")
    path = f"{ADMIN}/users/{bob}/groups"
    body = {"groups": ["readers"]}

    approved = pending(client.put(path, json=body, headers=ann))
    approve(client, approved, root)
    deleted = client.delete(f"{ADMIN}/groups/{first}", headers=root)
    again = make_group("readers", "accounting.view_entry")
    fresh = pending(client.put(path, json=body, headers=ann))

    assert deleted.status_code == 204
    assert fresh != approved
    assert read(client, fresh, ann)["arguments"]["groups"] == [
        bound_group("readers", again, "accounting.view_entry")
    ]
    assert user_of(bob).groups == set()


def deactivated(
    name: str, *, is_staff: bool = False, is_superuser: bool = False
) -> UUID:
    """A user ``name``, staff or a superuser as asked, then made inactive."""
    user_id = create_user(name, is_staff=is_staff, is_superuser=is_superuser)

    async def deactivate() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)  # type: ignore[arg-type]
            user.set_active(False)
            await uow.commit()

    run_async(deactivate())
    return user_id


def test_making_someone_who_holds_access_active_again_waits(
    client: TestClient,
) -> None:
    """``auth.reactivate``: a superuser, a member of a group wait.

    Before 2.7 ``{"is_active": true}`` gave a deactivated superuser
    everything back with no approval (user report A21). A user who holds
    nothing comes back at once; the user made active does not decide.
    """
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    make_group("readers", "accounting.view_entry")
    boss = deactivated("boss", is_superuser=True)
    clerk = deactivated("clerk")
    join("clerk", "readers")
    plain = deactivated("plain")
    body = {"is_active": True}

    held = pending(client.patch(f"{ADMIN}/users/{boss}", json=body, headers=root))
    request = read(client, held, root)
    approve(client, held, other)
    back = client.patch(f"{ADMIN}/users/{boss}", json=body, headers=root)
    member = pending(client.patch(f"{ADMIN}/users/{clerk}", json=body, headers=root))
    free = client.patch(f"{ADMIN}/users/{plain}", json=body, headers=root)

    assert (request["action"], request["policy"]) == (REACTIVATE, "auth.reactivation")
    assert request["arguments"] == {
        "user_id": str(boss),
        "is_staff": False,
        "is_superuser": True,
        "groups": [],
        "permissions": [],
        "object_grants": 0,
        "object_grants_digest": "",
    }
    returning = guards()[REACTIVATE].load(request["arguments"])
    assert guards()[REACTIVATE].parties_of(returning) == {str(boss)}
    assert back.status_code == 200, back.json()
    assert back.json()["is_active"] is True
    assert read(client, member, root)["action"] == REACTIVATE
    assert free.status_code == 200, free.json()


def test_a_change_held_twice_over_is_refused_and_each_hold_is_its_own(
    client: TestClient,
) -> None:
    """Made staff and active again in one call: refused, so no approval is spent.

    ``auth.set_staff`` and ``auth.reactivate`` each want their approvals;
    one request for both would spend one hold's for the other's change.
    """
    ann = staff("ann")
    bob = deactivated("bob")

    both = client.patch(
        f"{ADMIN}/users/{bob}", json={"is_staff": True, "is_active": True}, headers=ann
    )

    problem(both, 403, "approval_required")
    assert both.json()["detail"] == (
        "Make the user staff and active again in two changes: each waits for "
        "its own approval."
    )
    with override("ar"):
        assert gettext(both.json()["detail"]) != both.json()["detail"]
    assert client.get(API, headers=ann).json()["items"] == []


def test_which_hold_a_user_made_active_again_waits_for(client: TestClient) -> None:
    """``auth.reactivate`` where it is held; else ``auth.set_staff`` for staff.

    The run of making staff covers a staff member made active again (user
    report A21): without ``auth.reactivate``, a deactivated staff member's
    return waits for ``auth.set_staff``. Making staff and active at once,
    with only one of them held, is that one's.
    """
    staff_id = deactivated("staffer", is_staff=True)
    plain_id = deactivated("plain")
    registered = guards()
    only_staff = {SET_STAFF: registered[SET_STAFF]}
    only_reactivate = {REACTIVATE: registered[REACTIVATE]}
    back = AdminChange("update", values={"is_active": True})
    unmade = AdminChange("update", values={"is_active": True, "is_staff": False})
    both = AdminChange("update", values={"is_active": True, "is_staff": True})

    async def decide() -> list[object]:
        async with AuthUnitOfWork() as uow:
            staffer = await uow.users.get(staff_id)  # type: ignore[arg-type]
            plain = await uow.users.get(plain_id)  # type: ignore[arg-type]
        return [
            await _updated(staffer, back, only_staff),
            await _updated(staffer, unmade, only_staff),
            await _updated(plain, back, only_staff),
            await _updated(plain, both, only_reactivate),
            await _updated(plain, both, only_staff),
            await _updated(staffer, back, {}),
        ]

    assert run_async(decide()) == [
        (SET_STAFF, SetStaff(user_id=staff_id)),
        None,
        None,
        (REACTIVATE, comes_back(plain_id, is_staff=True)),
        (SET_STAFF, SetStaff(user_id=plain_id)),
        None,
    ]


def comes_back(user_id: UUID, **held: Any) -> Reactivate:  # noqa: ANN401 - fields.
    """The return of ``user_id`` with ``held``, and nothing else."""
    fields: dict[str, Any] = {
        "is_staff": False,
        "is_superuser": False,
        "groups": (),
        "permissions": (),
        "object_grants": 0,
        "object_grants_digest": "",
    }
    return Reactivate(user_id=user_id, **{**fields, **held})


def test_an_approved_return_brings_back_only_what_it_held(
    client: TestClient,
) -> None:
    """A user given more while inactive comes back with another approval.

    The approver of a return saw what the user would hold: their groups,
    flags, and permissions. Before, the request held the user alone: while
    they were still inactive, the maker made them a superuser and the
    return approved for a reader brought back a superuser (found in
    review of user report A21).
    """
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    readers = make_group("readers", "accounting.view_entry")
    user = deactivated("user")
    join("user", "readers")
    body = {"is_active": True}

    approved = pending(client.patch(f"{ADMIN}/users/{user}", json=body, headers=root))
    shown = read(client, approved, root)["arguments"]
    approve(client, approved, other)
    made = client.patch(
        f"{ADMIN}/users/{user}", json={"is_superuser": True}, headers=root
    )
    wider = pending(client.patch(f"{ADMIN}/users/{user}", json=body, headers=root))
    unmade = client.patch(
        f"{ADMIN}/users/{user}", json={"is_superuser": False}, headers=root
    )
    granted = grant_directly(user, "accounting.add_entry")
    widened = pending(client.patch(f"{ADMIN}/users/{user}", json=body, headers=root))
    revoke_directly(user, "accounting.add_entry")
    back = client.patch(f"{ADMIN}/users/{user}", json=body, headers=root)

    assert shown["groups"] == [bound_group("readers", readers, "accounting.view_entry")]
    assert (shown["is_superuser"], shown["permissions"]) == (False, [])
    assert made.status_code == 200, made.json()
    assert read(client, wider, root)["arguments"]["is_superuser"] is True
    assert unmade.status_code == 200, unmade.json()
    assert granted == ["accounting.add_entry"]
    assert read(client, widened, root)["arguments"]["permissions"] == [
        "accounting.add_entry"
    ]
    assert wider not in (approved, widened)
    assert back.status_code == 200, back.json()
    assert (back.json()["is_active"], back.json()["is_superuser"]) == (True, False)
    assert read(client, approved, root)["status"] == "executed"


def grant_directly(user_id: UUID, *keys: str) -> list[str]:
    """Grant ``user_id`` permissions, as a way round the admin API would; their keys."""

    async def run() -> list[str]:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)  # type: ignore[arg-type]
            user.grant(*await uow.permissions.get_many(keys))
            await uow.commit()
        return sorted(permission.key for permission in user.user_permissions)

    return run_async(run())


def revoke_directly(user_id: UUID, *keys: str) -> None:
    """Take permissions from ``user_id`` again."""

    async def run() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)  # type: ignore[arg-type]
            user.revoke(*await uow.permissions.get_many(keys))
            await uow.commit()

    run_async(run())


def test_a_superuser_comes_back_as_one_is_made(migrated: Migrated) -> None:
    """With ``SUPERUSER_APPROVALS``, a deactivated superuser is not made active here.

    Bringing one back gives every permission, as making one does; before,
    it passed with no approval (found in review of user report A21). Taking
    superuser away first, the user comes back plain, and an approved
    ``auth.grant_superuser`` makes them one again.
    """
    with migrated(APPROVALS_API), TestClient(get_asgi_application()) as client:
        root = staff("root", superuser=True)
        boss = deactivated("boss", is_superuser=True)
        body = {"is_active": True}

        refused = client.patch(f"{ADMIN}/users/{boss}", json=body, headers=root)
        demoted = client.patch(
            f"{ADMIN}/users/{boss}", json={"is_superuser": False}, headers=root
        )
        back = client.patch(f"{ADMIN}/users/{boss}", json=body, headers=root)

    problem(refused, 403, "approval_required")
    assert demoted.status_code == 200, demoted.json()
    assert back.status_code == 200, back.json()
    assert (back.json()["is_active"], back.json()["is_superuser"]) == (True, False)


REACH: Final = "auth.delete_group"
"""The key ``TENANCY.ALL_TENANTS_PERMISSION`` names in the tests of making staff."""
REACHING: Final = {
    "MEMBER_PERMISSIONS": "domainkit_tests.anywhere.members",
    "MEMBER_TENANTS": "domainkit_tests.anywhere.tenants",
    "ALL_TENANTS_PERMISSION": REACH,
}
"""``TENANCY`` with ``REACH`` reaching every tenant: the key a screen names."""


def test_making_staff_again_holds_and_shows_what_the_user_comes_back_with(
    client: TestClient,
) -> None:
    """Made non-staff, a staff member keeps her groups: made staff again, they show.

    The request holds what she comes back with, as ``auth.reactivate``
    holds a return: the superuser flag, each group (``HeldGroup``), her own
    permissions, and her grants on objects. The approver reads the key
    that reaches every tenant by name ("Grants") and the group that gives
    it ("Groups"), and a grant made since the request makes another one,
    which waits for its own approval (user report A26).
    """
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    officers = make_group("officers", REACH)
    ada = create_user("ada", is_staff=True)
    join("ada", "officers")
    path = f"{ADMIN}/users/{ada}"
    making = {"is_staff": True}

    with override_settings(TENANCY=REACHING):
        unmade = client.patch(path, json={"is_staff": False}, headers=root)
        held = pending(client.patch(path, json=making, headers=root))
        arguments = read(client, held, root)["arguments"]
        screen = screen_of(SET_STAFF, guards()[SET_STAFF].load(arguments), whole=True)
        approve(client, held, other)
        grant_directly(ada, "accounting.add_entry")
        wider = pending(client.patch(path, json=making, headers=root))
        revoke_directly(ada, "accounting.add_entry")
        made = client.patch(path, json=making, headers=root)

    assert unmade.status_code == 200, unmade.json()
    assert arguments == {
        "user_id": str(ada),
        "is_superuser": False,
        "groups": [bound_group("officers", officers, REACH)],
        "permissions": [],
        "object_grants": 0,
        "object_grants_digest": "",
    }
    assert screen == [
        ("Action", SET_STAFF),
        ("User", f"ada ({end_of(ada)})"),
        ("Grants", REACH),
        ("Groups", "officers"),
    ]
    assert wider != held
    assert read(client, wider, root)["arguments"]["permissions"] == [
        "accounting.add_entry"
    ]
    assert made.status_code == 200, made.json()
    assert made.json()["is_staff"] is True
    assert read(client, held, root)["status"] == "executed"


def test_a_superuser_made_staff_is_shown_as_one(client: TestClient) -> None:
    """A superuser who is not staff, made staff: the approver reads "Superuser: yes".

    Before 2.10 the screen said "User: boss" alone, as for anyone made
    staff (user report A26). A flag the call sets beside is the flag
    held, shown once.
    """
    root = staff("root", superuser=True)
    boss = create_user("boss", is_superuser=True)
    plain = create_user("plain")
    path = f"{ADMIN}/users/{{}}"

    held = pending(
        client.patch(path.format(boss), json={"is_staff": True}, headers=root)
    )
    both = pending(
        client.patch(
            path.format(plain),
            json={"is_staff": True, "is_superuser": True},
            headers=root,
        )
    )
    screens = [
        screen_of(
            SET_STAFF,
            guards()[SET_STAFF].load(read(client, each, root)["arguments"]),
            whole=True,
        )
        for each in (held, both)
    ]

    assert screens[0] == [
        ("Action", SET_STAFF),
        ("User", f"boss ({end_of(boss)})"),
        ("Superuser", "yes"),
    ]
    assert screens[1] == [
        ("Action", SET_STAFF),
        ("User", f"plain ({end_of(plain)})"),
        ("Superuser", "yes"),
    ]
    assert read(client, both, root)["arguments"]["is_superuser"] is True
    assert "values" not in read(client, both, root)["arguments"]


def test_making_staff_shows_what_the_user_holds_in_words(client: TestClient) -> None:
    """Their groups by name, how many permissions and grants on objects, then values.

    As a return shows them (``show_reactivation``), in the same order:
    "Superuser", "Grants", "Groups", "Also holds", then what else the
    call sets (user report A26).
    """
    bob = create_user("bob")
    readers = make_group("readers", "auth.view_user")
    making = SetStaff(
        user_id=bob,
        is_superuser=True,
        groups=(HeldGroup(readers, "readers", ("auth.view_user",)),),
        permissions=("auth.add_user", "auth.change_user"),
        object_grants=1,
        object_grants_digest="0" * 64,
        values={"is_active": False},
    )

    screen = screen_of(SET_STAFF, making, whole=True)

    assert screen == [
        ("Action", SET_STAFF),
        ("User", f"bob ({end_of(bob)})"),
        ("Superuser", "yes"),
        ("Groups", "readers"),
        ("Also holds", "2 permissions; 1 object grant"),
        ("Active", "no"),
    ]


async def stored_as_in_2_9(user_id: UUID, maker: UUID) -> str:
    """A request to make ``user_id`` staff, as 2.9 stored it: the user alone."""
    request = ApprovalRequest.submit(
        SET_STAFF,
        {"user_id": str(user_id)},
        maker=str(maker),
        policy="auth.staff",
        required=1,
        permission=None,
        group=None,
        expires_at=clock.now() + timedelta(days=1),
    )
    async with ApprovalsUnitOfWork() as uow:
        await uow.requests.add(request)
        await uow.commit()
    return str(request.id)


def test_a_request_of_2_9_to_make_staff_is_never_spent(client: TestClient) -> None:
    """Approved before the upgrade, it held the user alone: the call asks again.

    A request stored by 2.9 holds none of what the user comes back with,
    so its approver read none of it. The same call finds no request that
    holds what the user holds, and submits one (``403
    approval_pending``), which is approved again; the old one is never
    spent (user report A26).
    """
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    bob = create_user("bob")
    old = run_async(stored_as_in_2_9(bob, user_of_name("root")))
    loaded = guards()[SET_STAFF].load(read(client, old, root)["arguments"])
    approve(client, old, other)

    fresh = pending(
        client.patch(f"{ADMIN}/users/{bob}", json={"is_staff": True}, headers=root)
    )

    assert loaded == SetStaff(user_id=bob)
    assert fresh != old
    assert read(client, old, root)["status"] == "approved"
    assert user_of(bob).is_staff is False


def test_granting_a_group_permissions_waits_and_its_members_do_not_decide(
    client: TestClient,
) -> None:
    """``auth.set_group_permissions``: every member gains them (user report A20).

    Taking permissions away, and creating a group with some (it has no
    members), wait for no one.
    """
    root = staff("root", superuser=True)
    other = staff("other", superuser=True)
    member = staff("member", superuser=True)
    readers = make_group("readers", "accounting.view_entry")
    join("member", "readers")
    path = f"{ADMIN}/groups/{readers}/permissions"
    wider = {"permissions": ["accounting.view_entry", "accounting.add_entry"]}

    held = pending(client.put(path, json=wider, headers=root))
    request = read(client, held, root)
    by_member = approve(client, held, member)
    approve(client, held, other)
    granted = client.put(path, json=wider, headers=root)
    narrowed = client.put(path, json={"permissions": []}, headers=root)
    created = client.post(
        f"{ADMIN}/groups",
        json={"name": "clerks", "permissions": ["accounting.add_entry"]},
        headers=root,
    )

    assert (request["action"], request["policy"]) == (
        SET_GROUP_PERMISSIONS,
        "auth.group_permissions",
    )
    assert request["arguments"] == {
        "group_id": str(readers),
        "name": "readers",
        "added": ["accounting.add_entry"],
        "removed": [],
    }
    problem(by_member, 403, "interested_party")
    assert granted.status_code == 200, granted.json()
    assert granted.json()["permissions"] == [
        "accounting.add_entry",
        "accounting.view_entry",
    ]
    assert narrowed.status_code == 200, narrowed.json()
    assert created.status_code == 201, created.json()


def test_the_projects_checks_run_before_the_hold_spends_an_approval(
    client: TestClient,
) -> None:
    """A check of a domain installed after approvals refuses first: nothing is spent.

    Approvals registers its hold with ``order=100``, after the checks
    registered without an order, whatever their domain (user report A19).
    Before 2.7 it ran in domain order, and spent the approval of a change
    that a later check refused.
    """
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    body = {"is_staff": True}
    held = pending(client.patch(f"{ADMIN}/users/{bob}", json=body, headers=ann))
    approve(client, held, root)

    async def not_today(actor: Any, target: Any, change: AdminChange) -> None:  # noqa: ANN401
        raise PermissionDenied("No staff are made today.")

    on_admin_change(not_today, domain="alpha")
    refused = client.patch(f"{ADMIN}/users/{bob}", json=body, headers=ann)

    problem(refused, 403, "permission_denied")
    assert read(client, held, ann)["status"] == "approved"


def test_a_reused_password_spends_no_approval(client: TestClient) -> None:
    """The password history is checked before the hold, with the validators' care.

    A password among the user's last ones is refused, and the approved
    request stays approved for a password that will do (user report A19).
    """
    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob", password=NEW_PASSWORD)
    path = f"{ADMIN}/users/{bob}/password"
    history = {**APPROVALS_ADMIN.settings["AUTH"], "PASSWORD_HISTORY": 2}

    with override_settings(AUTH=history):
        held = pending(
            client.post(path, json={"password": "Other-pass-42"}, headers=ann)
        )
        approve(client, held, root)
        reused = client.post(path, json={"password": NEW_PASSWORD}, headers=ann)
        still = read(client, held, ann)["status"]
        made = client.post(path, json={"password": "Other-pass-42"}, headers=ann)

    problem(reused, 422, "invalid_password")
    assert still == "approved"
    assert made.status_code == 204, made.json()
    assert read(client, held, ann)["status"] == "executed"


def test_a_change_of_groups_must_name_each_group_it_adds(
    client: TestClient,
) -> None:
    """Without ``AdminChange.groups``, a held change of groups could not be bound.

    It is refused, rather than approved by name. A change of a group that
    grants nothing, a group's name, or no group at all is not held.
    """
    actor = user_of(create_user("root", is_staff=True, is_superuser=True))
    target = user_of(create_user("bob"))
    unbound = AdminChange("groups", added=frozenset({"readers"}))
    readers = run_async(_group("readers"))

    with pytest.raises(ImproperlyConfigured, match=r"AdminChange\.groups"):
        run_async(check_admin_change(actor, target, unbound))
    for group, change in [
        (readers, GroupChange("group_rename", name="other")),
        (readers, GroupChange("group_permissions", removed=frozenset({"x.y"}))),
        (None, GroupChange("group_create", name="new", added=frozenset({"x.y"}))),
        (readers, GroupChange("group_delete")),
    ]:
        run_async(check_group_change(actor, group, change))
    assert SET_GROUPS in guards()


async def _group(name: str) -> Any:  # noqa: ANN401 - a group.
    """A group ``name``, stored."""
    async with AuthUnitOfWork() as uow:
        group = await uow.groups.create(name)
        await uow.commit()
    return group


GROUP_GRANTS: dict[str, frozenset[ObjectGrant]] = {}
"""What ``GroupGrantsBackend`` grants each group, by name, on single objects."""


class GroupGrantsBackend(BaseBackend):
    """Grants groups permissions on single objects, as an object-permissions app."""

    async def object_grants(
        self, holder: AbstractBaseUser | Group
    ) -> frozenset[ObjectGrant]:
        """``GROUP_GRANTS``'s, for a group; a user's are their groups'."""
        if isinstance(holder, Group):
            return GROUP_GRANTS.get(holder.name, frozenset())
        return frozenset().union(*(GROUP_GRANTS.get(g.name, ()) for g in holder.groups))


def test_an_approval_binds_a_groups_grants_on_objects(client: TestClient) -> None:
    """A grant on an object given to the group since, or renewed, is another group.

    ``HeldGroup`` holds how many grants on single objects the group holds,
    and their digest, each with when it ends: a grant made after the
    approval (by an object permissions app, say), or renewed to a later
    date, no longer rides it (found in review of user report A21).
    """
    ann = staff("ann", "accounting.view_entry")
    root = staff("root", superuser=True)
    bob = create_user("bob")
    readers = make_group("readers", "accounting.view_entry")
    path = f"{ADMIN}/users/{bob}/groups"
    body = {"groups": ["readers"]}
    auth = {
        **APPROVALS_ADMIN.settings["AUTH"],
        "BACKENDS": [
            "domainkit.contrib.auth.backends.ModelBackend",
            f"{__name__}.GroupGrantsBackend",
        ],
    }
    ends = datetime(2027, 1, 1, tzinfo=UTC)
    vault = ObjectGrant("accounting.view_entry", "accounting.entry", 7, ends)
    renewed = ObjectGrant(
        "accounting.view_entry", "accounting.entry", 7, ends + timedelta(days=365)
    )
    ledger = ObjectGrant("accounting.view_entry", "accounting.entry", 8)

    GROUP_GRANTS.clear()
    GROUP_GRANTS["readers"] = frozenset({vault})
    with override_settings(AUTH=auth):
        approved = pending(client.put(path, json=body, headers=ann))
        approve(client, approved, root)
        GROUP_GRANTS["readers"] = frozenset({renewed})
        later = pending(client.put(path, json=body, headers=ann))
        GROUP_GRANTS["readers"] = frozenset({vault, ledger})
        wider = pending(client.put(path, json=body, headers=ann))
        held = read(client, wider, ann)["arguments"]["groups"]
        GROUP_GRANTS["readers"] = frozenset({vault})
        joined = client.put(path, json=body, headers=ann)
    GROUP_GRANTS.clear()

    assert len({approved, later, wider}) == 3
    assert renewed == vault  # The same grant, to sets; another, to an approval.
    assert [
        (each["object_grants"], len(each["object_grants_digest"])) for each in held
    ] == [(2, 64)]
    assert held[0]["id"] == str(readers)
    assert joined.status_code == 200, joined.json()
    assert read(client, approved, ann)["status"] == "executed"


def test_a_group_permissions_hold_alone_is_warned_about(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``approvals.W001``: a new group made with the permissions, then joined, passes.

    Where deployed, ``auth.set_group_permissions`` held without
    ``auth.set_groups`` is reported (found in review of user report A20).
    """
    from domainkit.contrib.approvals.apps import ApprovalsConfig

    config = ApprovalsConfig.__new__(ApprovalsConfig)
    alone = {"auth.set_group_permissions": 1}
    both = {**alone, "auth.set_groups": 1}

    with override_settings(APPROVALS={"ADMIN_APPROVALS": alone}):
        local = config.weak_settings()
    monkeypatch.setattr(
        "domainkit.contrib.approvals.apps.DEPLOYED_ENVIRONMENTS", frozenset({"test"})
    )
    with override_settings(APPROVALS={"ADMIN_APPROVALS": alone}):
        warned = config.weak_settings()
    with override_settings(APPROVALS={"ADMIN_APPROVALS": both}):
        paired = config.weak_settings()

    assert [each.id for each in warned if isinstance(each, CheckMessage)] == [
        "approvals.W001"
    ]
    assert paired == local == []


def test_one_approval_is_one_comparison_when_calls_race(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calls at once on one approval: only the one that claims it compares.

    The history is compared under the approved request's lock, by the call
    that spends it; a call that loses the claim compares nothing and
    answers ``approval_pending``, whatever its password. Found then
    executed apart, each call compared, and one approval answered as many
    guesses as calls were sent (found in review of user report A17).
    """
    import httpx2

    from domainkit.contrib.auth.repositories import PasswordHistoryStore

    ann = staff("ann")
    root = staff("root", superuser=True)
    bob = create_user("bob", password=NEW_PASSWORD)
    path = f"{ADMIN}/users/{bob}/password"
    history = {
        **APPROVALS_ADMIN.settings["AUTH"],
        "PASSWORD_HISTORY": 3,
        "LOGIN_RATE": "50/minute",
    }
    compared: list[str] = []
    original = PasswordHistoryStore.refuse_reuse

    async def counted(
        self: PasswordHistoryStore, user_id: UUID, password: str, *, priority: bool
    ) -> None:
        compared.append(password)
        await original(self, user_id, password, priority=priority)

    guesses = [f"Guess-pass-{each:02d}" for each in range(6)] + [NEW_PASSWORD] * 2

    async def at_once() -> list[Any]:
        transport = httpx2.ASGITransport(app=client.app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            return list(
                await asyncio.gather(
                    *(
                        http.post(path, json={"password": guess}, headers=ann)
                        for guess in guesses
                    )
                )
            )

    with override_settings(AUTH=history):
        held = pending(
            client.post(path, json={"password": "Fresh-pass-1"}, headers=ann)
        )
        approve(client, held, root)
        monkeypatch.setattr(PasswordHistoryStore, "refuse_reuse", counted)
        answers = run_async(at_once())

    codes = [
        each.status_code if each.status_code == 204 else each.json()["code"]
        for each in answers
    ]
    claimed = [code for code in codes if code in (204, "invalid_password")]
    assert codes.count(204) == 1, codes
    assert set(codes) <= {204, "invalid_password", "approval_pending"}, codes
    assert len(compared) == len(claimed), (compared, codes)
    assert read(client, held, ann)["status"] == "executed"


HELD_SCREENS: Final = Shape(
    "approvals_admin_screens",
    "approvals",
    {
        **ACTS.settings,
        "APPROVALS": {
            **ACTS.settings["APPROVALS"],
            "APPROVE_ACTION": True,
            "ADMIN_APPROVALS": {
                "auth.set_groups": 1,
                "auth.reactivate": 1,
                "auth.set_group_permissions": 1,
            },
        },
    },
)
"""Every change an act; groups, returns, and a group's grants held, approved
with an act of their own."""
WHOLE_AUTH: Final = {**ACTS.settings["AUTH"], "AUTHORIZATIONS": {"WHOLE": True}}
"""``AUTH`` of ``HELD_SCREENS`` with ``AUTHORIZATIONS.WHOLE`` on."""


def end4(value: object) -> str:
    """What a group's item shows of its id: ``…`` and its last 4 characters."""
    return "…" + str(value)[-4:]


def screen_of(action: str, command: Any, *, whole: bool) -> list[tuple[str, str]]:  # noqa: ANN401 - a command.
    """What approving ``command`` shows: its action, then its guard's ``show``."""
    guard = guards()[action]
    return details_of(action, run_async(shown_of(guard, command)), whole=whole)


def keys_on(lines: int) -> list[str]:
    """Permission keys that take ``lines`` lines of a screen as one list, no more."""
    every = run_async(_keys())
    for count in range(1, len(every)):
        try:
            taken = len(laid_out([("Adds", every[:count])], whole=True))
        except TooMuchToShow:
            break
        if taken == lines and len(laid_out([("Adds", every[: count + 1])])) > lines:
            return every[:count]
    raise AssertionError(f"No keys take {lines} lines.")


async def _keys() -> list[str]:
    """Every permission's key, sorted."""
    async with AuthUnitOfWork() as uow:
        return sorted(permission.key for permission in await uow.permissions.all())


def test_each_hold_shows_what_it_grants_in_words(migrated: Migrated) -> None:
    """A group added by name, id, and what it grants; a return; a group's grant.

    A group's name of 150 characters is cut beside its id, so even with
    ``WHOLE`` on it is never refused for its name; thirty keys granted to
    a group are counted, or, with ``WHOLE``, refused (user reports A21,
    A23).
    """
    with migrated(HELD_SCREENS), TestClient(get_asgi_application()):
        bob = create_user("bob")
        long = make_group("g" * 150, "auth.view_user", "auth.add_user")
        readers = make_group("readers")
        long_held, readers_held = (
            HeldGroup(each, name, keys)
            for each, name, keys in (
                (long, "g" * 150, ("auth.add_user", "auth.view_user")),
                (readers, "readers", ()),
            )
        )
        joined = SetGroups(
            user_id=bob, added=("g" * 150, "readers"), groups=(long_held, readers_held)
        )
        returning = comes_back(
            bob,
            is_staff=True,
            is_superuser=True,
            groups=(readers_held,),
            permissions=("auth.add_user", "auth.view_user"),
            object_grants=1,
            object_grants_digest="0" * 64,
        )
        thirty = run_async(_keys())[:30]
        granted = SetGroupPermissions(
            group_id=readers, name="readers", added=tuple(thirty)
        )

        groups_screen = screen_of(SET_GROUPS, joined, whole=True)
        return_screen = screen_of(REACTIVATE, returning, whole=True)
        counted = screen_of(SET_GROUP_PERMISSIONS, granted, whole=False)
        with pytest.raises(TooMuchToShow):
            screen_of(SET_GROUP_PERMISSIONS, granted, whole=True)

    long_line = groups_screen[2][1]
    assert groups_screen[:2] == [
        ("Action", SET_GROUPS),
        ("User", f"bob ({end_of(bob)})"),
    ]
    assert groups_screen[2][0] == "Adds"
    assert long_line.startswith("ggg")
    assert long_line.endswith(f"… ({end4(long)}) 2 perms")
    assert len(long_line) == 64
    assert groups_screen[3] == ("Adds (2)", f"readers ({end4(readers)}) 0 perms")
    assert return_screen == [
        ("Action", REACTIVATE),
        ("User", f"bob ({end_of(bob)})"),
        ("Superuser", "yes"),
        ("Staff", "yes"),
        ("Groups", "readers"),
        ("Also holds", "2 permissions; 1 object grant"),
    ]
    assert counted[:2] == [
        ("Action", SET_GROUP_PERMISSIONS),
        ("Group", f"readers ({end_of(readers)})"),
    ]
    assert [label for label, _ in counted[2:]] == [
        "Adds",
        "Adds (2)",
        "Adds (3)",
        "Adds (4)",
    ]
    assert counted[-1][1].endswith("more")
    assert all(len(value) <= 64 for _, value in groups_screen + counted)


def test_a_grant_to_a_group_its_approver_could_not_see_whole_is_refused_at_its_twin(
    migrated: Migrated, frozen: FrozenClock
) -> None:
    """With ``WHOLE``, a held grant to a group that would not fit is never asked for.

    The approver's screen shows the action above what the staff member's
    shows: a grant that fills the staff member's screen would not fit the
    approver's, and the group's twin refuses it, as a user's does
    (``on_admin_group_screen``). Thirty keys fit neither.
    """
    with (
        migrated(HELD_SCREENS),
        TestClient(get_asgi_application()) as client,
        override_settings(
            AUTH={**ACTS.settings["AUTH"], "AUTHORIZATIONS": {"WHOLE": True}}
        ),
    ):
        root = Staff(client, frozen, "root", superuser=True)
        group = make_group("readers")
        path = f"/groups/{group}/permissions/authorization"
        full = keys_on(5)
        filled = root.ask(path, {"permissions": full})
        thirty = root.ask(path, {"permissions": run_async(_keys())[:30]})
        fewer = root.ask(path, {"permissions": full[:2]})

    problem(filled, 422, "too_much_to_show")
    problem(thirty, 422, "too_much_to_show")
    assert fewer.status_code == 201, fewer.json()


def test_a_renamed_group_shows_its_name_now_when_approved(
    migrated: Migrated, frozen: FrozenClock
) -> None:
    """The approver reads the group's name as it is now, beside the id it binds."""
    with migrated(HELD_SCREENS), TestClient(get_asgi_application()) as client:
        root = Staff(client, frozen, "root", superuser=True)
        boss = Staff(client, frozen, "boss", superuser=True)
        group = make_group("readers")
        body = {"permissions": ["auth.view_user"]}
        path = f"/groups/{group}/permissions"
        authorization = root.authorized(f"{path}/authorization", body)
        request_id = root.call("PUT", path, body, authorization).json()[
            "approval_request_id"
        ]
        renamed = root.authorized(
            f"/groups/{group}/authorization/patch", {"name": "auditors"}
        )
        root.call("PATCH", f"/groups/{group}", {"name": "auditors"}, renamed)
        read = client.get(f"/approvals/requests/{request_id}", headers=boss.headers)
        asked = client.post(
            f"/approvals/requests/{request_id}/approve/authorization",
            json={"fingerprint": read.json()["fingerprint"]},
            headers=boss.headers,
        )

    assert asked.status_code == 201, asked.json()
    details = [
        (each["label"], each["value"]) for each in asked.json()["shown"]["details"]
    ]
    assert details == [
        ("Action", SET_GROUP_PERMISSIONS),
        ("Group", f"auditors ({end_of(group)})"),
        ("Adds", "auth.view_user"),
    ]


def test_a_user_in_many_groups_comes_back_in_steps_with_whole_on(
    migrated: Migrated, frozen: FrozenClock
) -> None:
    """Too many groups for one screen: take some away, come back, add them again.

    With ``WHOLE`` on, the return of a staff member in twelve groups would
    not fit the approver's screen, and is refused at its twin. Taking six
    groups away waits for no one; the return then fits, and so does adding
    the six back, a held change of its own (review of user report A21).
    """
    with (
        migrated(HELD_SCREENS),
        TestClient(get_asgi_application()) as client,
        override_settings(AUTH=WHOLE_AUTH),
    ):
        root = Staff(client, frozen, "root", superuser=True)
        names = [f"finance_team_{each:02d}" for each in range(12)]
        for name in names:
            make_group(name)
        user = deactivated("user", is_staff=True)
        join("user", *names)
        back = {"is_active": True}

        refused = root.ask(f"/users/{user}/authorization/patch", back)
        kept = {"groups": names[:6]}
        authorization = root.authorized(f"/users/{user}/groups/authorization", kept)
        taken = root.call("PUT", f"/users/{user}/groups", kept, authorization)
        returning = root.ask(f"/users/{user}/authorization/patch", back)
        adding = root.ask(f"/users/{user}/groups/authorization", {"groups": names})

    problem(refused, 422, "too_much_to_show")
    assert taken.status_code == 200, taken.json()
    assert returning.status_code == 201, returning.json()
    assert adding.status_code == 201, adding.json()


EARLY: Final = datetime(2027, 1, 1, tzinfo=UTC)
LATE: Final = datetime(2028, 1, 1, tzinfo=UTC)
VAULT: Final = ("accounting.view_entry", "accounting.entry", 7)


class EarlyBackend(BaseBackend):
    """Grants every group the vault until early 2027."""

    async def object_grants(
        self, holder: AbstractBaseUser | Group
    ) -> frozenset[ObjectGrant]:
        """The vault, ending early."""
        return frozenset({ObjectGrant(*VAULT, EARLY)})


class LateBackend(BaseBackend):
    """Grants every group the vault until 2028; with ``never``, for good."""

    never: bool = False

    async def object_grants(
        self, holder: AbstractBaseUser | Group
    ) -> frozenset[ObjectGrant]:
        """The vault, ending late, or never."""
        return frozenset({ObjectGrant(*VAULT, None if self.never else LATE)})


class NeverBackend(LateBackend):
    """Grants every group the vault for good."""

    never = True


def test_a_grant_held_twice_ends_as_its_last_copy_in_any_order(
    client: TestClient,
) -> None:
    """The same grant from two places keeps its latest end, whichever came first.

    A set keeps one copy of a grant (its end is not compared), so which end
    a digest bound depended on the order the copies came in (found in
    review of user report A21). Never ending outlasts any date.
    """
    from domainkit.contrib.approvals.builtin import _grants_digest
    from domainkit.contrib.auth.backends import object_grants

    group_id = make_group("readers")
    model = "domainkit.contrib.auth.backends.ModelBackend"

    async def held(*backends: str) -> frozenset[ObjectGrant]:
        async with AuthUnitOfWork() as uow:
            group = await uow.groups.get(group_id)  # type: ignore[arg-type]
        with override_settings(
            AUTH={**APPROVALS_ADMIN.settings["AUTH"], "BACKENDS": [model, *backends]}
        ):
            return await object_grants(group)

    early, late, never = (
        f"{__name__}.{each}" for each in ("EarlyBackend", "LateBackend", "NeverBackend")
    )
    one = run_async(held(early, late))
    other = run_async(held(late, early))
    forever = run_async(held(never, early))

    assert [grant.expires_at for grant in one] == [LATE]
    assert [grant.expires_at for grant in other] == [LATE]
    assert _grants_digest(one) == _grants_digest(other)
    assert [grant.expires_at for grant in forever] == [None]
    assert _grants_digest(forever) != _grants_digest(one)
    lasting = ObjectGrant.lasting([ObjectGrant(*VAULT), ObjectGrant(*VAULT, EARLY)])
    assert [grant.expires_at for grant in lasting] == [None]
