"""Search and places: full-text and ``within`` lookups, and search engines."""

import json
import os
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, NewType
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.conf import override_settings
from domainkit.db import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    connections,
    map_imperatively,
    table_for,
)
from domainkit.db.places import PointType, within
from domainkit.ddd import AggregateRoot, InMemoryRepository, MaxLength
from domainkit.ddd.fields import InvalidQuery
from domainkit.ddd.places import Point
from domainkit.ddd.query import F
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http import client as client_module
from domainkit.search import (
    MemorySearchBackend,
    SearchError,
    SearchResult,
    search_backend,
)
from domainkit.search.meilisearch import MeilisearchBackend
from domainkit.search.opensearch import OpenSearchBackend
from domainkit_tests.settings import SECRET_KEY

pytestmark = pytest.mark.anyio

ShopId = NewType("ShopId", UUID)
PARIS = Point(latitude=48.8566, longitude=2.3522)
LOUVRE = Point(latitude=48.8606, longitude=2.3376)  # About 1.2 km away.
LYON = Point(latitude=45.7640, longitude=4.8357)  # About 392 km away.


class Shop(AggregateRoot[ShopId]):
    """A shop, somewhere, that sells things."""

    name: Annotated[str, MaxLength(60)]
    blurb: Annotated[str, MaxLength(200)]
    location: Point | None = None


metadata = MetaData()
shops = table_for(Shop, "framework_test_shops", metadata=metadata)
map_imperatively(Shop, shops)


class Shops(SqlAlchemyRepository[Shop, ShopId]):
    """Shops in SQL."""


class InMemoryShops(InMemoryRepository[Shop, ShopId]):
    """Shops in memory."""


class ShopsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction over the shops."""

    shops: Shops

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.shops = Shops(session)


def shop(name: str, blurb: str, location: Point | None) -> Shop:
    """A new shop."""
    return Shop(id=ShopId(uuid4()), name=name, blurb=blurb, location=location)


SHOPS = [
    shop("Café Louvre", "fresh bread and late coffee", LOUVRE),
    shop("Lyon Books", "late night books", LYON),
    shop("Nowhere", "coffee beans", None),
]


@pytest.fixture
async def sqlite_shops(database_access: None, tmp_path: Path) -> AsyncIterator[None]:
    """The shops table in a SQLite file, filled."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'shops.sqlite3'}"
    with override_settings(DATABASES={"default": {"URL": url}}):
        async with connections.engine().begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with ShopsUnitOfWork() as uow:
            for each in SHOPS:
                await uow.shops.add(shop(each.name, each.blurb, each.location))
            await uow.commit()
        yield
        await connections.close_all()


async def names(criteria: Any) -> set[str]:  # noqa: ANN401
    """The names of the shops ``criteria`` select, in SQL."""
    async with ShopsUnitOfWork() as uow:
        return {s.name for s in await uow.shops.filter(criteria)}


async def test_points_are_stored_and_found_near_a_place(sqlite_shops: None) -> None:
    """Read back whole; within 2 km of Paris: the Louvre's, not Lyon's."""
    async with ShopsUnitOfWork() as uow:
        stored = {s.name: s.location for s in await uow.shops.filter()}

    assert stored["Café Louvre"] == LOUVRE
    assert stored["Nowhere"] is None
    assert await names(F("location").within(PARIS, 2_000)) == {"Café Louvre"}
    assert await names(F("location").within(PARIS, 500_000)) == {
        "Café Louvre",
        "Lyon Books",
    }


async def test_search_needs_every_word(sqlite_shops: None) -> None:
    """Each word as characters, ignoring case, all of them."""
    assert await names(F("blurb").search("LATE coffee")) == {"Café Louvre"}
    assert await names(F("blurb").search("late")) == {"Café Louvre", "Lyon Books"}
    assert await names(F("blurb").search("tea")) == set()


async def test_the_same_lookups_in_memory() -> None:
    """InMemoryRepository agrees with SQL."""
    repository = InMemoryShops()
    for each in SHOPS:
        await repository.add(each)

    near = await repository.filter(F("location").within(PARIS, 2_000))
    found = await repository.filter(F("blurb").search("late coffee"))

    assert [s.name for s in near] == ["Café Louvre"]
    assert [s.name for s in found] == ["Café Louvre"]


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        (F("name").within(PARIS, 10), "not a Point"),
        (F("location").within(PARIS, 0), "more than 0"),
        (F("location").within("paris", 10), "a Point"),  # type: ignore[arg-type]
        (F("location").within(PARIS, True), "more than 0"),
        (F("blurb").search("   "), "one word"),
        (F("location").search("x"), "not text"),
    ],
)
def test_lookups_that_do_not_fit_are_refused(criteria: Any, message: str) -> None:  # noqa: ANN401
    """Wrong field, distance, or words: InvalidQuery."""
    with pytest.raises(InvalidQuery, match=message):
        criteria.bind(Shop)


def test_points_hold_only_the_earths_coordinates() -> None:
    """Off the Earth: refused; distances are great-circle meters."""
    for latitude, longitude in ((91, 0), (0, 181), (float("nan"), 0)):
        with pytest.raises(ValueError, match="latitude"):
            Point(latitude=latitude, longitude=longitude)
    assert 1_100 < PARIS.distance_to(LOUVRE) < 1_300
    assert 390_000 < PARIS.distance_to(LYON) < 395_000
    assert PARIS.wkt() == "SRID=4326;POINT(2.3522 48.8566)"


def test_postgresql_uses_postgis_and_full_text_search() -> None:
    """geography, ST_GeogFromText, ST_DWithin, ST_AsText, to_tsvector @@ websearch."""
    from sqlalchemy import insert, select

    from domainkit.db.query import compile_criteria

    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    assert PointType().load_dialect_impl(dialect).get_col_spec() == (  # type: ignore[attr-defined]
        "geography(Point, 4326)"
    )
    near = str(
        within(shops.c.location, PARIS, 100.0, "postgresql").compile(dialect=dialect)
    )
    assert "ST_DWithin(framework_test_shops.location, ST_GeogFromText(" in near
    written = str(insert(shops).values(id="1", location=PARIS).compile(dialect=dialect))
    assert "ST_GeogFromText(" in written
    read = str(select(shops.c.location).compile(dialect=dialect))
    assert "ST_AsText(framework_test_shops.location)" in read
    searched = str(
        compile_criteria(
            F("blurb").search("late coffee").bind(Shop), Shop, "postgresql"
        ).compile(dialect=dialect)
    )
    assert "to_tsvector" in searched
    assert "websearch_to_tsquery" in searched
    plain = str(select(shops.c.location).compile(dialect=sqlite.dialect()))
    assert "ST_AsText" not in plain
    assert PointType().process_bind_param(None, dialect) is None
    assert PointType().process_result_value(None, dialect) is None
    assert PointType().process_result_value("POINT(2.3522 48.8566)", dialect) == PARIS


async def test_postgis_finds_shops_near_a_place(database_access: None) -> None:
    """On PostGIS itself, where DOMAINKIT_TEST_POSTGIS_URL names one."""
    url = os.environ.get("DOMAINKIT_TEST_POSTGIS_URL")
    if not url:
        pytest.skip("set DOMAINKIT_TEST_POSTGIS_URL")
    with override_settings(DATABASES={"default": {"URL": url}}):
        try:
            async with connections.engine().begin() as connection:
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
                await connection.run_sync(metadata.drop_all)
                await connection.run_sync(metadata.create_all)
            async with ShopsUnitOfWork() as uow:
                for each in SHOPS:
                    await uow.shops.add(shop(each.name, each.blurb, each.location))
                await uow.commit()
            assert await names(F("location").within(PARIS, 2_000)) == {"Café Louvre"}
            assert await names(F("blurb").search("late coffee")) == {"Café Louvre"}
            async with ShopsUnitOfWork() as uow:
                found = await uow.shops.filter(F("name") == "Café Louvre")
            assert found[0].location == LOUVRE
        finally:
            async with connections.engine().begin() as connection:
                await connection.run_sync(metadata.drop_all)
            await connections.close_all()


# Search engines -----------------------------------------------------------------


class Engine:
    """A search engine's API, answering ``answers`` in turn, keeping requests."""

    def __init__(self, *answers: httpx2.Response | Exception) -> None:
        """Answer these, the last again once they run out."""
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Keep the request; answer."""
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def json(self, index: int = -1) -> Any:  # noqa: ANN401
        """A request's JSON."""
        return json.loads(self.requests[index].content)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Engine]:
    """``engine(*answers)``: answer every call of the test."""

    def make(*answers: httpx2.Response | Exception) -> Engine:
        fake = Engine(*answers)
        monkeypatch.setattr(
            client_module,
            "_stub",
            ContextVar("stubbed", default=httpx2.MockTransport(fake)),  # noqa: B039
        )
        return fake

    return make


async def test_meilisearch_makes_indexes_and_finds(engine: Any) -> None:  # noqa: ANN401
    """Index (409: exists), settings, documents, deletes, a quoted filter."""
    calls = engine(
        httpx2.Response(409, json={"code": "index_already_exists"}),
        httpx2.Response(202, json={"taskUid": 1}),
        httpx2.Response(202),  # No body: nothing to read.
        httpx2.Response(202, json={"taskUid": 3}),
        httpx2.Response(200, json={"hits": [{"id": "s1"}], "estimatedTotalHits": 1}),
    )
    meili = MeilisearchBackend("https://search.example/", api_key="key")

    await meili.ensure("shops", searchable=["name"], filterable=["city"])
    await meili.index("shops", [{"id": "s1", "name": "Café"}])
    await meili.delete("shops", ["s2"])
    found = await meili.search("shops", "café", filters={"city": 'Pa"ris', "n": 3})
    await meili.index("shops", [])
    await meili.delete("shops", [])
    await meili.ensure("plain")

    assert found == SearchResult(["s1"], 1)
    assert calls.requests[0].headers["authorization"] == "Bearer key"
    assert calls.json(1) == {
        "searchableAttributes": ["name"],
        "filterableAttributes": ["city"],
    }
    assert calls.requests[3].url.path == "/indexes/shops/documents/delete-batch"
    assert calls.json(4)["filter"] == 'city = "Pa\\"ris" AND n = "3"'
    assert "key" not in repr(meili)
    with pytest.raises(ValueError, match="attribute"):
        await meili.search("shops", "x", filters={"a = 1 OR b": 1})


async def test_meilisearch_failures_are_named(engine: Any) -> None:  # noqa: ANN401
    """A refusal, no hits, garbage, no server."""
    meili = MeilisearchBackend("https://search.example")
    for answer, message in (
        (httpx2.Response(401, json={}), "refused POST /indexes/shops/search"),
        (httpx2.Response(200, json={"x": 1}), "without hits"),
        (httpx2.Response(200, text="<html>"), "without JSON"),
        (httpx2.ConnectError("no"), "could not be reached"),
    ):
        engine(answer)
        with pytest.raises(SearchError, match=message):
            await meili.search("shops", "x")


async def test_opensearch_bulk_writes_and_searches(engine: Any) -> None:  # noqa: ANN401
    """Mappings, ndjson bulk with refresh, multi_match and term filters."""
    calls = engine(
        httpx2.Response(200, json={"acknowledged": True}),
        httpx2.Response(
            400, json={"error": {"type": "resource_already_exists_exception"}}
        ),
        httpx2.Response(200, json={"errors": False, "items": []}),
        httpx2.Response(
            200,
            json={
                "errors": True,
                "items": [{"delete": {"status": 404}}],
            },
        ),
        httpx2.Response(
            200,
            json={
                "hits": {"total": {"value": 2}, "hits": [{"_id": "s1"}, {"_id": "s2"}]}
            },
        ),
    )
    search = OpenSearchBackend("https://os.example", username="app", password="pw")

    await search.ensure("shops", searchable=["name"], filterable=["city"])
    await search.ensure("shops", searchable=["name"])
    await search.index("shops", [{"id": 1, "name": "Café"}])
    await search.delete("shops", ["gone"])
    found = await search.search("shops", "café", filters={"city": "Paris"}, limit=5)
    await search.index("shops", [])

    assert found == SearchResult(["s1", "s2"], 2)
    assert calls.json(0)["mappings"]["properties"] == {
        "name": {"type": "text"},
        "city": {"type": "keyword"},
    }
    bulk = calls.requests[2]
    assert bulk.url.params["refresh"] == "wait_for"
    assert bulk.content.decode().splitlines()[0] == json.dumps(
        {"index": {"_index": "shops", "_id": "1"}}
    )
    query = calls.json(4)
    assert query["size"] == 5
    assert query["query"]["bool"]["must"][0]["multi_match"]["fields"] == ["name"]
    assert query["query"]["bool"]["filter"] == [{"term": {"city": "Paris"}}]
    assert calls.requests[0].headers["authorization"].startswith("Basic ")
    assert "pw" not in repr(search)


async def test_opensearch_refusals_are_named(engine: Any) -> None:  # noqa: ANN401
    """A bad mapping, failed actions, no hits."""
    search = OpenSearchBackend("https://os.example")
    engine(httpx2.Response(400, json={"error": {"type": "mapper_parsing_exception"}}))
    with pytest.raises(SearchError, match="refused the index"):
        await search.ensure("shops")
    engine(
        httpx2.Response(
            200, json={"errors": True, "items": [{"index": {"status": 400}}]}
        )
    )
    with pytest.raises(SearchError, match="1 of the actions"):
        await search.index("shops", [{"id": 1}])
    engine(httpx2.Response(200, json={"hits": {"total": 3, "hits": [{"_id": "a"}]}}))
    assert (await search.search("other", "x")).total == 3
    engine(httpx2.Response(200, json={"nothing": True}))
    with pytest.raises(SearchError, match="without hits"):
        await search.search("shops", "x")


async def test_the_memory_backend_and_the_setting() -> None:
    """Every word, the filters, a page; SEARCH names the backend."""
    memory = MemorySearchBackend()
    await memory.ensure("shops", searchable=["name"])
    await memory.index(
        "shops",
        [
            {"id": 1, "name": "Late Café", "city": "Paris"},
            {"id": 2, "name": "Late Books", "city": "Lyon"},
        ],
    )
    await memory.delete("shops", ["9"])

    assert (await memory.search("shops", "late")).ids == ["1", "2"]
    assert (await memory.search("shops", "late", filters={"city": "Lyon"})).ids == ["2"]
    assert await memory.search("shops", "late", limit=1, offset=1) == SearchResult(
        ["2"], 2
    )
    await memory.delete("shops", ["1"])
    assert (await memory.search("shops", "café")).ids == []
    for index, limit in (("Bad Name", 1), ("shops", 0)):
        with pytest.raises(ValueError, match=r"index|results"):
            await memory.search(index, "x", limit=limit)

    with override_settings(SEARCH={"BACKEND": "domainkit.search.MemorySearchBackend"}):
        assert isinstance(search_backend(), MemorySearchBackend)
    with pytest.raises(ImproperlyConfigured, match="Set SEARCH"):
        search_backend()
    for path, message in (
        ("domainkit.search.Nope", "SEARCH.BACKEND"),
        ("json.dumps", "not a"),
    ):
        with (
            override_settings(SEARCH={"BACKEND": path}),
            pytest.raises(ImproperlyConfigured, match=message),
        ):
            search_backend()


def test_a_memory_index_is_refused_when_deployed() -> None:
    """deploy.E007: the index would live in one process; Meilisearch is fine."""
    from pydantic import ValidationError

    from domainkit.conf import Settings

    base = {
        "ENVIRONMENT": "production",
        "ALLOWED_HOSTS": ["api.example.com"],
        "SECRET_KEY": SECRET_KEY,
    }
    with pytest.raises(ValidationError, match=r"deploy\.E007: a memory search"):
        Settings.model_validate({**base, "SEARCH": {}})
    meili = {"BACKEND": "domainkit.search.meilisearch.MeilisearchBackend"}
    assert Settings.model_validate({**base, "SEARCH": meili}).SEARCH is not None
