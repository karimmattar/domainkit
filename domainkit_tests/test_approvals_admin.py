"""Changes of the admin API held for approval (user report A9).

``APPROVALS.ADMIN_APPROVALS`` names the changes of auth's admin API that
wait for other people: making staff, adding groups or permissions,
setting a password, impersonating. The admin API submits the change as the
staff member's request and answers ``403 approval_pending`` with its id;
once approved, the same call, made again by the same staff member, makes
exactly that change, once.
"""

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from domainkit import clock, setup
from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.context import RequestContext, bind_request
from domainkit.contrib.approvals import (
    IMPERSONATE,
    REACTIVATE,
    SET_GROUP_PERMISSIONS,
    SET_GROUPS,
    SET_STAFF,
    ApprovalsSettings,
    Reactivate,
    SetStaff,
    approvers_of,
    fingerprint,
    guards,
)
from domainkit.contrib.approvals.builtin import (
    _updated,
    check_admin_change,
    check_group_change,
)
from domainkit.contrib.approvals.services import _spend
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.auth import AuthUnitOfWork, User
from domainkit.contrib.auth.gates import (
    AdminChange,
    GroupChange,
    on_admin_change,
)
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.i18n import gettext, override
from domainkit.management.base import run_async
from domainkit_tests.conftest import ALPHA, Migrated, Shape
from domainkit_tests.support.auth import create_user, grant
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
    assert request["arguments"] == {"user_id": str(bob)}
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
    """A group as ``SetGroups`` holds it: its id, name, and permissions."""
    return {"id": str(group_id), "name": name, "permissions": sorted(permissions)}


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

    arguments = {"user_id": str(bob)}
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
    assert request["arguments"] == {"user_id": str(boss)}
    assert guards()[REACTIVATE].parties_of(Reactivate(user_id=boss)) == {str(boss)}
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
        (REACTIVATE, Reactivate(user_id=plain_id)),
        (SET_STAFF, SetStaff(user_id=plain_id)),
        None,
    ]


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
