"""Staff APIs count the keys a staff member holds everywhere (ADR 0183's amendment).

Users, groups, flags, and the audit trail belong to no tenant. A staff
member whose keys come only from a role in the tenant served (a member of
tenant b, through ``MembershipBackend``) reads no user, writes no flag, and
reads no history there; granted the same keys everywhere, they do, from
any tenant. The admin site asks each model's keys everywhere, but a
``TenantOwned`` model's, whose rows are the tenant's served.
"""

from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from domainkit.contrib.admin import AdminSite
from domainkit.contrib.admin.sites import HISTORY
from domainkit.contrib.audit import api as audit_api
from domainkit.http import get_asgi_application
from domainkit.http.access import HasPermissions
from domainkit.resources import HasModelPermission
from domainkit_tests.conftest import (
    ALPHA,
    AUTH,
    CONTENTTYPES,
    FAST_HASHER,
    SHOP,
    Migrated,
    Shape,
)
from domainkit_tests.gadgets import FakeGadgetsUnitOfWork, Gadget, InMemoryGadgets
from domainkit_tests.support.auth import create_user, grant
from domainkit_tests.test_auth_admin import token_for
from domainkit_tests.test_tenancy import Note, Notes

__all__ = ["member_permissions", "member_tenants"]

KEYS: Final = frozenset(
    {
        "auth.view_user",
        "auth.change_user",
        "auth.view_group",
        "flags.view_flag",
        "flags.add_flag",
        "audit.view_history",
    }
)
"""A role b's admins gave: the keys of the staff APIs."""
MEMBERS: Final = {"staffb": {"b"}, "staffall": {"b"}, "victim": {"a"}}


def member_permissions(user: Any, tenant_id: str) -> frozenset[str] | None:  # noqa: ANN401 - a user.
    """The role, for staffb in b; the others belong with no keys."""
    name = str(getattr(user, "username", ""))
    if tenant_id not in MEMBERS.get(name, ()):
        return None
    return KEYS if name == "staffb" else frozenset()


def member_tenants(user: Any) -> set[str]:  # noqa: ANN401 - a user.
    """The tenants each user belongs to."""
    return set(MEMBERS.get(str(getattr(user, "username", "")), ()))


STAFF_APIS: Final = Shape(
    "staff_apis_tenancy",
    "staff",
    {
        "INSTALLED_DOMAINS": [
            CONTENTTYPES,
            AUTH,
            "domainkit.contrib.audit",
            "domainkit.contrib.flags",
            SHOP,
            ALPHA,
        ],
        "ROOT_ROUTER": "domainkit_tests.staff_routes.router",
        "MIDDLEWARE": [
            "domainkit.http.middleware.RequestContextMiddleware",
            "domainkit.http.middleware.RequestBodyLimitMiddleware",
            "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
            "domainkit.tenancy.TenantMiddleware",
        ],
        "TENANCY": {
            "HEADER": "X-Tenant",
            "REQUIRED": False,
            "MEMBER_PERMISSIONS": f"{__name__}.member_permissions",
            "MEMBER_TENANTS": f"{__name__}.member_tenants",
        },
        "AUTH": {
            "PASSWORD_HASHERS": [FAST_HASHER],
            "BACKENDS": [
                "domainkit.contrib.auth.backends.ModelBackend",
                "domainkit.contrib.auth.backends.MembershipBackend",
            ],
        },
    },
)
"""Auth's admin API, flags', and the audit trail's, served per tenant by a header."""
type Headers = dict[str, str]


@pytest.fixture
def client(migrated: Migrated) -> Iterator[TestClient]:
    """The staff APIs; staffb holds the keys through b's role, staffall everywhere."""
    with migrated(STAFF_APIS), TestClient(get_asgi_application()) as test_client:
        create_user("staffb", is_staff=True)
        create_user("staffall", is_staff=True)
        grant("staffall", *KEYS)
        create_user("victim", email="victim@tenant-a.example")
        yield test_client


def in_b(name: str) -> Headers:
    """Headers of ``name``, served as tenant b."""
    return {**token_for(name), "X-Tenant": "b"}


def test_a_role_opens_no_users_list(client: TestClient) -> None:
    """The admin API's users, and their names, are no tenant's: the role is 403."""
    for path in ("/admin/auth/users", "/admin/auth/users/names"):
        assert client.get(path, headers=in_b("staffb")).status_code == 403, path
        listed = client.get(path, headers=in_b("staffall"))
        assert listed.status_code == 200, (path, listed.json())
    names = [each["username"] for each in listed.json()["items"]]
    assert "victim" in names
    assert client.get("/admin/auth/users", headers=token_for("staffall")).is_success


def test_a_role_writes_no_flag(client: TestClient) -> None:
    """A flag every tenant shares: the role makes none; the key held everywhere does."""
    body = {"name": "global-kill", "everyone": True}

    refused = client.post("/admin/flags/flags", json=body, headers=in_b("staffb"))
    made = client.post("/admin/flags/flags", json=body, headers=in_b("staffall"))

    assert refused.status_code == 403, refused.json()
    assert made.status_code == 201, made.json()
    assert client.get("/admin/flags/flags", headers=in_b("staffb")).status_code == 403


def test_a_role_reads_no_history(client: TestClient) -> None:
    """The audit trail is no tenant's: ``audit.view_history`` counts everywhere."""
    users = client.get("/admin/auth/users", headers=in_b("staffall")).json()["items"]
    victim = next(each["id"] for each in users if each["username"] == "victim")
    asked = {"field": "user_id", "id": victim}

    refused = client.get("/admin/audit/history", params=asked, headers=in_b("staffb"))
    read = client.get("/admin/audit/history", params=asked, headers=in_b("staffall"))

    assert refused.status_code == 403
    assert read.status_code == 200, read.json()


def test_the_admin_site_asks_keys_everywhere_but_a_tenants_rows() -> None:
    """A model of no tenant asks its keys everywhere; a ``TenantOwned`` one, as served.

    Histories ask ``audit.view_history`` everywhere, in the admin site and
    in the audit trail's own API.
    """
    site = AdminSite()
    gadgets = site.register(
        Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(InMemoryGadgets([])),
        repository=lambda uow: uow.gadgets,
        label="shop.gadget",
    )
    notes = site.register(
        Note,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(InMemoryGadgets([])),
        repository=lambda uow: Notes(),
        label="alpha.note",
        history=False,
    )

    def asked(admin: Any, action: str) -> HasModelPermission:  # noqa: ANN401 - an admin.
        *_, rule = admin.resource.rule.rules_for(action)
        assert isinstance(rule, HasModelPermission)
        return rule

    assert all(asked(gadgets, action).everywhere for action in ("view", "change"))
    assert not any(asked(notes, action).everywhere for action in ("view", "change"))
    assert isinstance(HISTORY, HasPermissions)
    assert HISTORY.everywhere
    history = [
        each for each in audit_api.staff.rules if isinstance(each, HasPermissions)
    ]
    assert [each.everywhere for each in history] == [True]
