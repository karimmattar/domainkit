"""A project with its own authentication, and no ``domainkit.contrib.auth`` (DSGN-02).

The gateway project (``gateway_project``) puts a principal of its own in
``scope["user"]`` and serves a ``ResourceRouter`` guarded by the core's
rules: anonymous callers get its 401, callers without the permission 403,
and the rest what the scope and the field policy let them reach and see.
Deny by default holds as in a project with auth, when the application is
built and when it starts, and secrets stay out of derived schemas. In a
process of its own, serving it imports no contrib app at all.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from domainkit.apps import registry
from domainkit.conf import override_settings
from domainkit.ddd import AggregateRoot, Sensitive, ValueObject
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import get_asgi_application
from domainkit.http.access import (
    DEFAULT_CHALLENGE,
    AnonymousPrincipal,
    HasPermissions,
    NotAuthenticated,
    Require,
)
from domainkit.http.application import application_problems
from domainkit.resources import schema_for
from domainkit.testing import user_token
from domainkit_tests import gateway_project
from domainkit_tests.gadgets import Gadget, make_gadget
from domainkit_tests.gateway_project import (
    CHALLENGE,
    RENAME,
    SEE_PRICE,
    STORE,
    VIEW,
    GatewayUser,
)

ROOT: Final = Path(__file__).parents[1]
MIDDLEWARE: Final = [
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit_tests.gateway_project.GatewayAuthentication",
]
PROJECT: Final = {
    "MIDDLEWARE": MIDDLEWARE,
    "ROOT_ROUTER": "domainkit_tests.gateway_project.router",
}
FORGOTTEN: Final = (
    "GET /forgotten declares no access rule: add Depends(Require(...)), or "
    "dependencies=[Public] if anyone may call it."
)


def as_(name: str, *permissions: str) -> dict[str, str]:
    """Headers the gateway sends for ``name``, granted ``permissions``."""
    return {"X-User": name, "X-Permissions": " ".join(permissions)}


@pytest.fixture
def gadgets() -> Iterator[dict[str, Gadget]]:
    """Gadgets of alice's and bob's, one of them inactive; gone afterwards."""
    made = {
        "alice": make_gadget("alice-1"),
        "bob": make_gadget("bob-1"),
        "old": make_gadget("alice-old", active=False),
    }
    STORE.items.update({gadget.id: gadget for gadget in made.values()})
    yield made
    STORE.items.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The gateway project's application, built without auth installed."""
    assert not registry.is_installed("domainkit.contrib.auth")
    assert not registry.is_installed("domainkit.contrib.contenttypes")
    with override_settings(**PROJECT), TestClient(get_asgi_application()) as client:
        yield client


@pytest.mark.usefixtures("gadgets")
def test_anonymous_callers_get_the_providers_challenge(client: TestClient) -> None:
    """A rule refuses an anonymous principal with 401 and its ``challenge()``."""
    listed = client.get("/gadgets")
    whoami = client.get("/whoami")

    for refused in (listed, whoami):
        assert refused.status_code == 401
        assert refused.headers["WWW-Authenticate"] == CHALLENGE
        assert refused.json()["code"] == "not_authenticated"
    assert client.get("/hello").json() == {"hello": "world"}


@pytest.mark.usefixtures("gadgets")
def test_a_principal_that_fails_the_rule_is_refused(client: TestClient) -> None:
    """403 for a known principal without the permission, on every route."""
    for path in ("/gadgets", "/whoami"):
        status = client.get(path, headers=as_("mallory", "shop.other")).status_code
        assert status == (403 if path == "/gadgets" else 200)
    refused = client.get("/gadgets", headers=as_("mallory"))

    assert refused.status_code == 403
    assert "WWW-Authenticate" not in refused.headers
    assert refused.json()["code"] == "permission_denied"


def test_the_rule_the_scope_and_the_policy_decide_what_is_seen(
    client: TestClient, gadgets: dict[str, Gadget]
) -> None:
    """Listed and read within the scope, the price only with its permission."""
    viewer = as_("bob", VIEW)
    pricing = as_("bob", VIEW, SEE_PRICE)

    listed = client.get("/gadgets", headers=viewer).json()["items"]
    priced = client.get("/gadgets", headers=pricing).json()["items"]
    read = client.get(f"/gadgets/{gadgets['bob'].id}", headers=viewer)
    out_of_scope = client.get(f"/gadgets/{gadgets['old'].id}", headers=viewer)

    assert sorted(item["name"] for item in listed) == ["alice-1", "bob-1"]
    assert all("price" not in item for item in listed)
    assert all(item["price"] == "1.00" for item in priced)
    assert read.status_code == 200
    assert read.headers["ETag"]
    assert out_of_scope.status_code == 404
    assert client.get("/whoami", headers=viewer).json() == {"user": "bob"}


def test_commands_need_their_own_rule_and_the_aggregate_in_reach(
    client: TestClient, gadgets: dict[str, Gadget]
) -> None:
    """The route's rule, the resource's, and the scope, before the service runs."""
    bob = gadgets["bob"]
    path = f"/gadgets/{bob.id}/rename"

    without = client.post(path, json={"name": "x"}, headers=as_("bob", VIEW))
    renamed = client.post(
        path, json={"name": "bob-2"}, headers=as_("bob", VIEW, RENAME)
    )
    old = client.post(
        f"/gadgets/{gadgets['old'].id}/rename",
        json={"name": "x"},
        headers=as_("alice", VIEW, RENAME),
    )

    assert without.status_code == 403
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "bob-2"
    assert old.status_code == 404
    assert gadgets["old"].name == "alice-old"


def test_rules_that_look_at_the_object_ask_the_principal(
    client: TestClient, gadgets: dict[str, Gadget]
) -> None:
    """A rule of the project's own reads its own principal and the aggregate."""
    own = f"/gadgets/{gadgets['alice'].id}/rename-own"
    other = f"/gadgets/{gadgets['bob'].id}/rename-own"

    renamed = client.post(own, json={"name": "alice-2"}, headers=as_("alice", VIEW))
    refused = client.post(other, json={"name": "alice-3"}, headers=as_("alice", VIEW))

    assert renamed.status_code == 200
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Only the gadget's namesake may rename it."
    assert gadgets["bob"].name == "bob-1"


def test_routes_without_a_rule_are_refused_without_auth() -> None:
    """Deny by default is the core's: built in production, a forgotten route fails."""
    with override_settings(**PROJECT):
        app = get_asgi_application()
        app.include_router(gateway_project.forgotten)
        assert application_problems(app) == [FORGOTTEN]
        with override_settings(REQUIRE_ROUTE_RULES=False):
            assert application_problems(app) == []
        with override_settings(ENVIRONMENT="production"):
            built = get_asgi_application()
            built.include_router(gateway_project.forgotten)
            with pytest.raises(ImproperlyConfigured) as raised, TestClient(built):
                pass  # pragma: no cover - the start is refused.

    assert str(raised.value) == (
        f"The application has problems since it was built:\n  {FORGOTTEN}"
    )


def test_the_projects_own_reader_counts_as_a_rule() -> None:
    """``/whoami`` depends on a ``reads_principal`` dependency: not open."""
    with override_settings(**PROJECT):
        app = get_asgi_application()

    assert application_problems(app) == []


def test_secrets_stay_out_of_schemas_without_auth() -> None:
    """A ``Sensitive`` type of the project's own is refused, as a password hash is."""

    class PinHash(ValueObject, Sensitive):
        """A stored PIN."""

        value: str

    class Locker(AggregateRoot[int]):
        """A locker opened by a PIN."""

        pin: PinHash
        size: int

    with pytest.raises(TypeError, match=r"Locker\.pin holds a secret \(Sensitive\)"):
        schema_for(Locker)

    assert set(schema_for(Locker, exclude=["pin"]).model_fields) == {
        "id",
        "version",
        "size",
    }


def test_an_anonymous_principal_holds_nothing_and_names_the_default_challenge() -> None:
    """``AnonymousPrincipal``: no id, no permission, ``Bearer realm="api"``."""
    nobody = AnonymousPrincipal()
    request = Request({"type": "http", "method": "GET", "headers": [], "user": nobody})

    with pytest.raises(NotAuthenticated) as refused:
        asyncio.run(Require(HasPermissions(VIEW))(request, None))

    assert refused.value.headers == {"WWW-Authenticate": DEFAULT_CHALLENGE}
    assert DEFAULT_CHALLENGE == 'Bearer realm="api"'
    assert nobody == AnonymousPrincipal()
    assert (nobody.id, nobody.is_authenticated, nobody.is_active) == (
        None,
        False,
        False,
    )
    assert asyncio.run(nobody.ahas_perms([VIEW])) is False
    assert asyncio.run(GatewayUser("bob", frozenset({VIEW})).ahas_perms([VIEW]))


SCRIPT: Final = textwrap.dedent(
    """
    import json
    import sys

    from fastapi.testclient import TestClient

    from domainkit.http import get_asgi_application
    from domainkit_tests.gadgets import make_gadget
    from domainkit_tests.gateway_project import STORE, VIEW

    gadget = make_gadget("bob-1")
    STORE.items[gadget.id] = gadget
    with TestClient(get_asgi_application()) as client:
        statuses = [
            client.get("/gadgets").status_code,
            client.get("/gadgets", headers={"X-User": "bob"}).status_code,
            client.get(
                f"/gadgets/{gadget.id}",
                headers={"X-User": "bob", "X-Permissions": VIEW},
            ).status_code,
        ]
    contrib = sorted(
        name for name in sys.modules if name.startswith("domainkit.contrib")
    )
    print(json.dumps({"statuses": statuses, "contrib": contrib}))
    """
)


def test_serving_the_project_imports_no_contrib_app() -> None:
    """In a process of its own: the guarded resource answers, and no app is loaded."""
    environment: dict[str, Any] = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("DOMAINKIT_")
    }
    environment |= {
        "DOMAINKIT_SETTINGS_MODULE": "domainkit_tests.gateway_settings",
        "DOMAINKIT_ENV_FILE": "",
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
    }
    done = subprocess.run(  # noqa: S603 - the test interpreter and a fixed script.
        [sys.executable, "-c", SCRIPT],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[-1]) == {
        "statuses": [401, 403, 200],
        "contrib": [],
    }


def test_test_tokens_need_a_domain_that_issues_them() -> None:
    """Without auth, nothing issues tokens: ``user_token`` says so."""
    with pytest.raises(ImproperlyConfigured, match="issues tokens, such as"):
        asyncio.run(user_token("shop.view_gadget"))
