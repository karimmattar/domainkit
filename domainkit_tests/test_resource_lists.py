"""Lists that name their rows' people: ids, a page's presenter, search by reference.

A project reported (D2) that a list could not show what another domain
holds without a request per row: the admin API's user list took no ids,
``present`` is called per aggregate and cannot wait, and a ``FilterSet``
searched only the aggregate's own fields, so a member could not be found
by their user's name (ADR 0170).
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, ClassVar, Final, NewType
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.ddd import AggregateRoot, InMemoryRepository, MaxLength, UnitOfWork
from domainkit.ddd.fields import UnknownField
from domainkit.http import Filter, FilterSet
from domainkit.http.access import IsAuthenticated
from domainkit.http.filters import Finder
from domainkit.resources import FieldPolicy, ResourceRouter
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.gadgets import Gadget, InMemoryGadgets, make_gadget
from domainkit_tests.support.auth import create_user
from domainkit_tests.test_auth_admin import ADMIN, staff
from domainkit_tests.test_resources import (
    Dearest,
    GadgetFilters,
    RenameGadget,
    RenameIn,
    as_,
    client_of,
    dearest,
    gadgets,
    rename,
)

pytestmark = pytest.mark.anyio


def test_the_admin_list_names_users_by_id(api: Api) -> None:
    """``?id=`` repeated: the users of a page of rows, in one request.

    Unknown ids are left out; ``search`` narrows them further; more ids
    than a page holds (200) are refused.
    """
    client = api()
    headers = staff("admin", "auth.view_user")
    ann, bob, cy = (create_user(name) for name in ("ann", "bob", "cy"))
    url = f"{ADMIN}/users"

    def named(**params: Any) -> list[str]:  # noqa: ANN401 - query parameters.
        answer = client.get(url, params=params, headers=headers)
        assert answer.status_code == 200, answer.text
        return [each["username"] for each in answer.json()["items"]]

    assert named(id=[str(cy), str(ann), str(uuid4()), str(ann)]) == ["ann", "cy"]
    assert named(id=[str(ann), str(bob)], search="B") == ["bob"]
    assert named(id=str(bob)) == ["bob"]
    too_many = client.get(
        url, params={"id": [str(uuid4()) for _ in range(201)]}, headers=headers
    )
    assert too_many.status_code == 422
    assert client.get(url, params={"id": "x"}, headers=headers).status_code == 422


class Shown(BaseModel):
    """A gadget, and who owns it: what another domain holds."""

    id: UUID
    name: str
    owner: str


OWNERS: Final = {"a": "Ann", "b": "Bob"}
"""Another domain's people, by the gadget they own."""
ASKED: list[tuple[str, ...]] = []
"""The gadgets each call of ``owned`` was given, by name."""


async def owned(found: Sequence[Gadget], request: HTTPConnection) -> list[Shown]:
    """``present_many``: one load of the owners of every gadget shown."""
    assert request.headers["x-user"]
    ASKED.append(tuple(each.name for each in found))
    return [
        Shown(id=each.id, name=each.name, owner=OWNERS.get(each.name, "-"))
        for each in found
    ]


@pytest.fixture
def store() -> InMemoryGadgets:
    """Gadgets a, b, and c (the dearest)."""
    ASKED.clear()
    return InMemoryGadgets(
        [
            make_gadget("a", price="1.00"),
            make_gadget("b", price="2.00"),
            make_gadget("c", price="3.00"),
        ]
    )


def test_present_many_shows_each_answer_in_one_call(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """A page, an export, a query's items, a read, and a write: one call each.

    What it loads for the page is shown with each item, and the read's
    ``ETag`` covers it.
    """
    use_auth()
    resource = gadgets(
        store,
        schema=Shown,
        present_many=owned,
        filters=GadgetFilters,
        exports=("csv",),
        links=True,
    )
    resource.query_route(Dearest, handler=dearest, path="/dearest")
    resource.command_route(RenameGadget, service=rename, body=RenameIn, path="/rename")
    client = client_of(resource)
    viewer = as_("viewer")

    page = client.get("/gadgets", headers=viewer).json()["items"]
    exported = client.get("/gadgets", headers={**viewer, "Accept": "text/csv"})
    a = page[0]["id"]
    read = client.get(f"/gadgets/{a}", headers=viewer)
    dearest_one = client.get("/gadgets/dearest", headers=viewer)
    renamed = client.post(
        f"/gadgets/{a}/rename", json={"name": "z"}, headers=as_("changer")
    )

    assert [(each["name"], each["owner"]) for each in page] == [
        ("a", "Ann"),
        ("b", "Bob"),
        ("c", "-"),
    ]
    assert page[0]["links"]["self"] == f"/gadgets/{a}"
    assert exported.text.splitlines()[1].endswith(",a,Ann")
    assert read.json()["owner"] == "Ann"
    assert read.headers["ETag"]
    assert dearest_one.json()["owner"] == "-"
    assert renamed.json() == {
        "id": a,
        "name": "z",
        "owner": "-",
        "links": {"self": f"/gadgets/{a}"},
    }
    assert ASKED == [("a", "b", "c"), ("a", "b", "c"), ("a",), ("c",), ("z",)]


def test_present_many_is_not_asked_for_an_empty_page(use_auth: UseAuth) -> None:
    """Nothing to show, no call."""
    use_auth()
    ASKED.clear()
    client = client_of(gadgets(InMemoryGadgets(), schema=Shown, present_many=owned))

    assert client.get("/gadgets", headers=as_("viewer")).json()["items"] == []
    assert ASKED == []


async def one_short(found: Sequence[Gadget], request: HTTPConnection) -> list[Shown]:
    """A ``present_many`` that loses an item."""
    return [Shown(id=each.id, name=each.name, owner="-") for each in found][1:]


def test_present_many_answers_one_schema_per_aggregate(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """One short is a bug of the project's: refused, never shown misaligned."""
    use_auth()
    client = client_of(gadgets(store, schema=Shown, present_many=one_short))

    with pytest.raises(RuntimeError, match="answered 2 for 3 aggregates"):
        client.get("/gadgets", headers=as_("viewer"))
    with pytest.raises(TypeError, match="not both"):
        gadgets(store, schema=Shown, present=Shown.model_validate, present_many=owned)


MembershipId = NewType("MembershipId", UUID)


class Membership(AggregateRoot[MembershipId]):
    """A user's membership: a reference to the user, by id, and a role."""

    user_id: UUID
    role: Annotated[str, MaxLength(20)]


class Memberships(InMemoryRepository[Membership, MembershipId]):
    """Memberships, in memory."""


class MembershipsUnitOfWork(UnitOfWork):
    """Memberships in memory."""

    def __init__(self, memberships: Memberships) -> None:
        """Work on ``memberships``."""
        super().__init__()
        self.memberships = memberships

    async def _commit(self) -> None:
        """Nothing to write."""

    async def _rollback(self) -> None:
        """Nothing to undo."""


PEOPLE: Final = {uuid4(): name for name in ("Ann Smith", "Ann Jones", "Bob Smith")}
"""Another domain's users, by id."""
FOUND: list[tuple[str, int]] = []
"""Each word ``users_named`` was asked, and its limit."""


async def users_named(word: str, limit: int) -> list[UUID]:
    """The users whose name holds ``word``, at most ``limit``, as auth finds them."""
    FOUND.append((word, limit))
    return [
        user_id for user_id, name in PEOPLE.items() if word.lower() in name.lower()
    ][:limit]


def everyone_named(word: str, limit: int) -> Iterable[UUID]:
    """A finder that ignores the limit, sync."""
    return [*PEOPLE, *PEOPLE]


class MemberFilters(FilterSet):
    """Members, searched by role and by their user's name."""

    model = Membership
    role = Filter()
    search_fields = ("role",)
    search_through: ClassVar[Mapping[str, Finder]] = {"user_id": users_named}


def members_by(name: str) -> UUID:
    """The id of the user named ``name``."""
    return next(user_id for user_id, each in PEOPLE.items() if each == name)


@pytest.fixture
def memberships() -> Memberships:
    """Ann Smith an owner, Ann Jones and Bob Smith viewers."""
    FOUND.clear()
    return Memberships(
        [
            Membership(id=MembershipId(uuid4()), user_id=members_by(name), role=role)
            for name, role in (
                ("Ann Smith", "owner"),
                ("Ann Jones", "viewer"),
                ("Bob Smith", "viewer"),
            )
        ]
    )


def roles_found(client: TestClient, path: str, **params: Any) -> list[str]:  # noqa: ANN401
    """The members a listing answers, as ``role:name``."""
    answer = client.get(path, params=params, headers=as_("viewer"))
    assert answer.status_code == 200, answer.text
    return sorted(
        f"{each['role']}:{PEOPLE[UUID(each['user_id'])]}"
        for each in answer.json()["items"]
    )


def test_a_list_searches_through_what_a_field_refers_to(
    memberships: Memberships, use_auth: UseAuth
) -> None:
    """``search_through``: each word in the role, or in the member's user's name.

    The finder is asked once per distinct word, with the bound; its ids
    are kept with ``in`` on the field.
    """
    use_auth()
    resource = ResourceRouter(
        "/members",
        model=Membership,
        unit_of_work=lambda: MembershipsUnitOfWork(memberships),
        repository=lambda uow: uow.memberships,
        filters=MemberFilters,
        rule=IsAuthenticated(),
    )
    client = client_of(resource)

    assert roles_found(client, "/members", search="ann") == [
        "owner:Ann Smith",
        "viewer:Ann Jones",
    ]
    assert roles_found(client, "/members", search="smith ann") == ["owner:Ann Smith"]
    assert roles_found(client, "/members", search="owner smith") == ["owner:Ann Smith"]
    assert roles_found(client, "/members", search="SMITH", role="viewer") == [
        "viewer:Bob Smith"
    ]
    assert roles_found(client, "/members", search="nobody") == []
    assert FOUND == [
        ("ann", 100),
        ("smith", 100),
        ("ann", 100),
        ("owner", 100),
        ("smith", 100),
        ("SMITH", 100),
        ("nobody", 100),
    ]
    documented = client.get("/openapi.json").json()["paths"]["/members"]["get"]
    search = next(p for p in documented["parameters"] if p["name"] == "search")
    assert search["description"] == (
        "Words to find in role, what user_id refers to, ignoring case."
    )
    assert "_through" not in {p["name"] for p in documented["parameters"]}


async def test_searching_through_a_reference_is_bounded(
    memberships: Memberships,
) -> None:
    """At most ``max_reference_matches`` ids a word; a refused search asks nothing.

    By hand, without FastAPI, the set searches its own fields unless given
    what ``through`` found.
    """

    class Few(FilterSet):
        model = Membership
        search_through: ClassVar[Mapping[str, Finder]] = {"user_id": everyone_named}
        max_reference_matches = 2
        max_search_words = 2

    found = await Few.through("ann")
    refused = [await Few.through(each) for each in ("a b c", "x" * 101, None)]
    once = await MemberFilters.through("ann ann")

    assert found == {"ann": {"user_id": tuple(PEOPLE)[:2]}}
    assert refused == [{}, {}, {}]
    assert list(once) == ["ann"]
    assert FOUND == [("ann", 100)]
    assert await memberships.filter(Few(search="ann").query()) == []
    kept = await memberships.filter(Few(search="ann", _through=found).query())
    assert {each.user_id for each in kept} == set(tuple(PEOPLE)[:2])


def test_a_filter_set_used_as_a_dependency_searches_through_too(
    memberships: Memberships,
) -> None:
    """``Annotated[MemberFilters, Depends()]`` in a route of one's own."""
    app = FastAPI()

    @app.get("/members")
    async def listing(filters: Annotated[MemberFilters, Depends()]) -> list[str]:
        return sorted(
            PEOPLE[each.user_id] for each in await memberships.filter(filters.query())
        )

    client = TestClient(app)
    assert client.get("/members", params={"search": "jones"}).json() == ["Ann Jones"]


def test_search_through_names_fields_and_finders() -> None:
    """An unknown field, a finder that is no function, or no bound: refused."""
    with pytest.raises(UnknownField, match="no field 'owner_id'"):

        class Unknown(FilterSet):
            model = Membership
            search_through: ClassVar[Mapping[str, Finder]] = {"owner_id": users_named}

    with pytest.raises(TypeError, match="function of the word and a limit"):

        class NotAFunction(FilterSet):
            model = Membership
            search_through: ClassVar[Mapping[str, Finder]] = {"user_id": "users_named"}  # type: ignore[dict-item]

    with pytest.raises(TypeError, match="at least 1"):

        class Unbounded(FilterSet):
            model = Membership
            search_through: ClassVar[Mapping[str, Finder]] = {"user_id": users_named}
            max_reference_matches = 0


def test_a_resource_refuses_to_search_through_a_field_it_hides(
    memberships: Memberships,
) -> None:
    """Searching through ``user_id`` tells whom a hidden ``user_id`` names."""
    with pytest.raises(TypeError, match="user_id"):
        ResourceRouter(
            "/members",
            model=Membership,
            unit_of_work=lambda: MembershipsUnitOfWork(memberships),
            repository=lambda uow: uow.memberships,
            filters=MemberFilters,
            policy=FieldPolicy(visible={"user_id": "shop.view_member"}),
        )
