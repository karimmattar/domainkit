"""A resource whose items are tenants; members' rules; limits across tenants.

A project reported (D3) three things that kept it on memberships of its
own after 2.4 took the tenant from the path (ADR 0150): ``PathTenant``
did not fit a ``ResourceRouter`` whose list has no tenant in its path,
``MembershipBackend``'s keys were counted with a user's global grants, so
staff who are members acted in a tenant as staff, and a limit inside a
tenant's routes counted per tenant only (ADR 0170).
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Annotated, Any, Final
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.testclient import WebSocketDenialResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.conf import override_settings
from domainkit.contrib.auth import AnonymousUser, AnyUser
from domainkit.ddd.query import CriteriaLike
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    AnonymousPrincipal,
    BasePermission,
    HasPermissions,
    IsAuthenticated,
    Public,
    Require,
)
from domainkit.http.application import application_problems
from domainkit.http.errors import install_exception_handlers
from domainkit.http.ratelimit import MemoryBackend, Throttle
from domainkit.resources import (
    HasMemberModelPermission,
    MemberActions,
    ModelActions,
    ResourceRouter,
)
from domainkit.tenancy import (
    HasMemberPermissions,
    InTenant,
    PathTenant,
    current_tenant,
    tenant,
)
from domainkit_tests.conftest import UseAuth
from domainkit_tests.gadgets import (
    FakeGadgetsUnitOfWork,
    Gadget,
    InMemoryGadgets,
    make_gadget,
)
from domainkit_tests.test_auth_access import user
from domainkit_tests.test_resources import (
    GADGETS,
    GadgetsLabelled,
    MakeGadget,
    MakeIn,
    RenameGadget,
    RenameIn,
    Users,
    labelled,
    make,
    rename,
)

pytestmark = pytest.mark.anyio

HERE: Final = "domainkit_tests.test_tenant_resources"
BACKENDS: Final = [
    "domainkit.contrib.auth.backends.ModelBackend",
    "domainkit.contrib.auth.backends.MembershipBackend",
]
VIEWER: Final = frozenset({"shop.view_gadget"})
CHANGER: Final = frozenset({"shop.view_gadget", "shop.change_gadget"})
MEMBERS: dict[tuple[str, str], frozenset[str]] = {}
"""``(username, tenant)``: the keys of each membership."""
SERVED: list[str | None] = []
"""The tenant each request reached the resource's scope as."""


def keys(user: Any, tenant_id: str) -> frozenset[str] | None:  # noqa: ANN401
    """``MEMBER_PERMISSIONS``: the keys a member holds in ``tenant_id``, or None."""
    return MEMBERS.get((getattr(user, "username", ""), tenant_id))


def belongs(user: Any, tenant_id: str) -> bool:  # noqa: ANN401
    """``MEMBERSHIP``: whether ``user`` belongs to ``tenant_id``."""
    return keys(user, tenant_id) is not None


async def served(request: HTTPConnection) -> CriteriaLike | None:
    """A scope that notes the tenant the request is served as, and narrows nothing."""
    SERVED.append(current_tenant())
    return None


def people() -> dict[str, AnyUser]:
    """Who ``X-User`` names: members, a staff member, a superuser, an inactive one."""
    return {
        "ann": user(username="ann"),
        "bob": user(username="bob"),
        "officer": user(
            "shop.view_gadget",
            "shop.change_gadget",
            username="officer",
            is_staff=True,
        ),
        "root": user(username="root", is_superuser=True),
        "idle": user(username="idle", is_active=False),
    }


@pytest.fixture
def store() -> Iterator[InMemoryGadgets]:
    """Gadgets a and b, the tenants: ann owns a; bob views b; officer views a.

    The superuser views a; ann views b; the inactive user owns a.
    """
    found = InMemoryGadgets([make_gadget("a"), make_gadget("b")])
    a, b = (str(gadget.id) for gadget in found.items.values())
    MEMBERS.clear()
    MEMBERS.update(
        {
            ("ann", a): CHANGER,
            ("ann", b): VIEWER,
            ("bob", b): VIEWER,
            ("officer", a): VIEWER,
            ("root", a): VIEWER,
            ("idle", a): CHANGER,
        }
    )
    SERVED.clear()
    yield found
    MEMBERS.clear()


@pytest.fixture
def members(use_auth: UseAuth) -> None:
    """Auth with ``MembershipBackend``, and members' keys from ``keys``."""
    use_auth(
        BACKENDS=BACKENDS,
        settings={"TENANCY": {"MEMBER_PERMISSIONS": f"{HERE}.keys"}},
    )


def ids(store: InMemoryGadgets) -> dict[str, UUID]:
    """Gadget ids by name."""
    return {gadget.name: gadget.id for gadget in store.items.values()}


def companies(store: InMemoryGadgets, **options: Any) -> ResourceRouter:  # noqa: ANN401
    """Gadgets as tenants: each one's routes are served as it, by its id."""
    options.setdefault("rule", MemberActions(GADGETS, outside=IsAuthenticated()))
    options.setdefault("tenant", PathTenant("gadget_id"))
    resource = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        scope=served,
        **options,
    )
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    return resource


def client_of(*routers: APIRouter) -> TestClient:
    """A client of an application serving ``routers``, behind ``Users``."""
    app = FastAPI()
    install_exception_handlers(app)
    for router in routers:
        app.include_router(router)
    app.add_middleware(Users, users=people())
    return TestClient(app)


def as_(name: str, **headers: str) -> dict[str, str]:
    """Headers that send a request as ``name``."""
    return {"X-User": name, **headers}


def test_a_resource_serves_each_item_as_the_tenant_its_id_names(
    store: InMemoryGadgets, members: None
) -> None:
    """``tenant=``: the item routes are the tenant's; the list is no tenant's.

    The list takes the rules ``outside`` gives; one who does not belong to
    an item's tenant gets the 404 of a route that does not exist, and an
    anonymous caller 401, as ``PathTenant`` answers on any router.
    """
    client = client_of(companies(store))
    a, b = ids(store)["a"], ids(store)["b"]

    listed = client.get("/gadgets", headers=as_("bob"))
    after_list = list(SERVED)
    read = client.get(f"/gadgets/{a}", headers=as_("ann"))
    renamed = client.post(
        f"/gadgets/{a}/rename", json={"name": "a2"}, headers=as_("ann")
    )
    stranger = client.get(f"/gadgets/{a}", headers=as_("bob"))
    nowhere = client.get("/no/such/route", headers=as_("bob"))
    anonymous = [client.get(path) for path in ("/gadgets", f"/gadgets/{b}")]

    assert listed.status_code == 200
    assert {item["name"] for item in listed.json()["items"]} == {"a", "b"}
    assert after_list == [None]
    assert read.status_code == 200
    assert renamed.json()["name"] == "a2"
    assert SERVED[1:] == [str(a), str(a)]
    assert stranger.status_code == 404
    assert stranger.json()["detail"] == nowhere.json()["detail"] == "Not Found"
    assert [answer.status_code for answer in anonymous] == [401, 401]


def test_members_act_in_a_tenant_with_their_keys_alone(
    store: InMemoryGadgets, members: None
) -> None:
    """``MemberActions`` counts a member's keys there, not global grants.

    The officer, staff granted ``change`` everywhere, is a viewer of a:
    ``ModelActions`` (as ``HasPermissions``) lets them change a, since
    ``MembershipBackend``'s keys join ``ModelBackend``'s grants;
    ``MemberActions`` does not. Nor does it count a superuser's status, or
    an inactive member's keys. A member of b who is not of a still gets
    the 404, whatever they hold.
    """
    a = ids(store)["a"]
    by_members = client_of(companies(store))
    by_grants = client_of(companies(store, rule=ModelActions(GADGETS)))

    def renames(client: TestClient, name: str) -> int:
        path = f"/gadgets/{a}/rename"
        return client.post(path, json={"name": "x"}, headers=as_(name)).status_code

    assert renames(by_grants, "officer") == 200
    assert {name: renames(by_members, name) for name in people()} == {
        "ann": 200,
        "bob": 404,
        "officer": 403,
        "root": 403,
        "idle": 403,
    }
    assert by_members.get(f"/gadgets/{a}", headers=as_("officer")).status_code == 200
    assert by_members.get(f"/gadgets/{a}", headers=as_("idle")).status_code == 403


def test_member_actions_refuse_routes_served_as_no_tenant_without_outside(
    store: InMemoryGadgets, members: None
) -> None:
    """Without ``outside``, the list, which serves no tenant, is refused."""
    client = client_of(companies(store, rule=MemberActions(GADGETS)))
    a = ids(store)["a"]

    assert client.get("/gadgets", headers=as_("ann")).status_code == 403
    assert client.get(f"/gadgets/{a}", headers=as_("ann")).status_code == 200


def test_a_tenant_id_in_the_path_has_one_spelling(
    store: InMemoryGadgets, members: None
) -> None:
    """The id is read as the resource's: a UUID in capitals is the same tenant.

    A value that is no UUID is refused with 422, as the route refuses it,
    for members and strangers alike.
    """
    client = client_of(companies(store))
    a = str(ids(store)["a"])

    shouting = client.get(f"/gadgets/{a.upper()}", headers=as_("ann"))
    malformed = [
        client.get("/gadgets/not-a-uuid", headers=as_(name)) for name in ("ann", "bob")
    ]

    assert shouting.status_code == 200
    assert [a] == SERVED
    assert [answer.status_code for answer in malformed] == [422, 422]


def test_openapi_documents_a_tenant_resources_id_once(
    store: InMemoryGadgets, members: None
) -> None:
    """The item's path parameter is a UUID, once; its routes may answer 401 and 404.

    So may those of a public resource whose items are tenants: ``PathTenant``
    still asks who belongs.
    """
    paths = client_of(companies(store)).get("/openapi.json").json()["paths"]
    read = paths["/gadgets/{gadget_id}"]["get"]
    public = companies(store, rule=None, dependencies=[Public])
    opened = client_of(public).get("/openapi.json").json()["paths"]

    def in_path(operation: dict[str, Any]) -> list[tuple[str, str | None]]:
        return [
            (each["name"], each["schema"].get("format"))
            for each in operation.get("parameters", [])
            if each["in"] == "path"
        ]

    assert in_path(read) == [("gadget_id", "uuid")]
    assert {"401", "404"} <= set(read["responses"])
    assert in_path(paths["/gadgets"]["get"]) == []
    assert {"401", "404"} <= set(opened["/gadgets/{gadget_id}"]["get"]["responses"])
    assert "401" not in opened["/gadgets"]["get"]["responses"]


def test_check_finds_no_problem_in_a_tenant_resource(
    store: InMemoryGadgets, members: None
) -> None:
    """``tenant=`` puts ``PathTenant`` before the rules, on the routes that hold it.

    On the router itself, as 2.4 offered, it would refuse the list
    (``tenancy.E001``): ``/gadgets`` has no ``{gadget_id}``.
    """
    on_the_router = ResourceRouter(
        "/gadgets",
        model=Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        rule=ModelActions(GADGETS),
        dependencies=[Depends(PathTenant("gadget_id"))],
        name_prefix="old_",
    )
    app = FastAPI()
    app.include_router(companies(store))
    old = FastAPI()
    old.include_router(on_the_router)

    def tenancy(served: FastAPI) -> list[tuple[str, str]]:
        return [
            (getattr(problem, "id", ""), problem.split(" ")[1])
            for problem in application_problems(served)
            if getattr(problem, "id", "").startswith("tenancy.")
        ]

    assert tenancy(app) == []
    assert tenancy(old) == [("tenancy.E001", "/gadgets")]


def test_a_nested_resource_takes_its_parents_tenant(
    store: InMemoryGadgets, members: None
) -> None:
    """Every route under ``/gadgets/{gadget_id}`` is the gadget's, its list too.

    A grandchild reads the gadget's id as a UUID still, its own id being
    another.
    """
    parent = companies(store)

    def nested(prefix: str, basename: str, under: ResourceRouter) -> ResourceRouter:
        return ResourceRouter(
            prefix,
            model=Gadget,
            basename=basename,
            unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
            repository=lambda uow: uow.gadgets,
            parent=under,
            parent_field="id",
            rule=MemberActions(GADGETS),
            scope=served,
            id_type=str,
        )

    child = nested("/twins", "twin", parent)
    grandchild = nested("/clones", "clone", child)
    client = client_of(parent, child)
    a = ids(store)["a"]

    assert child.tenant is grandchild.tenant is parent.tenant
    mine = client.get(f"/gadgets/{a}/twins", headers=as_("ann"))
    theirs = client.get(f"/gadgets/{a}/twins", headers=as_("bob"))
    assert mine.status_code == 200
    assert [item["name"] for item in mine.json()["items"]] == ["a"]
    assert theirs.status_code == 404
    assert str(a) in SERVED


def test_routes_of_ones_own_on_the_resource_are_the_tenants_too(
    store: InMemoryGadgets, members: None
) -> None:
    """A route or a WebSocket added to the resource, by its path, is served as it.

    So a stranger gets the 404, and an anonymous handshake the 401; one
    whose path names no gadget serves no tenant.
    """
    resource = companies(store)

    @resource.get("/{gadget_id}/label", dependencies=[Depends(Require())])
    async def label(gadget_id: UUID) -> dict[str, str | None]:
        return {"tenant": current_tenant()}

    @resource.websocket("/{gadget_id}/live")
    async def live(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"tenant": current_tenant()})
        await socket.close()

    @resource.websocket("/feed")
    async def feed(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json({"tenant": current_tenant()})
        await socket.close()

    client = client_of(resource)
    a = str(ids(store)["a"])

    with client.websocket_connect("/gadgets/feed") as socket:
        assert socket.receive_json() == {"tenant": None}
    assert client.get(f"/gadgets/{a}/label", headers=as_("ann")).json() == {"tenant": a}
    assert client.get(f"/gadgets/{a}/label", headers=as_("bob")).status_code == 404
    with (  # ``Users`` names HTTP callers only: the handshake is anonymous.
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"/gadgets/{a}/live"),
    ):
        pass
    assert denied.value.status_code == 401


def test_a_tenant_in_the_prefix_serves_every_route(
    store: InMemoryGadgets, members: None
) -> None:
    """``/orgs/{org_id}/gadgets``: the list and the items are the org's."""
    MEMBERS[("ann", "acme")] = VIEWER
    resource = ResourceRouter(
        "/orgs/{org_id}/gadgets",
        model=Gadget,
        unit_of_work=lambda: FakeGadgetsUnitOfWork(store),
        repository=lambda uow: uow.gadgets,
        rule=MemberActions(GADGETS),
        tenant=PathTenant("org_id"),
        scope=served,
    )
    client = client_of(resource)
    a = ids(store)["a"]

    assert client.get("/orgs/acme/gadgets", headers=as_("ann")).status_code == 200
    assert client.get(f"/orgs/acme/gadgets/{a}", headers=as_("ann")).status_code == 200
    assert client.get("/orgs/acme/gadgets", headers=as_("bob")).status_code == 404
    assert SERVED == ["acme", "acme"]


def test_a_tenant_must_be_one_a_route_names(store: InMemoryGadgets) -> None:
    """A parameter no route's path holds, or rules for no tenant without one."""
    with pytest.raises(TypeError, match=r"No route of this resource has \{org_id\}"):
        companies(store, tenant=PathTenant("org_id"))
    with pytest.raises(TypeError, match="serve no tenant"):
        companies(store, tenant=None)


def test_a_batch_whose_items_name_tenants_is_refused(store: InMemoryGadgets) -> None:
    """Its items name tenants it is not served as: no member's rule holds.

    Under ``outside=IsAuthenticated()`` it would let any signed-in caller
    rename every company. A batch of creations names none, and stays.
    """
    resource = companies(store)

    with pytest.raises(TypeError, match=r"names a tenant per item \(gadget_id\)"):
        resource.batch_route(RenameGadget, service=rename, body=RenameIn)
    resource.batch_route(MakeGadget, service=make, body=MakeIn, path="/batch")


def test_outside_rules_only_viewing_and_making_on_the_collection(
    store: InMemoryGadgets, members: None
) -> None:
    """A rule as ``outside`` lists and creates; a change there is refused.

    ``Actions`` as ``outside`` name each action's rules themselves: the
    officer, who may change gadgets everywhere, merges; bob may not.
    """

    def merging(rule: MemberActions) -> TestClient:
        resource = companies(store, rule=rule)
        resource.command_route(
            MakeGadget, service=make, body=MakeIn, path="/merge", action="change"
        )
        resource.command_route(MakeGadget, service=make, body=MakeIn, name="found")
        return client_of(resource)

    plain = merging(MemberActions(GADGETS, outside=IsAuthenticated()))
    explicit = merging(MemberActions(GADGETS, outside=ModelActions(GADGETS)))
    body = {"name": "m", "price": "1.00"}

    def sent(client: TestClient, path: str, name: str) -> int:
        return client.post(path, json=body, headers=as_(name)).status_code

    assert sent(plain, "/gadgets/merge", "bob") == 403
    assert sent(plain, "/gadgets/merge", "officer") == 403
    assert sent(plain, "/gadgets", "bob") == 201
    assert sent(explicit, "/gadgets/merge", "officer") == 201
    assert sent(explicit, "/gadgets/merge", "bob") == 403


class AnyObject(BasePermission):
    """Signed-in callers, on every object: a rule that looks at objects."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """A caller who proved who they are."""
        return bool(user.is_authenticated)

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Any object."""
        return True


def test_outside_rules_ask_objects_on_the_collection(
    store: InMemoryGadgets, members: None
) -> None:
    """A query's items on the collection are asked of ``outside``'s object rule.

    ``~InTenant()`` refused every object, so every item was left out.
    """
    resource = companies(store, rule=MemberActions(GADGETS, outside=AnyObject()))
    resource.query_route(GadgetsLabelled, handler=labelled, path="/labelled")
    client = client_of(resource)

    found = client.get("/gadgets/labelled", params={"label": "A"}, headers=as_("bob"))

    assert [item["name"] for item in found.json()["items"]] == ["a"]


async def test_in_tenant_answers_for_objects_as_for_the_request() -> None:
    """``~InTenant()`` allows an object served as no tenant, and no other."""
    rule = ~InTenant()
    ann = user(username="ann")

    assert await rule.has_object_permission(connection(), ann, object())
    with tenant("acme"):
        assert not await rule.has_object_permission(connection(), ann, object())


def test_scoped_credentials_reach_a_members_routes(
    store: InMemoryGadgets, members: None
) -> None:
    """Credentials limited to the key read a member's item; the collection is closed.

    ``outside=IsAuthenticated()`` checks no scopes, so scoped credentials
    stay off the list; the member's rule checks them on the item.
    """
    client = client_of(companies(store))
    a = ids(store)["a"]
    scoped = as_("ann", **{"X-Scopes": "shop.view_gadget"})

    assert client.get(f"/gadgets/{a}", headers=scoped).status_code == 200
    assert client.get("/gadgets", headers=scoped).status_code == 403
    assert client.get("/gadgets", headers=as_("ann")).status_code == 200


def test_a_nested_resource_keeps_its_parents_tenant(store: InMemoryGadgets) -> None:
    """A tenant of its own would serve ``/gadgets/{a}/twins`` as none.

    Bob, a stranger to a, would then list a's twins and add one. Under a
    parent without a tenant, a resource may have its own.
    """
    options: dict[str, Any] = {
        "model": Gadget,
        "basename": "twin",
        "unit_of_work": lambda: FakeGadgetsUnitOfWork(store),
        "repository": lambda uow: uow.gadgets,
        "parent_field": "id",
        "rule": MemberActions(GADGETS, outside=IsAuthenticated()),
        "tenant": PathTenant("twin_id"),
        "id_type": str,
    }
    with pytest.raises(TypeError, match=r"nested under the tenant \{gadget_id\}"):
        ResourceRouter("/twins", parent=companies(store), **options)
    plain = companies(store, tenant=None, rule=ModelActions(GADGETS))
    assert ResourceRouter("/twins", parent=plain, **options).tenant is not None


def test_path_tenants_read_their_id_as_text_an_int_or_a_uuid() -> None:
    """Other types are refused when the dependency is made."""
    assert PathTenant("n", id_type=int).id_type is int
    with pytest.raises(ImproperlyConfigured, match="text, an int, or a UUID"):
        PathTenant("n", id_type=float)


def connection(scopes: frozenset[str] | None = None) -> HTTPConnection:
    """A request, with credentials limited to ``scopes`` if given."""
    scope: dict[str, Any] = {"type": "http", "headers": []}
    if scopes is not None:
        scope["auth"] = SimpleNamespace(scopes=scopes)
    return HTTPConnection(scope)


async def test_member_rules_count_the_keys_of_the_tenant_served(
    members: None,
) -> None:
    """In a tenant, the member's keys; outside one, nothing; scopes bound them."""
    ann = user(username="ann")
    MEMBERS[("ann", "acme")] = VIEWER
    rule = HasMemberPermissions("shop.view_gadget")
    model_rule = HasMemberModelPermission("view", GADGETS)

    assert not await rule.has_permission(connection(), ann)
    assert not await InTenant().has_permission(connection(), ann)
    with tenant("acme"):
        assert await rule.has_permission(connection(), ann)
        assert await model_rule.has_permission(connection(), ann)
        assert await InTenant().has_permission(connection(), ann)
        assert not await HasMemberPermissions(
            "shop.view_gadget", "shop.change_gadget"
        ).has_permission(connection(), ann)
        assert await rule.has_permission(connection(frozenset(VIEWER)), ann)
        assert not await rule.has_permission(connection(frozenset()), ann)
        assert not await rule.has_permission(connection(), AnonymousUser())
        assert not await rule.has_permission(connection(), AnonymousPrincipal())
    assert rule.checks_scopes()


async def test_member_rules_need_members_keys(use_auth: UseAuth) -> None:
    """Without ``MEMBER_PERMISSIONS`` no member holds a key: a misconfiguration."""
    with pytest.raises(ImproperlyConfigured, match="at least one"):
        HasMemberPermissions()
    for tenancy in (None, {"MEMBERSHIP": f"{HERE}.belongs"}):
        with (
            override_settings(TENANCY=tenancy),
            tenant("acme"),
            pytest.raises(ImproperlyConfigured, match="MEMBER_PERMISSIONS"),
        ):
            await HasMemberPermissions("a.b").has_permission(
                connection(), user(username="ann")
            )


def test_plain_routes_take_member_rules_too(members: None) -> None:
    """A router of 2.4's kind: ``HasPermissions`` counts staff grants, members' not.

    The officer views acme as a member and changes everywhere as staff.
    """
    MEMBERS[("officer", "acme")] = VIEWER
    router = APIRouter(
        prefix="/orgs/{org_id}", dependencies=[Depends(PathTenant("org_id"))]
    )

    @router.post(
        "/staff", dependencies=[Depends(Require(HasPermissions("shop.change_gadget")))]
    )
    async def as_staff() -> None: ...

    @router.post(
        "/member",
        dependencies=[Depends(Require(HasMemberPermissions("shop.change_gadget")))],
    )
    async def as_member() -> None: ...

    client = client_of(router)
    assert client.post("/orgs/acme/staff", headers=as_("officer")).status_code == 200
    assert client.post("/orgs/acme/member", headers=as_("officer")).status_code == 403


class Named:
    """Middleware: the person ``X-User`` names, by name only (anonymous if none)."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Name the user, then serve."""
        name = Headers(scope=scope).get("x-user", "")
        scope["user"] = (
            SimpleNamespace(username=name, is_authenticated=True)
            if name
            else AnonymousPrincipal()
        )
        await self.app(scope, receive, send)


def by_name(connection: HTTPConnection) -> str:
    """A throttle's key: the caller's name."""
    return str(connection.scope["user"].username)


@pytest.fixture
def limited() -> Iterator[TestClient]:
    """Two routes of an organisation, throttled per tenant and across tenants."""
    backend = MemoryBackend()
    org = APIRouter(
        prefix="/orgs/{org_id}", dependencies=[Depends(PathTenant("org_id"))]
    )
    each = Throttle("2/day", key=by_name, scope="notes", backend=backend)
    every = Throttle(
        "2/day", key=by_name, scope="invites", backend=backend, per_tenant=False
    )

    @org.post("/notes", dependencies=[Depends(each)])
    async def note(request: Request) -> dict[str, str | None]:
        return {"tenant": current_tenant()}

    @org.post("/invitations", dependencies=[Depends(every)])
    async def invite(org_id: Annotated[str, Depends(PathTenant("org_id"))]) -> str:
        return org_id

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(org)
    app.add_middleware(Named)
    MEMBERS.update({("ann", "acme"): VIEWER, ("ann", "globex"): VIEWER})
    with override_settings(TENANCY={"MEMBERSHIP": f"{HERE}.belongs"}):
        yield TestClient(app)
    MEMBERS.clear()


def test_a_limit_may_hold_across_tenants(limited: TestClient) -> None:
    """``per_tenant=False``: a member of two tenants gets the rate once, not twice.

    By default each tenant counts on its own, as every key is the tenant's.
    """

    def sent(path: str) -> list[int]:
        return [
            limited.post(f"/orgs/{org}/{path}", headers=as_("ann")).status_code
            for org in ("acme", "globex", "acme", "globex")
        ]

    assert sent("notes") == [200, 200, 200, 200]
    assert sent("invitations") == [200, 200, 429, 429]
    other = limited.post("/orgs/acme/invitations", headers=as_("bob"))
    assert other.status_code == 404  # Bob is no member: the tenant comes first.
