"""The admin API's act binds each group by id and grants (user report A17).

With ``AUTH.ADMIN_ACTION`` on, an admin change is confirmed as one act,
``auth.admin_change``. Its act named the groups a user was added to, and
``AUTH.AUTHORIZATIONS.HELD_TTL`` kept its confirmation while the change
waited for approval: renamed meanwhile, another group took the name, the
same call submitted a request for it, and once that was approved the
confirmation kept for the first made the change. The maker's phone had
shown "Adds: readers". The project's audit proved it on 2.7.

Since 2.8 the act binds each group's id and grants, a kept confirmation
serves the request it was kept for alone, making staff or a user active
again holds the fields sent with it, a value another user has is refused
before the hold, and the keys a setting names show by name (ADR 0179).
"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.approvals import (
    REACTIVATE,
    SET_GROUP_PERMISSIONS,
    SET_GROUPS,
    SET_PERMISSIONS,
    SET_STAFF,
    ApprovalsUnitOfWork,
    HeldGroup,
    Reactivate,
    SetGroupPermissions,
    SetGroups,
    SetPermissions,
    SetStaff,
    held_groups,
    named_keys_held,
)
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.auth import AuthUnitOfWork, UnknownGroup, User
from domainkit.contrib.auth.admin_actions import ActGroup, AdminAct, show
from domainkit.contrib.auth.backends import ObjectGrant
from domainkit.contrib.auth.gates import AdminChange, on_admin_change
from domainkit.contrib.auth.integration import named_first, named_permissions
from domainkit.contrib.auth.screens import TooMuchToShow
from domainkit.contrib.auth.settings import AuthorizationsSettings
from domainkit.http import get_asgi_application
from domainkit.i18n import override
from domainkit.management.base import run_async
from domainkit_tests.conftest import Migrated, Shape, UseAuth
from domainkit_tests.support.auth import audited, create_user
from domainkit_tests.test_admin_actions import ACTS, ADMIN, Staff, problem, row
from domainkit_tests.test_admin_actions import frozen as frozen  # The fixture.
from domainkit_tests.test_approvals_admin import (
    GROUP_GRANTS,
    comes_back,
    deactivated,
    end4,
    screen_of,
)
from domainkit_tests.test_auth_admin import join, make_group

HELD: Final = Shape(
    "admin_acts_groups_held",
    "approvals",
    {
        **ACTS.settings,
        "AUTH": {
            **ACTS.settings["AUTH"],
            "AUTHORIZATIONS": {"HELD_TTL": timedelta(days=1)},
        },
        "APPROVALS": {
            **ACTS.settings["APPROVALS"],
            "ADMIN_APPROVALS": {
                "auth.set_staff": 1,
                "auth.set_groups": 1,
                "auth.set_permissions": 1,
                "auth.reactivate": 1,
                "auth.set_group_permissions": 1,
            },
        },
    },
)
"""Every change an act, its confirmation kept a day while it waits; grants
held for a superuser's approval."""
READS: Final = ("auth.view_user",)
WRITES: Final = ("auth.view_user", "auth.delete_user")
NUMBER: Final = "+447700900123"
OTHER_NUMBER: Final = "+447700900456"
NAMED: Final = "auth.delete_user"
"""The key the screens' tests name (``NAMED_PERMISSIONS``)."""


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API: each change an act, grants held, confirmations kept."""
    with migrated(HELD), TestClient(get_asgi_application()) as test_client:
        yield test_client


def approve(root: Staff, request_id: str) -> None:
    """Root approves the request ``request_id``, as read."""
    shown = root.client.get(f"/approvals/requests/{request_id}", headers=root.headers)
    approved = root.client.post(
        f"/approvals/requests/{request_id}/approve",
        json={"fingerprint": shown.json()["fingerprint"]},
        headers=root.headers,
    )
    assert approved.status_code == 200, approved.json()


def status_of(root: Staff, request_id: str) -> str:
    """The status of the request ``request_id``."""
    read = root.client.get(f"/approvals/requests/{request_id}", headers=root.headers)
    status: str = read.json()["status"]
    return status


def rename(root: Staff, group: UUID, name: str) -> None:
    """Root renames ``group`` to ``name``, confirming the change."""
    path = f"/groups/{group}"
    authorization = root.authorized(f"{path}/authorization/patch", {"name": name})
    renamed = root.call("PATCH", path, {"name": name}, authorization)
    assert renamed.status_code == 200, renamed.json()


def user_of(user_id: UUID) -> User:
    """The user ``user_id``, as stored now, with their groups."""

    async def read() -> User:
        async with AuthUnitOfWork() as uow:
            return await uow.users.get(user_id)  # type: ignore[arg-type,return-value]

    return run_async(read())


def groups_of(user_id: UUID) -> set[UUID]:
    """The ids of the groups ``user_id`` is in, as stored now."""
    return {group.id for group in user_of(user_id).groups}


def widen(group_id: UUID, *keys: str) -> None:
    """Grant the group ``group_id`` more, as a way round the admin API would."""

    async def grant_more() -> None:
        async with AuthUnitOfWork() as uow:
            group = await uow.groups.get(group_id)  # type: ignore[arg-type]
            group.grant(*await uow.permissions.get_many(keys))
            await uow.commit()

    run_async(grant_more())


def pending(response: Any) -> str:  # noqa: ANN401 - a response.
    """The id of the request ``response`` says the change waits for."""
    problem(response, 403, "approval_pending")
    return str(response.json()["approval_request_id"])


def stored_requests() -> list[dict[str, Any]]:
    """Every approval request stored: its id, status, and arguments."""

    async def read() -> list[dict[str, Any]]:
        async with ApprovalsUnitOfWork() as uow:
            found = await uow.session.execute(
                select(requests.c.id, requests.c.status, requests.c.arguments)
            )
            return [dict(each._mapping) for each in found]

    return run_async(read())


def at_once(staff: Staff, method: str, path: str, body: Any, held: str) -> list[Any]:  # noqa: ANN401 - JSON.
    """Six calls of ``staff``'s at once, each sending the authorization ``held``."""
    headers = {**staff.headers, "Action-Authorization": held}

    async def send() -> list[Any]:
        transport = httpx2.ASGITransport(app=staff.client.app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            return list(
                await asyncio.gather(
                    *(
                        http.request(method, ADMIN + path, json=body, headers=headers)
                        for _ in range(6)
                    )
                )
            )

    return run_async(send())


def code_of(response: Any) -> object:  # noqa: ANN401 - a response.
    """The status of a success, the problem's code otherwise."""
    if response.status_code < 400:
        return response.status_code
    return response.json()["code"]


# Row (4): the act binds each group's id and grants ---------------------------------


def test_a_kept_confirmation_never_adds_the_group_that_took_the_name(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The project's audit, step by step: the phone confirmed readers, not admins.

    1. Ann asks to add Bob to "readers", and confirms "Adds: readers".
    2. The change waits for approval; ``HELD_TTL`` keeps the confirmation.
    3. "readers" is renamed, and admins takes the name.
    4. The same call, the same confirmation, submits a new request: admins.
    5. Approved, the confirmation kept for the first request added Bob to
       admins (2.7).

    The act binds each group's id and grants: step 4 is refused, and the
    confirmation is void.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = create_user("bob")
    readers = make_group("readers", *READS)
    admins = make_group("admins", *WRITES)
    path = f"/users/{bob}/groups"
    body = {"groups": ["readers"]}

    authorization = ann.authorized(f"{path}/authorization", body)
    first = pending(ann.call("PUT", path, body, authorization))
    rename(root, readers, "old-readers")
    rename(root, admins, "readers")
    swapped = ann.call("PUT", path, body, authorization)
    if swapped.status_code == 403 and swapped.json()["code"] == "approval_pending":
        approve(root, swapped.json()["approval_request_id"])  # What 2.7 allowed.
    made = ann.call("PUT", path, body, authorization)

    problem(swapped, 403, "action_not_authorized")
    problem(made, 403, "action_not_authorized")
    assert row(authorization)["consumed_at"] is not None
    assert groups_of(bob) == set()
    assert [each["id"] for each in stored_requests()] == [UUID(first)]


def test_the_act_binds_what_each_group_grants_and_which_it_takes_away(
    client: TestClient, frozen: FrozenClock
) -> None:
    """A group granted more since, or another taken away under its name: refused.

    The phone showed "readers (…9abc) 1 perm"; readers granted a key since
    is another act (``403 action_not_authorized``, voided), as approvals'
    request is another command. Taking away "writers" binds that group's
    id: renamed, and another of the user's groups named so, the same body
    takes nothing away.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = create_user("bob")
    readers = make_group("readers", *READS)
    writers = make_group("writers")
    clerks = make_group("clerks")
    join("bob", "writers", "clerks")
    adding = {"groups": ["readers", "writers", "clerks"]}
    keeping = {"groups": ["clerks"]}
    path = f"/users/{bob}/groups"

    asked = ann.ask(f"{path}/authorization", adding)
    widened = ann.confirm(asked.json()["id"])
    widen(readers, "auth.delete_user")
    more = ann.call("PUT", path, adding, widened)
    removing = ann.authorized(f"{path}/authorization", keeping)
    rename(root, writers, "spare")
    rename(root, clerks, "writers")
    rename(root, writers, "clerks")
    other = ann.call("PUT", path, keeping, removing)

    assert asked.json()["shown"]["details"][1] == {
        "label": "Adds",
        "value": f"readers ({end4(readers)}) 1 perm",
    }
    problem(more, 403, "action_not_authorized")
    problem(other, 403, "action_not_authorized")
    assert row(widened)["consumed_at"] is not None
    assert row(removing)["consumed_at"] is not None
    assert stored_requests() == []
    assert groups_of(bob) == {writers, clerks}


def named_robert(user_id: UUID) -> None:
    """Store "Robert" as the user's first name, as a way round the admin API would."""

    async def rename() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(user_id)  # type: ignore[arg-type]
            user.update(first_name="Robert")
            await uow.commit()

    run_async(rename())


ROBERT: Final = {"is_active": True, "fields": {"first_name": "Robert"}}
"""A return that sets a name: held with it while the user is named otherwise."""


def test_a_kept_confirmation_serves_the_request_it_was_kept_for_alone(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Held for one request, a confirmation never submits or spends another.

    Bob's return, naming him Robert, was confirmed and waits. He is named
    so meanwhile: the request would hold the return alone now (a value he
    holds is not held), so the same call would submit a request of its
    own; it is refused (``action_not_authorized``, the trail's
    ``held_elsewhere``), and a new confirmation submits it. In 2.7 the
    same call submitted it, and once it was approved, the confirmation
    held for the first request, which waited still, made it.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = deactivated("bob")
    make_group("readers", *READS)
    join("bob", "readers")
    path = f"/users/{bob}"
    body = ROBERT

    authorization = ann.authorized(f"{path}/authorization/patch", body)
    first = pending(ann.call("PATCH", path, body, authorization))
    named_robert(bob)
    refused = ann.call("PATCH", path, body, authorization)
    if refused.status_code == 403 and refused.json()["code"] == "approval_pending":
        approve(root, refused.json()["approval_request_id"])  # What 2.7 allowed.
        late = ann.call("PATCH", path, body, authorization)
        assert late.status_code == 403, late.json()
    again = ann.authorized(f"{path}/authorization/patch", body)
    second = pending(ann.call("PATCH", path, body, again))
    waits_on_another = ann.call("PATCH", path, body, authorization)
    approve(root, second)
    late = ann.call("PATCH", path, body, authorization)
    made = ann.call("PATCH", path, body, again)

    problem(refused, 403, "action_not_authorized")
    problem(waits_on_another, 403, "action_not_authorized")
    assert "held_elsewhere" in [
        each["details"]["reason"] for each in audited("auth.authorization")
    ]
    assert row(authorization)["held_for"] == f"approvals.request:{first}"
    problem(late, 403, "action_not_authorized")
    assert row(authorization)["consumed_at"] is None  # It serves the first still.
    assert made.status_code == 200, made.json()
    assert status_of(root, first) == "pending"
    assert status_of(root, second) == "executed"


def test_a_kept_confirmation_of_a_return_given_more_since_is_void(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Bob's return waits; Bob is put in admins: the act is another one.

    The act binds what Bob comes back with (since 2.10, user report A26),
    as the request does: the call is refused (``action_not_authorized``),
    its confirmation voided and the trail told (``changed``), and nothing
    is submitted. A new confirmation, which shows admins, submits it.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = deactivated("bob")
    make_group("readers", *READS)
    make_group("admins", *WRITES)
    join("bob", "readers")
    path = f"/users/{bob}"
    body = {"is_active": True}

    authorization = ann.authorized(f"{path}/authorization/patch", body)
    first = pending(ann.call("PATCH", path, body, authorization))
    join("bob", "admins")
    refused = ann.call("PATCH", path, body, authorization)
    submitted = [each["id"] for each in stored_requests()]
    again = ann.authorized(f"{path}/authorization/patch", body)
    second = pending(ann.call("PATCH", path, body, again))
    approve(root, second)
    made = ann.call("PATCH", path, body, again)

    problem(refused, 403, "action_not_authorized")
    assert row(authorization)["consumed_at"] is not None
    assert "changed" in [
        each["details"]["reason"] for each in audited("auth.authorization")
    ]
    assert submitted == [UUID(first)]
    assert made.status_code == 200, made.json()
    assert status_of(root, first) == "pending"


def test_calls_at_once_on_a_kept_confirmation_make_the_change_once(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Six calls at once with one kept confirmation, approved: one change.

    The others are refused before anything is made
    (``action_not_authorized``), and the request runs once.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = create_user("bob")
    readers = make_group("readers", *READS)
    path = f"/users/{bob}/groups"
    body = {"groups": ["readers"]}
    authorization = ann.authorized(f"{path}/authorization", body)
    request_id = pending(ann.call("PUT", path, body, authorization))
    approve(root, request_id)

    answers = at_once(ann, "PUT", path, body, authorization)

    codes = sorted(map(str, map(code_of, answers)))
    assert codes == ["200"] + ["action_not_authorized"] * 5, codes
    assert status_of(root, request_id) == "executed"
    assert groups_of(bob) == {readers}
    assert len(stored_requests()) == 1  # None submitted by the calls that lost.


def test_calls_at_once_after_the_change_moved_on_submit_nothing(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Approved, then the user put in a group: calls at once make nothing.

    Each builds another command than the approved one, and is refused
    before it submits a request of its own: the confirmation serves the
    approved request alone. Nothing is submitted, the approval stays, and
    Bob stays inactive.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = deactivated("bob")
    make_group("readers", *READS)
    make_group("admins", *WRITES)
    join("bob", "readers")
    path = f"/users/{bob}"
    body = {"is_active": True}
    authorization = ann.authorized(f"{path}/authorization/patch", body)
    first = pending(ann.call("PATCH", path, body, authorization))
    approve(root, first)
    join("bob", "admins")

    answers = at_once(ann, "PATCH", path, body, authorization)
    others = [each["id"] for each in stored_requests() if each["id"] != UUID(first)]
    for each in others:  # What 2.7 allowed: approved, the confirmation made it.
        if status_of(root, str(each)) == "pending":
            approve(root, str(each))
    late = ann.call("PATCH", path, body, authorization)

    assert {code_of(each) for each in answers} == {"action_not_authorized"}
    assert others == []
    problem(late, 403, "action_not_authorized")
    assert status_of(root, first) == "approved"
    assert user_of(bob).is_active is False


def test_a_kept_confirmation_never_adds_a_group_made_again_or_given_a_grant(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Approved for readers: deleted and made again, or granted an object since.

    Each is another act (``403 action_not_authorized``, voided): the
    approval is never spent and nothing is submitted.
    """
    auth = {
        **HELD.settings["AUTH"],
        "BACKENDS": [
            "domainkit.contrib.auth.backends.ModelBackend",
            "domainkit_tests.test_approvals_admin.GroupGrantsBackend",
        ],
    }
    GROUP_GRANTS.clear()
    try:
        with override_settings(AUTH=auth):
            ann = Staff(client, frozen, "ann", *WRITES)
            root = Staff(client, frozen, "root", superuser=True)
            bob = create_user("bob")
            readers = make_group("readers", *READS)
            make_group("clerks", *READS)
            path = f"/users/{bob}/groups"
            body, clerks = {"groups": ["readers"]}, {"groups": ["clerks"]}
            made_again = ann.authorized(f"{path}/authorization", body)
            first = pending(ann.call("PUT", path, body, made_again))
            approve(root, first)
            gone = root.authorized(f"/groups/{readers}/authorization/delete")
            deleted = root.call("DELETE", f"/groups/{readers}", None, gone)
            make_group("readers", *READS)
            again = ann.call("PUT", path, body, made_again)
            granted = ann.authorized(f"{path}/authorization", clerks)
            second = pending(ann.call("PUT", path, clerks, granted))
            approve(root, second)
            GROUP_GRANTS["clerks"] = frozenset(
                {ObjectGrant("auth.view_user", "auth.user", 7)}
            )
            widened = ann.call("PUT", path, clerks, granted)
    finally:
        GROUP_GRANTS.clear()

    assert deleted.status_code == 204
    problem(again, 403, "action_not_authorized")
    problem(widened, 403, "action_not_authorized")
    assert row(made_again)["consumed_at"] is not None
    assert row(granted)["consumed_at"] is not None
    assert {each["id"] for each in stored_requests()} == {UUID(first), UUID(second)}
    assert groups_of(bob) == set()


def test_calls_at_once_after_a_rename_are_all_refused(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Approved for readers, admins takes the name: six calls at once, none adds."""
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = create_user("bob")
    readers = make_group("readers", *READS)
    admins = make_group("admins", *WRITES)
    path = f"/users/{bob}/groups"
    body = {"groups": ["readers"]}
    authorization = ann.authorized(f"{path}/authorization", body)
    first = pending(ann.call("PUT", path, body, authorization))
    approve(root, first)
    rename(root, readers, "old")
    rename(root, admins, "readers")

    answers = at_once(ann, "PUT", path, body, authorization)

    assert {code_of(each) for each in answers} == {"action_not_authorized"}
    assert [each["id"] for each in stored_requests()] == [UUID(first)]
    assert status_of(root, first) == "approved"
    assert groups_of(bob) == set()


def test_calls_at_once_on_a_pending_return_that_moved_on_submit_nothing(
    client: TestClient, frozen: FrozenClock
) -> None:
    """Bob's return waits; he is named meanwhile: six calls at once submit nothing."""
    ann = Staff(client, frozen, "ann", *WRITES)
    bob = deactivated("bob")
    make_group("readers", *READS)
    join("bob", "readers")
    path = f"/users/{bob}"
    body = ROBERT
    authorization = ann.authorized(f"{path}/authorization/patch", body)
    first = pending(ann.call("PATCH", path, body, authorization))
    named_robert(bob)

    answers = at_once(ann, "PATCH", path, body, authorization)

    assert {code_of(each) for each in answers} == {"action_not_authorized"}
    assert [each["id"] for each in stored_requests()] == [UUID(first)]
    assert row(authorization)["consumed_at"] is None  # It serves the first still.
    assert user_of(bob).is_active is False


def test_a_retry_after_an_approval_lapsed_unrun_is_made(
    client: TestClient, frozen: FrozenClock
) -> None:
    """An approved request that lapsed unrun is passed over, never refused for.

    Approved requests never expire in the table: they lapse. Its retry,
    on a new confirmation kept for the new request, once approved, makes
    the change, and the later ones too. On the first form of 2.8 it was
    refused (``held_elsewhere``) for the lapsed one, found first (found in
    review).
    """
    ann = Staff(client, frozen, "ann")
    root = Staff(client, frozen, "root", superuser=True)
    bob = create_user("bob")
    carl = create_user("carl")
    body = {"is_staff": True}

    def held_change(user: UUID) -> Any:  # noqa: ANN401 - a response.
        path = f"/users/{user}"
        authorization = ann.authorized(f"{path}/authorization/patch", body)
        request_id = pending(ann.call("PATCH", path, body, authorization))
        approve(root, request_id)
        return ann.call("PATCH", path, body, authorization)

    first = ann.authorized(f"/users/{bob}/authorization/patch", body)
    lapsed = pending(ann.call("PATCH", f"/users/{bob}", body, first))
    approve(root, lapsed)
    frozen.tick(timedelta(days=3, minutes=10))  # EXPIRES_AFTER: it lapses unrun.
    made = held_change(bob)
    later = held_change(carl)

    assert made.status_code == 200, made.json()
    assert later.status_code == 200, later.json()
    assert "held_elsewhere" not in [
        each["details"]["reason"] for each in audited("auth.authorization")
    ]
    assert status_of(root, lapsed) == "approved"


def test_a_number_taken_between_the_check_and_the_commit_spends_the_approval(
    client: TestClient, frozen: FrozenClock
) -> None:
    """What is left: a number taken after the check is refused by the commit.

    The approval is spent by then (approvals' hold, in its own
    transaction), as the docs say; the act's authorization is not, and
    Bob stays inactive. A lock taken for the check would not hold off an
    insert that takes none, and on SQLite it is the database's write lock,
    which approvals' hold then waits on.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = deactivated("bob")
    make_group("readers", *READS)
    join("bob", "readers")
    path = f"/users/{bob}"
    body = {"is_active": True, "fields": {"mobile_number": NUMBER}}
    authorization = ann.authorized(f"{path}/authorization/patch", body)
    request_id = pending(ann.call("PATCH", path, body, authorization))
    approve(root, request_id)
    armed = [True]

    async def taker(actor: Any, target: Any, change: AdminChange) -> None:  # noqa: ANN401
        if armed.pop() if armed else False:
            async with AuthUnitOfWork() as uow:
                await uow.users.create_user("carol", None, mobile_number=NUMBER)
                await uow.commit()

    on_admin_change(taker, domain="auth", order=200)  # After approvals' hold.
    refused = ann.call("PATCH", path, body, authorization)

    problem(refused, 409, "mobile_number_taken")
    assert status_of(root, request_id) == "executed"
    assert row(authorization)["consumed_at"] is None
    assert user_of(bob).is_active is False


def test_held_groups_holds_a_projects_own_grant_as_set_groups_holds(
    client: TestClient,
) -> None:
    """``held_groups(names)``: by id, with what each grants; read again, compared.

    An invitation of the project's that names groups holds what this
    returns: renamed and replaced, or granted more, the groups read again
    otherwise, and the project refuses the invitation.
    """
    readers = make_group("readers", *READS)
    make_group("admins", *WRITES)

    held = run_async(held_groups(["readers", "admins", "readers"]))

    async def in_uow() -> tuple[HeldGroup, ...]:
        async with AuthUnitOfWork() as uow:
            return await held_groups(["readers"], uow)

    alone = run_async(in_uow())
    widen(readers, "auth.add_user")
    wider = run_async(held_groups(["readers"]))

    assert [(each.name, each.permissions) for each in held] == [
        ("admins", tuple(sorted(WRITES))),
        ("readers", READS),
    ]
    assert held[1].id == readers
    assert alone == (held[1],)
    assert wider != alone
    with pytest.raises(UnknownGroup):
        run_async(held_groups(["nobody"]))


def test_named_keys_held_reads_the_keys_a_screen_names_that_a_user_holds(
    client: TestClient,
) -> None:
    """``named_keys_held(user)``: the keys a screen names, held directly or by group.

    For a grant of the project's own, as the holds read them: inactive or
    not, sorted; a superuser's flag is apart, as the screens show it
    ("Superuser"). None while the setting names none (user report A26).
    """
    make_group("admins", *WRITES)
    bob = deactivated("bob")
    join("bob", "admins")
    carol = create_user("carol")
    boss = create_user("boss", is_superuser=True)

    async def grant_directly() -> None:
        async with AuthUnitOfWork() as uow:
            user = await uow.users.get(carol)  # type: ignore[arg-type]
            user.grant(*await uow.permissions.get_many([NAMED, "auth.add_user"]))
            await uow.commit()

    run_async(grant_directly())
    with override_settings(AUTH=named_auth()):
        held = [named_keys_held(user_of(each)) for each in (bob, carol, boss)]
    unnamed = named_keys_held(user_of(bob))

    assert held == [(NAMED,), (NAMED,), ()]
    assert unnamed == ()


# Row (5): the fields sent with a grant are held; values taken refused first --------


def test_making_staff_holds_the_fields_sent_with_it(
    client: TestClient, frozen: FrozenClock
) -> None:
    """The approval binds the number sent with it: another one waits apart.

    In 2.7 ``auth.set_staff`` held the user alone: approved for Bob with
    one number, the call made with another spent the approval, and the
    approver never saw either.
    """
    ann = Staff(client, frozen, "ann")
    root = Staff(client, frozen, "root", superuser=True)
    bob = create_user("bob")
    path = f"/users/{bob}"
    shown = {"is_staff": True, "fields": {"mobile_number": NUMBER}}
    other = {"is_staff": True, "fields": {"mobile_number": OTHER_NUMBER}}

    first = ann.authorized(f"{path}/authorization/patch", shown)
    request_id = pending(ann.call("PATCH", path, shown, first))
    arguments = stored_requests()[0]["arguments"]
    approve(root, request_id)
    second = ann.authorized(f"{path}/authorization/patch", other)
    swapped = ann.call("PATCH", path, other, second)
    made = ann.call("PATCH", path, shown, first)

    assert arguments == {
        "user_id": str(bob),
        "values": {"mobile_number": NUMBER},
        "is_superuser": False,
        "groups": [],
        "permissions": [],
        "object_grants": 0,
        "object_grants_digest": "",
    }
    assert pending(swapped) != request_id
    assert made.status_code == 200, made.json()
    assert made.json()["fields"]["mobile_number"] == NUMBER


def test_a_number_taken_is_refused_before_the_approval_is_spent(
    client: TestClient, frozen: FrozenClock
) -> None:
    """A number another user took meanwhile: ``409``, and the approval stays.

    In 2.7 the number was refused by the change's commit, after
    approvals' hold had spent the approval: the change was refused, and
    approved again. The twin refuses a number taken already.
    """
    ann = Staff(client, frozen, "ann", *WRITES)
    root = Staff(client, frozen, "root", superuser=True)
    bob = deactivated("bob")
    make_group("readers", *READS)
    join("bob", "readers")
    path = f"/users/{bob}"
    body = {"is_active": True, "fields": {"mobile_number": NUMBER}}

    authorization = ann.authorized(f"{path}/authorization/patch", body)
    request_id = pending(ann.call("PATCH", path, body, authorization))
    approve(root, request_id)
    create_user("carol", mobile_number=NUMBER)
    refused = ann.call("PATCH", path, body, authorization)
    asked = ann.ask(f"{path}/authorization/patch", body)

    problem(refused, 409, "mobile_number_taken")
    problem(asked, 409, "mobile_number_taken")
    assert status_of(root, request_id) == "approved"
    assert row(authorization)["consumed_at"] is None
    assert user_of(bob).is_active is False


def test_what_2_7_held_and_bound_reads_as_it_did() -> None:
    """Left out when empty: a request or an authorization of 2.7 still holds.

    ``values`` of making staff or a return, and an act's ``groups`` and
    ``taken``: a pending return of 2.7 is found by its fingerprint and
    spent, and an authorization of any change but a group's still binds.
    Making staff holds what the user comes back with since 2.10, so a
    request of 2.9 or before is submitted again (user report A26).
    """
    bob = UUID(int=7)
    staff = TypeAdapter(SetStaff).dump_python(SetStaff(user_id=bob), mode="json")
    returning = TypeAdapter(Reactivate).dump_python(comes_back(bob), mode="json")
    act = AdminAct(kind="update", user_id=bob, values={"is_staff": True})

    assert staff == {
        "user_id": str(bob),
        "is_superuser": False,
        "groups": [],
        "permissions": [],
        "object_grants": 0,
        "object_grants_digest": "",
    }
    assert "values" not in returning
    assert act.model_dump(mode="json") == {
        "kind": "update",
        "user_id": str(bob),
        "group_id": None,
        "name": None,
        "values": {"is_staff": True},
        "added": [],
        "removed": [],
        "password": None,
    }


# Row (6): the keys a setting names show by name -------------------------------------


def named_auth() -> dict[str, Any]:
    """``AUTH`` of ``HELD``, naming ``NAMED``."""
    return {
        **HELD.settings["AUTH"],
        "AUTHORIZATIONS": {"NAMED_PERMISSIONS": [NAMED]},
    }


def test_each_hold_names_the_keys_the_setting_names(client: TestClient) -> None:
    """A group shown as "readers (…9abc) 3 perms" names the key that matters too.

    Adding a group, a user's return, a group's grant, a user's grant: the
    keys ``NAMED_PERMISSIONS`` names show by name ("Grants"), or first in
    a list, so a list cut and counted keeps them. Each fits the screen
    whole; one line too many is refused under ``WHOLE``, and counted
    without it, the named keys kept.
    """
    bob = create_user("bob")
    admins = make_group("admins", *WRITES)
    held = HeldGroup(admins, "admins", WRITES)
    joined = SetGroups(user_id=bob, added=("admins",), groups=(held,))
    returning = comes_back(bob, groups=(held,), values={"mobile_number": NUMBER})
    granted = SetGroupPermissions(
        group_id=admins, name="admins", added=("auth.add_user", NAMED)
    )
    direct = SetPermissions(user_id=bob, added=("auth.add_user", NAMED))
    staff = SetStaff(user_id=bob, values={"is_active": True, "first_name": "B"})
    many = SetGroups(
        user_id=bob,
        added=tuple(f"g{each}" for each in range(10)),
        groups=tuple(
            HeldGroup(UUID(int=each), f"g{each}" * 12, (NAMED,)) for each in range(10)
        ),
    )
    with override_settings(AUTH=named_auth()):
        screens = [
            screen_of(action, command, whole=True)
            for action, command in (
                (SET_GROUPS, joined),
                (REACTIVATE, returning),
                (SET_GROUP_PERMISSIONS, granted),
                (SET_PERMISSIONS, direct),
                (SET_STAFF, staff),
            )
        ]
        with pytest.raises(TooMuchToShow):
            screen_of(SET_GROUPS, many, whole=True)
        counted = screen_of(SET_GROUPS, many, whole=False)
    unnamed = screen_of(SET_GROUPS, joined, whole=True)

    user = ("User", screens[0][1][1])
    assert screens[0] == [
        ("Action", SET_GROUPS),
        user,
        ("Grants", NAMED),
        ("Adds", f"admins ({end4(admins)}) 2 perms"),
    ]
    assert screens[1] == [
        ("Action", REACTIVATE),
        user,
        ("Grants", NAMED),
        ("Groups", "admins"),
        ("Mobile number", NUMBER),
    ]
    assert screens[2][2] == ("Adds", f"{NAMED}, auth.add_user")
    assert screens[3][2] == ("Adds", f"{NAMED}, auth.add_user")
    assert screens[4] == [
        ("Action", SET_STAFF),
        user,
        ("Active", "yes"),
        ("First name", "B"),
    ]
    assert counted[2] == ("Grants", NAMED)
    assert ("Grants", NAMED) not in unnamed


def test_the_holds_screens_name_what_they_take_away_and_older_shapes(
    client: TestClient,
) -> None:
    """Grants on objects counted, what is taken away named, a field cleared in words.

    The words moved to auth (``group_item``, ``list_item``) and read as in
    2.7; no key is named while ``NAMED_PERMISSIONS`` names none.
    """
    bob = create_user("bob")
    readers = make_group("readers", *READS)
    held = HeldGroup(readers, "readers", READS, 2, "0" * 64)
    joined = SetGroups(
        user_id=bob, added=("readers",), removed=("clerks",), groups=(held,)
    )
    older = SetGroups(user_id=bob, added=("readers",))
    alone = comes_back(
        bob, is_superuser=True, values={"is_staff": "x", "mobile_number": None}
    )
    taking = SetGroupPermissions(
        group_id=readers, name="readers", added=(NAMED,), removed=("auth.add_user",)
    )
    act = AdminAct(kind="groups", user_id=bob, added=("readers",))

    screens = [
        screen_of(SET_GROUPS, joined, whole=True),
        screen_of(SET_GROUPS, older, whole=True),
        screen_of(REACTIVATE, alone, whole=True),
        screen_of(SET_GROUP_PERMISSIONS, taking, whole=True),
    ]
    with override("ar"):
        cleared = screen_of(REACTIVATE, alone, whole=True)[-1]
    shown = run_async(show(act)).details

    assert screens[0][2:] == [
        ("Adds", f"readers ({end4(readers)}) 1 perm + 2 object grants"),
        ("Takes away", "clerks"),
    ]
    assert screens[1][2:] == [("Adds", "readers")]
    assert screens[2][2:] == [
        ("Superuser", "yes"),
        ("Staff", "x"),
        ("Mobile number", "(none)"),
    ]
    assert cleared[1] == "(لا شيء)"
    assert screens[3][2:] == [("Adds", NAMED), ("Takes away", "auth.add_user")]
    assert shown[1:] == (("Adds", "readers"),)


def test_the_admin_acts_screen_names_the_keys_too(client: TestClient) -> None:
    """The staff member's phone: "Grants" before the groups, named keys first."""
    bob = create_user("bob")
    admins = ActGroup(id=UUID(int=1), name="admins", permissions=WRITES)
    groups = AdminAct(kind="groups", user_id=bob, added=("admins",), groups=(admins,))
    keys = AdminAct(
        kind="permissions", user_id=bob, added=("auth.add_user", NAMED, "auth.x_y")
    )

    with override_settings(AUTH=named_auth()):
        shown = [run_async(show(each)).details for each in (groups, keys)]
        assert named_first(["b.c", NAMED, "a.b"]) == [NAMED, "a.b", "b.c"]
    plain = run_async(show(groups)).details

    assert shown[0][1:] == (
        ("Grants", NAMED),
        ("Adds", "admins (…0001) 2 perms"),
    )
    assert shown[1][1] == ("Adds", f"{NAMED}, auth.add_user, auth.x_y")
    assert plain[1:] == (("Adds", "admins (…0001) 2 perms"),)


def test_the_named_keys_default_to_the_key_that_reaches_every_tenant(
    use_auth: UseAuth,
) -> None:
    """Unset, ``TENANCY.ALL_TENANTS_PERMISSION``, where it is set; ``()``: none."""
    use_auth()
    tenancy = {
        "MEMBER_PERMISSIONS": "domainkit_tests.anywhere.members",
        "MEMBER_TENANTS": "domainkit_tests.anywhere.tenants",
        "ALL_TENANTS_PERMISSION": "companies.administer_company",
    }
    unset = named_permissions()
    with override_settings(TENANCY=tenancy):
        reaching = named_permissions()
        with override_settings(AUTH={"AUTHORIZATIONS": {"NAMED_PERMISSIONS": []}}):
            none = named_permissions()

    assert unset == frozenset()
    assert reaching == {"companies.administer_company"}
    assert none == frozenset()


@pytest.mark.parametrize(
    "named",
    [
        ["auth"],
        ["auth.view user"],
        [f"auth.{'x' * 60}"],
        [f"a.b{each}" for each in range(21)],
    ],
)
def test_the_named_keys_are_keys_on_one_line_and_few(named: list[str]) -> None:
    """A key ``<domain>.<codename>`` of 64 characters at most; 20 keys at most."""
    with pytest.raises(ValidationError):
        AuthorizationsSettings(NAMED_PERMISSIONS=named)
    assert hash(AuthorizationsSettings(NAMED_PERMISSIONS=[NAMED])) != hash(
        AuthorizationsSettings()
    )
