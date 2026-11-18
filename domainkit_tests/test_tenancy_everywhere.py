"""Rows of no tenant count the keys held everywhere, never a member's role (ADR 0183).

Limits and the ledger belong to no tenant. A member's role in the tenant
served (``MembershipBackend``) reaches none of their routes, nor the
guarded postings of approvals; what a user holds everywhere still does.
``HasPermissions``, ``HasModelPermission``, ``ModelActions`` and a guard
take ``everywhere=True`` for the same. The audit's finding is A28-SEC-04.
"""

from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from domainkit.contrib.accounting import PostEntry
from domainkit.contrib.approvals import (
    POST_ENTRY,
    ApprovalsUnitOfWork,
    RequestStatus,
    guards,
    submit_command,
)
from domainkit.contrib.messaging.testing import drain
from domainkit.ddd.tenancy import current_tenant
from domainkit.http import get_asgi_application
from domainkit.http.access import HasObjectPermissions, HasPermissions
from domainkit.management.base import run_async
from domainkit.resources import HasModelPermission, ModelActions
from domainkit.tenancy import all_tenants, tenant
from domainkit_tests.conftest import Migrated, Shape
from domainkit_tests.support.auth import create_user, grant
from domainkit_tests.test_accounting_api import open_books, sale
from domainkit_tests.test_approvals_api import APPROVALS_API, PROOF
from domainkit_tests.test_auth_admin import token_for
from domainkit_tests.test_limits_api import DAILY, EVERY, LIMITS_API

BOOKS: Final = (
    "accounting.view_account",
    "accounting.add_account",
    "accounting.view_entry",
    "accounting.add_entry",
    "accounting.view_period",
    "accounting.add_period",
)
ROLE: Final = frozenset(
    {
        *EVERY,
        *BOOKS,
        "approvals.view_approvalrequest",
        "approvals.view_every_approvalrequest",
        "approvals.add_approvalrequest",
    }
)
"""A company's finance role: every key of limits, the ledger, and approvals."""
STAFF: Final = ("clerk", "checker", "checker2")
"""Members of b with no role there: what they hold everywhere is theirs."""
TENANCY: Final = {
    "HEADER": "X-Tenant",
    "REQUIRED": False,
    "MEMBER_PERMISSIONS": f"{__name__}.member_permissions",
}
BACKENDS: Final = [
    "domainkit.contrib.auth.backends.ModelBackend",
    "domainkit.contrib.auth.backends.MembershipBackend",
]
type Headers = dict[str, str]


def member_permissions(user: Any, tenant_id: str) -> frozenset[str] | None:  # noqa: ANN401 - a user.
    """The finance role of b, for its finance members; staff belong with no keys."""
    name = getattr(user, "username", None)
    if tenant_id != "b":
        return None
    if name in {"finance", "finance2"}:
        return ROLE
    return frozenset() if name in STAFF else None


def with_tenancy(shape: Shape, name: str) -> Shape:
    """``shape``, served per tenant by a header, with members' roles."""
    middleware = list(shape.settings["MIDDLEWARE"])
    middleware.insert(
        middleware.index("domainkit.contrib.auth.middleware.AuthenticationMiddleware")
        + 1,
        "domainkit.tenancy.TenantMiddleware",
    )
    return Shape(
        name,
        shape.message,
        {
            **shape.settings,
            "MIDDLEWARE": middleware,
            "TENANCY": TENANCY,
            "AUTH": {**shape.settings["AUTH"], "BACKENDS": BACKENDS},
        },
    )


LIMITS: Final = with_tenancy(LIMITS_API, "limits_api_tenancy")
APPROVALS: Final = with_tenancy(APPROVALS_API, "approvals_api_everywhere")


def in_b(name: str) -> Headers:
    """Headers of ``name``, served as tenant b."""
    return {**token_for(name), "X-Tenant": "b"}


@pytest.fixture
def limits_client(migrated: Migrated) -> Iterator[TestClient]:
    """Limits and the ledger over HTTP, per tenant, with a role and a clerk."""
    with migrated(LIMITS), TestClient(get_asgi_application()) as client:
        create_user("finance")  # Nothing everywhere: the role in b alone.
        create_user("clerk")
        grant("clerk", *EVERY)
        yield client


def test_a_role_in_a_tenant_reaches_no_limit(limits_client: TestClient) -> None:
    """The finance role of b lists, makes, and changes no limit; staff do, in b too."""
    clerk, finance = in_b("clerk"), in_b("finance")
    made = limits_client.post("/limits/limits", json=DAILY, headers=clerk)
    assert made.status_code == 201, made.json()
    limit = made.json()["id"]

    assert limits_client.get("/limits/limits", headers=finance).status_code == 403
    assert (
        limits_client.get(f"/limits/limits/{limit}", headers=finance).status_code == 403
    )
    assert (
        limits_client.post(
            "/limits/limits", json={**DAILY, "code": "x.y"}, headers=finance
        ).status_code
        == 403
    )
    changed = limits_client.post(
        f"/limits/limits/{limit}/change", json={"max_count": 1}, headers=finance
    )
    assert changed.status_code == 403
    usage = limits_client.get(
        "/limits/usage", params={"kind": "holder", "subject": "s"}, headers=finance
    )
    assert usage.status_code == 403
    assert limits_client.get("/limits/limits", headers=clerk).status_code == 200


def test_a_role_in_a_tenant_reaches_no_account(limits_client: TestClient) -> None:
    """The ledger's routes ask the keys held everywhere: b's role reads no account."""
    clerk, finance = in_b("clerk"), in_b("finance")
    opened = limits_client.post(
        "/accounting/accounts",
        json={"code": "1000", "name": "Cash", "type": "asset", "currency": "USD"},
        headers=clerk,
    )
    assert opened.status_code == 201, opened.json()
    account = opened.json()["id"]

    assert limits_client.get("/accounting/accounts", headers=finance).status_code == 403
    assert (
        limits_client.get(
            f"/accounting/accounts/{account}", headers=finance
        ).status_code
        == 403
    )
    assert (
        limits_client.post(
            "/accounting/accounts",
            json={"code": "2000", "name": "X", "type": "asset", "currency": "USD"},
            headers=finance,
        ).status_code
        == 403
    )
    assert limits_client.get("/accounting/accounts", headers=clerk).status_code == 200


# Approvals of postings ----------------------------------------------------------------


@pytest.fixture
def approvals_client(migrated: Migrated) -> Iterator[TestClient]:
    """Approvals and the ledger, per tenant, with roles and staff."""
    with migrated(APPROVALS), TestClient(get_asgi_application()) as client:
        for name in ("finance", "finance2"):
            create_user(name)
        for name in STAFF:
            create_user(name)
            grant(
                name,
                *BOOKS,
                "approvals.view_approvalrequest",
                "approvals.add_approvalrequest",
            )
        yield client


def posting(client: TestClient) -> dict[str, Any]:
    """A posting of 5000 dollars, over the threshold, between real accounts."""
    return sale(open_books(client, in_b("clerk")), "big", "5000.00")


def test_a_role_neither_makes_nor_checks_a_posting(
    approvals_client: TestClient,
) -> None:
    """The guard of postings counts keys held everywhere: b's role is no maker.

    Nor is it a checker or a reader: the role holds every request's view
    key in b, yet a posting the clerk submitted in b is neither listed to
    it nor read by id, and its approval is refused; a checker who holds
    that key everywhere lists it, and the checkers, who hold the posting's
    key everywhere, approve it, and it runs.
    """
    assert guards()[POST_ENTRY].everywhere
    entry = posting(approvals_client)
    body = {"action": POST_ENTRY, "arguments": entry}

    refused = approvals_client.post(
        "/approvals/requests", json=body, headers=in_b("finance")
    )
    assert refused.status_code == 403, refused.json()
    made = approvals_client.post(
        "/approvals/requests", json=body, headers=in_b("clerk")
    )
    assert made.status_code == 201, made.json()
    request = made.json()
    grant("checker2", "approvals.view_every_approvalrequest")

    def listed(name: str) -> list[str]:
        found = approvals_client.get("/approvals/requests", headers=in_b(name))
        assert found.status_code == 200, found.json()
        return [each["id"] for each in found.json()["items"]]

    assert listed("finance") == []
    read = approvals_client.get(
        f"/approvals/requests/{request['id']}", headers=in_b("finance")
    )
    assert read.status_code == 404
    assert listed("checker2") == [request["id"]]

    def approve(name: str) -> Any:  # noqa: ANN401 - a response.
        return approvals_client.post(
            f"/approvals/requests/{request['id']}/approve",
            json={"fingerprint": request["fingerprint"]},
            headers={**PROOF, **in_b(name)},
        )

    assert approve("finance").status_code == 403
    assert approve("finance2").status_code == 403
    assert approve("checker").status_code == 200
    assert approve("checker2").json()["status"] == "approved"
    run_async(drain())
    done = approvals_client.get(
        f"/approvals/requests/{request['id']}", headers=in_b("clerk")
    )
    assert done.json()["status"] == RequestStatus.EXECUTED.value


def test_a_maker_holding_the_key_through_a_role_alone_runs_nothing(
    approvals_client: TestClient,
) -> None:
    """Submitted by code in b for the finance role, approved by staff: it fails."""
    entry = posting(approvals_client)
    finance = str(user_id("finance"))

    async def submit() -> Any:  # noqa: ANN401 - a request.
        with tenant("b"):
            return await submit_command(
                POST_ENTRY, _post_entry(entry), maker=finance, uow=ApprovalsUnitOfWork()
            )

    made = run_async(submit())
    for name in ("checker", "checker2"):
        approved = approvals_client.post(
            f"/approvals/requests/{made.id}/approve",
            json={"fingerprint": made.fingerprint},
            headers={**PROOF, **in_b(name)},
        )
        assert approved.status_code == 200, approved.json()
    run_async(drain())

    async def stored() -> Any:  # noqa: ANN401 - a request.
        with all_tenants():
            async with ApprovalsUnitOfWork() as uow:
                return await uow.requests.get(made.id)

    done = run_async(stored())
    assert (done.status, done.reason) == (RequestStatus.FAILED, "maker_not_allowed")


def _post_entry(entry: dict[str, Any]) -> PostEntry:
    """``entry``, as the API takes it, as the ledger's command."""
    from domainkit.contrib.approvals.guards import guard_for

    return guard_for(POST_ENTRY).load(entry)  # type: ignore[no-any-return]


def user_id(name: str) -> Any:  # noqa: ANN401 - a user's id.
    """The id of the user ``name``."""
    from domainkit.contrib.auth import AuthUnitOfWork

    async def find() -> Any:  # noqa: ANN401
        async with AuthUnitOfWork() as uow:
            found = await uow.users.get_by_natural_key(name)
            assert found is not None
            return found.id

    return run_async(find())


# The rules ----------------------------------------------------------------------


class Member:
    """A principal that holds ``billing.view_invoice`` in tenant a alone."""

    is_authenticated = True
    is_active = True
    id = "member"

    async def ahas_perms(self, perms: Any, obj: object = None) -> bool:  # noqa: ANN401
        """In tenant a, every key; elsewhere none."""
        return current_tenant() == "a"


def connection(scopes: frozenset[str] | None = None) -> HTTPConnection:
    """A request, its credentials limited to ``scopes`` if given."""
    scope: dict[str, Any] = {"type": "http", "headers": []}
    if scopes is not None:
        scope["auth"] = type("Credentials", (), {"scopes": scopes})()
    return HTTPConnection(scope)


@pytest.mark.anyio
async def test_permissions_held_everywhere_leave_a_tenants_keys_out() -> None:
    """``everywhere=True`` asks as where no tenant is served, on objects too."""
    member, request = Member(), connection()
    key = "billing.view_invoice"

    with tenant("a"):
        assert await HasPermissions(key).has_permission(request, member)
        assert not await HasPermissions(key, everywhere=True).has_permission(
            request, member
        )
        assert await HasObjectPermissions(key).has_object_permission(
            request, member, object()
        )
        assert not await HasObjectPermissions(
            key, everywhere=True
        ).has_object_permission(request, member, object())
        assert not await HasModelPermission(
            "view", "alpha.widget", key=key, everywhere=True
        ).has_permission(request, member)
        assert current_tenant() == "a"


@pytest.mark.anyio
async def test_scopes_still_bound_permissions_held_everywhere() -> None:
    """Credentials whose scopes leave the key out are refused, held or not."""
    everywhere = Everywhere()
    key = "billing.view_invoice"

    for scopes, allowed in ((frozenset(), False), (frozenset({key}), True)):
        request = connection(scopes)
        for rule in (HasPermissions(key), HasPermissions(key, everywhere=True)):
            assert await rule.has_permission(request, everywhere) is allowed
        for rule in (
            HasObjectPermissions(key),
            HasObjectPermissions(key, everywhere=True),
        ):
            held = await rule.has_object_permission(request, everywhere, object())
            assert held is allowed


class Everywhere(Member):
    """A principal that holds every key, whatever tenant is served."""

    async def ahas_perms(self, perms: Any, obj: object = None) -> bool:  # noqa: ANN401
        """Every key, always."""
        return True


def test_model_actions_say_everywhere_to_each_action() -> None:
    """``ModelActions(..., everywhere=True)`` asks each action's key everywhere."""
    for action in ("view", "add", "change", "delete"):
        (rule,) = ModelActions("alpha.widget", everywhere=True).rules_for(action)
        assert isinstance(rule, HasModelPermission)
        assert rule.everywhere
    (plain,) = ModelActions("alpha.widget").rules_for("view")
    assert isinstance(plain, HasModelPermission)
    assert not plain.everywhere
