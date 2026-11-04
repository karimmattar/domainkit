# Search and places

Some questions are not "equal to" or "less than". A customer types
"red shoes" and wants the best matches. A map asks for the shops near a
place. domainkit answers both kinds of question in three ways: words in
one column, a search engine beside the database, and places on the
Earth.

On this page you learn how to:

- find rows whose text holds some words, with `F(...).search(...)`;
- keep an index in Meilisearch or OpenSearch, and search it;
- keep that index in step with your data, through the outbox;
- store a place as a `Point`, and find what is near it.

| You need | Use |
| --- | --- |
| Rows that hold some words, in one table | `F("body").search("late fee")`: full-text search on PostgreSQL |
| Relevance across fields, typos, millions of documents | A search engine: `domainkit.search` (Meilisearch, OpenSearch) |
| Rows near a place | A `Point` field and `F("location").within(point, meters)`: PostGIS |

## A first example

A `MemorySearchBackend` keeps its indexes in the process. It shows the
four calls every backend has:

```python
import asyncio

from domainkit.search import MemorySearchBackend


async def main() -> None:
    search = MemorySearchBackend()
    await search.ensure("products", searchable=["name"], filterable=["shop_id"])
    await search.index(
        "products",
        [
            {"id": "1", "name": "Red shoes", "shop_id": "a"},
            {"id": "2", "name": "Red hat", "shop_id": "a"},
            {"id": "3", "name": "red shoes", "shop_id": "b"},
        ],
    )
    print(await search.search("products", "red shoes", filters={"shop_id": "a"}))
    await search.delete("products", ["1"])
    print(await search.search("products", "red"))


asyncio.run(main())
```

```text
SearchResult(ids=['1'], total=1)
SearchResult(ids=['2', '3'], total=2)
```

In production you set `SEARCH` to a real engine, and the same four
calls go to it.

## Words in a column

`search` is a lookup of [queries](queries.md). It finds the aggregates
whose text holds every word of the query, in any case:

```python
from domainkit.ddd import F

late = await uow.invoices.filter(F("note").search("late fee"))
```

On PostgreSQL it is full-text search:

```text
to_tsvector('simple', note) @@ websearch_to_tsquery('simple', :words)
```

- The query reads the way people type into a search box: `"late fee"`
  in quotes is a phrase, `-paid` leaves out rows with that word, and
  `or` gives a choice.
- The `simple` configuration splits the text into words and folds case.
  It does no stemming and drops no stop words, so it means the same in
  every language.

An index makes it fast on a large table. Create it in a migration:

```sql
CREATE INDEX invoices_note_search ON invoices
    USING gin (to_tsvector('simple', note));
```

On SQLite and in memory (in tests), `search` matches each word as a piece
of the text: `icontains` for each word. So `"fee"` finds `"fees"` there,
but not on PostgreSQL, which matches whole words. Test what depends on
this difference against PostgreSQL.

A `FilterSet` can offer it: `note = Filter("search")` answers
`?note__search=late+fee` ([filters over HTTP](queries.md#filters-over-http)).
A query with no words is refused: `InvalidQuery: search takes one word or
more.` Over HTTP, that is a 422.

## Search engines

A search engine ranks across fields, forgives typos, and counts facets.
It keeps an index of documents beside the database. Choose it with the
`SEARCH` setting:

```python
SEARCH = {
    "BACKEND": "domainkit.search.meilisearch.MeilisearchBackend",
    "OPTIONS": {"url": "https://search.internal:7700", "api_key": env.str("MEILI_KEY")},
}
```

| Backend | Server | `OPTIONS` |
| --- | --- | --- |
| `domainkit.search.meilisearch.MeilisearchBackend` | Meilisearch | `url`, `api_key` (optional), `timeout` (10.0 seconds) |
| `domainkit.search.opensearch.OpenSearchBackend` | OpenSearch or Elasticsearch | `url`, `username` and `password` (HTTP Basic, optional), `timeout` (10.0 seconds) |
| `domainkit.search.MemorySearchBackend` | None: indexes in the process, for tests | None |

Both servers are called through `domainkit.http.client` (the
`httpclient` extra), with no SDK. `repr()` of a backend shows its server,
never its key or password. Staging and production refuse
`MemorySearchBackend`: its index lives in one process (`deploy.E007`).

### The four calls

`search_backend()` makes a new backend from `SEARCH`. Every backend, a
`SearchBackend`, has the same four calls:

```python
from uuid import UUID

from domainkit.search import search_backend

search = search_backend()
await search.ensure("products", searchable=["name", "description"], filterable=["shop_id"])
await search.index("products", [{"id": str(p.id), "name": p.name}])
await search.delete("products", [str(p.id)])
found = await search.search("products", "red shoes", filters={"shop_id": str(shop)}, limit=20)
held = await uow.products.get_many(ProductId(UUID(i)) for i in found.ids)
products = [held[p] for i in found.ids if (p := ProductId(UUID(i))) in held]
```

| Call | Does |
| --- | --- |
| `ensure(index, *, searchable=(), filterable=())` | Makes the index if it is missing, and sets the fields it searches and filters by. |
| `index(index, documents)` | Adds or replaces documents. Each has an `id`. |
| `delete(index, ids)` | Removes documents. A missing id is not an error. |
| `search(index, query, *, filters=None, limit=20, offset=0)` | Finds the documents that match `query` and whose fields equal `filters`. `limit` is 1 to 1,000. |

`search` returns a `SearchResult`: `ids`, the ids of the matching
documents, best first, and `total`, about how many match.

Read the aggregates themselves from their [repository](learning.md#words),
as the example does with `get_many`. The repository keeps the rules
(tenancy, soft deletion, access), and the index may be a moment behind.

### Keep the index in step

Update the index from a durable event handler. The outbox stores the
call with the commit, so the index never holds what was rolled back. A
worker retries the call while the engine is down
([messaging](contrib/messaging.md)):

```python
from domainkit.ddd import handles
from domainkit.search import search_backend


@handles(ProductRenamed, durable=True)
async def reindex(event: ProductRenamed) -> None:
    await search_backend().index(
        "products", [{"id": str(event.product_id), "name": event.name}]
    )
```

### Your own backend

A backend of your own is a subclass of `SearchBackend` that writes the
four calls. Set `deployable = False` on one made for tests, so
deployments refuse it. Name it in `SEARCH.BACKEND`; `SEARCH.OPTIONS` are
the keyword arguments of its constructor.

## Places

`Point` is a [value object](learning.md#words): a latitude and a
longitude in degrees (WGS 84, as GPS gives them). An aggregate holds one
like any other field:

```python
from typing import Annotated, NewType
from uuid import UUID

from domainkit.ddd import AggregateRoot, F, MaxLength
from domainkit.ddd.places import Point

ShopId = NewType("ShopId", UUID)


class Shop(AggregateRoot[ShopId]):
    name: Annotated[str, MaxLength(100)]
    location: Point


PARIS = Point(latitude=48.8566, longitude=2.3522)
LOUVRE = Point(latitude=48.8606, longitude=2.3376)
print(round(PARIS.distance_to(LOUVRE)))  # meters along the surface

near = await uow.shops.filter(F("location").within(PARIS, 2_000))  # meters
```

```text
1157
```

`distance_to` uses the haversine formula, on a sphere of radius
`EARTH_RADIUS` (6,371,008.8 meters). `point.wkt()` gives the point as
PostGIS reads it: `SRID=4326;POINT(2.3522 48.8566)`.

| Database | Column | `within` |
| --- | --- | --- |
| PostgreSQL | PostGIS's `geography(Point, 4326)` | `ST_DWithin`, which uses a spatial index |
| SQLite | `[longitude, latitude]` as JSON text | The great-circle distance, worked out in SQL |
| In memory | The `Point` | `Point.distance_to` |

`table_for` stores a `Point` field with `PointType`, from
`domainkit.db.places`. On PostgreSQL its column type is `Geography`, from
the same module. PostgreSQL needs PostGIS. Install it in a migration, and
give the column a spatial index:

```python
from alembic import op

op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
op.create_index("shops_location", "shops", ["location"], postgresql_using="gist")
```

`domainkit.db.places.within(column, point, meters, dialect)` writes the
same condition for a SQLAlchemy query of your own.

A `FilterSet` does not offer `within`. Read the point and the distance
from the request in your route, and filter by them there.

## Options

| Setting | Default | What it does |
| --- | --- | --- |
| `SEARCH` | `None` | The search engine `search_backend()` makes. None: no search engine. |
| `SEARCH.BACKEND` | `domainkit.search.MemorySearchBackend` | The dotted path of a `SearchBackend` class. |
| `SEARCH.OPTIONS` | `{}` | The keyword arguments of the backend. |

[The settings reference](reference/backends.md) has the full rows.

## Errors and refusals

| What goes wrong | What you see |
| --- | --- |
| The engine refuses a call, or cannot be reached. | `SearchError`. Its message names the engine and the call, never a document. |
| An index name is not `a-z`, `0-9`, `_`, and `-` (64 characters at most), or `limit` is not 1 to 1,000. | `ValueError`, before anything reaches the engine. |
| `search_backend()` with no `SEARCH`, or a `BACKEND` that is not a `SearchBackend`. | `ImproperlyConfigured`. |
| `MemorySearchBackend` in staging or production. | The check `deploy.E007`. |
| `F(...).search("")`, with no words. | `InvalidQuery`; 422 over HTTP. |
| `F(...).within(point, 0)`, or a negative distance. | `InvalidQuery: within takes a Point and a distance of more than 0 meters.` |
| `Point(latitude=91, longitude=0)`, or a value that is not a number. | `ValueError: a point is a latitude of -90 to 90, a longitude of -180 to 180` |

Filter names are checked before they reach the engine. A filter compares
one field to one value.

## Common mistakes

- **Showing documents from the index.** The index may hold an old name,
  or a product the caller may not see. Show the aggregates the
  repository returns for `found.ids`.
- **Indexing in the use case.** A call to the engine inside the
  transaction runs even when the commit fails. Index from a durable
  handler.
- **Testing word matching on SQLite only.** SQLite matches pieces of
  words; PostgreSQL matches whole words. Run those tests on PostgreSQL.
- **Swapping latitude and longitude.** Pass both by name:
  `Point(latitude=..., longitude=...)`.

## See also

- [Queries](queries.md): every lookup, and how a query means the same in
  memory and in SQL.
- [Messaging](contrib/messaging.md): durable handlers and the outbox.
- [Backends](reference/backends.md): the `SEARCH` setting.
- [Checks](reference/checks.md): `deploy.E007`.
