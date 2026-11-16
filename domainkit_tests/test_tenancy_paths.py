"""A tenant a route's path names, 404 for non-members, and members' permissions.

A project reported (D2) that tenancy fitted one tenant per request,
chosen before routing: no tenant from a path parameter, no ``404`` for
those who do not belong, and no memberships that carry permissions, so it
wrote its own ``MembershipBackend`` (ADR 0150).
"""

from collections.abc import Iterator
from typing import Annotated, Any, Final, NewType
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import insert
from starlette.datastructures import Headers
from starlette.testclient import WebSocketDenialResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.conf import TenancyConfig, override_settings
from domainkit.contrib.auth.backends import MembershipBackend, ObjectGrant
from domainkit.db import connections
from domainkit.ddd import AggregateRoot, InMemoryRepository
from domainkit.ddd.tenancy import TenantOwned
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import AnonymousPrincipal
from domainkit.http.errors import install_exception_handlers
from domainkit.http.live import reauthorize
from domainkit.management.base import run_async
from domainkit.tenancy import (
    PathTenant,
    TenantMiddleware,
    current_tenant,
    member_permissions,
    tenant,
)
from domainkit_tests import sample_tenant_orgs
from domainkit_tests.conftest import API, Api
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_auth_access import user
from domainkit_tests.test_auth_admin import token_for

pytestmark = pytest.mark.anyio

HERE: Final = "domainkit_tests.test_tenancy_paths"
PEOPLE: Final = {
    "ann": user(username="ann"),
    "bob": user(username="bob"),
    "idle": user(username="idle", is_active=False),
}
"""Who the ``X-User`` header names; anyone else is anonymous."""
MEMBERS: set[tuple[str, str]] = {("ann", "acme"), ("ann", "globex"), ("bob", "globex")}
"""``(username, tenant)`` of each membership."""
ROLES: Final = {"acme": frozenset({"notes.view_note", "notes.add_note"})}
"""The keys a member of each tenant holds (a role's)."""
ASKED: list[tuple[str, str]] = []
"""Each time ``keys`` was asked, for whom and which tenant."""
LATER: list[str | None] = []
"""The tenant each background task ran as."""


def belongs(user: Any, tenant_id: str) -> bool:  # noqa: ANN401
    """``MEMBERSHIP``: whether ``user`` is one of ``tenant_id``'s members."""
    return (getattr(user, "username", None), tenant_id) in MEMBERS


async def keys(user: Any, tenant_id: str) -> frozenset[str] | None:  # noqa: ANN401
    """``MEMBER_PERMISSIONS``: a member's keys in ``tenant_id``, or None."""
    ASKED.append((getattr(user, "username", "-"), tenant_id))
    if not belongs(user, tenant_id):
        return None
    return ROLES.get(tenant_id, frozenset())


def one_key(user: Any, tenant_id: str) -> str:  # noqa: ANN401
    """A hook that answers one key as text, which reads as its letters."""
    return "notes.view_note"


class Users:
    """Put the person ``X-User`` names in ``scope["user"]`` (anonymous if none)."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Name the user, then serve."""
        if scope["type"] in {"http", "websocket"}:
            name = Headers(scope=scope).get("x-user", "")
            scope["user"] = PEOPLE.get(name, AnonymousPrincipal())
        await self.app(scope, receive, send)


NoteId = NewType("NoteId", UUID)


class Note(TenantOwned, AggregateRoot[NoteId]):
    """One tenant's note."""

    text: str = ""


class Notes(InMemoryRepository[Note, NoteId]):
    """Notes, in memory."""


BY_PATH: Final = PathTenant("org_id")


def app(*, middleware: bool = False) -> FastAPI:
    """Routes under ``/orgs/{org_id}``, served as that tenant, behind ``Users``.

    With ``middleware``, ``TenantMiddleware`` runs after ``Users``.
    """
    notes = Notes()
    orgs = APIRouter(prefix="/orgs/{org_id}", dependencies=[Depends(BY_PATH)])

    @orgs.get("/whoami")
    async def whoami(
        request: Request, named: Annotated[str, Depends(BY_PATH)]
    ) -> dict[str, Any]:
        held = await member_permissions(request.scope["user"])
        return {"tenant": current_tenant(), "named": named, "keys": sorted(held)}

    @orgs.post("/notes")
    async def add(body: dict[str, str], tasks: BackgroundTasks) -> dict[str, Any]:
        await notes.add(Note(id=NoteId(uuid4()), text=body["text"]))
        tasks.add_task(lambda: LATER.append(current_tenant()))
        return {"texts": sorted(note.text for note in await notes.filter())}

    @orgs.get("/still")
    async def still(request: Request) -> dict[str, bool]:
        before = await reauthorize(request.scope)
        MEMBERS.discard(("ann", "acme"))
        try:
            return {"before": before, "after": await reauthorize(request.scope)}
        finally:
            MEMBERS.add(("ann", "acme"))

    @orgs.websocket("/live")
    async def live(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"tenant": current_tenant()})
        await socket.close()

    served = FastAPI()
    install_exception_handlers(served)
    served.include_router(orgs)
    if middleware:
        served.add_middleware(TenantMiddleware)
    served.add_middleware(Users)  # Added last, so it runs first.
    return served


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A client of ``app``, with ``MEMBERSHIP`` saying who belongs."""
    with override_settings(TENANCY={"MEMBERSHIP": f"{HERE}.belongs"}):
        yield TestClient(app())


def as_(name: str) -> dict[str, str]:
    """Headers that make ``name`` the caller."""
    return {"X-User": name}


def body(answer: Any) -> tuple[int, dict[str, Any]]:  # noqa: ANN401
    """An answer's status and body, without its request id."""
    found = dict(answer.json())
    found.pop("request_id", None)
    return answer.status_code, found


def test_a_router_takes_its_tenant_from_the_path(client: TestClient) -> None:
    """``/orgs/acme/...`` is served as acme, and the dependency names it."""
    answer = client.get("/orgs/acme/whoami", headers=as_("ann"))
    other = client.get("/orgs/globex/whoami", headers=as_("ann"))

    assert answer.json() == {"tenant": "acme", "named": "acme", "keys": []}
    assert other.json()["tenant"] == "globex"
    assert current_tenant() is None


def test_a_path_tenants_rows_and_tasks_are_its_own(client: TestClient) -> None:
    """Rows added under one tenant's path are not another's; its tasks run as it."""
    LATER.clear()
    client.post("/orgs/acme/notes", json={"text": "a"}, headers=as_("ann"))
    theirs = client.post("/orgs/globex/notes", json={"text": "g"}, headers=as_("ann"))
    mine = client.post("/orgs/acme/notes", json={"text": "b"}, headers=as_("ann"))

    assert theirs.json() == {"texts": ["g"]}
    assert mine.json() == {"texts": ["a", "b"]}
    assert LATER == ["acme", "globex", "acme"]


def test_a_non_member_gets_the_404_of_a_route_that_does_not_exist(
    client: TestClient,
) -> None:
    """Not a member, no such tenant, or no tenant id: all answer as no route does.

    So a caller cannot tell a tenant that exists from one that does not
    (RFC 9110 section 15.5.4; OWASP's IDOR prevention cheat sheet).
    """
    nowhere = body(client.get("/no/such/route", headers=as_("bob")))
    refused = [
        body(client.get(path, headers=as_("bob")))
        for path in (
            "/orgs/acme/whoami",  # exists; bob is not a member
            "/orgs/initech/whoami",  # no such tenant
            "/orgs/a.b/whoami",  # not a tenant id
        )
    ]
    sent = [
        body(client.post(f"/orgs/{name}/notes", json={"text": "x"}, headers=as_("bob")))
        for name in ("acme", "initech")
    ]

    assert nowhere == (404, {"detail": "Not Found", "code": "not_found"})
    assert refused == [nowhere] * 3
    assert sent == [nowhere] * 2


def test_an_anonymous_caller_is_asked_to_log_in(client: TestClient) -> None:
    """401, as ``Require`` answers, for any id: it tells no tenant apart."""
    answers = [client.get(f"/orgs/{name}/whoami") for name in ("acme", "initech")]

    assert [body(answer) for answer in answers] == [
        (
            401,
            {
                "detail": "Authentication credentials were not provided.",
                "code": "not_authenticated",
            },
        )
    ] * 2
    assert answers[0].headers["WWW-Authenticate"] == 'Bearer realm="api"'


def test_a_websocket_takes_its_tenant_from_the_path(client: TestClient) -> None:
    """A member's handshake is served as the tenant; another's gets the 404."""
    with client.websocket_connect("/orgs/acme/live", headers=as_("ann")) as socket:
        assert socket.receive_json() == {"tenant": "acme"}
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect("/orgs/acme/live", headers=as_("bob")),
    ):
        pass
    assert denied.value.status_code == 404


def test_membership_is_checked_again_while_a_connection_stays_open(
    client: TestClient,
) -> None:
    """The re-check of a live connection asks afresh: a removed member fails it."""
    assert client.get("/orgs/acme/still", headers=as_("ann")).json() == {
        "before": True,
        "after": False,
    }


def test_openapi_documents_the_path_parameter_once(client: TestClient) -> None:
    """The dependency declares the parameter; the route may declare it too."""
    operation = client.get("/openapi.json").json()["paths"]["/orgs/{org_id}/whoami"]
    parameters = operation["get"]["parameters"]

    assert [(p["name"], p["in"], p["required"]) for p in parameters] == [
        ("org_id", "path", True)
    ]


def test_a_path_must_agree_with_the_tenant_the_request_is_served_as() -> None:
    """With ``TenantMiddleware`` too: another tenant in the path is 404.

    The prefix is in ``EXEMPT_PATHS``, so a request that names no tenant
    before routing takes the path's.
    """
    with override_settings(
        TENANCY={
            "HEADER": "X-Tenant",
            "MEMBERSHIP": f"{HERE}.belongs",
            "EXEMPT_PATHS": ["/orgs/"],
        }
    ):
        client = TestClient(app(middleware=True))
        both = {**as_("ann"), "X-Tenant": "acme"}
        same = client.get("/orgs/acme/whoami", headers=both)
        other = client.get("/orgs/globex/whoami", headers=both)
        unnamed = client.get("/orgs/globex/whoami", headers=as_("ann"))

    assert same.json()["tenant"] == "acme"
    assert body(other) == (404, {"detail": "Not Found", "code": "not_found"})
    assert unnamed.json()["tenant"] == "globex"


def test_a_path_tenant_needs_someone_to_say_who_belongs() -> None:
    """No ``TENANCY``, or no ``MEMBERSHIP``: refused, since any id can be typed."""
    unsaid: tuple[dict[str, str] | None, ...] = (None, {})
    for tenancy in unsaid:
        with override_settings(TENANCY=tenancy):
            client = TestClient(app())
            with pytest.raises(ImproperlyConfigured, match="MEMBERSHIP"):
                client.get("/orgs/acme/whoami", headers=as_("ann"))


@pytest.mark.parametrize("name", ["org-id", "class", "", "domainkit_connection"])
def test_a_path_tenant_names_a_parameter(name: str) -> None:
    """What cannot be a path parameter's name is refused at once."""
    with pytest.raises(ImproperlyConfigured, match="path parameter"):
        PathTenant(name)


def test_member_permissions_are_asked_once_per_request() -> None:
    """``MEMBER_PERMISSIONS`` admits a member, and its keys serve the request.

    The hook is asked once for the request, however often the keys are
    read in it.
    """
    ASKED.clear()
    with override_settings(TENANCY={"MEMBER_PERMISSIONS": f"{HERE}.keys"}):
        client = TestClient(app())
        mine = client.get("/orgs/acme/whoami", headers=as_("ann"))
        asked = list(ASKED)
        refused = client.get("/orgs/acme/whoami", headers=as_("bob"))

    assert mine.json()["keys"] == ["notes.add_note", "notes.view_note"]
    assert asked == [("ann", "acme")]
    assert refused.status_code == 404


async def test_member_permissions_outside_a_request() -> None:
    """Asked afresh for a tenant named in code; none without a tenant or the setting."""
    ann = PEOPLE["ann"]
    with override_settings(TENANCY={"MEMBER_PERMISSIONS": f"{HERE}.keys"}):
        assert await member_permissions(ann) == frozenset()
        with tenant("acme"):
            assert "notes.view_note" in await member_permissions(ann)
            assert await member_permissions(PEOPLE["bob"]) == frozenset()
    with override_settings(TENANCY={"MEMBERSHIP": f"{HERE}.belongs"}), tenant("acme"):
        assert await member_permissions(ann) == frozenset()
    with override_settings(TENANCY=None), tenant("acme"):
        assert await member_permissions(ann) == frozenset()


async def test_member_permissions_must_be_keys() -> None:
    """A hook that answers one string, or a bool, is refused, not read as letters."""
    with (
        override_settings(TENANCY={"MEMBER_PERMISSIONS": f"{HERE}.one_key"}),
        tenant("acme"),
        pytest.raises(ImproperlyConfigured, match="returned a str"),
    ):
        await member_permissions(PEOPLE["ann"])
    with (
        override_settings(TENANCY={"MEMBER_PERMISSIONS": f"{HERE}.belongs"}),
        tenant("acme"),
        pytest.raises(ImproperlyConfigured, match="returned a bool"),
    ):
        await member_permissions(PEOPLE["ann"])


def test_the_middleware_admits_members_by_their_permissions() -> None:
    """``TenantMiddleware`` with ``MEMBER_PERMISSIONS``: 403 for one with none."""
    ASKED.clear()
    inner = FastAPI()

    @inner.get("/keys")
    async def held(request: Request) -> dict[str, Any]:
        found = await member_permissions(request.scope["user"])
        return {"tenant": current_tenant(), "keys": sorted(found)}

    with override_settings(
        TENANCY={"HEADER": "X-Tenant", "MEMBER_PERMISSIONS": f"{HERE}.keys"}
    ):
        client = TestClient(Users(TenantMiddleware(inner)))
        mine = client.get("/keys", headers={**as_("ann"), "X-Tenant": "acme"})
        asked = list(ASKED)
        theirs = client.get("/keys", headers={**as_("bob"), "X-Tenant": "acme"})

    assert mine.json() == {
        "tenant": "acme",
        "keys": ["notes.add_note", "notes.view_note"],
    }
    assert asked == [("ann", "acme")]
    assert (theirs.status_code, theirs.json()["code"]) == (403, "not_a_member")


def test_membership_is_said_one_way() -> None:
    """``MEMBERSHIP`` and ``MEMBER_PERMISSIONS`` together are refused."""
    with pytest.raises(ValidationError, match="not both"):
        TenancyConfig(MEMBERSHIP="a.b", MEMBER_PERMISSIONS="a.c")


async def test_the_membership_backend_grants_a_members_keys() -> None:
    """In the tenant, what the member holds; none on objects, nor for the inactive."""
    with override_settings(TENANCY={"MEMBER_PERMISSIONS": f"{HERE}.keys"}):
        backend = MembershipBackend()
        ann = PEOPLE["ann"]
        assert not await backend.has_perm(ann, "notes.view_note")
        with tenant("acme"):
            assert await backend.has_perm(ann, "notes.view_note")
            assert not await backend.has_perm(ann, "notes.delete_note")
            assert not await backend.has_perm(ann, "notes.view_note", object())
            assert not await backend.has_perm(PEOPLE["idle"], "notes.view_note")
        assert await backend.object_grants(ann) == frozenset[ObjectGrant]()


def test_the_membership_backend_needs_member_permissions() -> None:
    """Listed without ``MEMBER_PERMISSIONS``, it would grant nothing: refused."""
    for tenancy in (None, {"MEMBERSHIP": f"{HERE}.belongs"}):
        with (
            override_settings(TENANCY=tenancy),
            pytest.raises(ImproperlyConfigured, match="MEMBER_PERMISSIONS"),
        ):
            MembershipBackend()


def test_roles_the_project_stores_answer_has_perm(api: Api) -> None:
    """Memberships in the project's table, a role each, decide its routes.

    ``MembershipBackend`` grants a role's keys in the path's tenant, so
    ``HasPermissions`` reads them: an owner changes, a viewer only reads,
    one who belongs elsewhere gets the 404, and no one is asked to write a
    backend of their own. On SQLite, and on PostgreSQL where it is set.
    """
    ann, bob, cy = (create_user(name) for name in ("ann", "bob", "cy"))
    with override_settings(
        ROOT_ROUTER="domainkit_tests.sample_tenant_orgs.router",
        TENANCY={
            "MEMBER_PERMISSIONS": "domainkit_tests.sample_tenant_orgs.role_permissions"
        },
        AUTH={
            **API.settings["AUTH"],
            "BACKENDS": [
                "domainkit.contrib.auth.backends.ModelBackend",
                "domainkit.contrib.auth.backends.MembershipBackend",
            ],
        },
    ):
        client = api()

        async def seed() -> None:
            async with connections.engine().begin() as connection:
                await connection.run_sync(
                    sample_tenant_orgs.MEMBERS.metadata.create_all
                )
                await connection.execute(
                    insert(sample_tenant_orgs.MEMBERS),
                    [
                        {"user_id": ann, "org": "acme", "role": "owner"},
                        {"user_id": bob, "org": "acme", "role": "viewer"},
                        {"user_id": cy, "org": "globex", "role": "owner"},
                    ],
                )

        run_async(seed())
        owner, viewer, outsider = (token_for(name) for name in ("ann", "bob", "cy"))
        answers = {
            "owner reads": client.get("/orgs/acme/products", headers=owner),
            "owner changes": client.patch("/orgs/acme/products", headers=owner),
            "viewer reads": client.get("/orgs/acme/products", headers=viewer),
            "viewer changes": client.patch("/orgs/acme/products", headers=viewer),
            "outsider reads": client.get("/orgs/acme/products", headers=outsider),
            "no such org": client.get("/orgs/initech/products", headers=outsider),
            "in their own": client.get("/orgs/globex/products", headers=outsider),
            "anonymous": client.get("/orgs/acme/products"),
        }

    assert {name: answer.status_code for name, answer in answers.items()} == {
        "owner reads": 200,
        "owner changes": 200,
        "viewer reads": 200,
        "viewer changes": 403,
        "outsider reads": 404,
        "no such org": 404,
        "in their own": 200,
        "anonymous": 401,
    }
    assert answers["owner changes"].json() == {"tenant": "acme", "changed": True}
    assert body(answers["outsider reads"]) == body(answers["no such org"])
