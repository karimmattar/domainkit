"""Approval requests and tenants: read, decided, and run in their own (ADR 0183).

A request belongs to the tenant it was submitted in. It is listed, read,
approved, rejected, and cancelled where that tenant is served alone, by
the routes and by the services, and its command runs there, whatever
tenant the message that runs it names. A request of no tenant, as every
request stored before 2.8.1 is, is read where no tenant is served. The
audit's reproducer (A28-SEC-02) is the first test.

Members' roles come from ``MEMBER_PERMISSIONS``: ``MembershipBackend``
grants them in the tenant served, beside what users hold everywhere.
"""

from collections.abc import Iterator
from typing import Any, ClassVar, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit.contrib.accounting import AccountingUnitOfWork
from domainkit.contrib.approvals import (
    ApprovalPending,
    ApprovalRequest,
    ApprovalsUnitOfWork,
    ApproveRequest,
    CancelRequest,
    Policy,
    RejectRequest,
    RequestApproved,
    RequestCancelled,
    RequestExpired,
    RequestId,
    RequestNotFound,
    RequestRejected,
    RequestStatus,
    RequestTampered,
    approve_request,
    approvers_of,
    cancel_request,
    expire_requests,
    guard,
    reject_request,
    requires_approval,
)
from domainkit.contrib.approvals.models import seal_of
from domainkit.contrib.approvals.personal import erase_subject
from domainkit.contrib.approvals.services import (
    check_approval,
    check_cancellation,
    check_rejection,
    held_request_waits,
)
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.messaging.tables import messages
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import Command, event_bus
from domainkit.ddd.tenancy import (
    TenantRequired,
    all_tenants,
    current_tenant,
    no_tenant,
    row_tenant,
)
from domainkit.http import get_asgi_application
from domainkit.http.access import BasePermission, Principal
from domainkit.management.base import run_async
from domainkit.tenancy import tenant
from domainkit_tests.conftest import Migrated, Shape
from domainkit_tests.test_approvals import acting
from domainkit_tests.test_approvals_api import APPROVALS_API, PROOF
from domainkit_tests.test_approvals_viewers import person, submitted
from domainkit_tests.test_auth_admin import token_for

API: Final = "/approvals/requests"
VIEW: Final = "approvals.view_approvalrequest"
VIEW_EVERY: Final = "approvals.view_every_approvalrequest"
APPROVER: Final = "accounting.add_account"
ROLE: Final = frozenset({VIEW, VIEW_EVERY, "approvals.add_approvalrequest", APPROVER})
"""A member's role in each of their tenants: maker, viewer, and approver."""
MEMBERS: Final = {"maker": {"a", "b"}, "mia": {"a"}, "anna": {"a"}, "bob": {"b"}}
ACTION: Final = "tests.noted"
RAN: list[tuple[str, str | None]] = []
"""What the guarded use case was run with: its note, and the tenant served."""


def member_permissions(user: Any, tenant_id: str) -> frozenset[str] | None:  # noqa: ANN401 - a user.
    """Each user's role, in their own tenants only."""
    name = str(getattr(user, "username", ""))
    return ROLE if tenant_id in MEMBERS.get(name, ()) else None


class Noted(Command):
    """What the sample use case notes when it runs."""

    note: str


async def note(command: Noted, uow: AccountingUnitOfWork) -> None:
    """Note the command, and the tenant it runs in."""
    RAN.append((command.note, current_tenant()))


noted = requires_approval(ACTION)(note)

_settings = dict(APPROVALS_API.settings)
_middleware = list(_settings["MIDDLEWARE"])
_middleware.insert(
    _middleware.index("domainkit.contrib.auth.middleware.AuthenticationMiddleware") + 1,
    "domainkit.tenancy.TenantMiddleware",
)
TENANTS: Final = Shape(
    "approvals_tenancy",
    "approvals",
    {
        **_settings,
        "MIDDLEWARE": _middleware,
        "TENANCY": {
            "HEADER": "X-Tenant",
            "REQUIRED": False,
            "MEMBER_PERMISSIONS": f"{__name__}.member_permissions",
        },
        "AUTH": {
            **_settings["AUTH"],
            "BACKENDS": [
                "domainkit.contrib.auth.backends.ModelBackend",
                "domainkit.contrib.auth.backends.MembershipBackend",
            ],
        },
    },
)
"""The approvals API's project, served per tenant by a header, with members' roles."""
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API, and a guard whose policy names its approvers by a key."""
    RAN.clear()
    with migrated(TENANTS), TestClient(get_asgi_application()) as test_client:
        guard(
            ACTION,
            noted,
            unit_of_work=AccountingUnitOfWork,
            policies=[Policy("tests.by_permission", permission=APPROVER)],
            permission=APPROVER,
            domain="alpha",
        )
        for name in MEMBERS:
            person(name)  # No permission anywhere: only a role in each tenant.
        yield test_client


def made_in(
    tenant_id: str | None, maker: str = "maker", text: str = "x"
) -> ApprovalRequest:
    """A request of ``maker``'s, submitted in ``tenant_id`` (None: in none)."""
    maker_id = user_id(maker)
    with no_tenant() if tenant_id is None else tenant(tenant_id):
        return submitted(ACTION, Noted(note=text), maker_id)


def user_id(name: str) -> UUID:
    """The id of the user ``name``."""
    from domainkit.contrib.auth import AuthUnitOfWork

    async def find() -> UUID:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(name)
            assert found is not None
            return found.id

    return run_async(find())


def as_member(name: str, tenant_id: str | None) -> Headers:
    """Headers of ``name``, served as ``tenant_id`` (None: as no tenant)."""
    headers = token_for(name)
    return headers if tenant_id is None else {**headers, "X-Tenant": tenant_id}


def listed(client: TestClient, headers: Headers) -> set[str]:
    """The ids of the requests ``headers``' caller lists."""
    found = client.get(API, headers=headers)
    assert found.status_code == 200, found.json()
    return {each["id"] for each in found.json()["items"]}


def approve(client: TestClient, made: ApprovalRequest, headers: Headers) -> Any:  # noqa: ANN401 - a response.
    """Approve ``made`` as ``headers`` say, stating its fingerprint."""
    return client.post(
        f"{API}/{made.id}/approve",
        json={"fingerprint": made.fingerprint},
        headers={**PROOF, **headers},
    )


def stored(request_id: UUID) -> ApprovalRequest:
    """The request ``request_id`` as committed, whatever its tenant."""

    async def read() -> ApprovalRequest:
        with all_tenants():
            async with ApprovalsUnitOfWork() as uow:
                return await uow.requests.get(RequestId(request_id))

    return run_async(read())


# Over HTTP ---------------------------------------------------------------------------


def test_a_role_in_one_tenant_reaches_no_request_of_another(client: TestClient) -> None:
    """The audit's reproducer: Bob's role in b neither lists, reads, nor decides a's.

    His role holds the view keys, every request's included, and the
    policy's approver key; a's request is ``404`` to him, as one that does
    not exist, on every route.
    """
    of_a, of_b = made_in("a"), made_in("b")
    bob = as_member("bob", "b")

    assert listed(client, bob) == {str(of_b.id)}
    assert client.get(f"{API}/{of_a.id}", headers=bob).status_code == 404
    assert approve(client, of_a, bob).status_code == 404
    rejected = client.post(
        f"{API}/{of_a.id}/reject", json={"reason": "no"}, headers={**PROOF, **bob}
    )
    assert rejected.status_code == 404
    assert client.get(API, params={"action": ACTION}, headers=bob).json()["items"] == [
        client.get(f"{API}/{of_b.id}", headers=bob).json()
    ]
    assert stored(of_a.id).status is RequestStatus.PENDING
    assert stored(of_a.id).decisions == []


def test_a_request_is_decided_in_its_own_tenant(client: TestClient) -> None:
    """Anna, of a, lists, reads, and approves a's request; it runs as a."""
    of_a = made_in("a", "mia", "of a")
    anna = as_member("anna", "a")

    assert listed(client, anna) == {str(of_a.id)}
    assert client.get(f"{API}/{of_a.id}", headers=anna).status_code == 200
    approved = approve(client, of_a, anna)
    assert approved.status_code == 200, approved.json()
    assert queued_tenants() == ["a"]
    run_async(drain())

    assert RAN == [("of a", "a")]
    assert stored(of_a.id).status is RequestStatus.EXECUTED


def queued_tenants() -> list[str | None]:
    """The tenant each queued run of an approved command names."""

    async def read() -> list[str | None]:
        async with connections.engine().connect() as connection:
            rows = await connection.execute(
                select(messages.c.headers).where(
                    messages.c.target == "domainkit.approvals.execute"
                )
            )
        return [row.headers.get("tenant") for row in rows]

    return run_async(read())


def test_the_command_runs_in_the_requests_tenant_whatever_its_message_says(
    client: TestClient,
) -> None:
    """A message that names another tenant runs the command as the request's.

    Mia, the maker, holds the guard's key only through her role in a: the
    run checks her there, so it passes, and the use case sees tenant a.
    """
    of_a = made_in("a", "mia", "moved")
    assert approve(client, of_a, as_member("anna", "a")).status_code == 200

    async def name_another_tenant() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(messages)
                .where(messages.c.target == "domainkit.approvals.execute")
                .values(headers={"tenant": "b"})
            )

    run_async(name_another_tenant())
    assert queued_tenants() == ["b"]
    run_async(drain())

    assert RAN == [("moved", "a")]
    assert stored(of_a.id).status is RequestStatus.EXECUTED


def test_a_maker_cancels_in_the_tenant_they_submitted_in(client: TestClient) -> None:
    """The maker, of a and b, cancels a's request in a; in b it is not there."""
    of_a = made_in("a")

    in_b = client.post(
        f"{API}/{of_a.id}/cancel", json={}, headers=as_member("maker", "b")
    )
    assert in_b.status_code == 404
    in_a = client.post(
        f"{API}/{of_a.id}/cancel", json={}, headers=as_member("maker", "a")
    )
    assert in_a.status_code == 200, in_a.json()
    assert in_a.json()["status"] == "cancelled"


def test_a_request_of_no_tenant_is_read_where_no_tenant_is_served(
    client: TestClient,
) -> None:
    """Made outside a tenant: listed by a caller served as none, never in a tenant.

    Bob holds nothing everywhere, so served as no tenant he lists only
    his own; a user granted the view keys everywhere lists it, and none of
    a's.
    """
    of_none, of_a = made_in(None), made_in("a")
    person("auditor", VIEW, VIEW_EVERY)

    assert listed(client, as_member("auditor", None)) == {str(of_none.id)}
    assert str(of_none.id) not in listed(client, as_member("bob", "b"))
    assert (
        client.get(f"{API}/{of_a.id}", headers=as_member("auditor", None)).status_code
        == 404
    )
    assert (
        client.get(
            f"{API}/{of_none.id}", headers=as_member("auditor", None)
        ).status_code
        == 200
    )


def test_a_request_moved_to_another_tenant_in_the_database_is_refused(
    client: TestClient,
) -> None:
    """The seal covers the tenant: moved around the application, nothing decides it."""
    of_a = made_in("a")

    async def move() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests).where(requests.c.id == of_a.id).values(tenant_id="b")
            )

    run_async(move())
    refused = approve(client, of_a, as_member("bob", "b"))

    assert refused.status_code == 409, refused.json()
    assert refused.json()["code"] == RequestTampered.code


# The services and the worker ---------------------------------------------------------


def test_a_request_of_no_tenant_keeps_the_seal_it_had(client: TestClient) -> None:
    """A request stored before the tenant was recorded verifies as it did."""
    of_none = made_in(None)

    assert of_none.tenant_id is None
    assert of_none.seal == seal_of(
        of_none.id,
        of_none.action,
        of_none.fingerprint,
        of_none.maker,
        of_none.policy,
        of_none.required,
        of_none.permission,
        of_none.group,
        of_none.expires_at.isoformat(),
        of_none.status.value,
        of_none.run_token,
    )
    assert made_in("a").tenant_id == "a"


def test_the_services_find_no_request_of_another_tenant(client: TestClient) -> None:
    """From code too: deciding, cancelling, or asking to, in b, of a's request."""
    of_a = made_in("a")
    bob, maker = str(user_id("bob")), str(user_id("maker"))
    approval = ApproveRequest(
        request_id=of_a.id, approver=bob, fingerprint=of_a.fingerprint
    )
    rejection = RejectRequest(request_id=of_a.id, approver=bob, reason="no")
    cancellation = CancelRequest(request_id=of_a.id, maker=maker)

    async def in_b() -> list[str]:
        refused = []
        with tenant("b"):
            for attempt in (
                approve_request(approval, ApprovalsUnitOfWork()),
                reject_request(rejection, ApprovalsUnitOfWork()),
                cancel_request(cancellation, ApprovalsUnitOfWork()),
                check_approval(approval),
                check_rejection(rejection),
                check_cancellation(cancellation),
            ):
                with pytest.raises(RequestNotFound):
                    await attempt
                refused.append("refused")
            refused.append(
                str(await held_request_waits(f"approvals.request:{of_a.id}"))
            )
        with tenant("a"):
            refused.append(
                str(await held_request_waits(f"approvals.request:{of_a.id}"))
            )
        return refused

    assert run_async(in_b()) == ["refused"] * 6 + ["False", "True"]
    assert stored(of_a.id).status is RequestStatus.PENDING


def where() -> tuple[str | None, str | None]:
    """The tenant served, and the tenant rows are read in (None: every tenant's)."""
    try:
        rows = row_tenant()
    except TenantRequired:
        rows = "no tenant"
    return current_tenant(), rows


def test_code_that_reads_every_tenant_says_so(client: TestClient) -> None:
    """Inside ``all_tenants()`` a's request is decided, as a alone, and runs as a.

    Code across tenants reads every tenant's requests; the approval commits
    as the request's tenant alone, so its handlers read a's rows, and its
    run is queued as a.
    """
    of_a, made_in_b = made_in("a", "mia", "back office"), made_in("b")
    anna = str(user_id("anna"))
    heard: list[tuple[str | None, str | None]] = []

    async def note_approved(event: RequestApproved) -> None:
        heard.append(where())

    event_bus.subscribe(RequestApproved, note_approved)

    async def decide() -> int:
        with all_tenants():
            async with ApprovalsUnitOfWork() as uow:
                every = await uow.requests.count()
            await approve_request(
                ApproveRequest(
                    request_id=of_a.id, approver=anna, fingerprint=of_a.fingerprint
                ),
                ApprovalsUnitOfWork(),
            )
        return every

    assert run_async(decide()) == 2
    assert heard == [("a", "a")]
    assert queued_tenants() == ["a"]
    run_async(drain())
    assert RAN == [("back office", "a")]
    assert stored(made_in_b.id).status is RequestStatus.PENDING


def test_code_that_rejects_or_cancels_across_tenants_commits_as_the_tenant(
    client: TestClient,
) -> None:
    """Inside ``all_tenants()``, a rejection and a withdrawal commit as a alone."""
    rejected, cancelled = made_in("a", "mia", "no"), made_in("a", "mia", "gone")
    anna, mia = str(user_id("anna")), str(user_id("mia"))
    heard: list[tuple[str, tuple[str | None, str | None]]] = []

    async def note_rejected(event: RequestRejected) -> None:
        heard.append(("rejected", where()))

    async def note_cancelled(event: RequestCancelled) -> None:
        heard.append(("cancelled", where()))

    event_bus.subscribe(RequestRejected, note_rejected)
    event_bus.subscribe(RequestCancelled, note_cancelled)

    async def decide() -> None:
        with all_tenants():
            await reject_request(
                RejectRequest(request_id=rejected.id, approver=anna, reason="no"),
                ApprovalsUnitOfWork(),
            )
            await cancel_request(
                CancelRequest(request_id=cancelled.id, maker=mia),
                ApprovalsUnitOfWork(),
            )

    run_async(decide())

    assert heard == [("rejected", ("a", "a")), ("cancelled", ("a", "a"))]


async def note_by_maker(command: Noted, uow: AccountingUnitOfWork) -> None:
    """Note the command, and the tenant it runs in: its maker runs it."""
    RAN.append((command.note, current_tenant()))


noted_by_maker = requires_approval("tests.noted_by_maker")(note_by_maker)


def test_a_makers_call_across_tenants_spends_its_own_tenants_request(
    client: TestClient,
) -> None:
    """Inside ``all_tenants()``, served as b, Mia's call does not spend a's approval.

    It finds the requests of the tenant it is served as alone: a's
    approved request stays approved, and the call submits one in b.
    """
    guard(
        "tests.noted_by_maker",
        noted_by_maker,
        unit_of_work=AccountingUnitOfWork,
        policies=[Policy("tests.by_maker", permission=APPROVER)],
        by_maker=True,
        domain="alpha",
    )
    mia = user_id("mia")
    with tenant("a"):
        of_a = submitted("tests.noted_by_maker", Noted(note="once"), mia)
    assert approve(client, of_a, as_member("anna", "a")).status_code == 200

    async def call() -> UUID:
        with (
            acting(str(mia)),
            all_tenants(),
            tenant("b"),
            pytest.raises(ApprovalPending) as waiting,
        ):
            await noted_by_maker(Noted(note="once"), AccountingUnitOfWork())
        return UUID(str(waiting.value.request_id))

    submitted_in_b = run_async(call())

    assert submitted_in_b != of_a.id
    assert stored(of_a.id).status is RequestStatus.APPROVED
    assert stored(submitted_in_b).tenant_id == "b"
    assert RAN == []


def test_expiry_passes_over_a_tenant_that_is_not_one(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored tenant that is not a tenant id is held as tampered; the rest expire.

    A request found due, then gone from where its tenant is served, is
    passed over too: the round goes on.
    """
    import logging

    from domainkit.clock import freeze_time
    from domainkit.contrib.approvals.uow import RequestRepository

    caplog.set_level(logging.WARNING, logger="domainkit.contrib.approvals.services")
    bad, gone, good = made_in("a"), made_in("a"), made_in("b")
    garble(bad.id, "not a tenant!")
    found = RequestRepository.tenants_of

    async def raced(
        self: RequestRepository,
        ids: Any,  # noqa: ANN401 - ids.
    ) -> dict[Any, str | None]:
        tenants = await found(self, ids)
        return {**tenants, gone.id: "b"}  # Not where it is: not found there.

    monkeypatch.setattr(RequestRepository, "tenants_of", raced)
    with freeze_time(max(each.expires_at for each in (bad, gone, good))):
        assert run_async(expire_requests()) == 1

    assert stored(good.id).status is RequestStatus.EXPIRED
    assert {stored(each.id).status for each in (bad, gone)} == {RequestStatus.PENDING}
    assert f"Approval request {bad.id} is tampered with" in caplog.messages


def test_erasure_reaches_a_request_whose_tenant_is_not_one(client: TestClient) -> None:
    """A request changed around the application still names the person: erased too.

    It is read among every tenant's and committed as no tenant; its seal,
    which no longer held, is not made again, so it is not cancelled.
    """
    bad, good = made_in("a"), made_in("b")
    garble(bad.id, "")

    counts = run_async(erase_subject(str(user_id("maker"))))

    assert counts == {"requests": 2, "cancelled": 1}
    assert {stored(each.id).maker.startswith("erased:") for each in (bad, good)} == {
        True
    }
    assert (stored(bad.id).status, stored(good.id).status) == (
        RequestStatus.PENDING,
        RequestStatus.CANCELLED,
    )


def garble(request_id: UUID, value: str) -> None:
    """Set the request's stored tenant to ``value``, around the application."""

    async def work() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests)
                .where(requests.c.id == request_id)
                .values(tenant_id=value)
            )

    run_async(work())


def test_a_tenant_changed_around_the_application_is_never_served(
    client: TestClient,
) -> None:
    """An approved request whose tenant is not one fails as tampered, and runs nothing.

    Its seal is checked before its tenant is served: no error to retry.
    """
    of_a = made_in("a", "mia", "moved")
    assert approve(client, of_a, as_member("anna", "a")).status_code == 200

    async def garble() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests)
                .where(requests.c.id == of_a.id)
                .values(tenant_id="not a tenant!")
            )

    run_async(garble())
    run_async(drain())

    done = stored(of_a.id)
    assert (done.status, done.reason) == (RequestStatus.FAILED, RequestTampered.code)
    assert RAN == []


class Noting(BasePermission):
    """An approver rule that notes the tenant it is asked in, and allows."""

    asked: ClassVar[list[tuple[str | None, str | None]]] = []

    async def has_permission(self, request: Any, user: Principal) -> bool:  # noqa: ANN401 - a connection.
        """Allow, noting where."""
        self.asked.append(where())
        return True


async def note_ruled(command: Noted, uow: AccountingUnitOfWork) -> None:
    """Run nothing: a guard whose approvers a rule names."""


noted_by_rule = requires_approval("tests.noted_by_rule")(note_ruled)


def test_whom_to_tell_is_asked_in_the_requests_tenant(client: TestClient) -> None:
    """``approvers_of`` asks the approver rule as the request's tenant, from none."""
    guard(
        "tests.noted_by_rule",
        noted_by_rule,
        unit_of_work=AccountingUnitOfWork,
        policies=[Policy("tests.by_rule", approver_rule=Noting())],
        domain="alpha",
    )
    person("staffer", is_staff=True)
    with tenant("a"):
        made = submitted("tests.noted_by_rule", Noted(note="x"), user_id("maker"))
    Noting.asked.clear()

    found = run_async(approvers_of(made))

    assert [user.username for user in found] == ["staffer"]
    assert Noting.asked == [("a", "a")]


def test_expiry_reaches_every_tenant_and_commits_as_each(client: TestClient) -> None:
    """The periodic expiry, served as no tenant, expires a's, b's, and none's.

    Each ``RequestExpired`` is published as its request's tenant, so live
    events of it reach that tenant's connections.
    """
    from domainkit.clock import freeze_time

    made = {name: made_in(name) for name in ("a", "b")} | {None: made_in(None)}
    heard: list[tuple[UUID, tuple[str | None, str | None]]] = []

    async def note_expired(event: RequestExpired) -> None:
        heard.append((event.request_id, where()))

    event_bus.subscribe(RequestExpired, note_expired)
    later = max(each.expires_at for each in made.values())
    with freeze_time(later):
        assert run_async(expire_requests()) == 3

    assert sorted(heard, key=str) == sorted(
        (
            (each.id, (name, "no tenant" if name is None else name))
            for name, each in made.items()
        ),
        key=str,
    )
    assert {stored(each.id).status for each in made.values()} == {RequestStatus.EXPIRED}


def test_erasing_a_person_reaches_every_tenants_requests(client: TestClient) -> None:
    """``privacy erase`` pseudonymises the maker in a's and b's requests alike.

    Each tenant's are committed as that tenant alone: a handler of their
    cancellation reads that tenant's rows.
    """
    of_a, of_b = made_in("a"), made_in("b")
    maker = str(user_id("maker"))
    heard: list[tuple[str | None, str | None]] = []

    async def note_cancelled(event: RequestCancelled) -> None:
        heard.append(where())

    event_bus.subscribe(RequestCancelled, note_cancelled)

    counts = run_async(erase_subject(maker))

    assert counts == {"requests": 2, "cancelled": 2}
    assert sorted(heard) == [("a", "a"), ("b", "b")]
    assert {stored(each.id).maker.startswith("erased:") for each in (of_a, of_b)} == {
        True
    }


def test_an_approvers_list_reads_through_an_index_of_its_scope() -> None:
    """The tenant, the action, the policy, and the order: one index (A28-PERF-04).

    An approver's list reads, of the tenant served, the requests of each
    action and policy that names them, newest first; without it, an
    approver of a rare policy read the whole table on every page.
    """
    (index,) = (
        each
        for each in requests.indexes
        if each.name == "ix_domainkit_approval_requests_scope"
    )

    assert [column.name for column in index.columns] == [
        "tenant_id",
        "action",
        "policy",
        "submitted_at",
    ]
