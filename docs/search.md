# Search and places

Three ways to find things beyond equal and less-than:

| Need | Use |
| --- | --- |
| Rows holding some words, in one table | `F("body").search("late fee")`: PostgreSQL's full-text search |
| Relevance across fields, typo tolerance, millions of documents | A search engine: `domainkit.search` (Meilisearch, OpenSearch) |
| Rows near a place | A `Point` field and `F("location").within(point, meters)`: PostGIS |

## Words in a column

`search` finds rows whose text holds every word of the query, ignoring
case:

```python
late = await uow.invoices.filter(F("note").search("late fee"))
```

On PostgreSQL it is full-text search:
`to_tsvector('simple', note) @@ websearch_to_tsquery('simple', :words)`.
The query reads as people type into a search box: `"late fee"` in quotes
is a phrase, `-paid` leaves out rows with that word, `or` gives a choice.
The `simple` configuration splits on words and folds case, with no
stemming and no stop words, so it means the same in every language. An
index makes it fast on a large table:

```sql
CREATE INDEX invoices_note_search ON invoices
    USING gin (to_tsvector('simple', note));
```

On SQLite and in memory (tests), `search` matches each word as a piece
of the text (`icontains` per word). So `"fee"` finds `"fees"` there but
not on PostgreSQL, which matches whole words: test what depends on the
difference against PostgreSQL.

A `FilterSet` can offer it: `note = Filter("search")` answers
`?note__search=late+fee`. A query with no words is refused (422).

## Search engines

For ranking across fields, typos, and facets, keep an index beside the
database:

```python
SEARCH = {
    "BACKEND": "domainkit.search.meilisearch.MeilisearchBackend",
    "OPTIONS": {"url": "https://search.internal:7700", "api_key": env.str("MEILI_KEY")},
}
```

| Backend | Server | `OPTIONS` |
| --- | --- | --- |
| `domainkit.search.meilisearch.MeilisearchBackend` | Meilisearch | `url`, `api_key`, `timeout` |
| `domainkit.search.opensearch.OpenSearchBackend` | OpenSearch or Elasticsearch | `url`, `username`, `password`, `timeout` |
| `domainkit.search.MemorySearchBackend` | none (tests; refused in staging and production) | none |

Both servers are called through `domainkit.http.client` (the
`httpclient` extra), with no SDK. Every backend offers the same four
calls:

```python
from domainkit.search import search_backend

search = search_backend()
await search.ensure("products", searchable=["name", "description"],
                    filterable=["shop_id"])
await search.index("products", [{"id": str(p.id), "name": p.name}])
await search.delete("products", [str(p.id)])
found = await search.search("products", "red shoes",
                            filters={"shop_id": str(shop)}, limit=20)
held = await uow.products.get_many(ProductId(UUID(i)) for i in found.ids)
products = [held[p] for i in found.ids if (p := ProductId(UUID(i))) in held]
```

`found.ids` are the matching documents' ids, best first, and
`found.total` about how many match. Read the aggregates themselves from
their repository: it keeps the rules (tenancy, soft deletion, access),
and the index may be a moment behind.

Keep the index in step from a durable event handler: the outbox stores
the call with the commit, so the index never holds what was rolled back,
and a worker retries it while the engine is down:

```python
@handles(ProductRenamed, durable=True)
async def reindex(event: ProductRenamed) -> None:
    await search_backend().index(
        "products", [{"id": str(event.product_id), "name": event.name}]
    )
```

Index names are `a-z`, `0-9`, `_`, and `-`; filters compare a field to a
value, and their names are checked before they reach the engine. A
refusal or an outage raises `SearchError`, which names the engine and
the call, never a document.

## Places

`Point` is a value object: a latitude and a longitude in degrees (WGS
84, as GPS gives them). An aggregate holds one like any other field:

```python
from domainkit.ddd.places import Point


class Shop(AggregateRoot[ShopId]):
    name: str
    location: Point


PARIS = Point(latitude=48.8566, longitude=2.3522)
near = await uow.shops.filter(F("location").within(PARIS, 2_000))  # meters
PARIS.distance_to(shop.location)  # meters along the surface
```

| Database | Column | `within` |
| --- | --- | --- |
| PostgreSQL | PostGIS's `geography(Point, 4326)` | `ST_DWithin`, which uses a spatial index |
| SQLite | `[longitude, latitude]` as JSON text | the great-circle distance, worked out in SQL |
| In memory | the `Point` | `Point.distance_to` (haversine) |

PostgreSQL needs PostGIS. Install it in a migration, and give the column
a spatial index:

```python
op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
op.create_index("shops_location", "shops", ["location"], postgresql_using="gist")
```

A `Point` off the Earth (a latitude past 90, a longitude past 180, or
not a number) is refused when it is made, and `within` takes a distance
of more than 0 meters. A `FilterSet` does not offer `within`: read the
point and the distance from the request in the view, and filter by them
there.
