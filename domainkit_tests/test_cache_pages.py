"""``@cache_page``: whole GET answers in a cache, safely shared."""

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from datetime import timedelta
from typing import Annotated, Any, Final

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, Response, Security
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.authentication import AuthCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit import deadlines
from domainkit.cache import cache_page, caches
from domainkit.cache.core import COMPUTE_TASK
from domainkit.cache.pages import (
    REQUEST,
    RESPONSE,
    _matches,
    _route_holds,
    _signature,
    _user,
)
from domainkit.clock import freeze_time
from domainkit.conf import override_settings
from domainkit.contrib.auth.authentication import Credentials
from domainkit.db import connections
from domainkit.http import get_asgi_application
from domainkit.tasks import owned
from domainkit.testing import AsyncTestClient
from domainkit.testing import TestClient as DomainkitTestClient
from domainkit_tests import sample_platform_routes
from domainkit_tests.conftest import MakePackage, UseAuth
from domainkit_tests.sample_platform_routes import COOKIE_USERS
from domainkit_tests.test_db import NotesUnitOfWork, database  # noqa: F401 - a fixture.

MIDDLEWARE: Final = [
    "domainkit.http.locale.LocaleMiddleware",
    "domainkit.http.middleware.SecurityHeadersMiddleware",
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit_tests.sample_platform_routes.ActorFromHeader",
]
DOWN: Final = {"URL": "redis://127.0.0.1:9/0", "KEY_PREFIX": "down", "RETRIES": 0}


PAGES: Final = "domainkit_tests.sample_platform_routes.pages"


@pytest.fixture
def computed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """What the cached endpoints computed during the test.

    The pages are decorated again for the test, so that the test, not the
    import, runs ``@cache_page``.
    """
    monkeypatch.setattr(
        sample_platform_routes, "pages", sample_platform_routes.page_routes()
    )
    sample_platform_routes.computed.clear()
    yield sample_platform_routes.computed
    sample_platform_routes.computed.clear()


@pytest.fixture
def client(computed: list[str]) -> Iterator[TestClient]:
    """An application with cached pages, users, and languages."""
    with override_settings(ROOT_ROUTER=PAGES, MIDDLEWARE=MIDDLEWARE):
        yield TestClient(get_asgi_application())


def test_answers_are_computed_once_then_served(
    client: TestClient, computed: list[str]
) -> None:
    """A second GET is served from the cache, with the same body and validator."""
    first, second = client.get("/products"), client.get("/products")

    assert computed == ["products:all:None"]
    assert first.json() == second.json() == [{"id": 1, "name": "all"}]
    assert first.headers["ETag"] == second.headers["ETag"]
    assert re.fullmatch(r'W/"[0-9a-f]{32}"', first.headers["ETag"])
    assert first.headers["Cache-Control"] == "public, no-cache"
    assert first.headers["Vary"] == "Authorization, Accept-Language"


def test_entries_are_kept_per_path_query_header_and_language(
    client: TestClient, computed: list[str]
) -> None:
    """Query order does not matter; repeated values, headers, and languages do."""
    for query in ("?category=a&tag=x&tag=y", "?tag=x&category=a&tag=y"):
        client.get(f"/products{query}")
    client.get("/products?category=a&tag=y&tag=x")
    client.get("/products?category=b")
    client.get("/products/5", headers={"X-Tenant": "one"})
    client.get("/products/5", headers={"X-Tenant": "one"})
    client.get("/products/5", headers={"X-Tenant": "two"})
    client.get("/products/6", headers={"X-Tenant": "one"})
    english = client.get("/greeting").json()
    arabic = client.get("/greeting", headers={"Accept-Language": "ar"}).json()

    assert computed == [
        "products:a:['x', 'y']",
        "products:a:['y', 'x']",
        "products:b:None",
        "product:5",
        "product:5",
        "product:6",
        "greeting:en",
        "greeting:ar",
    ]
    assert (english, arabic) == ({"language": "en"}, {"language": "ar"})


def test_entries_are_kept_per_scheme_and_host(computed: list[str]) -> None:
    """Hosts and schemes never share an entry, as Django's absolute URLs do not.

    A page whose answer depends on the host (a tenant chosen by ``Host``, a
    link to itself) was served to every host from the first one's entry.
    """
    bases = (
        "https://a.example",
        "http://a.example",
        "https://b.example",
        "https://b.example:8443",
        "https://a.example",
    )
    with override_settings(ROOT_ROUTER=PAGES, MIDDLEWARE=MIDDLEWARE):
        app = get_asgi_application()
        answers = [TestClient(app, base_url=b).get("/here").json() for b in bases]

    assert answers == [{"url": f"{base}/here"} for base in bases]
    assert computed == [
        "here:a.example",
        "here:a.example",
        "here:b.example",
        "here:b.example",
    ]


def test_declared_headers_and_ages_reach_the_client(client: TestClient) -> None:
    """``vary`` and ``max_age`` become ``Vary`` and ``Cache-Control``."""
    response = client.get("/products/5", headers={"X-Tenant": "one"})

    assert response.headers["Cache-Control"] == "public, max-age=30"
    assert response.headers["Vary"] == "Authorization, X-Tenant, Accept-Language"


def test_only_what_the_return_type_declares_is_cached(client: TestClient) -> None:
    """The endpoint returned a full record; the answer and the entry have less."""
    first = client.get("/products/5")
    second = client.get("/products/5")

    async def entries() -> list[bytes]:
        store = caches["default"].store
        keys = [key for key in store._entries if ":page:" in key]  # type: ignore[attr-defined]
        return [raw for raw in await store.get_many(keys) if raw is not None]

    stored = asyncio.run(entries())
    assert first.json() == second.json() == {"id": 5, "name": "widget"}
    assert len(stored) == 1
    assert b"cost" not in stored[0]
    assert stored[0].endswith(b'{"id":5,"name":"widget"}')


def test_users_are_never_served_shared_answers(
    client: TestClient, computed: list[str]
) -> None:
    """With an actor or credentials, the endpoint runs and nothing is cached."""
    client.get("/products")
    alice = client.get("/products", headers={"X-Test-Actor": "alice"})
    again = client.get("/products", headers={"X-Test-Actor": "alice"})
    unchecked = client.get("/products", headers={"Authorization": "Bearer x"})

    assert computed == ["products:all:None"] * 4
    assert alice.headers["Cache-Control"] == "no-store"
    assert "ETag" not in again.headers
    assert unchecked.status_code == 200


def test_users_without_an_actor_are_never_served_shared_answers(
    use_auth: UseAuth, computed: list[str]
) -> None:
    """A cookie's user is a user, though no ``RequestContextMiddleware`` binds them.

    Such a user sent no ``Authorization`` header and had no actor, so a page
    that forgot ``vary_on_user`` was stored for, and served to, everyone.
    """
    use_auth(
        AUTHENTICATION_CLASSES=["domainkit_tests.sample_platform_routes.SessionCookie"]
    )
    middleware = [
        MIDDLEWARE[1],
        "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
    ]
    with override_settings(ROOT_ROUTER=PAGES, MIDDLEWARE=middleware):
        app = get_asgi_application()  # Startup refuses this list; mounts may not.
        alice = TestClient(app, cookies={"session": "alice"})
        bob = TestClient(app, cookies={"session": "bob"})
        anonymous = TestClient(app)
        names = [alice.get("/me/name"), anonymous.get("/me/name")]
        greetings = [
            alice.get("/me/greeting"),
            alice.get("/me/greeting"),
            bob.get("/me/greeting"),
            anonymous.get("/me/greeting"),
        ]

    assert [r.json() for r in names] == [{"you": "alice"}, {"you": "anonymous"}]
    assert names[0].headers["Cache-Control"] == "no-store"
    assert [r.json()["hello"] for r in greetings] == [
        "alice",
        "alice",
        "bob",
        "anonymous",
    ]
    assert greetings[0].headers["Cache-Control"] == "private, no-cache"
    assert computed == [
        "name:alice",
        "name:anonymous",
        "greeting:alice",
        "greeting:bob",
        "greeting:anonymous",
    ]


def test_credentials_without_a_user_id_are_never_cached() -> None:
    """Another middleware's credentials name no user: whose they are is unknown."""
    other = {"type": "http", "headers": [], "auth": AuthCredentials(["authenticated"])}
    accepted = {**other, "auth": Credentials(user=COOKIE_USERS["bob"], scheme="x")}  # type: ignore[arg-type]

    assert _user(Request(other)) == ""
    assert _user(Request(accepted)) == str(COOKIE_USERS["bob"].id)
    assert _user(Request({**other, "auth": None})) is None


def test_pages_that_vary_on_the_user_keep_an_entry_each(
    client: TestClient, computed: list[str]
) -> None:
    """One entry per actor, one for anonymous callers, marked private."""
    alice = [client.get("/me/basket", headers={"X-Test-Actor": "alice"}) for _ in "ab"]
    bob = client.get("/me/basket", headers={"X-Test-Actor": "bob"})
    anonymous = [client.get("/me/basket") for _ in "ab"]
    unchecked = client.get("/me/basket", headers={"Authorization": "Bearer x"})

    assert computed == ["basket:alice", "basket:bob", "basket:None", "basket:None"]
    assert [r.json() for r in (*alice, bob, *anonymous)] == [
        {"owner": "alice"},
        {"owner": "alice"},
        {"owner": "bob"},
        {"owner": None},
        {"owner": None},
    ]
    assert alice[0].headers["Cache-Control"] == "private, no-cache"
    assert unchecked.json() == {"owner": None}


@pytest.mark.parametrize(
    "header",
    ["{etag}", "{bare}", '"other", {etag}', '"other",{etag}', "*", ' "x" , {bare} '],
)
def test_validators_that_match_get_304(client: TestClient, header: str) -> None:
    """``If-None-Match`` naming the answer, weakly or not, or ``*``: no body."""
    etag = client.get("/products/5").headers["ETag"]
    bare = etag.removeprefix("W/")

    unchanged = client.get(
        "/products/5",
        headers={"If-None-Match": header.format(etag=etag, bare=bare)},
    )

    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["ETag"] == etag
    assert unchanged.headers["Cache-Control"] == "public, max-age=30"
    assert unchanged.headers["Vary"] == "Authorization, X-Tenant, Accept-Language"


def test_validators_that_differ_get_the_answer(client: TestClient) -> None:
    """Another tag, an empty header, or another page's tag: 200 with the body."""
    etag = client.get("/products/5").headers["ETag"]
    other = client.get("/products/6").headers["ETag"]

    for header in ('"nope"', "", other, f'{etag[:-2]}x"'):
        response = client.get("/products/5", headers={"If-None-Match": header})
        assert (response.status_code, response.json()["id"]) == (200, 5)
    assert etag != other


def test_errors_and_other_methods_are_never_cached(
    client: TestClient, computed: list[str]
) -> None:
    """Refusals run again every time; only GET answers are cached."""
    missing = [client.get("/products/0").status_code for _ in "ab"]
    added = [client.post("/products", json={"id": 3, "name": "x"}) for _ in "ab"]

    assert missing == [404, 404]
    assert computed == ["product:0", "product:0", "add:3", "add:3"]
    assert added[0].headers["Cache-Control"] == "no-store"


def test_pages_work_while_the_cache_is_down(computed: list[str]) -> None:
    """The cache fails open: answers are computed, still with their validators."""
    with (
        override_settings(
            ROOT_ROUTER=PAGES,
            MIDDLEWARE=MIDDLEWARE,
            CACHES={"default": DOWN},
        ),
        TestClient(get_asgi_application()) as client,
    ):
        first, second = client.get("/products"), client.get("/products")

    assert (first.status_code, second.json()) == (200, [{"id": 1, "name": "all"}])
    assert computed == ["products:all:None"] * 2
    assert first.headers["ETag"] == second.headers["ETag"]


def test_named_pages_share_their_entries(computed: list[str]) -> None:
    """``name`` starts the key, so a renamed or moved endpoint keeps its entries."""
    app = FastAPI()

    @app.get("/greeting")
    @cache_page(ttl=60, name="shared.greeting")
    async def moved() -> dict[str, str]:
        computed.append("moved")
        return {"language": "moved"}

    with override_settings(ROOT_ROUTER=PAGES, MIDDLEWARE=MIDDLEWARE):
        assert TestClient(get_asgi_application()).get("/greeting").json() == {
            "language": "en"
        }
        assert TestClient(app).get("/greeting").json() == {"language": "en"}

    assert computed == ["greeting:en"]


def test_endpoints_keep_their_parameters_and_documentation(
    make_package: MakePackage,
) -> None:
    """Annotations written as strings are resolved; OpenAPI sees the endpoint."""
    make_package(
        "pages_pkg",
        {
            "routes.py": '''
                from __future__ import annotations

                from typing import Annotated

                from fastapi import FastAPI, Query, Request, Response
                from pydantic import BaseModel

                from domainkit.cache import cache_page


                class Item(BaseModel):
                    """An item."""

                    name: str


                app = FastAPI()


                @app.get("/labels")
                @cache_page(ttl=60, vary=["X-Shop"])
                async def labels(response: Response) -> list[str]:
                    """Set a header of its own."""
                    response.headers["X-Labels"] = "2"
                    response.headers["Vary"] = "Origin"
                    return ["new", "sale"]


                @app.get("/items/{item_id}")
                @cache_page(ttl=60)
                async def read_item(
                    item_id: int,
                    request: Request,
                    q: Annotated[str, Query(max_length=3)] = "x",
                ) -> Item:
                    """Read one item."""
                    return Item(name=f"{item_id}-{q}-{request.url.path}")
            ''',
        },
    )
    from pages_pkg.routes import app  # type: ignore[import-not-found]

    client = TestClient(app)
    operation = client.get("/openapi.json").json()["paths"]["/items/{item_id}"]["get"]
    names = [parameter["name"] for parameter in operation["parameters"]]

    labels = client.get("/labels")
    assert client.get("/items/4?q=ab").json() == {"name": "4-ab-/items/4"}
    assert labels.json() == client.get("/labels").json() == ["new", "sale"]
    assert labels.headers["X-Labels"] == "2"
    assert labels.headers["Vary"] == "Origin, Authorization, X-Shop"
    assert labels.headers["ETag"]
    assert client.get("/items/4?q=abcd").status_code == 422
    assert names == ["item_id", "q"]
    assert operation["summary"] == "Read Item"
    assert operation["description"] == "Read one item."
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"
    }


def test_cache_page_is_checked_when_applied() -> None:
    """Sync endpoints, ``Response`` returns, negative ages, and taken names fail."""

    def sync() -> dict[str, str]:
        return {}

    async def raw() -> Response:
        return Response()

    async def taken(_cache_page_request: str) -> None:
        return None

    with pytest.raises(TypeError, match=r"^@cache_page decorates async endpoints\.$"):
        cache_page()(sync)  # type: ignore[arg-type]
    with pytest.raises(
        TypeError,
        match=r"^@cache_page caches values FastAPI serializes, not Response objects\.$",
    ):
        cache_page()(raw)
    with pytest.raises(ValueError, match=r"^max_age cannot be negative\.$"):
        cache_page(max_age=-1)
    with pytest.raises(TypeError, match="parameter names"):
        cache_page()(taken)


def test_signatures_add_the_request_and_response_before_extra_keywords() -> None:
    """``**kwargs`` stays last; annotations are resolved; untyped ones stay empty."""

    async def endpoint(a, *rest: int, b: str = "x", **extra: Any) -> int:  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN401
        return 1

    signature = _signature(endpoint, {"b": str, "return": int})

    assert list(signature.parameters) == ["a", "rest", "b", REQUEST, RESPONSE, "extra"]
    assert signature.parameters[REQUEST].annotation is Request
    assert signature.parameters[RESPONSE].annotation is Response
    assert signature.parameters["a"].annotation is inspect.Parameter.empty
    assert signature.return_annotation is int


def test_signatures_resolve_the_return_type_or_leave_it_empty() -> None:
    """A return type written as a string is resolved; none stays none."""

    async def written() -> "int":
        return 1

    async def unwritten():  # type: ignore[no-untyped-def]  # noqa: ANN202 - on purpose.
        return 1

    assert _signature(written, {"return": int}).return_annotation is int
    assert _signature(unwritten, {}).return_annotation is inspect.Signature.empty


def test_entries_expire_after_their_ttl(computed: list[str]) -> None:
    """An entry is served for ``ttl`` seconds, then computed again."""
    with (
        override_settings(ROOT_ROUTER=PAGES, MIDDLEWARE=MIDDLEWARE),
        freeze_time("2026-10-19T12:00:00Z") as clock,
    ):
        client = DomainkitTestClient(get_asgi_application())
        client.get("/products")
        clock.tick(timedelta(seconds=30))
        client.get("/products")
        clock.tick(timedelta(seconds=31))
        client.get("/products")

    assert computed == ["products:all:None"] * 2


def test_endpoints_on_the_same_path_keep_their_own_entries() -> None:
    """Entries are named after the endpoint, not only its path."""
    first, second = FastAPI(), FastAPI()

    @first.get("/same")
    @cache_page(ttl=60)
    async def one() -> dict[str, str]:
        return {"from": "one"}

    @second.get("/same")
    @cache_page(ttl=60)
    async def two() -> dict[str, str]:
        return {"from": "two"}

    assert TestClient(first).get("/same").json() == {"from": "one"}
    assert TestClient(second).get("/same").json() == {"from": "two"}


def test_endpoints_without_a_return_type_are_cached_as_they_are() -> None:
    """Without an annotation, the value is cached as it comes."""
    app = FastAPI()

    @app.get("/plain")
    @cache_page(ttl=60)
    async def plain():  # type: ignore[no-untyped-def]  # noqa: ANN202 - on purpose.
        return {"plain": [1, 2]}

    client = TestClient(app)
    assert (
        client.get("/plain").json() == client.get("/plain").json() == {"plain": [1, 2]}
    )


def test_validator_matching() -> None:
    """Weak comparison, lists, and ``*``; nothing matches a missing header."""
    etag = 'W/"abc"'
    assert _matches('"abc"', etag)
    assert _matches('W/"abc"', etag)
    assert _matches('"x", W/"abc"', etag)
    assert _matches("*", etag)
    assert not _matches(None, etag)
    assert not _matches("", etag)
    assert not _matches('"abcd"', etag)
    assert not _matches('W/"ab"', etag)


@pytest.mark.anyio
@pytest.mark.usefixtures("database")
async def test_a_request_cut_off_during_a_miss_returns_its_connection() -> None:
    """The endpoint stops before its dependencies close, so nothing leaks.

    A request cancelled while its endpoint computes the entry (its deadline
    passed, its client went away) closes its unit of work on the way out:
    the endpoint runs no statement on it after that, and the pool gets its
    connection back.
    """
    app = FastAPI()
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[int] = []

    async def unit_of_work() -> AsyncIterator[NotesUnitOfWork]:
        async with NotesUnitOfWork() as uow:
            yield uow

    @app.get("/report")
    @cache_page(ttl=60)
    async def report(
        uow: Annotated[NotesUnitOfWork, Depends(unit_of_work)],
    ) -> dict[str, int]:
        ran.append(await uow.session.scalar(text("SELECT 1")) or 0)
        started.set()
        await release.wait()
        ran.append(await uow.session.scalar(text("SELECT 2")) or 0)
        return {"rows": 2}

    engine = connections.engine()
    try:
        async with AsyncTestClient(app, lifespan=False) as client:
            asking = asyncio.create_task(client.get("/report"))
            async with asyncio.timeout(30):
                await started.wait()
            computing = {t for t in owned() if t.get_name() == COMPUTE_TASK}
            asking.cancel()
            await asyncio.wait([asking], timeout=30)
            release.set()
            await asyncio.wait(computing, timeout=30)

        assert asking.cancelled()
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        assert ran == [1]
    finally:
        release.set()
        await connections.close_all()


@pytest.mark.anyio
async def test_a_page_that_borrows_nothing_is_cached_after_its_caller_left() -> None:
    """With ``borrows=False`` its computation goes on and fills the cache.

    A page slower than one request's deadline then heals instead of
    failing every request.
    """
    app = FastAPI()
    started, release = asyncio.Event(), asyncio.Event()
    computed: list[str] = []

    @app.get("/report")
    @cache_page(ttl=60, borrows=False)
    async def report(month: int = 1) -> dict[str, int]:
        computed.append("report")
        started.set()
        await release.wait()
        return {"month": month}

    async with AsyncTestClient(app, lifespan=False) as client:
        asking = asyncio.create_task(client.get("/report"))
        async with asyncio.timeout(30):
            await started.wait()
        (computing,) = {t for t in owned() if t.get_name() == COMPUTE_TASK}
        asking.cancel()
        await asyncio.wait([asking], timeout=30)
        release.set()
        await asyncio.wait([computing], timeout=30)
        later = await client.get("/report")

    assert asking.cancelled()
    assert later.json() == {"month": 1}
    assert computed == ["report"]  # Served from the entry the computation stored.


async def _session() -> AsyncIterator[str]:
    yield "session"


class Held:
    """A dependency object whose ``__call__`` holds a resource."""

    async def __call__(self) -> AsyncIterator[str]:
        """Open, yield, close."""
        yield "held"


class Plain:
    """A class dependency: FastAPI builds one per request."""

    def __init__(self, month: int = 1) -> None:
        """Keep the month."""
        self.month = month


async def _opens(session: Annotated[str, Depends(_session)]) -> str:
    return session


def test_detached_pages_refuse_dependencies_that_hold_a_resource() -> None:
    """``borrows=False`` cannot run an endpoint that uses what the request closes.

    Dependencies with ``yield`` are found as defaults, in ``Annotated``,
    through ``Security``, beneath other dependencies, as instances with a
    ``__call__`` that yields, and behind ``Depends()`` naming a class.
    """

    async def by_default(session: str = Depends(_session)) -> int:
        return 1

    async def annotated(session: Annotated[str, Depends(_session)]) -> int:
        return 1

    async def secured(session: Annotated[str, Security(_session)]) -> int:
        return 1

    async def documented(
        session: Annotated[str, "the session"] = Depends(_session),
    ) -> int:
        return 1

    async def beneath(session: Annotated[str, Depends(_opens)]) -> int:
        return 1

    async def instance(session: Annotated[str, Depends(Held())]) -> int:
        return 1

    async def plain(
        request: Request,
        kept: Annotated[Plain, Depends()],
        month: int = 1,
        held: int = Depends(len),
        largest: int = Depends(max),  # No signature to look into.
    ) -> int:
        return month

    for endpoint in (by_default, annotated, secured, documented, beneath, instance):
        with pytest.raises(TypeError, match=r"borrows=False\) cannot run"):
            cache_page(borrows=False)(endpoint)
        cache_page()(endpoint)  # Borrowing, the default, takes them all.
    cache_page(borrows=False)(plain)  # Values, the request, a class: nothing held.


def test_requests_see_dependencies_with_yield_beneath_the_routes() -> None:
    """A route dependency that holds nothing itself, but depends on one that does."""

    async def plain() -> None:
        return None

    async def via(opened: Annotated[None, Depends(_open_unit_of_work)]) -> None:
        return None

    app = FastAPI()

    @app.get("/holds", dependencies=[Depends(plain), Depends(via)])
    async def holds() -> int:
        return 1

    @app.get("/free", dependencies=[Depends(plain)])
    async def free() -> int:
        return 1

    def holds_on(path: str) -> bool:
        (route,) = [r for r in app.routes if getattr(r, "path", None) == path]
        scope = {"type": "http", "route": route, "router": app.router}
        return _route_holds(Request(scope))

    assert holds_on("/holds")
    assert not holds_on("/free")


async def _open_unit_of_work(request: Request) -> AsyncIterator[None]:
    async with NotesUnitOfWork() as uow:
        request.state.uow = uow
        yield


_current_uow: ContextVar[NotesUnitOfWork] = ContextVar("current_uow")


class UnitOfWorkMiddleware:
    """A project's unit of work per request, in ``request.state`` and a context var."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Open a unit of work around the request; close it when it ends."""
        async with NotesUnitOfWork() as uow:
            scope.setdefault("state", {})["uow"] = uow
            token = _current_uow.set(uow)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_uow.reset(token)


async def _cut_off_during_a_miss(
    app: ASGIApp, started: asyncio.Event, release: asyncio.Event
) -> list[asyncio.Task[Any]]:
    """Ask for ``/report``, cancel the request once its endpoint started, release."""
    async with AsyncTestClient(app, lifespan=False) as client:
        asking = asyncio.create_task(client.get("/report"))
        async with asyncio.timeout(30):
            await started.wait()
        computing = [t for t in owned() if t.get_name() == COMPUTE_TASK]
        asking.cancel()
        await asyncio.wait([asking], timeout=30)
        release.set()
        if computing:
            await asyncio.wait(computing, timeout=30)
    assert asking.cancelled()
    return computing


@pytest.mark.anyio
@pytest.mark.usefixtures("database")
@pytest.mark.parametrize(
    ("handed", "borrows"),
    [
        ("route", True),
        ("route", False),  # A route's dependency with yield: it borrows anyway.
        ("router", False),
        ("application", False),
        ("outer router", True),  # Not in the request: only borrowing covers it.
        ("middleware state", True),
        ("context variable", True),
    ],
)
async def test_every_way_a_page_reaches_the_requests_unit_of_work_is_safe(
    handed: str, borrows: bool
) -> None:
    """A cut-off request's unit of work is never used after it closed it.

    Pages borrow by default, so whatever hands the endpoint the request's
    unit of work (a route, router, or application dependency, a middleware
    through ``request.state`` or a context variable, which nothing can
    detect) the computation ends with the request. With ``borrows=False``,
    a dependency with ``yield`` of the route, its router, or the application
    makes the request borrow anyway; one of a router it was included
    through is not in the request, which is why pages borrow by default.
    """
    started, release = asyncio.Event(), asyncio.Event()
    ran: list[int] = []
    on_route = [Depends(_open_unit_of_work)] if handed == "route" else []
    router = APIRouter(
        dependencies=[Depends(_open_unit_of_work)] if handed == "router" else []
    )
    app = FastAPI(
        dependencies=[Depends(_open_unit_of_work)] if handed == "application" else []
    )

    @router.get("/report", dependencies=on_route)
    @cache_page(ttl=60, borrows=borrows)
    async def report(request: Request) -> dict[str, int]:
        uow = _current_uow.get() if handed == "context variable" else request.state.uow
        ran.append(await uow.session.scalar(text("SELECT 1")) or 0)
        started.set()
        await release.wait()
        ran.append(await uow.session.scalar(text("SELECT 2")) or 0)
        return {"rows": 2}

    if handed == "outer router":
        outer = APIRouter(dependencies=[Depends(_open_unit_of_work)])
        outer.include_router(router)
        app.include_router(outer)
    else:
        app.include_router(router)
    served: ASGIApp = app
    if handed in {"middleware state", "context variable"}:
        served = UnitOfWorkMiddleware(app)
    engine = connections.engine()
    try:
        await _cut_off_during_a_miss(served, started, release)

        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        assert ran == [1]
    finally:
        release.set()
        await connections.close_all()


@pytest.mark.anyio
async def test_a_page_that_borrows_nothing_runs_under_its_own_deadline() -> None:
    """Not its caller's: one short request must not cut the page short for all."""
    app = FastAPI()
    seen: list[float | None] = []

    @app.get("/report")
    @cache_page(ttl=60, borrows=False)
    async def report() -> dict[str, int]:
        seen.append(deadlines.remaining())
        return {"rows": 1}

    bounded = {"default": {"URL": "memory://", "COMPUTE_TIMEOUT": 60}}
    async with AsyncTestClient(app, lifespan=False) as client:
        with override_settings(CACHES=bounded):
            async with deadlines.timeout(5):
                await client.get("/report")
        async with deadlines.timeout(5):  # The default: unbounded.
            await client.get("/report?fresh=1")

    left, unbounded = seen
    assert left is not None
    assert 5 < left <= 60  # COMPUTE_TIMEOUT, not the caller's 5 seconds.
    assert unbounded is None
