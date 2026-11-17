"""An approver authorizes each approval, once (``APPROVE_ACTION``, user report A6).

With ``APPROVALS.APPROVE_ACTION`` on, approving a request asks for an
authorization of approvals' own action, ``approvals.approve``: the request's
id and the fingerprint the approver states, on ``approvals.request:<id>``,
showing the request's action and arguments as the server stores them
(ADR 0144). The approver asks for it at ``POST
/requests/{id}/approve/authorization``, confirms it with a code, and
approves with it once; the approval's commit spends it. Off (the default),
approving is as it was, and the twin answers ``404``.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from domainkit import setup
from domainkit.apps import registry
from domainkit.clock import FrozenClock, freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.approvals import (
    ApprovalsSettings,
    ApprovalsUnitOfWork,
    ApproveRequest,
    Policy,
    approve_request,
    guard,
    requires_approval,
)
from domainkit.contrib.approvals.authorizing import MAX_TEXT, details_of, fitted
from domainkit.contrib.approvals.tables import requests
from domainkit.contrib.auth import StepUp
from domainkit.contrib.auth.actions import AuthorizationRequired, action_for
from domainkit.contrib.auth.screens import TooMuchToShow
from domainkit.contrib.auth.tables import authorizations
from domainkit.db import connections
from domainkit.ddd import fingerprint
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.access import IsAuthenticated
from domainkit.management.base import run_async
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    MIDDLEWARE,
    Migrated,
    Shape,
)
from domainkit_tests.sample_approval_action_routes import (
    TRANSFER,
    Transfer,
    transfer,
)
from domainkit_tests.support.auth import audited, create_user, grant
from domainkit_tests.test_approvals import FAST
from domainkit_tests.test_approvals_api import PROOF
from domainkit_tests.test_approvals_api import client as client  # The fixture.
from domainkit_tests.test_auth_admin import join, make_group, token_for
from domainkit_tests.test_auth_stepup import enrol, now_code

ROUTES: Final = "domainkit_tests.sample_approval_action_routes"
MESSAGING: Final = "domainkit.contrib.messaging"
AUDIT: Final = "domainkit.contrib.audit"
APPROVALS: Final = "domainkit.contrib.approvals"
APPROVE_ACTION: Final = Shape(
    "approvals_approve_action",
    "approvals",
    {
        "INSTALLED_DOMAINS": [CONTENTTYPES, AUTH, MESSAGING, AUDIT, APPROVALS, ALPHA],
        "ROOT_ROUTER": f"{ROUTES}.router",
        "MIDDLEWARE": ["domainkit.http.locale.LocaleMiddleware", *MIDDLEWARE[:-1]],
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
        "ERROR_FORMAT": "problem+json",
        "MESSAGING": FAST,
        "APPROVALS": {"APPROVE_ACTION": True},
    },
)
API: Final = "/approvals/requests"
START: Final = "2027-01-04T10:00:00Z"
TREASURERS: Final = "treasurers"
QUICK: Final = "quick"
"""The reference of a transfer whose request waits one minute only."""
PAYMENT: Final = {
    "payee": "Ada Lovelace",
    "iban": "DE89 3704 0044 0532 0130 00",
    "amount": "2500.00",
    "currency": "EUR",
    "memo": "Invoice 7",
    "reference": "inv-7",
}
SHOWN: Final = {
    "title": "Approve a request",
    "details": [
        {"label": "Action", "value": TRANSFER},
        {"label": "payee", "value": "Ada Lovelace"},
        {"label": "iban", "value": "DE89 3704 0044 0532 0130 00"},
        {"label": "amount", "value": "2500.00"},
        {"label": "currency", "value": "EUR"},
        {"label": "…", "value": "2 more arguments"},
    ],
}
"""What a treasurer is shown of ``PAYMENT``: six values, the last two counted."""


@pytest.fixture
def frozen() -> Iterator[FrozenClock]:
    """One clock for codes, requests, and authorizations."""
    with freeze_time(START) as clock:
        yield clock


@pytest.fixture
def api(migrated: Migrated) -> Iterator[TestClient]:
    """Approvals with ``APPROVE_ACTION`` on; transfers wait for two treasurers."""
    with migrated(APPROVE_ACTION), TestClient(get_asgi_application()) as test_client:
        guard(
            TRANSFER,
            transfer,
            unit_of_work=ApprovalsUnitOfWork,
            policies=[
                Policy(
                    "tests.quick_transfers",
                    group=TREASURERS,
                    approvals=2,
                    expires_after=timedelta(minutes=1),
                    when=lambda command: command.reference == QUICK,
                ),
                Policy("tests.transfers", group=TREASURERS, approvals=2),
            ],
            domain="alpha",
        )
        make_group(TREASURERS)
        yield test_client


class Person:
    """Someone logged in, with an authenticator app, a treasurer or not."""

    def __init__(
        self,
        http: TestClient,
        clock: FrozenClock,
        name: str,
        *,
        treasurer: bool = True,
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

    def submit(self, **arguments: str) -> dict[str, Any]:
        """Submit a transfer of ``PAYMENT`` (changed by ``arguments``); the request."""
        made = self.client.post(
            API,
            json={"action": TRANSFER, "arguments": {**PAYMENT, **arguments}},
            headers=self.headers,
        )
        assert made.status_code == 201, made.json()
        request: dict[str, Any] = made.json()
        return request

    def approve(
        self,
        request: dict[str, Any],
        authorization: str | None = None,
        fingerprint: str | None = None,
        **headers: str,
    ) -> Any:  # noqa: ANN401 - a response.
        """Approve ``request``, sending ``authorization`` if given."""
        sent = {**self.headers, **headers}
        if authorization is not None:
            sent["Action-Authorization"] = authorization
        return self.client.post(
            f"{API}/{request['id']}/approve",
            json={"fingerprint": fingerprint or request["fingerprint"]},
            headers=sent,
        )

    def ask(
        self, request: dict[str, Any], fingerprint: str | None = None, **headers: str
    ) -> Any:  # noqa: ANN401 - a response.
        """Ask for an authorization of approving ``request``."""
        return self.client.post(
            f"{API}/{request['id']}/approve/authorization",
            json={"fingerprint": fingerprint or request["fingerprint"]},
            headers={**self.headers, **headers},
        )

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

    def authorized(self, request: dict[str, Any], **headers: str) -> str:
        """A confirmed authorization of approving ``request``."""
        asked = self.ask(request, **headers)
        assert asked.status_code == 201, asked.json()
        return self.confirm(asked.json()["id"])

    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        """``request`` as it is now."""
        found: dict[str, Any] = self.client.get(
            f"{API}/{request['id']}", headers=self.headers
        ).json()
        return found


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


def test_off_by_default_approving_is_unchanged_and_the_twin_is_not_there(
    client: TestClient,
) -> None:
    """Off, an approval asks for no authorization, and the twin answers 404."""
    create_user("root", is_superuser=True)
    create_user("other", is_superuser=True)
    promoted = create_user("promoted")
    made = client.post(
        API,
        json={
            "action": "auth.grant_superuser",
            "arguments": {"user_id": str(promoted)},
        },
        headers=token_for("root"),
    )
    assert made.status_code == 201, made.json()
    request = made.json()
    approver = {**PROOF, **token_for("other")}

    twin = client.post(
        f"{API}/{request['id']}/approve/authorization",
        json={"fingerprint": request["fingerprint"]},
        headers=approver,
    )
    approved = client.post(
        f"{API}/{request['id']}/approve",
        json={"fingerprint": request["fingerprint"]},
        headers=approver,
    )

    assert ApprovalsSettings().APPROVE_ACTION is False
    assert action_for("approvals.approve") is None
    problem(twin, 404, "not_found")
    assert approved.status_code == 200, approved.json()
    assert approved.json()["status"] == "approved"


def test_an_approval_without_an_authorization_asks_for_one(
    api: TestClient, frozen: FrozenClock
) -> None:
    """401 ``authorization_required``, naming ``approvals.approve``; in code, too."""
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann")
    request = maker.submit()

    refused = ann.approve(request)

    problem(refused, 401, "authorization_required")
    assert 'purpose="approvals.approve"' in refused.headers["WWW-Authenticate"]
    with pytest.raises(AuthorizationRequired):
        run_async(
            approve_request(
                ApproveRequest(
                    request_id=UUID(request["id"]),
                    approver=str(ann.id),
                    fingerprint=request["fingerprint"],
                ),
                ApprovalsUnitOfWork(),
            )
        )
    assert ann.read(request)["approvals"] == 0


def test_a_treasurer_asks_confirms_with_a_code_and_approves_once(
    api: TestClient, frozen: FrozenClock
) -> None:
    """Asked, shown, confirmed, spent by the approval; sent again, refused.

    The authorization is of this request (its subject) and of this
    decision (its digest: the request, the approver, and the fingerprint).
    Each approval needs one of its own: the second treasurer's completes
    the request.
    """
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann")
    bob = Person(api, frozen, "bob")
    request = maker.submit()

    asked = ann.ask(request)
    authorization = ann.confirm(asked.json()["id"])
    first = ann.approve(request, authorization)
    again = ann.approve(request, authorization)
    second = bob.approve(request, bob.authorized(request))

    assert asked.status_code == 201, asked.json()
    assert asked.json()["purpose"] == "approvals.approve"
    assert authorization.startswith(f"{asked.json()['id']}.")
    assert first.status_code == 200, first.json()
    assert (first.json()["approvals"], first.json()["status"]) == (1, "pending")
    problem(again, 403, "action_not_authorized")
    assert second.status_code == 200, second.json()
    assert second.json()["status"] == "approved"
    stored = row(authorization)
    assert stored["subject"] == f"approvals.request:{request['id']}"
    assert stored["digest"] == fingerprint(
        "approvals.approve",
        {
            "request_id": request["id"],
            "approver": str(ann.id),
            "fingerprint": request["fingerprint"],
        },
    )


def test_the_approval_spends_the_authorization_with_its_commit(
    api: TestClient, frozen: FrozenClock
) -> None:
    """Spent with the approval that commits; an approval refused spends nothing.

    The requests share auth's database, so the spend joins the approval's
    transaction: ``ActionAuthorizationUsed`` and ``ApprovalGiven`` commit
    together. A request that expired between asking and approving is
    refused, and the authorization stays unspent.
    """
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann")
    request = maker.submit()
    quick = maker.submit(reference=QUICK)

    spent = ann.authorized(request)
    approved = ann.approve(request, spent)
    unspent = ann.authorized(quick)
    frozen.tick(61)
    late = ann.approve(quick, unspent)

    assert approved.status_code == 200, approved.json()
    assert row(spent)["consumed_at"] is not None
    problem(late, 409, "request_not_pending")
    assert row(unspent)["consumed_at"] is None
    assert len(audited("auth.action_authorization_used")) == 1
    assert len(audited("approvals.approval_given")) == 1


def test_another_fingerprint_is_refused_when_asking_and_when_approving(
    api: TestClient, frozen: FrozenClock
) -> None:
    """A fingerprint not the request's, a decision of another, a request changed.

    Asking for another fingerprint is 409 ``fingerprint_mismatch``; an
    authorization of the request's, sent with another, is voided (403); a
    request whose arguments changed around the application is not approved
    (409 ``request_tampered``), and spends nothing.
    """
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann")
    request = maker.submit()
    changed = maker.submit(reference="inv-8")

    mismatched = ann.ask(request, fingerprint="0" * 64)
    authorization = ann.authorized(request)
    other = ann.approve(request, authorization, fingerprint="1" * 64)
    voided = ann.approve(request, authorization)
    held = ann.authorized(changed)
    _change(changed["id"])
    tampered = ann.approve(changed, held)

    problem(mismatched, 409, "fingerprint_mismatch")
    problem(other, 403, "action_not_authorized")
    problem(voided, 403, "action_not_authorized")
    assert ann.read(request)["approvals"] == 0
    problem(tampered, 409, "request_tampered")
    assert row(held)["consumed_at"] is None


def _change(request_id: str) -> None:
    """Change a request's command in the database, as someone without the key would."""

    async def write() -> None:
        async with connections.engine().begin() as connection:
            await connection.execute(
                update(requests)
                .where(requests.c.id == UUID(request_id))
                .values(
                    arguments={**PAYMENT, "payee": "Mallory"},
                    fingerprint=fingerprint(TRANSFER, {**PAYMENT, "payee": "Mallory"}),
                )
            )

    run_async(write())


def test_the_maker_still_never_approves(api: TestClient, frozen: FrozenClock) -> None:
    """A maker who is a treasurer cannot ask, so never approves; nor can others.

    Someone who is not an approver is refused the twin as the approval; an
    authorization of another treasurer's does not hold for the maker.
    """
    maker = Person(api, frozen, "maker")
    ann = Person(api, frozen, "ann")
    outsider = Person(api, frozen, "outsider", treasurer=False)
    request = maker.submit()

    asked = maker.ask(request)
    bare = maker.approve(request)
    borrowed = maker.approve(request, ann.authorized(request))
    stranger = outsider.ask(request)

    problem(asked, 403, "maker_cannot_decide")
    problem(bare, 401, "authorization_required")
    problem(borrowed, 403, "action_not_authorized")
    problem(stranger, 403, "permission_denied")
    assert ann.read(request)["approvals"] == 0


def test_the_words_shown_are_the_servers(api: TestClient, frozen: FrozenClock) -> None:
    """Built from the stored request, kept, and the same wherever they are read.

    The action and the arguments, in the command's order, each made
    printable and cut to 64 characters, six values at most. A client sends
    no words of its own: the body takes the fingerprint only.
    """
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann")
    memo = "Invoice 7\nand a description much longer than any phone shows whole"
    request = maker.submit(memo=memo, payee="Ada Lovelace")

    asked = ann.ask(request)
    read = api.get(f"/auth/authorizations/{asked.json()['id']}", headers=ann.headers)
    worded = api.post(
        f"{API}/{request['id']}/approve/authorization",
        json={"fingerprint": request["fingerprint"], "title": "Approve a refund"},
        headers=ann.headers,
    )

    assert asked.json()["shown"] == SHOWN
    assert read.json()["shown"] == SHOWN
    assert worded.status_code == 422


def test_details_are_printable_cut_to_fit_and_six_at_most() -> None:
    """Control characters escaped, long values cut with ``…``, extra ones counted."""
    long = "x" * 100

    assert fitted("a\nb\u2028c") == "a\\nb\\u2028c"
    assert fitted(long) == "x" * (MAX_TEXT - 1) + "…"
    assert details_of("tests.act", {"kind": "a", "n": 2, "flags": [1]}) == [
        ("Action", "tests.act"),
        ("kind", "a"),
        ("n", "2"),
        ("flags", "1"),
    ]
    assert details_of("tests.act", {"Action": "x", "kind": {"b": 1, "a": None}}) == [
        ("Action", "tests.act"),
        ("kind", '{"a":null,"b":1}'),
        ("…", "1 more argument"),
    ]
    many = details_of("a" * 70, {f"k{each}": long for each in range(7)})
    assert len(many) == 6
    assert many[0] == ("Action", "a" * (MAX_TEXT - 1) + "…")
    assert many[-1] == ("…", "3 more arguments")
    assert all(len(value) <= MAX_TEXT for _, value in many)


def test_a_list_argument_goes_over_lines_or_says_how_many_more() -> None:
    """A grant's keys, each shown, or counted; never cut unsaid (user report A23).

    A list of strings goes over as many lines as it takes ("added (2)");
    past the screen, its last line says how many more. With ``whole``, it
    is refused instead. A list of objects stays compact JSON.
    """
    keys = [f"accounts.permission_{each:02}" for each in range(30)]

    fits = details_of("auth.set_permissions", {"user_id": "u", "added": keys[:4]})
    counted = details_of("auth.set_permissions", {"user_id": "u", "added": keys})
    with pytest.raises(TooMuchToShow):
        details_of("auth.set_permissions", {"added": keys}, whole=True)
    lines = details_of("tests.act", {"lines": [{"a": 1}]}, whole=True)

    assert fits == [
        ("Action", "auth.set_permissions"),
        ("user_id", "u"),
        ("added", "accounts.permission_00, accounts.permission_01"),
        ("added (2)", "accounts.permission_02, accounts.permission_03"),
    ]
    assert [label for label, _ in counted] == [
        "Action",
        "user_id",
        "added",
        "added (2)",
        "added (3)",
        "added (4)",
    ]
    assert (
        counted[-1][1] == "accounts.permission_06, accounts.permission_07 and 22 more"
    )
    assert lines == [("Action", "tests.act"), ("lines", '[{"a":1}]')]


def test_other_approve_rules_still_apply_beside_the_action(
    api: TestClient, frozen: FrozenClock
) -> None:
    """``APPROVE_RULE`` still asks, on the twin as on the approval: 403 without it."""
    maker = Person(api, frozen, "maker", treasurer=False)
    ann = Person(api, frozen, "ann")
    request = maker.submit()
    present = {"X-Present": "yes"}

    with override_settings(
        APPROVALS={"APPROVE_ACTION": True, "APPROVE_RULE": f"{ROUTES}.Present"}
    ):
        absent = ann.ask(request)
        authorization = ann.authorized(request, **present)
        refused = ann.approve(request, authorization)
        approved = ann.approve(request, authorization, **present)

    problem(absent, 403, "permission_denied")
    problem(refused, 403, "permission_denied")
    assert refused.json()["detail"] == "You cannot approve this request now."
    assert approved.status_code == 200, approved.json()


def test_check_refuses_a_step_up_window_beside_the_action(api: TestClient) -> None:
    """``approvals.E001``: a ``StepUp`` in ``APPROVE_RULE`` or a policy's rule.

    The action takes the place of the step-up window; other rules may stay,
    and with the setting off, a window is what approving asks.
    """
    config = registry.get_domain_config("approvals")
    served = FastAPI()

    def problems() -> list[str]:
        found = config.application_problems(served)  # type: ignore[attr-defined]
        return [problem.id for problem in found]

    window = {"APPROVE_RULE": f"{ROUTES}.WINDOW"}
    assert problems() == []
    with override_settings(APPROVALS={"APPROVE_ACTION": True, **window}):
        assert problems() == ["approvals.E001"]
    with override_settings(APPROVALS={"APPROVE_ACTION": False, **window}):
        assert problems() == []
    with override_settings(
        APPROVALS={"APPROVE_ACTION": True, "APPROVE_RULE": f"{ROUTES}.Present"}
    ):
        assert problems() == []
    guard(
        "tests.windowed",
        windowed,
        unit_of_work=ApprovalsUnitOfWork,
        policies=[
            Policy(
                "tests.windowed",
                group=TREASURERS,
                approve_rule=IsAuthenticated() & StepUp(max_age=timedelta(minutes=1)),
            )
        ],
        domain="alpha",
    )
    found = config.application_problems(served)  # type: ignore[attr-defined]
    assert [problem.id for problem in found] == ["approvals.E001"]
    assert "'tests.windowed'" in found[0]


@requires_approval("tests.windowed")
async def windowed(command: Transfer, uow: ApprovalsUnitOfWork) -> None:
    """A guarded use case whose policy asks a step-up window."""


def test_the_action_needs_auth() -> None:
    """``APPROVE_ACTION`` without ``domainkit.contrib.auth`` fails ``setup()``."""
    registry.clear()
    try:
        with (
            override_settings(
                INSTALLED_DOMAINS=[MESSAGING, AUDIT, APPROVALS, ALPHA],
                APPROVALS={"APPROVE_ACTION": True},
            ),
            pytest.raises(ImproperlyConfigured, match="APPROVE_ACTION"),
        ):
            setup()
    finally:
        registry.clear()
