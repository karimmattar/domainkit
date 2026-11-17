"""Lists that name their rows' people: ids, a page's presenter, search by reference.

A project reported (D2) that a list could not show what another domain
holds without a request per row: the admin API's user list took no ids,
``present`` is called per aggregate and cannot wait, and a ``FilterSet``
searched only the aggregate's own fields, so a member could not be found
by their user's name (ADR 0170). Then (2.6) that a search of two common
words asked per word could miss a row holding both, and that the user
list answered each user whole to a page that shows a name (ADR 0177).
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Annotated, Any, ClassVar, Final, NewType
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from domainkit.contrib.auth import get_user_model
from domainkit.ddd import AggregateRoot, InMemoryRepository, MaxLength, UnitOfWork
from domainkit.ddd.fields import UnknownField
from domainkit.http import EveryWord, Filter, FilterSet
from domainkit.http.access import IsAuthenticated
from domainkit.http.errors import install_exception_handlers
from domainkit.http.filters import Finder
from domainkit.resources import FieldPolicy, ResourceRouter
from domainkit_tests.conftest import Api, UseAuth
from domainkit_tests.gadgets import Gadget, InMemoryGadgets, make_gadget
from domainkit_tests.support.auth import create_user, grant
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


async def backwards(found: Sequence[Gadget], request: HTTPConnection) -> list[Shown]:
    """A ``present_many`` that answers in the wrong order."""
    return [Shown(id=each.id, name=each.name, owner="-") for each in reversed(found)]


def test_present_many_answers_one_schema_per_aggregate(
    store: InMemoryGadgets, use_auth: UseAuth
) -> None:
    """One short, or out of order, is a bug of the project's: refused.

    Never shown misaligned: a schema's ``id`` must be its aggregate's, or
    links and tags would land on another row.
    """
    use_auth()
    client = client_of(gadgets(store, schema=Shown, present_many=one_short))
    turned = client_of(gadgets(store, schema=Shown, present_many=backwards))

    with pytest.raises(RuntimeError, match="answered 2 for 3 aggregates"):
        client.get("/gadgets", headers=as_("viewer"))
    with pytest.raises(RuntimeError, match="not in their order"):
        turned.get("/gadgets", headers=as_("viewer"))
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


def everyone_named(word: str, limit: int) -> Iterator[UUID]:
    """A finder that ignores the limit, sync: everyone, twice, then a failure.

    Read past what the bound needs, it fails the test.
    """
    yield from PEOPLE
    yield from PEOPLE
    raise AssertionError("read past the bound")


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

    Repeats are dropped as the ids come, so no more are read than the bound
    takes. A word asked in two cases is asked once. By hand, without
    FastAPI, the set searches its own fields unless given what ``through``
    found.
    """

    class Few(FilterSet):
        model = Membership
        search_through: ClassVar[Mapping[str, Finder]] = {"user_id": everyone_named}
        max_reference_matches = 2
        max_search_words = 2

    class Repeated(Few):
        search_through: ClassVar[Mapping[str, Finder]] = {
            "user_id": lambda word, limit: [first, first, second]
        }

    first, second, _ = PEOPLE
    found = await Few.through("ann")
    repeated = await Repeated.through("ann")
    refused = [await Few.through(each) for each in ("a b c", "x" * 101, None)]
    once = await MemberFilters.through("Ann ann ANN")

    assert found == {"ann": {"user_id": tuple(PEOPLE)[:2]}}
    assert repeated == {"ann": {"user_id": (first, second)}}
    assert refused == [{}, {}, {}]
    assert list(once) == ["ann"]
    assert FOUND == [("Ann", 100)]
    assert await memberships.filter(Few(search="ann").query()) == []
    kept = await memberships.filter(Few(search="ANN", _through=found).query())
    assert {each.user_id for each in kept} == set(tuple(PEOPLE)[:2])


class Tight(MemberFilters):
    """Members' filters, of two conditions at most."""

    max_conditions = 2


def test_finders_are_asked_only_of_a_query_the_set_accepts(
    memberships: Memberships,
) -> None:
    """An unknown lookup, or too many conditions: refused before any finder runs."""
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/members")
    async def listing(filters: Annotated[Tight, Depends()]) -> int:
        return len(await memberships.filter(filters.query()))

    client = TestClient(app)
    refused = [
        client.get("/members", params=params)
        for params in (
            {"search": "ann", "role__icontains": "own"},
            {"search": "ann smith", "role": "owner"},
            {"search": "ann", "role": ["owner", "viewer"]},
        )
    ]

    assert [answer.json()["code"] for answer in refused] == ["invalid_filter"] * 3
    assert FOUND == []
    assert client.get("/members", params={"search": "ann", "role": "owner"}).json() == 1
    assert FOUND == [("ann", 100)]


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


def test_the_admin_names_users_without_what_they_hold(api: Api) -> None:
    """``GET /users/names``: id, username, full name, identifiers; no grants.

    It takes ``search`` and ``id`` as ``GET /users`` does, answers in the
    same order with the same cursors, and needs the same permission. The
    identifiers are the fields of ``AUTH.IDENTIFIERS`` that ``/users``
    shows among ``fields`` (user report D2).
    """
    client = api(IDENTIFIERS=("username", "email"))
    headers = staff("admin", "auth.view_user")
    ann = create_user("ann", email="ann@example.com", first_name="Ann", last_name="Lee")
    bob = create_user("bob")
    grant("bob", "auth.view_user")
    url = f"{ADMIN}/users"

    def answer(path: str, **params: Any) -> Any:  # noqa: ANN401 - a body.
        found = client.get(path, params=params, headers=headers)
        assert found.status_code == 200, found.text
        return found.json()

    named = answer(f"{url}/names", id=[str(bob), str(ann)])
    whole = answer(url, id=[str(bob), str(ann)])
    first = answer(f"{url}/names", limit=1)
    after = answer(url, limit=1, cursor=first["next_cursor"])
    refused = client.get(f"{url}/names", headers=staff("helper", "auth.change_user"))

    assert named == {
        "items": [
            {
                "id": str(ann),
                "username": "ann",
                "name": "Ann Lee",
                "identifiers": {"email": "ann@example.com"},
            },
            {
                "id": str(bob),
                "username": "bob",
                "name": None,
                "identifiers": {"email": ""},
            },
        ],
        "next_cursor": None,
    }
    assert [each["id"] for each in whole["items"]] == [str(ann), str(bob)]
    assert whole["items"][1]["permissions"] == ["auth.view_user"]
    assert answer(f"{url}/names", search="BO")["items"][0]["id"] == str(bob)
    assert [each["username"] for each in first["items"]] == ["admin"]
    assert [each["username"] for each in after["items"]] == ["ann"]
    assert refused.status_code == 403


def test_the_names_show_no_field_the_user_list_hides(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project that shows staff no names gets none from ``/users/names`` either.

    ``EDITABLE_FIELDS`` without ``first_name`` and ``last_name`` hides them
    from ``/users``' ``fields``; the names view gives no ``name`` then, and
    its search, ``/users``' own, matches the username and the identifiers,
    never a hidden name (review of user report D2).
    """
    client = api(IDENTIFIERS=("username", "email"))
    headers = staff("admin", "auth.view_user")
    ann = create_user(
        "ann", email="ann@example.com", first_name="Ann", last_name="Secret"
    )
    model = get_user_model()
    monkeypatch.setattr(model, "EDITABLE_FIELDS", frozenset({"email"}))
    url = f"{ADMIN}/users"

    def answer(path: str, **params: Any) -> Any:  # noqa: ANN401 - a body.
        found = client.get(path, params=params, headers=headers)
        assert found.status_code == 200, found.text
        return found.json()

    (whole,) = answer(url, id=str(ann))["items"]
    (named,) = answer(f"{url}/names", id=str(ann))["items"]

    assert {"first_name", "last_name"} & set(whole["fields"]) == set()
    assert named == {
        "id": str(ann),
        "username": "ann",
        "name": None,
        "identifiers": {"email": "ann@example.com"},
    }
    assert answer(f"{url}/names", search="Secret")["items"] == []
    assert answer(url, search="Secret")["items"] == []


CROWD: Final = {
    uuid4(): name
    for name in (
        "Ada Byron",
        "Ada Smith",
        "Bob Lovelace",
        "Cy Lovelace",
        "Ada Lovelace",
    )
}
"""Another domain's users: three named Ada, three Lovelace, one both."""
WORDS_ASKED: list[tuple[tuple[str, ...], int]] = []
"""The words each finder of ``CROWD`` was asked, and its limit."""


def crowd_named(words: tuple[str, ...], limit: int) -> list[UUID]:
    """The users whose name holds every one of ``words``, at most ``limit``."""
    WORDS_ASKED.append((words, limit))
    return [
        user_id
        for user_id, name in CROWD.items()
        if all(word.lower() in name.lower() for word in words)
    ][:limit]


def crowd_word(word: str, limit: int) -> list[UUID]:
    """``crowd_named`` asked one word at a time, as a plain finder is."""
    return crowd_named((word,), limit)


async def test_every_word_finds_whom_common_words_name_together() -> None:
    """``EveryWord``: asked with every word too, a bound cuts no match away.

    "ada" and "lovelace" each name three users, past the bound of two:
    asked per word, the two answers share no one, and Ada Lovelace's
    membership is missed (user report D2). ``EveryWord`` asks with both
    words too, where one alone reached the bound, and finds her. A search
    whose words stay under the bound, and one word alone, are asked word
    by word only, and a row still matches its role and a user apart.
    """
    rows = Memberships(
        [
            Membership(
                id=MembershipId(uuid4()),
                user_id=user_id,
                role="owner" if name == "Ada Lovelace" else "viewer",
            )
            for user_id, name in CROWD.items()
        ]
    )

    class PerWord(FilterSet):
        model = Membership
        search_fields = ("role",)
        search_through: ClassVar[Mapping[str, Finder | EveryWord]] = {
            "user_id": crowd_word
        }
        max_reference_matches = 2

    class Together(PerWord):
        search_through: ClassVar[Mapping[str, Finder | EveryWord]] = {
            "user_id": EveryWord(crowd_named)
        }

    async def named(filters: type[FilterSet], search: str) -> list[str]:
        WORDS_ASKED.clear()
        found = await filters.through(search)
        kept = await rows.filter(filters(search=search, _through=found).query())
        return sorted(CROWD[each.user_id] for each in kept)

    assert await named(PerWord, "ada lovelace") == []
    assert await named(Together, "Ada LOVELACE ada") == ["Ada Lovelace"]
    assert WORDS_ASKED == [(("Ada",), 2), (("LOVELACE",), 2), (("Ada", "LOVELACE"), 2)]
    assert list(await Together.through("Ada LOVELACE ada")) == [
        "ada",
        "lovelace",
        "ada lovelace",
    ]
    assert await named(Together, "ada bob") == []
    assert WORDS_ASKED[-1] == (("ada", "bob"), 2)
    assert await named(Together, "viewer byron") == ["Ada Byron"]
    assert WORDS_ASKED == [(("viewer",), 2), (("byron",), 2)]
    assert await named(Together, "ada") == ["Ada Byron", "Ada Smith"]
    assert WORDS_ASKED == [(("ada",), 2)]
    with pytest.raises(TypeError, match="function of the words and a limit"):
        EveryWord("crowd_named")  # type: ignore[arg-type]
