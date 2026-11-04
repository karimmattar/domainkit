"""What belongs to no tenant is counted and kept once, whichever tenant is served.

The audit found login throttles counted per tenant, so naming another one
started them afresh (A5-1); principals' stamps written in a tenant's
request that authentication, which runs before the tenant is known, never
read (A5-2); and an ``Idempotency-Key`` replaying one tenant's answer in
another (A3-2).
"""

from typing import Final

from fakeredis import FakeServer
from fastapi.testclient import TestClient
from sqlalchemy import update

from domainkit.conf import override_settings
from domainkit.contrib.auth import principals
from domainkit.contrib.auth.principals import user_target
from domainkit.contrib.auth.users import get_user_table
from domainkit.db import connections
from domainkit.ddd.tenancy import all_tenants, scoped_key, shared_keys, tenant
from domainkit.http import get_asgi_application
from domainkit.management.base import run_async
from domainkit_tests import sample_tenant_payouts
from domainkit_tests.conftest import API, MIDDLEWARE, Api, Migrated, Shape
from domainkit_tests.test_auth_admin import token_for
from domainkit_tests.test_auth_http import create_user
from domainkit_tests.test_auth_principal_changes import outcome
from domainkit_tests.test_auth_principals import STAMPED, issue
from domainkit_tests.test_auth_principals import (
    shared as shared,
)

TENANT_MIDDLEWARE: Final = [
    *MIDDLEWARE[:3],
    "domainkit.tenancy.TenantMiddleware",
    *MIDDLEWARE[3:],
]
"""Authentication first, then the tenant, as docs/tenancy.md orders them."""


def test_shared_keys_are_every_tenants_and_rows_stay_scoped() -> None:
    """Inside ``shared_keys`` a key has no tenant; ``all_tenants`` is still off."""
    with tenant("acme"):
        assert scoped_key("k") == "t:acme:k"
        with shared_keys():
            assert scoped_key("k") == "k"
        assert scoped_key("k") == "t:acme:k"
    with all_tenants(), tenant("acme"):
        assert scoped_key("k") == "k"


def test_a_tenant_named_afresh_starts_no_login_count_afresh(api: Api) -> None:
    """An account's wrong logins are counted once, whatever the subdomain."""
    create_user("alice")
    with override_settings(
        TENANCY={"SUBDOMAIN_OF": "example.com"}, MIDDLEWARE=TENANT_MIDDLEWARE
    ):
        client = api(LOGIN_RATE="2/minute", ACCOUNT_LOGIN_RATE="3/day")
        wrong = {"username": "alice", "password": "wrong"}
        answers = [
            client.post(f"http://x{n}.example.com/auth/login", json=wrong).status_code
            for n in range(6)
        ]

    assert answers == [401, 401, 429, 429, 429, 429]


def test_a_change_stamped_in_a_tenants_request_reaches_authentication(
    api: Api, shared: FakeServer
) -> None:
    """A stamp written while a tenant is served is the one read without one."""
    alice = create_user("alice")
    api(PRINCIPAL_CACHE=STAMPED)
    raw = run_async(issue("alice"))

    async def seen() -> list[str]:
        before = await outcome(raw)
        async with connections.engine().begin() as connection:
            table = get_user_table()
            await connection.execute(
                update(table).where(table.c.id == alice).values(is_active=False)
            )
        with tenant("acme"):
            await principals.write_stamps("shared", [user_target(alice)])
        return [before, await outcome(raw)]

    assert run_async(seen()) == ["served", "User inactive or deleted."]


PAYOUTS: Final = Shape(
    "tenant_payouts",
    API.message,
    {
        **API.settings,
        "ROOT_ROUTER": "domainkit_tests.sample_tenant_payouts.router",
        "MIDDLEWARE": TENANT_MIDDLEWARE,
        "TENANCY": {"SUBDOMAIN_OF": "example.com"},
        "ALLOWED_HOSTS": ["*"],
    },
)


def test_an_idempotency_key_is_one_tenants(migrated: Migrated) -> None:
    """The same key and body in two tenants are two payouts, not one replayed."""
    sample_tenant_payouts.RAN.clear()
    with migrated(PAYOUTS):
        create_user("ann")
        headers = {**token_for("ann"), "Idempotency-Key": "k-1"}
        answers = []
        for name in ("acme", "globex"):
            with TestClient(
                get_asgi_application(), base_url=f"http://{name}.example.com"
            ) as client:
                answers.append(
                    client.post("/payouts", json={"amount": "100.00"}, headers=headers)
                )

    assert sample_tenant_payouts.RAN == ["acme", "globex"]
    assert [answer.json()["tenant"] for answer in answers] == ["acme", "globex"]
    assert "idempotent-replayed" not in answers[1].headers
