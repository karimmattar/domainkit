"""Approvals over HTTP: who may submit, approve, reject, and cancel; and step-up.

The project guards postings over 1000 dollars (two approvers holding
``accounting.add_entry``) and making superusers (one approver holding
``auth.change_user``), and asks every approver for fresh proof, as a
step-up rule would (``APPROVE_RULE``: the sample ``Proof``).
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from domainkit.clock import freeze_time
from domainkit.contrib.accounting import AccountingUnitOfWork
from domainkit.contrib.approvals import Policy, guard
from domainkit.contrib.approvals.rules import REFUSED_BY, MayApprove
from domainkit.contrib.auth import AuthUnitOfWork, StepUp
from domainkit.contrib.messaging.testing import drain
from domainkit.http import get_asgi_application
from domainkit.http.access import AnonymousPrincipal, BasePermission, open_routes
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
from domainkit_tests.sample_approval_routes import Never, OnlyPending
from domainkit_tests.test_accounting_api import open_books, sale
from domainkit_tests.test_approvals import FAST, acting_guarded
from domainkit_tests.test_auth_admin import join, make_group, token_for
from domainkit_tests.test_auth_http import bearer, create_user, grant
from domainkit_tests.test_auth_stepup import enrol, now_code

ROUTES: Final = "domainkit_tests.sample_approval_routes"
APPROVALS_API: Final = Shape(
    "approvals_api",
    "approvals",
    {
        "INSTALLED_DOMAINS": [
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.messaging",
            "domainkit.contrib.audit",
            "domainkit.contrib.accounting",
            "domainkit.contrib.approvals",
            ALPHA,
        ],
        "ROOT_ROUTER": f"{ROUTES}.router",
        "MIDDLEWARE": ["domainkit.http.locale.LocaleMiddleware", *MIDDLEWARE[:-1]],
        "AUTH": {"PASSWORD_HASHERS": [FAST_HASHER]},
        "ERROR_FORMAT": "problem+json",
        "MESSAGING": FAST,
        "APPROVALS": {
            "ENTRY_THRESHOLDS": {"USD": "1000.00"},
            "SUPERUSER_APPROVALS": 1,
            "APPROVE_RULE": f"{ROUTES}.Proof",
        },
    },
)
API: Final = "/approvals/requests"
PROOF: Final = {"X-Proof": "fresh"}
CLERK: Final = (
    "accounting.view_account",
    "accounting.add_account",
    "accounting.view_entry",
    "accounting.add_entry",
    "accounting.view_period",
    "accounting.add_period",
    "approvals.view_approvalrequest",
    "approvals.add_approvalrequest",
)
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The project's API, migrated, behind token authentication."""
    with migrated(APPROVALS_API), TestClient(get_asgi_application()) as test_client:
        yield test_client


def user(name: str, *permissions: str) -> Headers:
    """A user granted ``permissions``; headers that authenticate them."""
    create_user(name)
    grant(name, *permissions)
    return token_for(name)


def problem(response: Any, status: int, code: str) -> None:  # noqa: ANN401 - a response.
    """``response`` is the problem ``code`` with ``status``."""
    assert response.status_code == status, response.json()
    assert response.json()["code"] == code, response.json()


def submit(client: TestClient, headers: Headers, action: str, arguments: Any) -> Any:  # noqa: ANN401 - JSON.
    """Submit ``action`` with ``arguments``; the response."""
    return client.post(
        API, json={"action": action, "arguments": arguments}, headers=headers
    )


def decide(
    client: TestClient,
    request: dict[str, Any],
    headers: Headers,
    verdict: str = "approve",
    **body: Any,  # noqa: ANN401 - JSON.
) -> Any:  # noqa: ANN401 - a response.
    """Approve (with its fingerprint and fresh proof) or reject ``request``."""
    if verdict == "approve":
        body.setdefault("fingerprint", request["fingerprint"])
        headers = {**PROOF, **headers}
    return client.post(f"{API}/{request['id']}/{verdict}", json=body, headers=headers)


def test_every_route_needs_a_rule(client: TestClient) -> None:
    """No route of approvals is open."""
    assert [r for r in open_routes(client.app) if f" {API}" in r] == []  # type: ignore[arg-type]


def test_a_large_entry_waits_for_two_people_then_posts_once(client: TestClient) -> None:
    """Refused when posted; submitted; approved by two others, once each; posted.

    The same approver through another token is the same person: refused.
    The maker, though they may approve postings, never approves their own.
    """
    clerk = user("clerk", *CLERK)
    ann = user("ann", *CLERK)
    ann_again = token_for("ann")
    bob = user("bob", *CLERK)
    books = open_books(client, clerk)
    large = sale(books, "big", "5000.00")
    problem(
        client.post("/accounting/entries", json=large, headers=clerk),
        403,
        "approval_required",
    )

    made = submit(client, clerk, "accounting.post_entry", large)
    assert made.status_code == 201, made.json()
    request = made.json()
    assert made.headers["Location"].endswith(f"{API}/{request['id']}")
    assert (request["status"], request["required"], request["approvals"]) == (
        "pending",
        2,
        0,
    )
    listed = client.get(API, params={"status": "pending"}, headers=ann).json()
    assert [each["id"] for each in listed["items"]] == [request["id"]]

    problem(decide(client, request, clerk), 403, "maker_cannot_decide")
    first = decide(client, request, ann)
    assert first.status_code == 200, first.json()
    assert first.json()["approvals"] == 1
    problem(decide(client, request, ann_again), 409, "already_decided")
    problem(
        decide(client, request, bob, fingerprint="0" * 64), 409, "fingerprint_mismatch"
    )
    second = decide(client, request, bob)
    assert second.json()["status"] == "approved"
    assert [each["approver"] for each in second.json()["decisions"]] == [
        str(each) for each in (_id("ann"), _id("bob"))
    ]

    run_async(drain())
    done = client.get(f"{API}/{request['id']}", headers=ann).json()
    assert done["status"] == "executed"
    assert done["decided_at"] is not None
    assert done["executed_at"] is not None
    posted = client.get(
        "/accounting/entries", params={"reference": "big"}, headers=clerk
    )
    assert len(posted.json()["items"]) == 1


def _id(username: str) -> UUID:
    """The id of the user ``username``."""

    async def find() -> UUID:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(username)
            assert found is not None
            return found.id

    return run_async(find())


def test_approvers_are_the_policys_and_prove_themselves_again(
    client: TestClient,
) -> None:
    """Without the policy's permission, or without fresh proof, no approval."""
    clerk = user("clerk", *CLERK)
    viewer = user("viewer", "approvals.view_approvalrequest")
    ann = user("ann", *CLERK)
    books = open_books(client, clerk)
    request = submit(
        client, clerk, "accounting.post_entry", sale(books, "b", "5000.00")
    ).json()

    problem(decide(client, request, viewer), 403, "permission_denied")
    assert decide(client, request, viewer).json()["detail"] == (
        "You are not one of the approvers of this request."
    )
    stale = client.post(
        f"{API}/{request['id']}/approve",
        json={"fingerprint": request["fingerprint"]},
        headers=ann,
    )
    problem(stale, 403, "permission_denied")
    assert stale.json()["detail"] == "You cannot approve this request now."
    problem(
        decide(client, request, viewer, "reject", reason="No."),
        403,
        "permission_denied",
    )
    anonymous = client.post(
        f"{API}/{request['id']}/approve", json={"fingerprint": request["fingerprint"]}
    )
    assert anonymous.status_code == 401


def test_rejections_and_cancellations(client: TestClient) -> None:
    """One rejection ends it, with its reason; only the maker cancels."""
    clerk = user("clerk", *CLERK)
    ann = user("ann", *CLERK)
    books = open_books(client, clerk)
    first = submit(
        client, clerk, "accounting.post_entry", sale(books, "r", "5000.00")
    ).json()
    second = submit(
        client, clerk, "accounting.post_entry", sale(books, "c", "5000.00")
    ).json()

    rejected = decide(client, first, ann, "reject", reason="Not ours.")
    assert (rejected.json()["status"], rejected.json()["reason"]) == (
        "rejected",
        "Not ours.",
    )
    problem(decide(client, first, user("bob", *CLERK)), 409, "request_not_pending")
    assert decide(client, second, ann, "reject").status_code == 422
    problem(
        client.post(f"{API}/{second['id']}/cancel", json={}, headers=ann),
        403,
        "not_the_maker",
    )
    cancelled = client.post(
        f"{API}/{second['id']}/cancel", json={"reason": "Typo."}, headers=clerk
    )
    assert cancelled.json()["status"] == "cancelled"


def test_submissions_need_the_makers_permission_and_a_command(
    client: TestClient,
) -> None:
    """The guard's permission, a known action, arguments that make the command."""
    clerk = user("clerk", *CLERK)
    outsider = user("outsider", "approvals.add_approvalrequest")
    books = open_books(client, clerk)
    large = sale(books, "s", "5000.00")

    problem(
        submit(client, outsider, "accounting.post_entry", large),
        403,
        "permission_denied",
    )
    problem(submit(client, clerk, "tests.nothing", {}), 404, "unknown_action")
    problem(
        submit(client, clerk, "accounting.post_entry", {"source": 1}),
        422,
        "invalid_arguments",
    )
    problem(
        submit(client, clerk, "accounting.post_entry", sale(books, "s", "10.00")),
        422,
        "approval_not_needed",
    )
    assert (
        submit(client, user("nobody"), "accounting.post_entry", large).status_code
        == 403
    )


def test_a_policy_of_a_group_and_its_own_approve_rule(client: TestClient) -> None:
    """Members approve, others do not, nor scoped credentials; a policy's rule wins."""
    guard(
        "tests.act",
        acting_guarded,
        unit_of_work=AccountingUnitOfWork,
        policies=[
            Policy(
                "tests.never",
                group="treasurers",
                approve_rule=Never,
                when=lambda c: c.kind == "never",
            ),
            Policy("tests.pending", group="treasurers", approve_rule=OnlyPending()),
        ],
        domain="alpha",
    )
    maker = user(
        "maker", "approvals.add_approvalrequest", "approvals.view_approvalrequest"
    )
    make_group("treasurers")
    member = user("member", "approvals.view_approvalrequest")
    join("member", "treasurers")
    outsider = user(
        "outsider", "approvals.view_approvalrequest", "accounting.add_account"
    )
    scoped = _scoped("member", frozenset({"approvals.view_approvalrequest"}))

    request = submit(client, maker, "tests.act", {"kind": "vault"}).json()
    never = submit(client, maker, "tests.act", {"kind": "never"}).json()
    assert request["policy"] == "tests.pending"
    problem(decide(client, request, outsider), 403, "permission_denied")
    problem(decide(client, request, scoped), 403, "permission_denied")
    problem(decide(client, never, member), 403, "permission_denied")
    assert decide(client, request, member).json()["status"] == "approved"
    run_async(drain())
    assert (
        client.get(f"{API}/{request['id']}", headers=member).json()["status"]
        == "executed"
    )


def _scoped(username: str, scopes: frozenset[str]) -> Headers:
    """Headers with a token of ``username``'s limited to ``scopes``."""

    async def issue() -> str:
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(username)
            assert found is not None
            raw = await uow.tokens.issue(
                found.id, ttl=None, name="scoped", scopes=scopes
            )
            await uow.commit()
        return raw

    return bearer(run_async(issue()))


def test_making_a_superuser_takes_an_approved_request(client: TestClient) -> None:
    """The admin API refuses it; a superuser's approved request does it."""
    root = user("root")
    run_async(_superuser("root"))
    other = user("other")
    run_async(_superuser("other"))
    target = create_user("target")

    problem(
        client.patch(
            f"/admin/auth/users/{target}", json={"is_superuser": True}, headers=root
        ),
        403,
        "approval_required",
    )
    problem(
        client.post(
            "/admin/auth/users",
            json={"username": "new", "is_superuser": True},
            headers=root,
        ),
        403,
        "approval_required",
    )
    request = submit(
        client, root, "auth.grant_superuser", {"user_id": str(target)}
    ).json()
    assert decide(client, request, other).json()["status"] == "approved"
    run_async(drain())

    async def is_superuser() -> bool:
        async with AuthUnitOfWork() as uow:
            return (await uow.users.get(target)).is_superuser  # type: ignore[arg-type]

    assert run_async(is_superuser())
    unmade = client.patch(
        f"/admin/auth/users/{target}", json={"is_superuser": False}, headers=root
    )
    assert unmade.status_code == 200, unmade.json()


async def _superuser(username: str) -> None:
    """Make ``username`` a superuser, as a test may."""
    async with AuthUnitOfWork() as uow:
        found = await uow.users.get_by_natural_key(username)
        assert found is not None
        found.set_superuser(True)
        found.set_staff(True)  # type: ignore[attr-defined]
        await uow.commit()


def test_a_policy_asks_its_approvers_to_step_up(client: TestClient) -> None:
    """``approve_rule=StepUp(...)``: ``401 step_up_required`` until they step up."""
    with freeze_time("2027-01-04T10:00:00Z") as clock:
        guard(
            "tests.act",
            acting_guarded,
            unit_of_work=AccountingUnitOfWork,
            policies=[
                Policy(
                    "tests.stepped",
                    group="treasurers",
                    approve_rule=StepUp(max_age=timedelta(minutes=5)),
                )
            ],
            domain="alpha",
        )
        maker = user(
            "maker", "approvals.add_approvalrequest", "approvals.view_approvalrequest"
        )
        make_group("treasurers")
        member = user("member", "approvals.view_approvalrequest")
        join("member", "treasurers")
        secret = enrol(client, member, clock)
        request = submit(client, maker, "tests.act", {"kind": "vault"}).json()

        refused = decide(client, request, member)
        stepped = client.post(
            "/auth/step-up", json={"code": now_code(secret, clock)}, headers=member
        )
        approved = decide(client, request, member)

    problem(refused, 401, "step_up_required")
    assert refused.headers["WWW-Authenticate"].endswith('max_age="300"')
    assert stepped.status_code == 200, stepped.json()
    assert approved.json()["status"] == "approved"


@pytest.mark.anyio
async def test_an_approval_refused_by_no_rule_names_no_refusal() -> None:
    """Asked before any rule refused, ``MayApprove`` leaves the 403 as it is."""
    request = HTTPConnection({"type": "http", "headers": []})

    assert await MayApprove().refusal(request, AnonymousPrincipal()) is None


@pytest.mark.anyio
async def test_an_approval_refused_by_a_rule_passes_on_its_refusal() -> None:
    """The rule that refused is asked, with the request and the principal."""

    class Named(BasePermission):
        async def refusal(self, request: HTTPConnection, user: Any) -> Exception:  # noqa: ANN401
            return LookupError(request, user)

    request = HTTPConnection({"type": "http", "headers": [], REFUSED_BY: Named()})
    principal = AnonymousPrincipal()

    refusal = await MayApprove().refusal(request, principal)

    assert isinstance(refusal, LookupError)
    assert refusal.args == (request, principal)
