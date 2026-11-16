"""Submitting, rejecting, and cancelling as acts, authorized once (user report A6).

With ``APPROVALS.SUBMIT_ACTION``, ``REJECT_ACTION`` and ``CANCEL_ACTION``
on (ADR 0163), each asks for an authorization of its own, as approving
does with ``APPROVE_ACTION``: ``approvals.submit`` on the action and its
arguments, ``approvals.reject`` and ``approvals.cancel`` on the request and
the reason. The caller asks for one at the route's twin, confirms it, and
acts with it once; the screen shows the action, the reason, and the
arguments, built by the server. Off (the default), each is as it was, and
the twins answer ``404``.

Code the project trusts takes every step without an authorization, through
``record_submission``, ``record_approval``, ``record_rejection``, and
``record_cancellation`` (ADR 0163, amended 2026-11-17), and is refused all
the rest the services refuse.
"""

import warnings
from collections.abc import Callable, Iterator
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import FrozenClock
from domainkit.conf import override_settings
from domainkit.contrib.approvals import (
    AlreadyDecided,
    ApprovalRequest,
    ApprovalRequired,
    ApprovalsSettings,
    ApprovalsUnitOfWork,
    ApproveRequest,
    CancelRequest,
    FingerprintMismatch,
    InvalidArguments,
    MakerCannotDecide,
    NotTheMaker,
    Policy,
    RejectRequest,
    RequestNotPending,
    RequestStatus,
    RequestTampered,
    SubmitRequest,
    UnknownAction,
    approve_request,
    cancel_request,
    guard,
    record_approval,
    record_cancellation,
    record_rejection,
    record_submission,
    reject_request,
    submit_request,
)
from domainkit.contrib.approvals.api import (
    ApproveIn,
    CancelIn,
    RejectIn,
    RequestOut,
    SubmitIn,
    caller,
)
from domainkit.contrib.approvals.builtin import (
    check_admin_change,
    refuse_staff_creation,
)
from domainkit.contrib.approvals.rules import (
    ApproveAction,
    CancelAction,
    DecisionAction,
    RejectAction,
    RequestActions,
    SubmitAction,
)
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.auth.actions import AuthorizationRequired, action_for
from domainkit.contrib.auth.gates import AdminChange
from domainkit.contrib.messaging.testing import drain
from domainkit.db import connections
from domainkit.ddd import fingerprint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.management.base import run_async
from domainkit.resources import ResourceRouter
from domainkit_tests.conftest import ALPHA, Migrated, Shape
from domainkit_tests.sample_admin_action_routes import TRANSFER, transfer
from domainkit_tests.support.auth import audited, create_user, grant
from domainkit_tests.test_admin_actions import ACTS, problem, row
from domainkit_tests.test_admin_actions import frozen as frozen  # The fixture.
from domainkit_tests.test_approvals import APPROVALS, AUDIT, MESSAGING
from domainkit_tests.test_approvals_api import PROOF
from domainkit_tests.test_approvals_api import client as client  # The fixture.
from domainkit_tests.test_auth_admin import join, make_group, token_for
from domainkit_tests.test_auth_stepup import enrol, now_code

API: Final = "/approvals/requests"
TREASURERS: Final = "treasurers"
PAYMENT: Final = {
    "payee": "Ada Lovelace",
    "iban": "DE89 3704 0044 0532 0130 00",
    "amount": "2500.00",
    "currency": "EUR",
    "memo": "Invoice 7",
    "reference": "inv-7",
}
EVERY_STEP: Final = Shape(
    "approvals_every_step",
    "approvals",
    {
        **ACTS.settings,
        "APPROVALS": {**ACTS.settings["APPROVALS"], "APPROVE_ACTION": True},
    },
)
"""``ACTS``, approving an act too: every step of a request asks the phone."""


def guard_transfers() -> None:
    """Transfers wait for two treasurers."""
    guard(
        TRANSFER,
        transfer,
        unit_of_work=ApprovalsUnitOfWork,
        policies=[Policy("tests.acted_transfers", group=TREASURERS, approvals=2)],
        domain="alpha",
    )
    make_group(TREASURERS)


@pytest.fixture
def api(migrated: Migrated) -> Iterator[TestClient]:
    """Approvals' acts on; transfers wait for two treasurers."""
    with migrated(ACTS), TestClient(get_asgi_application()) as test_client:
        guard_transfers()
        yield test_client


@pytest.fixture
def every_step(migrated: Migrated) -> Iterator[TestClient]:
    """Every step of a request an act; transfers wait for two treasurers."""
    with migrated(EVERY_STEP), TestClient(get_asgi_application()) as test_client:
        guard_transfers()
        yield test_client


class Person:
    """Someone logged in, with an authenticator app, a treasurer or not."""

    def __init__(
        self, http: TestClient, clock: FrozenClock, name: str, *, treasurer: bool
    ) -> None:
        """Create ``name``, who may submit and read requests; enrol a second factor."""
        self.id = create_user(name)
        grant(name, "approvals.add_approvalrequest", "approvals.view_approvalrequest")
        if treasurer:
            join(name, TREASURERS)
        self.client = http
        self.clock = clock
        self.headers = token_for(name)
        self.secret = enrol(http, self.headers, clock)

    def post(
        self,
        path: str,
        body: dict[str, Any],
        authorization: str | None = None,
    ) -> Any:  # noqa: ANN401 - a response.
        """``POST path`` with ``body``, sending ``authorization`` if given."""
        headers = dict(self.headers)
        if authorization is not None:
            headers["Action-Authorization"] = authorization
        return self.client.post(API + path, json=body, headers=headers)

    def authorized(self, path: str, body: dict[str, Any]) -> str:
        """A confirmed authorization of what ``body`` does at ``path``."""
        asked = self.post(f"{path}/authorization", body)
        assert asked.status_code == 201, asked.json()
        confirmed = self.client.post(
            f"/auth/authorizations/{asked.json()['id']}/confirm",
            json={"code": now_code(self.secret, self.clock)},
            headers=self.headers,
        )
        assert confirmed.status_code == 200, confirmed.json()
        authorization: str = confirmed.json()["authorization"]
        return authorization

    def submit(self, **arguments: str) -> dict[str, Any]:
        """Submit a transfer of ``PAYMENT`` (changed by ``arguments``), authorized."""
        body = {"action": TRANSFER, "arguments": {**PAYMENT, **arguments}}
        made = self.post("", body, self.authorized("", body))
        assert made.status_code == 201, made.json()
        request: dict[str, Any] = made.json()
        return request


def shown_of(response: Any) -> list[tuple[str, str]]:  # noqa: ANN401 - a response.
    """The title, then each label and value, a twin's answer shows."""
    assert response.status_code == 201, response.json()
    shown = response.json()["shown"]
    return [("", shown["title"])] + [
        (each["label"], each["value"]) for each in shown["details"]
    ]


def test_off_by_default_submit_reject_and_cancel_ask_nothing(
    client: TestClient,
) -> None:
    """Off, none asks for an authorization, and the three twins answer 404."""
    create_user("root", is_superuser=True)
    create_user("other", is_superuser=True)
    promoted = create_user("promoted")
    root = token_for("root")
    other = {**PROOF, **token_for("other")}
    body = {"action": "auth.grant_superuser", "arguments": {"user_id": str(promoted)}}

    submit_twin = client.post(f"{API}/authorization", json=body, headers=root)
    made = client.post(API, json=body, headers=root)
    request = f"{API}/{made.json()['id']}"
    reject_twin = client.post(
        f"{request}/reject/authorization", json={"reason": "No"}, headers=other
    )
    cancel_twin = client.post(f"{request}/cancel/authorization", json={}, headers=root)
    cancelled = client.post(f"{request}/cancel", json={}, headers=root)

    settings = ApprovalsSettings()
    assert (settings.SUBMIT_ACTION, settings.REJECT_ACTION) == (False, False)
    assert settings.CANCEL_ACTION is False
    assert action_for("approvals.submit") is None
    problem(submit_twin, 404, "not_found")
    assert made.status_code == 201, made.json()
    problem(reject_twin, 404, "not_found")
    problem(cancel_twin, 404, "not_found")
    assert cancelled.json()["status"] == "cancelled"


def test_a_maker_asks_confirms_and_submits_once(
    api: TestClient, frozen: FrozenClock
) -> None:
    """Asked, shown, confirmed, spent by the submission; again, or changed, refused."""
    maker = Person(api, frozen, "maker", treasurer=False)
    body = {"action": TRANSFER, "arguments": PAYMENT}
    changed = {"action": TRANSFER, "arguments": {**PAYMENT, "payee": "Mallory"}}

    bare = maker.post("", body)
    asked = maker.post("/authorization", body)
    authorization = maker.authorized("", body)
    made = maker.post("", body, authorization)
    again = maker.post("", body, authorization)
    other = maker.authorized("", body)
    voided = maker.post("", changed, other)

    problem(bare, 401, "authorization_required")
    assert 'purpose="approvals.submit"' in bare.headers["WWW-Authenticate"]
    assert asked.headers["Cache-Control"] == "no-store"
    assert shown_of(asked) == [
        ("", "Submit a request"),
        ("Action", TRANSFER),
        ("payee", "Ada Lovelace"),
        ("iban", "DE89 3704 0044 0532 0130 00"),
        ("amount", "2500.00"),
        ("currency", "EUR"),
        ("…", "2 more arguments"),
    ]
    assert made.status_code == 201, made.json()
    problem(again, 403, "action_not_authorized")
    problem(voided, 403, "action_not_authorized")
    stored = row(authorization)
    assert stored["subject"] == f"approvals.action:{TRANSFER}"
    assert stored["digest"] == fingerprint(
        "approvals.submit",
        {"action": TRANSFER, "arguments": PAYMENT, "maker": str(maker.id)},
    )
    assert len(audited("approvals.request_submitted")) == 1


def test_asking_to_submit_is_refused_as_submitting_would_be(
    api: TestClient, frozen: FrozenClock
) -> None:
    """An unknown action, arguments that make no command, a caller acting for none."""
    maker = Person(api, frozen, "maker", treasurer=False)

    unknown = maker.post("/authorization", {"action": "tests.nowhere", "arguments": {}})
    wrong = maker.post("/authorization", {"action": TRANSFER, "arguments": {"x": 1}})
    anonymous = api.post(
        f"{API}/authorization", json={"action": TRANSFER, "arguments": PAYMENT}
    )

    problem(unknown, 404, "unknown_action")
    problem(wrong, 422, "invalid_arguments")
    assert anonymous.status_code == 401, anonymous.json()


def test_an_approver_asks_confirms_and_rejects_once(
    api: TestClient, frozen: FrozenClock
) -> None:
    """The reason is shown and bound: another reason voids it; the maker is refused."""
    maker = Person(api, frozen, "maker", treasurer=True)
    ann = Person(api, frozen, "ann", treasurer=True)
    request = maker.submit()
    path = f"/{request['id']}/reject"
    reason = {"reason": "Wrong payee"}

    bare = ann.post(path, reason)
    asked = ann.post(f"{path}/authorization", reason)
    by_maker = maker.post(f"{path}/authorization", reason)
    first = ann.authorized(path, reason)
    voided = ann.post(path, {"reason": "Another"}, first)
    authorization = ann.authorized(path, reason)
    rejected = ann.post(path, reason, authorization)

    problem(bare, 401, "authorization_required")
    assert shown_of(asked) == [
        ("", "Reject a request"),
        ("Action", TRANSFER),
        ("Reason", "Wrong payee"),
        ("payee", "Ada Lovelace"),
        ("iban", "DE89 3704 0044 0532 0130 00"),
        ("amount", "2500.00"),
        ("…", "3 more arguments"),
    ]
    problem(by_maker, 403, "maker_cannot_decide")
    problem(voided, 403, "action_not_authorized")
    assert rejected.status_code == 200, rejected.json()
    assert (rejected.json()["status"], rejected.json()["reason"]) == (
        "rejected",
        "Wrong payee",
    )
    assert row(authorization)["subject"] == f"approvals.request:{request['id']}"


def test_the_maker_asks_confirms_and_cancels_once(
    api: TestClient, frozen: FrozenClock
) -> None:
    """Only the maker asks; the authorization withdraws that request, once."""
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann", treasurer=True)
    request = maker.submit()
    path = f"/{request['id']}/cancel"

    bare = maker.post(path, {})
    asked = maker.post(f"{path}/authorization", {})
    by_another = ann.post(f"{path}/authorization", {})
    authorization = maker.authorized(path, {})
    cancelled = maker.post(path, {}, authorization)
    again = maker.post(path, {}, authorization)
    late = maker.post(f"{path}/authorization", {})

    problem(bare, 401, "authorization_required")
    assert shown_of(asked)[:3] == [
        ("", "Cancel a request"),
        ("Action", TRANSFER),
        ("payee", "Ada Lovelace"),
    ]
    assert shown_of(asked)[-1] == ("…", "2 more arguments")
    problem(by_another, 403, "not_the_maker")
    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["status"] == "cancelled"
    problem(again, 403, "action_not_authorized")
    problem(late, 409, "request_not_pending")


def test_code_that_submits_rejects_or_cancels_needs_an_authorization(
    api: TestClient,
) -> None:
    """With the settings on, the services refuse a call no request authorized."""
    maker = str(create_user("maker"))
    request_id = UUID(int=7)

    calls = [
        submit_request(
            SubmitRequest(action=TRANSFER, arguments=PAYMENT, maker=maker),
            ApprovalsUnitOfWork(),
        ),
        reject_request(
            RejectRequest(request_id=request_id, approver=maker, reason="No"),
            ApprovalsUnitOfWork(),
        ),
        cancel_request(
            CancelRequest(request_id=request_id, maker=maker), ApprovalsUnitOfWork()
        ),
    ]

    for call in calls:
        with pytest.raises(AuthorizationRequired):
            run_async(call)


def treasurer(name: str) -> str:
    """The id of ``name``, a new user who is a treasurer: one of the approvers."""
    created = str(create_user(name))
    join(name, TREASURERS)
    return created


def submitted(maker: str) -> ApprovalRequest:
    """A transfer of ``PAYMENT``, submitted from code as ``maker``'s."""
    command = SubmitRequest(action=TRANSFER, arguments=PAYMENT, maker=maker)
    return run_async(record_submission(command, ApprovalsUnitOfWork()))


def approved_by(
    request: ApprovalRequest, approver: str, digest: str | None = None
) -> ApprovalRequest:
    """``approver`` approves ``request`` from code, stating ``digest`` or its own."""
    command = ApproveRequest(
        request_id=request.id,
        approver=approver,
        fingerprint=digest or request.fingerprint,
    )
    return run_async(record_approval(command, ApprovalsUnitOfWork()))


def rejected_by(
    request: ApprovalRequest, approver: str, reason: str = "No"
) -> ApprovalRequest:
    """``approver`` rejects ``request`` from code, for ``reason``."""
    command = RejectRequest(request_id=request.id, approver=approver, reason=reason)
    return run_async(record_rejection(command, ApprovalsUnitOfWork()))


def cancelled_by(
    request: ApprovalRequest, maker: str, reason: str = ""
) -> ApprovalRequest:
    """``maker`` withdraws ``request`` from code, for ``reason``."""
    command = CancelRequest(request_id=request.id, maker=maker, reason=reason)
    return run_async(record_cancellation(command, ApprovalsUnitOfWork()))


def stored(request: ApprovalRequest) -> ApprovalRequest:
    """``request`` as committed."""

    async def read() -> ApprovalRequest:
        async with ApprovalsUnitOfWork() as uow:
            return await uow.requests.get(request.id)

    return run_async(read())


def test_code_the_project_trusts_takes_every_step_without_an_authorization(
    every_step: TestClient,
) -> None:
    """The services refuse code; the record functions submit, decide, and withdraw.

    As the services do once an authorization is spent: the approval that
    completes a request queues its command, which the worker runs, and the
    trail records each step with the context's actor, ``system`` outside a
    request.
    """
    maker = str(create_user("maker"))
    ann, bob = treasurer("ann"), treasurer("bob")
    submission = SubmitRequest(action=TRANSFER, arguments=PAYMENT, maker=maker)

    with pytest.raises(AuthorizationRequired):
        run_async(submit_request(submission, ApprovalsUnitOfWork()))
    paid, refused, withdrawn = [submitted(maker) for _ in range(3)]
    for approver in (ann, bob):
        approval = ApproveRequest(
            request_id=paid.id, approver=approver, fingerprint=paid.fingerprint
        )
        with pytest.raises(AuthorizationRequired):
            run_async(approve_request(approval, ApprovalsUnitOfWork()))
        paid = approved_by(paid, approver)
    rejection = RejectRequest(request_id=refused.id, approver=ann, reason="Wrong")
    with pytest.raises(AuthorizationRequired):
        run_async(reject_request(rejection, ApprovalsUnitOfWork()))
    refused = rejected_by(refused, ann, "Wrong")
    cancellation = CancelRequest(request_id=withdrawn.id, maker=maker, reason="Stale")
    with pytest.raises(AuthorizationRequired):
        run_async(cancel_request(cancellation, ApprovalsUnitOfWork()))
    withdrawn = cancelled_by(withdrawn, maker, "Stale")
    run_async(drain())

    assert (paid.status, paid.approvals) == (RequestStatus.APPROVED, 2)
    assert stored(paid).status is RequestStatus.EXECUTED
    assert (refused.status, refused.reason) == (RequestStatus.REJECTED, "Wrong")
    assert (withdrawn.status, withdrawn.reason) == (RequestStatus.CANCELLED, "Stale")
    actors = {
        step: [each["actor"] for each in audited(f"approvals.{step}")]
        for step in (
            "request_submitted",
            "approval_given",
            "request_rejected",
            "request_cancelled",
        )
    }
    assert actors == {
        "request_submitted": ["system"] * 3,
        "approval_given": ["system"] * 2,
        "request_rejected": ["system"],
        "request_cancelled": ["system"],
    }


def test_code_is_refused_what_the_services_refuse_but_the_authorization(
    every_step: TestClient,
) -> None:
    """The maker deciding, a second decision, another fingerprint, another's withdrawal.

    And a reason too long, a request no longer waiting, an unknown action,
    arguments that make no command. What was refused changed nothing.
    """
    maker = str(create_user("maker"))
    ann, bob = treasurer("ann"), treasurer("bob")
    request = submitted(maker)

    with pytest.raises(MakerCannotDecide):
        approved_by(request, maker)
    with pytest.raises(MakerCannotDecide):
        rejected_by(request, maker)
    with pytest.raises(FingerprintMismatch):
        approved_by(request, ann, "0" * 64)
    approved_by(request, ann)
    with pytest.raises(AlreadyDecided):
        approved_by(request, ann)
    with pytest.raises(AlreadyDecided):
        rejected_by(request, ann)
    with pytest.raises(NotTheMaker):
        cancelled_by(request, ann)
    with pytest.raises(InvalidArguments):
        rejected_by(request, bob, "x" * 501)
    with pytest.raises(InvalidArguments):
        cancelled_by(request, maker, "x" * 501)
    assert (stored(request).status, stored(request).approvals) == (
        RequestStatus.PENDING,
        1,
    )
    cancelled_by(request, maker)
    with pytest.raises(RequestNotPending):
        approved_by(request, bob)
    with pytest.raises(RequestNotPending):
        rejected_by(request, bob)
    with pytest.raises(RequestNotPending):
        cancelled_by(request, maker)
    with pytest.raises(UnknownAction):
        run_async(
            record_submission(
                SubmitRequest(action="tests.nowhere", arguments={}, maker=maker),
                ApprovalsUnitOfWork(),
            )
        )
    with pytest.raises(InvalidArguments):
        run_async(
            record_submission(
                SubmitRequest(action=TRANSFER, arguments={"x": 1}, maker=maker),
                ApprovalsUnitOfWork(),
            )
        )
    assert stored(request).status is RequestStatus.CANCELLED
    assert len(audited("approvals.request_submitted")) == 1


def test_code_decides_no_request_changed_in_the_database_or_whose_time_is_up(
    every_step: TestClient, frozen: FrozenClock
) -> None:
    """A seal that does not hold refuses every step; nobody decides a late request."""
    maker = str(create_user("maker"))
    ann = treasurer("ann")
    changed, late = submitted(maker), submitted(maker)

    async def write() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests)
                .where(requests.c.id == changed.id)
                .values(arguments={**PAYMENT, "payee": "Mallory"})
            )

    run_async(write())
    for decide in (approved_by, rejected_by):
        with pytest.raises(RequestTampered):
            decide(changed, ann)
    with pytest.raises(RequestTampered):
        cancelled_by(changed, maker)
    frozen.tick(ApprovalsSettings().EXPIRES_AFTER)
    with pytest.raises(RequestNotPending):
        approved_by(late, ann)
    with pytest.raises(RequestNotPending):
        rejected_by(late, ann)

    assert stored(late).approvals == 0


def requests_router() -> ResourceRouter:
    """A router of the requests with approvals' own rules, as ``api.requests``."""
    return ResourceRouter(
        "/requests",
        model=ApprovalRequest,
        schema=RequestOut,
        present=RequestOut.of,
        unit_of_work=ApprovalsUnitOfWork,
        repository=lambda uow: uow.requests,
        lookup="request_id",
        rule=RequestActions(),
    )


def declare(command: type, service: Callable[..., Any], rule: DecisionAction) -> None:
    """Declare a route of ``command`` running ``service``, as approvals' routes are.

    A submission's route adds ``rule`` itself (``RequestActions`` asks it
    of no route); a decision's takes it from ``RequestActions``.
    """
    router = requests_router()
    if isinstance(rule, SubmitAction):
        router.command_route(
            SubmitRequest,
            service=service,
            body=SubmitIn,
            path="/submitted",
            action="add",
            rule=rule,
            provide={"maker": caller},
        )
        return
    step = {ApproveAction: "approve", RejectAction: "reject", CancelAction: "cancel"}
    action = step[type(rule)]
    body = {"approve": ApproveIn, "reject": RejectIn, "cancel": CancelIn}
    router.command_route(
        command,
        service=service,
        body=body[action],
        path=f"/{action}",
        action=action,
        provide={"maker" if action == "cancel" else "approver": caller},
    )


STEPS: Final = [
    (SubmitRequest, SubmitAction(), submit_request, record_submission),
    (ApproveRequest, ApproveAction(), approve_request, record_approval),
    (RejectRequest, RejectAction(), reject_request, record_rejection),
    (CancelRequest, CancelAction(), cancel_request, record_cancellation),
]
"""Each step's command, the rule that asks for its act, its service, and its
record function."""


@pytest.mark.parametrize(
    ("command", "rule", "service", "record"),
    STEPS,
    ids=["submit", "approve", "reject", "cancel"],
)
def test_a_route_of_approvals_rules_that_runs_a_record_function_is_refused(
    command: type,
    rule: DecisionAction,
    service: Callable[..., Any],
    record: Callable[..., Any],
) -> None:
    """Declared, it raises: its rule asks for an authorization nothing would spend.

    The rule takes any authorization of its act, and the service spends it
    on the command, which binds it; a record function spends none, so an
    authorization of one request would take the step on another. The
    step's own service declares, with no warning, as approvals' routes do.
    """
    with pytest.raises(
        ImproperlyConfigured,
        match=rf"asks for an authorization of its act, but runs {record.__name__}",
    ):
        declare(command, record, rule)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        declare(command, service, rule)


@pytest.mark.parametrize(
    ("rule", "own", "record"),
    [step[1:] for step in STEPS],
    ids=["submit", "approve", "reject", "cancel"],
)
def test_approvals_rules_say_which_services_never_spend_their_act(
    rule: DecisionAction, own: Callable[..., Any], record: Callable[..., Any]
) -> None:
    """A record function spends none, another step's service another's; others pass."""
    others = [step[2] for step in STEPS if step[2] is not own]

    async def of_the_project(command: object, uow: object) -> None:
        """A service of the project's own, which the rule cannot judge."""

    assert rule.service_problem(own) is None
    assert rule.service_problem(of_the_project) is None
    assert f"runs {record.__name__}, which asks for no authorization" in str(
        rule.service_problem(record)
    )
    for other in others:
        assert f"runs {other.__name__}, which spends the authorization" in str(
            rule.service_problem(other)
        )


def test_creating_staff_is_refused_only_while_making_staff_is_held(
    client: TestClient,
) -> None:
    """``refuse_staff_creation``, auth's ``on_create_staff`` check, asks the guards.

    Without ``ADMIN_APPROVALS`` holding ``auth.set_staff``, it lets the
    creation through; ``check_admin_change`` asks it of a creation too.
    """
    created = AdminChange("create", frozenset({"is_staff"}), values={"is_staff": True})

    refuse_staff_creation()  # Raises nothing.
    assert run_async(check_admin_change(None, None, created)) is None


def test_creating_staff_is_refused_while_making_staff_is_held(
    api: TestClient,
) -> None:
    """With ``auth.set_staff`` held, the check refuses, from either side."""
    created = AdminChange("create", frozenset({"is_staff"}), values={"is_staff": True})

    with pytest.raises(ApprovalRequired):
        refuse_staff_creation()
    with pytest.raises(ApprovalRequired):
        run_async(check_admin_change(None, None, created))


@pytest.mark.parametrize("setting", ["SUBMIT_ACTION", "REJECT_ACTION", "CANCEL_ACTION"])
def test_each_action_needs_auth(setting: str) -> None:
    """Any of them without ``domainkit.contrib.auth`` fails ``setup()``, named."""
    registry.clear()
    try:
        with (
            override_settings(
                INSTALLED_DOMAINS=[MESSAGING, AUDIT, APPROVALS, ALPHA],
                APPROVALS={setting: True},
            ),
            pytest.raises(ImproperlyConfigured, match=setting),
        ):
            setup()
    finally:
        registry.clear()
