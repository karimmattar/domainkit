# Caches and rate limits

A cache keeps results that are expensive to compute, such as a query that
joins several tables, so that the next request gets them faster. A rate
limit counts the requests of each caller and refuses the caller who sends
too many. In domainkit both live in the same places: in the memory of the
process, or in a Redis or Valkey server that every replica shares.

On this page you learn how to:

- configure caches in memory, in Redis or Valkey, and in a Sentinel group
  or a Cluster;
- read and write a cache, with `get_or_set` and the other operations;
- cache what a function returns (`@cached`) and what a GET route answers
  (`@cache_page`);
- remove entries when your data changes (`invalidate_on`, `refresh`);
- know what happens when many requests miss at once, and when the cache
  is down;
- limit the requests of every caller (`RATE_LIMIT`) and of one route
  (`Throttle`), in one process or shared in Redis;
- write a cache store of your own.

## A first example

A product summary is slow to build, so you keep it in the `default`
cache for 2 minutes. `get_or_set` returns the cached value, or calls the
function you give it, stores the result, and returns it:

```python
import asyncio
from uuid import UUID

from pydantic import BaseModel

from domainkit.cache import caches


class ProductSummary(BaseModel):
    product_id: UUID
    name: str
    in_stock: int


async def load_summary(product_id: UUID) -> ProductSummary:
    print("loading")  # a slow query, in a real service
    return ProductSummary(product_id=product_id, name="Lamp", in_stock=3)


async def main() -> None:
    product_id = UUID("6f1c2b4e-8a0d-4c55-9b7e-3d2f1a0e9c11")
    for _ in range(3):
        summary = await caches["default"].get_or_set(
            f"product-summary:{product_id}",
            lambda: load_summary(product_id),  # runs only on a miss
            ProductSummary,
            ttl=120,
        )
    print(summary)


asyncio.run(main())
```

The output shows that `load_summary` ran once, for three reads:

```text
loading
product_id=UUID('6f1c2b4e-8a0d-4c55-9b7e-3d2f1a0e9c11') name='Lamp' in_stock=3
```

Run it with your project's settings loaded. In `python manage.py shell`,
paste the code and write `await main()` in place of the last line. You
need no cache setting: `default` is a
memory cache unless you set `CACHES`. The third argument, `ProductSummary`, is the type the
value is read back as. Values are stored as JSON
([Values are JSON](#values-are-json)).

A cache is an optimization, never the source of truth. When Redis is
down, `get_or_set` and `@cached` compute the value instead of failing.
The service keeps working, only slower. The design is in
[ADR 0012](adr/0012-caches-json-values-fail-open.md); rate limits in
Redis are in [ADR 0013](adr/0013-shared-rate-limits-in-redis.md).

## Kinds of cache

The scheme of a cache's `URL` says where its entries live:

| URL | Where entries live | Use it for |
| --- | --- | --- |
| `memory://` | This process. | Values that each replica may hold its own copy of, for a short time: reference data, configuration, feature flags. It is the fastest (about 2 µs per read). |
| `redis://`, `rediss://` | A Redis or Valkey server. | Values that all replicas share, and that a change must remove everywhere at once. Also shared rate limits. |
| `redis+sentinel://`, `redis+cluster://` | A Sentinel group or a Cluster ([below](#sentinel-and-cluster)). | The same, on a server that fails over or is split over several nodes. |
| `dummy://` | Nowhere. | Turning caching off without changing code. Every read is a miss. |

## Configuring caches

You set caches in `CACHES`, a dict of aliases. `default` is required. For
Redis, install the `redis` extra, which brings redis-py:

```bash
pip install "domainkit[redis]"
```

Then add a cache with a `redis://` or `rediss://` URL:

```python
# config/settings/production.py
CACHES = {
    "default": {"URL": "memory://", "TTL": 60},
    "shared": {
        "URL": env.str("CACHE_URL"),  # rediss://user:password@cache.internal:6380/0
        "KEY_PREFIX": "billing",
        "TTL": 300,
        "COMPUTE_TIMEOUT": 30.0,
    },
}
```

- **`KEY_PREFIX` is required for Redis.** Every key starts with it, so
  several services can share one server without reading or clearing each
  other's entries. A memory cache uses its alias when you leave it out.
- **TLS where deployed.** In staging and production, a Redis on another
  host must use `rediss://`, with a password or a client certificate, and
  certificate checks on. The [deployment checks](reference/checks.md)
  refuse anything else (`security.E010` to `security.E012`). Set `TLS_CA`
  when a private certificate authority signed the server's certificate,
  and `TLS_CERT` with `TLS_KEY` for a client certificate.
- **Valkey** speaks the same protocol: use the same `redis://` URLs. CI
  runs the suite against Redis 8.8 (plain and TLS) and Valkey 9.1.
  Redis 8 is licensed AGPLv3, RSALv2, or SSPLv1; Valkey is BSD-3-Clause.
  domainkit and redis-py work with either, so the choice of server is
  yours.
- **One endpoint per cache**: a single server, or the primary endpoint of
  a managed service (which handles failover itself), a Sentinel group, or
  a Cluster.

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

- **Sentinel.** The client asks the Sentinels where the primary is. It
  asks again after a failover. Reads and writes go to the primary. The
  URL's user and password are the primary's; `SENTINEL_PASSWORD` is the
  Sentinels' own password, when they have one. TLS, `TLS_CA`, and the
  client certificate apply to both. The primary's address comes from the
  Sentinels, so its certificate must name the address that they announce.
- **Cluster.** Keys spread over the nodes by hash slot. Each operation of
  a cache touches one key per command, or splits its keys by slot (`MGET`,
  `DEL`). So no operation is refused for crossing slots, and the rate
  limiter's script reads and writes one key. A Cluster has only
  database 0. `cache clear` scans every primary.
- The URLs take no `?options`. Set timeouts and pool sizes with the
  cache's settings. A pool waits up to `CONNECT_TIMEOUT` for a free
  connection, as with one server. A Cluster has a pool of
  `MAX_CONNECTIONS` per node.
- The [deployment checks](reference/checks.md) apply as for one server:
  `rediss+sentinel://` or `rediss+cluster://` when any host is elsewhere, a
  password or a client certificate, and certificate checks on.
- A Sentinel group or a Cluster that cannot be reached, or that has slots
  no node serves, counts as unavailable. Caches then fail open, and rate
  limits let requests through, as with one server.
- The tests build these clients and run their commands on fakes. No real
  Sentinel or Cluster runs in CI. Check a deployment against its real
  servers before you depend on it: `python manage.py cache check`.

### Keys and versions

Every key that a cache stores looks like `<KEY_PREFIX>:<VERSION>:<key>`.
`make_key` shows it:

```python
caches["default"].make_key("product-summary:1")
```

```text
'default:1:product-summary:1'
```

Raise `VERSION` to drop every entry at once, for example after you change
the shape of a cached type. The old entries become unreachable, and they
expire with their TTL.

## Using a cache

`caches[alias]` gives you a `Cache`. Use caches in the application layer,
next to the queries that they make faster. The domain layer never sees
them.

Find a cache inside async code only. Redis connections belong to an event
loop, so `caches["shared"]` gives each event loop its own client. Outside
a running loop, `caches[...]` raises:

```text
RuntimeError: caches['default'] needs a running event loop: Redis connections belong to one, so look caches up inside async code.
```

An alias that `CACHES` does not have raises
`ImproperlyConfigured: CACHES has no 'nope' alias.`

These are all the operations of a cache, with what each one returned in a
run on a memory cache:

```python
cache = caches["default"]
await cache.set("visits", 0)
await cache.incr("visits")  # 1
await cache.incr("visits", delta=5)  # 6
await cache.add("visits", 100)  # False: the key exists
await cache.add("new", 1)  # True
await cache.set_many({"a": 1, "b": [1, 2]}, ttl=30)
await cache.get_many(["a", "b", "c"])  # {'a': 1, 'b': [1, 2]}
await cache.delete("a")  # True
await cache.delete("a")  # False: it is gone
await cache.delete_many(["b", "c"])  # 1
await cache.touch("visits", None)  # True: it never expires now
await cache.get("missing")  # None
await cache.clear()  # 3: the keys it deleted
```

| Call | What it does |
| --- | --- |
| `await cache.get(key, Type)` | Returns the value as `Type`, or `None` on a miss. Without `Type`, plain JSON. |
| `await cache.set(key, value, ttl=...)` | Stores a value. |
| `await cache.get_or_set(key, factory, Type, ttl=..., borrows=False)` | Returns the cached value, or calls `factory`, stores its result, and returns it. |
| `await cache.refresh(key, factory, ttl=...)` | Calls `factory` now and stores its result in place of the cached value ([below](#replace-a-value-that-changed-refresh)). |
| `await cache.add(key, value, ttl=...)` | Stores only if the key is missing. Returns `True` if it was missing. |
| `await cache.get_many(keys, Type)` | Returns a dict of the values found. Misses are left out. One round trip. |
| `await cache.set_many(mapping, ttl=...)` | Stores several values in one round trip. |
| `await cache.delete(key)` | Deletes a key. Returns `True` if it existed. |
| `await cache.delete_many(keys)` | Deletes several keys. Returns how many existed. |
| `await cache.incr(key, delta=1, ttl=...)` | Adds to an integer and returns the total. A new counter starts at 0 and expires after `ttl`. Later increments keep that expiry, so counters count in fixed windows. |
| `await cache.touch(key, ttl=...)` | Gives an entry a new expiry (`None`: never). Returns `True` if the key exists. |
| `await cache.clear()` | Deletes every key of this cache, all versions. Returns how many. |
| `await cache.check()` | Raises if the store cannot be reached. `cache check` and `/ready` use it. |
| `await cache.close()` | Stops the computations still running, then closes the store. Shutdown calls it. |
| `cache.make_key(key)` | Returns the full key stored for `key`. |

`ttl` is in seconds or a `timedelta`. Leave it out for the cache's `TTL`
setting (the value `DEFAULT_TTL` stands for this), or pass `None` to keep
the entry until it is evicted. The type `TTL` names what `ttl` accepts.

A miss reads as `None`. To cache a value that may be `None` (a missing
row), use `get_or_set`: it stores `null` and tells the two apart.

## Cached functions: `@cached`

`@cached` caches what an async function returns, by its arguments. It
calls `get_or_set` for you:

```python
from domainkit.cache import cached


@cached(ttl=120)
async def product_summary(product_id: UUID) -> ProductSummary:
    return await load_summary(product_id)


await product_summary(product_id)  # computed, then cached
product_summary.key(product_id)  # the key, to delete it elsewhere
await product_summary.invalidate(product_id)  # True: the entry is gone
await product_summary.invalidate(product_id)  # False: it was gone already
```

`product_summary.key(product_id)` returned
`'shop.queries.product_summary:04c7973c71f41c78524bd521d871a48a'`: the
function's import path, then a hash of its arguments.

- The return annotation types the cached value, as the `Type` of
  `get_or_set` does.
- The arguments must be JSON-friendly, because they are hashed into the
  key. Other arguments raise `TypeError`.
- Decorate functions, not methods.
- Pass `name="shop.product_summary"` to keep the entries when you move the
  function to another module. The name starts every key.
- Pass `cache="shared"` to use another cache than `default`.
- `@cached` refuses a function that is not async (`TypeError`).

The decorated function is a `CachedFunction`. Its attributes are
`function`, `cache`, `ttl`, `name`, and `return_type`.

## Caching whole responses

`@cache_page` caches what a GET endpoint returns. Put it under the route
decorator:

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from domainkit.cache import cache_page
from domainkit.http.access import Public, Require

router = APIRouter()


class ProductOut(BaseModel):
    id: int
    name: str


@router.get("/products", dependencies=[Public])
@cache_page(ttl=60)
async def list_products(category: str | None = None) -> list[ProductOut]:
    return [ProductOut(id=1, name="Lamp")]


@router.get("/products/{product_id}", dependencies=[Public])
@cache_page(ttl=300, vary=["X-Tenant"], max_age=60)
async def read_product(product_id: int) -> ProductOut: ...


@router.get("/me/basket", dependencies=[Depends(Require())])
@cache_page(ttl=30, vary_on_user=True)
async def my_basket() -> list[ProductOut]: ...
```

Two calls to `GET /products` run the endpoint once. The answer carries
these headers:

```text
Cache-Control: public, no-cache
ETag: W/"64119bdc155f4a3b7b7fde2be71b8d5b"
Vary: Authorization
```

A third request with `If-None-Match: W/"64119bdc155f4a3b7b7fde2be71b8d5b"`
gets `304` and no body.

- **What is cached** is the endpoint's return value, typed by its return
  annotation as with `@cached`. Only the fields the annotation declares
  are stored. FastAPI serializes the value on every request, so
  `response_model` filtering always applies. Annotate the return type
  with the model that the response shows. An endpoint that returns a
  `Response` cannot be cached this way (`TypeError`). Headers that an
  endpoint sets on its `Response` are not cached: on a hit, the endpoint
  does not run.
- **Entries are keyed** by the endpoint (or `name`); the URL's scheme,
  host, and path, so tenants chosen by `Host` never share entries; the
  query, in any order of names; the request's language (see
  [i18n.md](i18n.md)); and the request headers listed in `vary`. Only GET
  answers are cached. Refusals and errors never are.
- **Who asks.** The route's dependencies run on every request, the cached
  ones too, so rules (`Require`) and throttles still decide first. A
  request with credentials is never served from the cache, or stored in
  it. Credentials are an actor, the `Credentials` that an authentication
  middleware accepted (from a token, a cookie, or a client certificate),
  or an `Authorization` header. With `vary_on_user=True`, each actor (or,
  with no actor bound, each user id) has entries of their own, and
  anonymous callers share theirs. Credentials with neither an actor nor a
  user id are never cached either way.
- **Browsers and proxies.** The framework answers `Cache-Control:
  no-store` by default. Cached pages change this on purpose: `public,
  no-cache` (keep it, but ask each time), `private` when they vary on the
  user, or `max-age=<max_age>` to let clients reuse an answer without
  asking. They add `Vary: Authorization` and the `vary` headers, so that
  shared caches keep them apart too. With `LocaleMiddleware`, `Vary` also
  names `Accept-Language`.
- **Validators.** Each answer carries a weak `ETag` of its value. A
  request whose `If-None-Match` names it (or `*`) gets 304 without a body,
  from the cache.
- **Freshness** is the TTL's, as for any entry. To drop every page at
  once, raise the cache's `VERSION` or run `cache clear`. Keep TTLs short
  for data that changes.
- **Failures.** The cache fails open, as everywhere: while it is down,
  every request runs the endpoint.
- **Borrowing.** By default the endpoint may use what its request holds
  (`borrows=True`). See [What a factory may use](#what-a-factory-may-use)
  for `borrows=False`.

`@cache_page` raises `TypeError` for an endpoint that is not async, that
returns a `Response`, or that has a dependency written with `yield` while
`borrows=False`. A negative `max_age` raises `ValueError`.

## Values are JSON

Values are stored as JSON, never pickled. Pickle runs code while it
loads, so anyone who could write to Redis could take over every service
that reads from it. JSON cannot do that.

- Cache models, dataclasses, dicts, lists, strings, numbers, UUIDs,
  dates, and decimals. Anything else is refused when you store it:

  ```text
  TypeError: object values cannot be cached: Unable to serialize unknown type: <class 'object'>. Cache JSON-friendly values (models, dataclasses, dicts, lists, UUIDs).
  ```

- Pass a type to read a value back: `get(key, ProductSummary)`,
  `get(key, list[Line])`, `get(key, UUID)`. Pydantic validates the JSON,
  so a corrupted or outdated entry reads as a miss, with a warning in the
  log, instead of becoming a wrong object:

  ```text
  WARNING domainkit.cache.core: Cache 'default' holds an entry that is not a valid UUID (ValidationError); treating it as a miss
  ```

- `get_or_set` returns its value through JSON on a miss too, so a miss
  and a hit return the same shape. Code that works only on misses fails
  in tests.
- Cache read models, not aggregates. An
  [aggregate](learning.md#words) in a cache escapes its unit of work:
  changes to it are never saved, and its version check does not see the
  cached copy.

## Keys

- Keys have 1 to 1,024 characters. A longer key raises
  `ValueError: Cache keys have 1 to 1024 characters; hash long ones.`
  Hash long or user-supplied parts, as `@cached` does with its arguments.
- Anyone who can run `SCAN` on the server can see the keys. Keep secrets
  and personal data out of them; use ids.
- **Put the caller in the key of anything that depends on who asks.** An
  answer cached under `report:{report_id}` is served to every caller who
  asks for that report, also to callers that the first caller's
  permissions would have refused. Check permissions before you read the
  cache, or key by tenant and user (`report:{tenant_id}:{report_id}`).

## Keeping entries fresh

Entries expire after their TTL. To drop them sooner when the data
changes, delete them after the change commits. `invalidate_on` does that
for [domain events](learning.md#words), which units of work publish after
they commit:

```python
from domainkit.apps import DomainConfig
from domainkit.cache import invalidate_on


class ShopConfig(DomainConfig):
    def ready(self) -> None:
        invalidate_on(
            ProductRestocked,
            lambda event: [product_summary.key(event.product_id)],
            cache="default",
        )
```

- Call `invalidate_on` in `DomainConfig.ready()`. It subscribes a handler
  to the event bus (`bus=` names another bus).
- Deleting after the commit, not before, matters. If you delete first, a
  request between the delete and the commit can cache the old data again.
- A narrow race remains. A request that read the database shortly before the
  commit can store the old value right after the delete. The TTL limits
  how long that lasts, so keep TTLs as short as the data needs.
- A deletion that fails (Redis is down) is logged and reported by the
  event bus. The entry then lives until its TTL ends.
- Memory caches are per process: a deletion in one replica does not reach
  the others. Keep in memory only values that may be stale for one TTL.

### Replace a value that changed: `refresh`

`refresh` computes a key's value now and stores it in place of the cached
one. Callers are served the old value until the new one lands, so none of
them misses and computes it too, in any process that shares the cache:

```python
await caches["default"].refresh(
    f"product-summary:{product_id}", lambda: load_summary(product_id)
)
```

- It does not join a computation of the key that is already running,
  because that computation may have read the data from before the change.
  `get_or_set` callers that come next share this one.
- It stores the value as `get_or_set` stores it, so the entry is still
  refreshed early before it expires.
- It raises what `factory` raised, and leaves the old value. A store that
  fails is logged and skipped.

## Stampedes

When a popular entry expires, every request that misses it would compute
it again at once, and the database takes the whole load. `get_or_set`
(and so `@cached`) prevents that in two ways:

- **One computation per key per process.** Concurrent misses on the same
  key wait for one computation. This also holds while the cache fails and
  every call is a miss. Each caller still gets its own copy, so no two
  requests share a mutable object. Errors reach every waiting caller, and
  they are never cached. The computation serves every caller, so it runs
  as none of them: in a clean context, without the first caller's
  request, actor, language, span, or `Idempotency-Key` (see
  [runtime.md](runtime.md#clean-contexts)). A factory that needs one of
  them takes it as an argument, and the key must hold it. `@cache_page`,
  whose keys hold the user and the language, runs the endpoint as the
  caller.
- **Early refresh (XFetch).** Each entry records how long it took to
  compute. As its expiry comes near, a read may decide, at random, to
  compute it again while everyone else still gets the old value. The
  chance rises as the expiry comes nearer, and it is higher for costly
  entries. So hot entries are usually refreshed before they expire at
  all. If that refresh fails, the cached value is still valid, and it is
  served until it expires. `EARLY_RECOMPUTE` scales how early; `0` turns
  it off.

With several replicas, each replica may still compute a hot key once.
That is the worst case, not one computation per request.

### What a factory may use

A computation serves every caller that waits for its key, so by default
it belongs to none of them (ADR 0071):

- **It goes on when its callers stop waiting.** A caller cut off by its
  deadline, or whose client went away, stops waiting. The computation
  runs on, stores its value, and later requests are served from the
  cache. A factory slower than one request's deadline still fills the
  cache, so a slow upstream recovers instead of failing every request.
- **No caller's deadline reaches it**, so one short request never fails
  the others. Its limit is the cache's own `COMPUTE_TIMEOUT`. Its
  transactions lower `statement_timeout` to the time it has left, and
  its outgoing calls through `domainkit.http.client` keep to it. When the
  time runs out, every caller that waits gets the same `TimeoutError`.
  `COMPUTE_TIMEOUT` is `None` (no limit) by default, as in 0.9.0. Set it,
  for example to `REQUEST_TIMEOUT`'s value. `check` warns where deployed
  while it is not set (`caches.W001`), and the `init` template sets it to
  30 seconds.

  Each choice has its failure. With no limit, one computation that hangs
  (an upstream that never answers) holds its key until the process
  restarts: every later caller waits on it. With a limit, a healthy
  computation slower than the limit fails for every caller and never
  fills the cache. With 30 seconds, a 31-second computation fails at 30
  seconds, and a hung one frees its key at 30 seconds.
- **So the factory must use nothing that a caller holds and releases**:
  not the session of the caller's unit of work, not a client that the
  request closes. It opens what it needs
  (`async with ReportsUnitOfWork() as uow: ...`) and takes values as
  arguments. `@cached` functions take only JSON values, so they keep to
  this already.

A factory that does use what its caller holds says so with
`borrows=True`:

```python
async with ReportsUnitOfWork() as uow:
    report = await caches["default"].get_or_set(
        f"report:{month}", lambda: uow.reports.month(month), Report, borrows=True
    )
```

`@cache_page` borrows by default. An endpoint can reach its request's
resources in more ways than can be seen: its dependencies; its route's,
router's, or application's dependencies; `request.state` set by a
middleware; a context variable. `@cache_page(borrows=False)` says that
the endpoint uses none of them, so its computation fills the cache after
the request is cut off. It refuses an endpoint whose own dependencies
hold a resource: one written with `yield`, also beneath another, behind
`functools.partial`, or behind a `functools.wraps` decorator, as FastAPI
finds them, because FastAPI closes them when the request ends. A request
whose route, router, or application adds such a dependency borrows
anyway. A router that the route was included through, `request.state`,
and context variables are not in the request to check, so you must keep
them out yourself.

A computation that borrows is bound to the caller that started it, its
*owner*:

- **It never outlives its owner's wait.** When the owner is cancelled,
  the computation is cancelled, and the owner's cancellation goes on only
  once the factory has stopped. Nothing runs on the owner's session after
  the owner closed it, and no connection is left checked out.
- **It runs under the owner's deadline**, which the owner's timer
  enforces.
- **What is the owner's stays the owner's.** When the owner is cancelled,
  or the factory fails after the owner's deadline has passed, the callers
  still waiting do not get that error. They share a new computation,
  started by one of them with its own factory, under its own deadline.
  Any other error, the factory's own timeout among them, reaches every
  waiting caller, so a hung upstream is called once, not once per caller.
- It stores nothing once its owner is gone. So a factory slower than its
  callers' deadlines never fills the cache. Give such pages their own
  unit of work and `borrows=False`, and give such factories their own
  unit of work instead of borrowing.

A factory must not hand what it borrowed to anything that outlives the
call: a task it starts and does not await, or the value it returns. (The
value is stored as JSON, so it holds data, never a session.)

Shutdown and `close()` stop every computation and say so: callers still
waiting then compute their own value, without the cache.

Computations are background tasks of their event loop
(`domainkit.tasks.spawn`). At shutdown they are cancelled before caches,
databases, ledgers, and brokers close, so none outlives the connections
it uses. Closing a cache also cancels its own computations.

## When the cache fails

- A connection error or a timeout skips the cache for `FAILURE_COOLDOWN`
  seconds (5 by default), logged once. Requests do not each wait for a
  timeout during an outage.
- `get_or_set` and `@cached` compute the value and return it, once per
  key for concurrent callers, and do not write it back. They also go on
  when Redis refuses a write (for example, when it is out of memory).
- The plain operations (`get`, `set`, `delete`, and the others) raise
  `CacheUnavailable` when the cache cannot be reached or is skipped, and
  `CacheError` when Redis refuses a command. So code that depends on the
  cache can tell. `CacheUnavailable` is a kind of `CacheError`.
- Timeouts are short: a command may take `SOCKET_TIMEOUT` (1 second),
  with `RETRIES` (1) quick retries.
- `/ready` ignores caches unless their `READINESS` is on. If a cache that
  the service can work without made `/ready` fail, every replica would
  leave the load balancer at once.
- With `domainkit[metrics]`, `cache_lookups_total{cache, result}` counts
  hits, misses, invalid entries, and early refreshes, and
  `cache_errors_total{cache}` counts failures.

```python
from domainkit.cache import CacheError, CacheUnavailable

try:
    await caches["shared"].set("feature:new-checkout", True)
except CacheUnavailable:
    ...  # Redis is down, or skipped for FAILURE_COOLDOWN seconds
except CacheError:
    ...  # Redis refused the command
```

## A cache store of your own

A cache keeps its entries in a *store*. `Cache` builds the keys, encodes
the values, and handles failures on top of it, so a store only keeps
bytes under keys. To keep entries somewhere else, subclass
`domainkit.cache.Store` and name it in the cache's `BACKEND`:

```python
from collections.abc import Mapping, Sequence

from domainkit.cache import Store
from domainkit.conf import CacheConfig


class DictStore(Store):
    """Entries in a dict, with no expiry: an example, not for production."""

    def __init__(self, config: CacheConfig) -> None:
        self.entries: dict[str, bytes] = {}

    async def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        return [self.entries.get(key) for key in keys]

    async def set_many(self, items: Mapping[str, bytes], ttl: int | None) -> None:
        self.entries.update(items)

    async def add(self, key: str, value: bytes, ttl: int | None) -> bool:
        return self.entries.setdefault(key, value) is value

    async def delete_many(self, keys: Sequence[str]) -> int:
        return sum(self.entries.pop(key, None) is not None for key in keys)

    async def incr(self, key: str, delta: int, ttl: int | None) -> int:
        total = int(self.entries.get(key, b"0")) + delta
        self.entries[key] = str(total).encode()
        return total

    async def touch(self, key: str, ttl: int | None) -> bool:
        return key in self.entries

    async def clear(self, prefix: str) -> int:
        return await self.delete_many([k for k in self.entries if k.startswith(prefix)])
```

```python
CACHES = {
    "default": {"URL": "memory://"},
    "local": {"URL": "dict://", "BACKEND": "shop.cache.DictStore"},
}
```

- `ttl` is in milliseconds, and `None` keeps the entry.
- The store is made as `Store(config)` once per event loop, from its
  `CACHES` entry. The `URL` is the store's to read, with a scheme of its
  choosing.
- Raise `CacheUnavailable` when the backend cannot be reached, and
  `CacheError` when it refuses an operation.
- `check()` and `close()` are optional; override them for a store with
  connections.
- A `BACKEND` that names no `Store` class raises `ImproperlyConfigured`.

The [cache store recipe](recipes/cache-store.md) builds a complete store
in a SQLite file, with expiry. The built-in stores are public too:
`MemoryStore` (`memory://`, the least recently used entries are evicted
beyond `max_entries`) and `DummyStore` (`dummy://`, keeps nothing).

`caches` is the process's `CacheHandler`. Besides `caches[alias]`, it has
`config(alias)`, which returns the `CacheConfig` of an alias; `reset()`,
which empties every memory store (the test plugin calls it); and
`check_all`, `close_all`, and `stop_computations`, which `/ready` and
shutdown call.

## Rate limits for every request

`RATE_LIMIT` limits the requests of each caller to the whole service. A
caller is the authenticated actor, or else the client address:

```python
RATE_LIMIT = "300/minute"
MIDDLEWARE = [
    # ...
    "domainkit.http.middleware.RequestContextMiddleware",
    "domainkit.http.ratelimit.RateLimitMiddleware",  # Off until RATE_LIMIT is set.
    # ...
]
```

- `RateLimitMiddleware` does the counting. The project template lists it
  already, after `RequestContextMiddleware`.
- Each caller has a bucket of `limit` requests that refills smoothly at
  `limit` per period. It is implemented with GCRA (the generic cell rate
  algorithm), which keeps one timestamp per caller. After an idle time, a
  window of one period can see up to about twice `limit`.
- Answers carry `RateLimit-Limit`, `RateLimit-Remaining`, and
  `RateLimit-Reset`. A refused request gets 429 with the code
  `rate_limited` (the constant `RATE_LIMITED`) and `Retry-After`.
- A WebSocket handshake counts as one request. Over the limit, it gets
  the same 429 where the server can refuse a handshake with a status, and
  a close with code 1013 (try again later) where it cannot.
- IPv6 callers are counted per network of `RATE_LIMIT_IPV6_PREFIX` bits
  (a /64 by default), because one customer usually holds a whole /64.
- The paths in `RATE_LIMIT_EXEMPT_PATHS` (`/health`, `/ready`, `/metrics`)
  are never counted, so probes and scrapers are not refused.

`check` warns while `RATE_LIMIT` is not set, or the middleware is not
listed.

## Shared rate limits

`RATE_LIMIT` counts per process by default (`MemoryBackend`), so ten
replicas allow ten times the limit. The memory backend keeps the 100,000
most recently seen callers, so its memory and the cost of a request stay
limited. A caller seen longer ago starts again from a full quota. Keep the
counts in Redis to share them, and to count every caller:

```python
RATE_LIMIT = "300/minute"
RATE_LIMIT_BACKEND = "domainkit.http.ratelimit.RedisBackend"
RATE_LIMIT_CACHE = "shared"  # a redis:// or rediss:// cache
```

The same algorithm as in memory (GCRA) runs in one Lua script on the
server's clock, so replicas never race, and their clocks do not matter.
Each caller costs one key, `<KEY_PREFIX>:ratelimit:<caller>`, kept only
until the caller's quota is full again. `RedisBackend` refuses a cache
that is not Redis (`ImproperlyConfigured`).

If Redis fails, or refuses the count (out of memory, a read-only replica),
requests are let through and logged. The limit protects the service, and
its outage should not stop the service. To keep some limit during an
outage, set `RATE_LIMIT_FALLBACK_REPLICAS` to the number of replicas:

```python
RATE_LIMIT_FALLBACK_REPLICAS = 10
```

While Redis is down, each process then counts each caller on its own, at
`RATE_LIMIT` divided by 10, so all the replicas together let about the
limit through. It counts at most `FALLBACK_KEYS` (10,000) callers. A
caller who still owes requests is never forgotten, and while all of them
do, new callers are refused.

Password and code checks are the exception: with
`domainkit.contrib.auth`, staging and production refuse them while Redis
cannot count them, and refuse to start with a backend that counts in each
process ([auth.md](auth/logging-in.md#logging-in-and-out)). `cache clear`
resets these counters too.

### A backend of your own

`RATE_LIMIT_BACKEND` is the dotted path to a class with one method,
`hit(key, limit, period)`, that returns a `Decision`:

```python
from domainkit.http.ratelimit import Decision


class AllowEveryone:
    """A backend that lets every request through and counts nothing."""

    async def hit(self, key: str, limit: int, period: float) -> Decision:
        return Decision(allowed=True, remaining=limit, reset_after=0.0, retry_after=0.0)
```

The `Backend` protocol describes this class. `Decision` has `allowed`,
`remaining`, `reset_after` (seconds until the full quota is back),
`retry_after` (seconds until the next request would be allowed), and
`counted` (`False` when the backend could not count, as when Redis
fails). A backend that can also give a counted request back, with
`refund(key, limit, period)`, is a `RefundingBackend`: the login
throttles of `domainkit.contrib.auth` then count only the attempts that
fail. `MemoryBackend` and `RedisBackend` are both refunding backends.

## Throttles for one route

`RATE_LIMIT` counts every request of a caller. A route that costs more
(an export, a search, sending an email) can have its own, tighter limit
with `Throttle`, a FastAPI dependency. It counts with the same backend as
`RATE_LIMIT_BACKEND` (Redis if you set `RedisBackend`), apart from the
global limit, which it does not need:

```python
from fastapi import APIRouter, Depends, Request

from domainkit.http.access import Public, Require
from domainkit.http.ratelimit import Throttle

router = APIRouter()
export_limit = Throttle("5/hour")  # per user, or per address when anonymous


@router.post("/exports", dependencies=[Depends(Require()), Depends(export_limit)])
async def create_export() -> None: ...


def api_key(request: Request) -> str | None:
    return request.headers.get("X-API-Key")  # None: count by address


@router.get(
    "/search",
    dependencies=[Public, Depends(Throttle("30/minute", key=api_key))],
)
async def search(q: str = "") -> dict[str, str]:
    return {"q": q}


# Routes with one scope share one count.
mail_limit = Throttle("10/hour", key="address", scope="mail")
```

With `Throttle("2/minute")` on `/search`, the third request in a minute
got this answer:

```text
HTTP 429
ratelimit-limit: 2
ratelimit-remaining: 0
ratelimit-reset: 60
retry-after: 30
```

```json
{
  "type": "about:blank",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "Too many requests; slow down.",
  "instance": "/search",
  "code": "rate_limited",
  "request_id": "5fc4ce02c2df56870ec65d4ab50d9a04"
}
```

- `key="user"` (the default) counts the actor, and anonymous callers by
  address. `key="address"` counts by address, even for users. A function
  of the request (sync or async) counts by what it returns, hashed before
  it is stored, or by address when it returns `None`.
- Each route counts on its own unless routes share a `scope` (1 to 128
  letters, digits, or `_.-`).
- `backend=` counts in a backend you give, instead of the application's
  `RATE_LIMIT_BACKEND`.
- Over the rate, the route answers 429 `rate_limited`, with `Retry-After`
  and the `RateLimit-*` headers of that throttle. A WebSocket handshake
  is closed with 1013. Requests that pass carry no headers from the
  throttle.
- A throttle does not decide who may call a route: the route still needs
  `Require(...)` or `Public`. Put the throttle after `Require`, so that
  refused callers are not counted.
- Logging in and refused credentials keep their own limits
  (`AUTH.LOGIN_RATE`, and `RATE_LIMIT` for refused tokens; see
  [auth.md](auth.md)).

## Security on the server

Give each service its own Redis user, limited to its prefix and to the
commands domainkit uses. CI checks that this is enough for every
operation, the rate limiter included:

```text
ACL SETUSER billing on >long-random-password resetkeys ~billing:* resetchannels
    -@all +@connection +get +mget +set +del +unlink +exists +pexpire +persist
    +incrby +time +scan +evalsha +script|load
```

Also:

- Keep the server off the public network, and use TLS (`rediss://`) to
  it.
- Set `maxmemory` with an eviction policy, such as `allkeys-lru`, so that
  a full cache evicts entries instead of refusing writes.
- domainkit does not trust the server's data: entries are validated on
  the way out, and pickle is never used.

## Operations

```bash
python manage.py cache check            # ping every cache; fails if one is down
python manage.py cache check shared     # ping one cache
python manage.py cache clear shared     # delete the shared cache's keys
python manage.py cache clear --all
```

`clear` scans for the cache's prefix and deletes in batches. It never runs
`FLUSHDB`, so other services that share the server keep their keys.
`SCAN` walks the whole server, though. On a large shared server, raising
`VERSION` drops a cache's entries without touching the others.

On a laptop with Docker, one process reached:

| Operation | Rate |
| --- | --- |
| Typed read, memory | about 460,000 per second (2 µs) |
| Typed read, Redis or Valkey, one caller | about 4,400 per second (0.23 ms per round trip) |
| Typed read, Redis or Valkey, 50 concurrent callers | 14,000 to 25,000 per second, with or without TLS |
| Rate-limit check in Redis, 50 concurrent callers | about 25,000 per second |

Inside a data center, round trips are shorter than through Docker on a
laptop.

## Options

Each `CACHES` entry takes these keys. The
[backends reference](reference/backends.md#caches) has the full rows.

| Key | Default | What it does |
| --- | --- | --- |
| `URL` | `memory://` | Where entries live ([Kinds of cache](#kinds-of-cache)). Kept secret. |
| `TTL` | `300.0` | Seconds an entry lives unless a call says otherwise; `None` keeps entries. |
| `KEY_PREFIX` | the alias | Starts every key. Required for Redis. |
| `VERSION` | `1` | Part of every key; raise it to drop every entry. |
| `MAX_ENTRIES` | `10000` | `memory://` only: the least recently used entries are evicted beyond it. |
| `SOCKET_TIMEOUT` | `1.0` | Seconds one Redis command may take. |
| `CONNECT_TIMEOUT` | `1.0` | Seconds to connect, or to wait for a free connection. |
| `RETRIES` | `1` | Retries after a connection error or a timeout. |
| `MAX_CONNECTIONS` | `50` | Redis connections per process and event loop (per node in a Cluster). |
| `FAILURE_COOLDOWN` | `5.0` | Seconds a failed cache is skipped. |
| `EARLY_RECOMPUTE` | `1.0` | How early hot entries are refreshed; `0` turns it off. |
| `COMPUTE_TIMEOUT` | `None` | Seconds a shared computation may run (at most an hour). |
| `TLS_CA`, `TLS_CERT`, `TLS_KEY` | `None` | Certificate authority and client certificate, for `rediss://` only. |
| `READINESS` | `False` | Fail `/ready` while this cache cannot be reached. |
| `SENTINEL_PASSWORD` | `None` | The Sentinels' own password. |
| `BACKEND` | `None` | A `Store` class of your own. |

The rate-limit settings; the [HTTP reference](reference/http.md#http) has
the full rows:

| Setting | Default | What it does |
| --- | --- | --- |
| `RATE_LIMIT` | `None` | Requests per caller, such as `"300/minute"`; `None` turns the limit off. |
| `RATE_LIMIT_BACKEND` | `domainkit.http.ratelimit.MemoryBackend` | Where the counts live; `RedisBackend` shares them. |
| `RATE_LIMIT_CACHE` | `"default"` | The Redis cache that `RedisBackend` counts in. |
| `RATE_LIMIT_EXEMPT_PATHS` | `("/health", "/ready", "/metrics")` | Exact paths that are never counted. |
| `RATE_LIMIT_FALLBACK_REPLICAS` | `None` | While Redis is down, count in each process at the limit divided by this (1 to 1,024). |
| `RATE_LIMIT_IPV6_PREFIX` | `64` | IPv6 callers are counted per network of this many bits (1 to 128). |

The arguments of the decorators and of `Throttle`:

| Name | Arguments |
| --- | --- |
| `get_or_set(key, factory, type=None, ttl=DEFAULT_TTL, *, borrows=False)` | `borrows=True`: the factory uses what its caller holds. |
| `@cached(*, ttl=DEFAULT_TTL, cache="default", name=None)` | `name` starts every key; default: the function's import path. |
| `@cache_page(ttl=DEFAULT_TTL, *, cache="default", vary=(), vary_on_user=False, max_age=0, name=None, borrows=True)` | `vary`: request headers the answer depends on. `max_age`: seconds clients may reuse the answer. |
| `invalidate_on(event_type, keys, *, cache="default", bus=event_bus)` | `keys(event)` returns the keys to delete. |
| `Throttle(rate, *, key="user", scope=None, backend=None)` | `key`: `"user"`, `"address"`, or a function. `scope`: a shared count. |

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `ValidationError`: "Redis caches need a KEY_PREFIX, unique among the services that share the server" | A Redis cache without `KEY_PREFIX`. | Set `KEY_PREFIX`. |
| `ValidationError`: "a cache URL starts with memory://, redis://, ..." | An unknown scheme without `BACKEND`. | Fix the URL, or set `BACKEND`. |
| `ValidationError`: "TLS_CA, TLS_CERT and TLS_KEY need a rediss:// URL" | TLS settings on a cache without TLS. | Use `rediss://`. |
| `ImproperlyConfigured`: "Redis caches need the redis extra" | redis-py is not installed. | `pip install "domainkit[redis]"`. |
| `CacheUnavailable` | The cache cannot be reached, or is skipped after a failure. | Catch it, or use `get_or_set`, which computes the value instead. |
| `CacheError` | Redis refused a command, or `incr` met a value that is not an integer. | Check the server's memory and ACL. |
| `TypeError` from `set` | The value cannot be stored as JSON. | Cache a model, a dataclass, or plain data. |
| `TimeoutError` from `get_or_set` | The shared computation ran out of `COMPUTE_TIMEOUT`. | Make the factory faster, or raise the limit. |
| 429 `rate_limited` | The caller went over `RATE_LIMIT` or a `Throttle`. | The client waits `Retry-After` seconds. |
| WebSocket close 1013 | A handshake over the limit, where a status cannot be sent. | The client connects again later. |
| `caches.W001` from `check` | A deployed cache has no `COMPUTE_TIMEOUT`. | Set it. |

## Testing

- The `domainkit.testing` plugin empties memory caches before every test,
  so no test sees what another one cached.
- Point a test's `CACHES` at `dummy://` to check that code works without
  its cache, or at `memory://` to check that it uses it:

  ```python
  def test_the_summary_works_without_a_cache(settings, client) -> None:
      settings.CACHES = {"default": {"URL": "dummy://"}}
      assert client.get("/products").status_code == 200
  ```

- To test against Redis, give the test settings their own `KEY_PREFIX`.
  The plugin does not clear Redis.

More in [testing.md](testing.md).

## Common mistakes

- **Caching an aggregate.** Its changes are never saved, and its version
  check does not see the copy. Cache a read model instead.
- **Leaving the caller out of the key.** An answer that depends on who
  asks is then served to everyone. Put the tenant and the user in the
  key, or check permissions before the read.
- **Using the caller's session in a factory without `borrows=True`.** The
  computation runs on after the caller closed the session. Open your own
  unit of work in the factory, or pass `borrows=True`.
- **Deleting an entry before the commit.** A request in between caches the
  old data again. Use `invalidate_on`, which deletes after the commit.
- **Counting rate limits in memory behind several replicas.** Each
  replica allows the full limit. Set `RATE_LIMIT_BACKEND` to
  `RedisBackend`.

## See also

- [Runtime](runtime.md#clean-contexts): the clean context that a shared
  computation runs in.
- [Backends reference](reference/backends.md#caches): every `CACHES` key.
- [HTTP reference](reference/http.md#http): every rate-limit setting.
- [A cache store of your own](recipes/cache-store.md): a complete store,
  step by step.
- [Logging in](auth/logging-in.md#logging-in-and-out): the login
  throttles.
- [The glossary](learning.md#words): aggregate, domain event, unit of
  work.
