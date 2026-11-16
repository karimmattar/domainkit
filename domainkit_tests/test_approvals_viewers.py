"""Who reads which requests, whom to tell of one, and rules of a project's own.

``GET /requests`` shows the requests a caller made and those they may
decide; ``approvals.view_every_approvalrequest`` shows all (user report
P3). ``approvers_of`` finds whom to tell of a request, and a durable
handler of ``RequestSubmitted`` tells them. ``select_routes`` adds a staff
rule and a step-up to the routes of a project's choice.
"""

from collections.abc import Iterator
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from domainkit.conf import override_settings
from domainkit.contrib.approvals import (
    GRANT_SUPERUSER,
    ApprovalRequest,
    ApprovalsUnitOfWork,
    GrantSuperuser,
    Policy,
    RequestSubmitted,
    approvers_of,
    guard,
    submit_command,
)
from domainkit.contrib.approvals.api import router as approvals_router
from domainkit.contrib.auth import IsAdminUser
from domainkit.contrib.auth.tables import permissions
from domainkit.contrib.contenttypes.tables import content_types
from domainkit.contrib.mail import send_mail
from domainkit.contrib.mail.backends import outbox
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.http import get_asgi_application
from domainkit.http.access import Require, open_routes
from domainkit.http.routers import select_routes
from domainkit.management.base import run_async
from domainkit_tests.conftest import MEMORY_MAIL, Migrated, Shape
from domainkit_tests.sample_approval_routes import Proof
from domainkit_tests.support.auth import create_user, grant
from domainkit_tests.test_accounting_api import sale
from domainkit_tests.test_approvals import Act, acting_guarded
from domainkit_tests.test_approvals_api import (
    APPROVALS_API,
    PROOF,
    decide,
    problem,
    submit,
)
from domainkit_tests.test_auth_admin import join, make_group, token_for

API: Final = "/approvals/requests"
VIEW: Final = "approvals.view_approvalrequest"
VIEWERS: Final = Shape(
    "approvals_viewers",
    "approvals",
    {
        **APPROVALS_API.settings,
        "INSTALLED_DOMAINS": [
            *APPROVALS_API.settings["INSTALLED_DOMAINS"][:3],
            "domainkit.contrib.mail",
            *APPROVALS_API.settings["INSTALLED_DOMAINS"][3:],
        ],
        "MAIL": MEMORY_MAIL,
    },
)
"""The approvals API's project, with mail kept in memory."""
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API, with an action of two policies: by permission and by group."""
    with migrated(VIEWERS), TestClient(get_asgi_application()) as test_client:
        guard(
            "tests.act",
            acting_guarded,
            unit_of_work=ApprovalsUnitOfWork,
            policies=[
                Policy(
                    "tests.by_permission",
                    approvals=2,
                    permission="accounting.add_account",
                    when=lambda command: command.kind == "p",
                ),
                Policy("tests.by_group", group="treasurers"),
            ],
            domain="alpha",
        )
        make_group("treasurers")
        make_group("accountants", "accounting.add_account")
        outbox.clear()
        yield test_client


def person(name: str, *permissions: str, **fields: Any) -> UUID:  # noqa: ANN401 - user fields.
    """A user granted ``permissions``, with an address; their id."""
    made = create_user(name, email=f"{name}@example.com", **fields)
    grant(name, *permissions)
    return made


def submitted(action: str, command: object, maker: UUID) -> ApprovalRequest:
    """``command`` of ``action``, submitted by ``maker``."""
    return run_async(submit_command(action, command, maker=str(maker)))


def listed(client: TestClient, headers: Headers) -> set[str]:
    """The ids of the requests ``headers``' caller lists."""
    found = client.get(API, headers=headers)
    assert found.status_code == 200, found.json()
    return {each["id"] for each in found.json()["items"]}


def test_a_viewer_reads_their_own_and_those_they_may_decide(
    client: TestClient,
) -> None:
    """Makers, holders, members, and superusers each read their share; reads only.

    Without being maker or approver, ``view_approvalrequest`` reads none;
    ``view_every_approvalrequest`` reads all. Approving is not narrowed: a
    caller who may not decide is refused as before (403, not 404).
    """
    maker = person("maker", VIEW)
    person("holder", VIEW, "accounting.add_account")
    person("member", VIEW)
    join("member", "treasurers")
    person("auditor", VIEW)
    person("every", VIEW, "approvals.view_every_approvalrequest")
    person("staff", VIEW, "auth.change_user", is_staff=True)
    root = person("root", is_superuser=True)
    person("other", is_superuser=True)
    target = person("target")
    by_permission = str(submitted("tests.act", Act(kind="p"), maker).id)
    by_group = str(submitted("tests.act", Act(kind="g"), maker).id)
    grant_ = str(submitted(GRANT_SUPERUSER, GrantSuperuser(user_id=target), root).id)
    everything = {by_permission, by_group, grant_}

    hidden = client.get(f"{API}/{by_permission}", headers=token_for("auditor"))
    refused = decide(
        client, {"id": by_permission, "fingerprint": "0" * 64}, token_for("auditor")
    )

    assert listed(client, token_for("maker")) == {by_permission, by_group}
    assert listed(client, token_for("holder")) == {by_permission}
    assert listed(client, token_for("member")) == {by_group}
    assert listed(client, token_for("auditor")) == set()
    assert listed(client, token_for("staff")) == set()
    assert listed(client, token_for("every")) == everything
    assert listed(client, token_for("root")) == everything
    assert listed(client, token_for("other")) == everything
    problem(hidden, 404, "approval_request_not_found")
    problem(refused, 403, "permission_denied")


def lookalike_permission() -> str:
    """A permission of auth's whose codename is ``add_account``, as another app's."""

    async def store() -> None:
        async with connections.engine().begin() as connection:
            user_type = await connection.scalar(
                select(content_types.c.id).where(
                    content_types.c.domain == "auth", content_types.c.model == "user"
                )
            )
            await connection.execute(
                insert(permissions).values(
                    content_type_id=user_type, codename="add_account", name="Lookalike"
                )
            )

    run_async(store())
    return "auth.add_account"


def test_approvers_of_finds_whom_to_tell(client: TestClient) -> None:
    """By permission, group, or rule; never the maker, a party, or who decided.

    A permission of another app with the same codename is not the policy's.
    """
    maker = person("maker", "accounting.add_account")
    person("lookalike", lookalike_permission())
    join("maker", "treasurers")
    person("holder", "accounting.add_account")
    person("grouped")
    join("grouped", "accountants")
    person("idle", "accounting.add_account", is_active=False)
    person("member")
    join("member", "treasurers")
    person("plain")
    root = person("root", is_superuser=True)
    person("other", is_superuser=True)
    person("staff", "auth.change_user", is_staff=True)
    target = person("target", is_superuser=True)
    by_permission = submitted("tests.act", Act(kind="p"), maker)
    by_group = submitted("tests.act", Act(kind="g"), maker)
    grant_ = submitted(GRANT_SUPERUSER, GrantSuperuser(user_id=target), root)
    shown = {"id": str(by_permission.id), "fingerprint": by_permission.fingerprint}
    assert decide(client, shown, token_for("holder")).json()["approvals"] == 1

    def names(request: ApprovalRequest) -> list[str]:
        async def find() -> list[str]:
            async with ApprovalsUnitOfWork() as uow:
                stored = await uow.requests.get(request.id)
            return sorted(user.username for user in await approvers_of(stored))

        return run_async(find())

    assert names(by_permission) == ["grouped", "other", "root", "target"]
    assert names(by_group) == ["member"]
    assert names(grant_) == ["other"]


async def tell_approvers(event: RequestSubmitted) -> None:
    """The recipe: mail each approver of a request just submitted.

    A project subscribes it with ``@handles(RequestSubmitted, durable=True,
    name=...)``; here it is called with the event.
    """
    async with ApprovalsUnitOfWork() as uow:
        request = await uow.requests.get(event.request_id)
    addresses = [user.email for user in await approvers_of(request) if user.email]
    if addresses:
        await send_mail(
            f"An approval waits: {request.action}",
            f"Request {request.id} waits for your decision.",
            addresses,
        )


def test_the_recipe_mails_the_approvers_of_a_request(client: TestClient) -> None:
    """One message, to the people who may decide it, through the outbox."""
    maker = person("maker")
    person("member")
    join("member", "treasurers")
    person("plain")
    request = submitted("tests.act", Act(kind="g"), maker)
    event = RequestSubmitted(
        request_id=request.id,
        action=request.action,
        policy=request.policy,
        fingerprint=request.fingerprint,
        required=request.required,
    )

    run_async(tell_approvers(event))
    run_async(drain())

    [message] = outbox
    assert message.to == ("member@example.com",)
    assert message.subject == "An approval waits: tests.act"
    assert str(request.id) in message.body


STAFF_ONLY: Final = Depends(Require(IsAdminUser()))
STEP_UP: Final = Depends(Require(Proof()))
"""Stand-ins: a project puts ``StepUp(max_age=...)`` where ``Proof`` is."""
router = APIRouter()
router.include_router(
    select_routes(
        approvals_router,
        dependencies={
            "POST /requests": [STAFF_ONLY, STEP_UP],
            "POST /requests/{request_id}/reject": [STAFF_ONLY],
            "POST /requests/{request_id}/cancel": [STAFF_ONLY, STEP_UP],
        },
    ),
    prefix="/approvals",
)
"""The recipe of docs/contrib/approvals.md: staff submit, reject, and cancel."""


def test_a_project_asks_staff_and_a_step_up_of_the_routes_it_names(
    migrated: Migrated,
) -> None:
    """Submitting and cancelling ask staff and a fresh proof; rejecting asks staff."""
    with (
        migrated(VIEWERS),
        override_settings(ROOT_ROUTER=f"{__name__}.router"),
        TestClient(get_asgi_application()) as client,
    ):
        clerk = ("approvals.add_approvalrequest", VIEW, "accounting.add_entry")
        plain = token_for_new("plain", *clerk)
        clerk_staff = token_for_new("clerk", *clerk, is_staff=True)
        approver = token_for_new("approver", VIEW, "accounting.add_entry")
        approver_staff = token_for_new(
            "boss", VIEW, "accounting.add_entry", is_staff=True
        )
        entry = large_entry()

        by_plain = submit(client, plain, "accounting.post_entry", entry)
        unproved = submit(client, clerk_staff, "accounting.post_entry", entry)
        made = submit(client, {**PROOF, **clerk_staff}, "accounting.post_entry", entry)
        request = made.json()
        not_staff = decide(client, request, approver, "reject", reason="No.")
        cancel = f"{API}/{request['id']}/cancel"
        cancel_unproved = client.post(cancel, json={}, headers=clerk_staff)
        second = submit(
            client, {**PROOF, **clerk_staff}, "accounting.post_entry", entry
        )
        rejected = decide(client, second.json(), approver_staff, "reject", reason="No.")
        cancelled = client.post(cancel, json={}, headers={**PROOF, **clerk_staff})
        app = client.app

        problem(by_plain, 403, "permission_denied")
        problem(unproved, 403, "permission_denied")
        assert unproved.json()["detail"] == "Prove who you are again."
        assert made.status_code == 201, made.json()
        problem(not_staff, 403, "permission_denied")
        problem(cancel_unproved, 403, "permission_denied")
        assert rejected.json()["status"] == "rejected", rejected.json()
        assert cancelled.json()["status"] == "cancelled", cancelled.json()
        assert open_routes(app) == []  # type: ignore[arg-type]


def token_for_new(name: str, *permissions: str, **fields: Any) -> Headers:  # noqa: ANN401 - user fields.
    """Headers of a new user granted ``permissions``."""
    person(name, *permissions, **fields)
    return token_for(name)


def large_entry() -> dict[str, Any]:
    """A posting of 5000 dollars, over the threshold, as the API takes it."""
    accounts = {"cash": {"id": str(uuid4())}, "sales": {"id": str(uuid4())}}
    return sale(accounts, "big", "5000.00")
