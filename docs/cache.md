# Caches: memory, Redis, and Valkey

A cache keeps results that are expensive to compute, such as a query that
joins several tables, so the next request gets them faster. domainkit has
two kinds:

| URL | Where entries live | Use it for |
| --- | --- | --- |
| `memory://` | this process | Values every replica may hold its own copy of, for a short time: reference data, configuration, feature flags. Fastest (about 2 µs per read). |
| `redis://`, `rediss://` | a Redis or Valkey server | Values all replicas share, and that a change must remove everywhere at once. Also shared rate limits. |
| `dummy://` | nowhere | Turning caching off without changing code. |

A cache is an optimization, never the source of truth. When Redis is down,
`get_or_set` and `@cached` compute values instead of failing, and the
service keeps working, only slower. The design is explained in
[ADR 0012](adr/0012-caches-json-values-fail-open.md); rate limits in Redis
are in [ADR 0013](adr/0013-shared-rate-limits-in-redis.md).

## Configuring caches

`default` is a memory cache unless you set `CACHES`. Add Redis with the
`redis` extra (`pip install "domainkit[redis]"`), which brings redis-py:

```python
# config/settings/production.py
CACHES = {
    "default": {"URL": "memory://", "TTL": 60},
    "shared": {
        "URL": env.str("CACHE_URL"),  # rediss://user:password@cache.internal:6380/0
        "KEY_PREFIX": "billing",
        "TTL": 300,
    },
}
```

- `KEY_PREFIX` is required for Redis. Every key starts with it, so several
  services can share one server without reading or clearing each other's
  entries.
- In staging and production, a Redis on another host must use `rediss://`,
  with a password or a client certificate, and certificate checks on. The
  [deployment checks](reference.md#deployment-checks) refuse anything else.
  Set `TLS_CA` when a private CA signed the server's certificate.
- Valkey speaks the same protocol: use the same `redis://` URLs. CI runs the
  suite against Redis 8.8 (plain and TLS) and Valkey 9.1. Redis 8 is
  licensed AGPLv3, RSALv2, or SSPLv1; Valkey is BSD-3-Clause. domainkit and
  redis-py work with either, so the choice of server is yours.
- One endpoint per cache (a single server, or a managed service's primary
  endpoint, which handles failover), a Sentinel group, or a Cluster: see
  [Sentinel and Cluster](#sentinel-and-cluster).

### Sentinel and Cluster

A Sentinel group or a Cluster has several addresses, so it has a scheme of
its own. Add `s` after `redis` for TLS, as with one server:

```python
CACHES = {
    "default": {"URL": "memory://"},
    # Sentinels at port 26379 unless given; the service, then the database.
    "shared": {
        "URL": "rediss+sentinel://:password@sentinel-1,sentinel-2,sentinel-3/mymaster/0",
        "KEY_PREFIX": "billing",
        "SENTINEL_PASSWORD": env.str("SENTINEL_PASSWORD"),  # if they ask for one
    },
    # Any reachable nodes, at port 6379 unless given; the client finds the rest.
    "sessions": {
        "URL": "rediss+cluster://user:password@node-1:6379,node-2:6379",
        "KEY_PREFIX": "billing",
    },
}
```

- **Sentinel.** The client asks the Sentinels where the primary is, and
  asks again after a failover; reads and writes go to the primary. The
  URL's user and password are the primary's; `SENTINEL_PASSWORD` is the
  Sentinels' own, when they have one. TLS, `TLS_CA`, and the client
  certificate apply to both. The primary's address comes from the
  Sentinels, so its certificate must name the address they announce.
- **Cluster.** Keys spread over the nodes by hash slot. Every operation of
  a cache touches one key per command or splits its keys by slot (`MGET`,
  `DEL`), so none is refused for crossing slots, and the rate limiter's
  script reads and writes one key. A Cluster has only database 0.
  `cache clear` scans every primary.
- The URLs take no `?options`; set timeouts and pool sizes with the cache's
  settings. Pools wait up to `CONNECT_TIMEOUT` for a free connection, as
  with one server; a Cluster has a pool of `MAX_CONNECTIONS` per node.
- The [deployment checks](reference.md#deployment-checks) apply as for one
  server: `rediss+sentinel://` or `rediss+cluster://` when any host is
  elsewhere, a password or a client certificate, and certificate checks on.
- A Sentinel group or Cluster that cannot be reached, or with slots no node
  serves, counts as unavailable: caches fail open and rate limits let
  requests through, as with one server.
- The tests build these clients and run their commands on fakes; no real
  Sentinel or Cluster runs in CI. Check a deployment against real servers
  before relying on it: `python manage.py cache check`.

Every key looks like `<KEY_PREFIX>:<VERSION>:<key>`. Raise `VERSION` to drop
every entry at once, for example after changing the shape of a cached type:
old entries become unreachable and expire with their TTL.

## Using a cache

Caches belong in the application layer, next to the queries they speed up.
The domain layer never sees them. Look a cache up inside async code: Redis
connections belong to an event loop, so `caches["shared"]` gives each loop
its own client.

```python
from uuid import UUID

from domainkit.cache import cached, caches


async def invoice_summary(invoice_id: UUID) -> InvoiceSummary:
    return await caches["shared"].get_or_set(
        f"invoice-summary:{invoice_id}",
        lambda: load_summary(invoice_id),  # runs only on a miss
        InvoiceSummary,
        ttl=120,
    )
```

The same, as a decorator. The key is the function's import path and a hash
of its arguments, and the return annotation types the cached value:

```python
@cached(cache="shared", ttl=120)
async def invoice_summary(invoice_id: UUID) -> InvoiceSummary:
    return await load_summary(invoice_id)


await invoice_summary(invoice_id)  # cached
await invoice_summary.invalidate(invoice_id)  # forgotten
invoice_summary.key(invoice_id)  # the key, to delete it elsewhere
```

Arguments must be JSON-friendly. Decorate functions, not methods. Pass
`name="billing.invoice_summary"` to keep the entries when the function moves
to another module.

All the operations, on any cache:

| Call | Does |
| --- | --- |
| `await cache.get(key, Type)` | The value as `Type`, or `None` on a miss. Without `Type`, plain JSON. |
| `await cache.set(key, value, ttl=...)` | Store a value. |
| `await cache.get_or_set(key, factory, Type, ttl=...)` | Return the cached value, or call `factory`, store its result, and return it. |
| `await cache.add(key, value)` | Store only if missing; `True` if it was. |
| `await cache.get_many(keys, Type)`, `set_many(mapping)`, `delete_many(keys)` | Several keys in one round trip. |
| `await cache.delete(key)` | Delete; `True` if it existed. |
| `await cache.incr(key, delta=1, ttl=...)` | Add to an integer and return it. A new counter expires after `ttl`; later increments keep that expiry, so counters count in fixed windows. |
| `await cache.touch(key, ttl=...)` | Give an entry a new expiry (`None`: never). |
| `await cache.clear()` | Delete every key of this cache, all versions. |

`ttl` is in seconds or a `timedelta`; leave it out for the cache's `TTL`, or
pass `None` to keep the entry until it is evicted. A miss reads as `None`, so
use `get_or_set` to cache a missing row: it stores `null` and tells the two
apart.

## Caching whole responses

`@cache_page` caches what a GET endpoint returns, as Django's per-view cache
does. Put it under the route decorator:

```python
from domainkit.cache import cache_page
from domainkit.contrib.auth import Public


@router.get("/products", dependencies=[Public])
@cache_page(ttl=60)
async def list_products(category: str | None = None) -> list[ProductOut]:
    return await catalog.products(category)


@router.get("/products/{product_id}", dependencies=[Public])
@cache_page(ttl=300, vary=["X-Tenant"], max_age=60)
async def read_product(product_id: int) -> ProductOut: ...


@router.get("/me/basket", dependencies=[Depends(Require())])
@cache_page(ttl=30, vary_on_user=True)
async def my_basket(user: CurrentUser) -> BasketOut: ...
```

- **What is cached** is the endpoint's return value, typed by its return
  annotation as with `@cached`: only the fields the annotation declares
  are stored, and FastAPI serializes the value on every request, so
  `response_model` filtering always applies. Annotate the return type
  with the model the response shows. Endpoints that return a `Response`
  cannot be cached this way. Headers an endpoint sets on its `Response`
  are not cached: on a hit, the endpoint does not run.
- **Entries are keyed** by the endpoint (or `name`), the path, the query
  (in any order of names), the request's language (see
  [i18n.md](i18n.md)), and the request headers listed in `vary`. Only GET
  answers are cached; refusals and errors never are.
- **Who asks.** The route's dependencies run on every request, the cached
  ones too, so rules (`Require`) and throttles still decide first. A
  request with credentials (an actor, or an `Authorization` header) is
  never served from, or stored in, the cache, unless the page says
  `vary_on_user=True`: then each actor has entries of their own, and
  anonymous callers share theirs. Credentials that no middleware accepted
  (no actor) are never cached either way.
- **Browsers and proxies.** The framework answers `Cache-Control:
  no-store` by default. Cached pages override it on purpose: `public,
  no-cache` (keep it, but ask each time), `private` when they vary on the
  user, or `max-age=<max_age>` to let clients reuse an answer without
  asking. They add `Vary: Authorization` and the `vary` headers, so shared
  caches keep them apart too.
- **Validators.** Each answer carries a weak `ETag` of its value. A request
  whose `If-None-Match` names it (or `*`) gets 304 without a body, from
  the cache.
- **Freshness** is the TTL's, as for any entry. To drop every page at
  once, raise the cache's `VERSION` or run `cache clear`. Keep TTLs short
  for data that changes.
- **Failures.** The cache fails open, as everywhere: while it is down,
  every request runs the endpoint.

## Values are JSON

Values are stored as JSON, never pickled. Pickle runs code while it loads,
so anyone who could write to Redis could take over every service reading
from it. JSON cannot do that.

- Cache models, dataclasses, dicts, lists, strings, numbers, UUIDs, dates,
  and decimals. Anything else is refused with a `TypeError` when you store
  it.
- Pass a type to read it back: `get(key, InvoiceSummary)`,
  `get(key, list[Line])`, `get(key, UUID)`. Pydantic validates the JSON, so
  a corrupted or outdated entry reads as a miss, with a warning in the log,
  instead of becoming a wrong object.
- `get_or_set` returns its value through JSON on a miss too, so a miss and a
  hit return the same shape. Code that only works on misses fails in tests.
- Cache read models, not aggregates. An aggregate in a cache escapes its
  unit of work: changes to it are never saved, and its version check does
  not see the cached copy.

## Keys

- Keys have 1 to 1024 characters. Hash long or user-supplied parts, as
  `@cached` does with its arguments.
- Keys are visible to anyone who can run `SCAN` on the server. Keep secrets
  and personal data out of them; use ids.
- **Put the caller in the key of anything that depends on who asks.** An
  answer cached under `report:{report_id}` is served to every caller who
  asks for that report, including ones the original caller's permissions
  would have refused. Check permissions before reading the cache, or key by
  tenant and user (`report:{tenant_id}:{report_id}`).

## Keeping entries fresh

Entries expire after their TTL. To drop them sooner when the data changes,
delete them after the change commits. `invalidate_on` does that for domain
events, which units of work publish after commit:

```python
from domainkit.cache import invalidate_on


class InvoicesConfig(DomainConfig):
    def ready(self) -> None:
        invalidate_on(
            InvoicePaid,
            lambda event: [invoice_summary.key(event.invoice_id)],
            cache="shared",
        )
```

- Deleting after commit, not before, matters. Deleting first lets a request
  between the delete and the commit cache the old data again.
- A narrow race remains: a request that read the database just before the
  commit can store the old value just after the delete. The TTL bounds how
  long that lasts, so keep TTLs as short as the data needs.
- A failed deletion (Redis down) is logged and reported by the event bus;
  the entry then lives until its TTL ends.
- Memory caches are per process: a deletion in one replica does not reach
  the others. Only cache values in memory that may be stale for one TTL.

## Stampedes

When a popular entry expires, every request that misses it would recompute
it at once, and the database takes the whole load. `get_or_set` (and so
`@cached`) prevents that in two ways:

- **One computation per key per process.** Concurrent misses on the same
  key wait for the first caller's result, also while the cache fails, when
  every call is a miss. Each caller still gets its own copy, so no two
  requests share a mutable object. If the first caller is cancelled, the
  computation goes on for the others. Errors reach every waiting caller and
  are never cached.
- **Early refresh (XFetch).** Each entry records how long it took to
  compute. As its expiry nears, a read may decide, at random, to recompute
  it while everyone else still gets the old value. The chance rises as
  expiry nears and is higher for costly entries, so hot entries are usually
  refreshed before they expire at all. If that refresh fails, the cached
  value is still valid and is served until it expires. `EARLY_RECOMPUTE`
  scales how early; `0` turns it off.

With several replicas, each replica may still recompute a hot key once.
That is the worst case, not one recomputation per request.

Computations belong to their cache. At shutdown, caches close before
databases, ledgers, and brokers, and closing one cancels the computations
still running, so none outlives the connections it uses.

## When the cache fails

- A connection error or timeout skips the cache for `FAILURE_COOLDOWN`
  seconds (5 by default), logged once. Requests do not each wait for a
  timeout during an outage.
- `get_or_set` and `@cached` compute the value and return it, once per key
  for concurrent callers, and do not write it back. They also carry on
  when Redis refuses a write (for example, out of memory).
- The plain operations (`get`, `set`, `delete`, ...) raise
  `CacheUnavailable`, or `CacheError` when Redis refuses a command, so code
  that depends on the cache can tell.
- Timeouts are short: a command may take `SOCKET_TIMEOUT` (1 s), with
  `RETRIES` (1) quick retries.
- `/ready` ignores caches unless their `READINESS` is on. If a cache the
  service can work without made `/ready` fail, every replica would leave the
  load balancer at once.
- With `domainkit[metrics]`, `cache_lookups_total{cache, result}` counts
  hits, misses, invalid entries, and early refreshes, and
  `cache_errors_total{cache}` counts failures.

## Shared rate limits

`RATE_LIMIT` counts per process by default, so ten replicas allow ten times
the limit. The memory backend keeps the 100,000 most recently seen callers,
so its memory and the cost of a request stay bounded; a caller seen longer
ago starts afresh. Keep the counts in Redis to share them, and to count
every caller:

```python
RATE_LIMIT = "300/minute"
RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"
RATE_LIMIT_CACHE = "shared"  # a redis:// or rediss:// cache
```

The same algorithm as in memory (GCRA) runs in one Lua script on the
server's clock, so replicas never race and their clocks do not matter. Each
caller costs one key, `<KEY_PREFIX>:ratelimit:<caller>`, kept only until the
caller's quota is full again. If Redis fails, or refuses the count (out of
memory, a read-only replica), requests are let through and logged: the limit
protects the service, and its outage should not stop it.
`cache clear` resets these counters too.

## Throttles for one route

`RATE_LIMIT` counts every request of a caller. A route that costs more (an
export, a search, sending an email) can have its own, tighter limit with
`Throttle`, a FastAPI dependency. It counts with the same backend as
`RATE_LIMIT_BACKEND` (Redis if you set `RedisBackend`), separately from the
global limit, which it does not need:

```python
from fastapi import Depends

from domainkit.contrib.auth import Require
from domainkit.http.ratelimit import Throttle

export_limit = Throttle("5/hour")  # per user, or per address when anonymous


@router.post("/exports", dependencies=[Depends(Require()), Depends(export_limit)])
async def create_export(...): ...


def api_key(request: Request) -> str | None:
    return request.headers.get("X-API-Key")  # None: count by address


@router.get(
    "/search",
    dependencies=[Public, Depends(Throttle("30/minute", key=api_key))],
)
async def search(...): ...


# Routes with one scope share one count.
mail_limit = Throttle("10/hour", key="address", scope="mail")
```

- `key="user"` (the default) counts the actor, and anonymous callers by
  address; `key="address"` counts by address even for users; a function of
  the request (sync or async) counts by what it returns, hashed before it is
  stored, or by address when it returns `None`.
- Each route counts on its own unless routes share a `scope`.
- Over the rate, the route answers 429 `throttled`, with `Retry-After` and
  the `RateLimit-*` headers of that throttle; a WebSocket handshake is
  closed with 1013. Requests that pass carry no headers from the throttle.
- A throttle does not decide who may call a route: the route still needs
  `Require(...)` or `Public`. Put the throttle after `Require` so that
  refused callers are not counted.
- Logging in and refused credentials keep their own limits
  (`AUTH.LOGIN_RATE`, and `RATE_LIMIT` for refused tokens; see
  [auth.md](auth.md)).

## Security on the server

Give each service its own Redis user, limited to its prefix and to the
commands domainkit uses. CI checks that this is enough for every operation,
the rate limiter included:

```text
ACL SETUSER billing on >long-random-password resetkeys ~billing:* resetchannels
    -@all +@connection +get +mget +set +del +unlink +exists +pexpire +persist
    +incrby +time +scan +evalsha +script|load
```

Also:

- Keep the server off the public network, and use TLS (`rediss://`) to it.
- Set `maxmemory` with an eviction policy, such as `allkeys-lru`, so a full
  cache evicts entries instead of refusing writes.
- The server's data is not trusted: entries are validated on the way out,
  and pickle is never used.

## Operations

```bash
python manage.py cache check            # ping every cache; fails if one is down
python manage.py cache clear shared     # delete the shared cache's keys
python manage.py cache clear --all
```

`clear` scans for the cache's prefix and deletes in batches. It never runs
`FLUSHDB`, so other services sharing the server keep their keys. `SCAN` walks
the whole server, though: on a large shared server, raising `VERSION` drops
a cache's entries without touching the others.

On a laptop with Docker, one process reached:

| Operation | Rate |
| --- | --- |
| Typed read, memory | about 460,000 per second (2 µs) |
| Typed read, Redis or Valkey, one caller | about 4,400 per second (0.23 ms per round trip) |
| Typed read, Redis or Valkey, 50 concurrent callers | 14,000 to 25,000 per second, with or without TLS |
| Rate-limit check in Redis, 50 concurrent callers | about 25,000 per second |

Inside a data center, round trips are shorter than through Docker on a
laptop.

## Testing

- The `domainkit.testing` plugin empties memory caches before every test,
  so no test sees what another one cached.
- Point a test's `CACHES` at `dummy://` to check that code works without
  its cache, or at `memory://` to check that it uses it.
- To test against Redis, give the test settings their own `KEY_PREFIX`;
  the plugin does not clear Redis.
