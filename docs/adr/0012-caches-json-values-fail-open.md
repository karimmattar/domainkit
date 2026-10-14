# 0012. Caches with JSON values that fail open

- Status: Accepted
- Date: 2026-10-14

## Context

Services built on domainkit need to cache query results, both in each
process and shared through Redis or Valkey. A framework cache decides things
every project then lives with:

- **The API.** Django's cache API (`get`, `set`, `add`, `get_or_set`,
  `incr`, `touch`, `clear`) is familiar. Libraries such as aiocache and
  cashews add async support, but each brings its own settings, serializers,
  and decorators that do not follow domainkit's conventions (aliases in
  settings, per-loop connections, the layer rules).
- **Serialization.** Pickle stores any object, but loading it runs code.
  Anyone who can write to the cache server can then take over every process
  that reads from it. JSON stores less, but cannot run code, and Pydantic
  validates it back into types.
- **Failures.** Raising when Redis is down turns a cache outage into a
  service outage. Ignoring every error hides misconfigurations.
- **Stampedes.** When a hot entry expires, every request recomputes it.
  Distributed locks prevent that, but make every waiting request poll, and
  a lock held by a crashed process delays everyone. Probabilistic early
  refresh (XFetch: Vattani, Chierichetti and Lowenstein, 2015) needs no
  coordination: each read may refresh an entry shortly before it expires,
  more likely the closer the expiry and the costlier the entry.
- **Clearing.** `FLUSHDB` also deletes every other service's keys on a
  shared server.
- **Connections.** redis-py's asyncio connections belong to the event loop
  that opened them, like database connections.

## Decision

- `CACHES` holds caches by alias, chosen by URL scheme: `memory://` (per
  process, least recently used eviction), `redis://`/`rediss://` (Redis or
  Valkey through redis-py, the `redis` extra), and `dummy://` (keeps
  nothing). `default` always exists and is a memory cache unless set.
- One `Cache` class gives the API: keys, values, expiry, and failure
  handling. Backends implement a small `Store` port of byte operations.
  Keys are `<KEY_PREFIX>:<VERSION>:<key>`. Redis caches must set
  `KEY_PREFIX`, and `clear` deletes only that prefix (`SCAN` and `UNLINK`).
- Values are JSON (`pydantic_core`), never pickled. Reads take a type and
  validate against it. An entry that does not fit counts as a miss, so a
  changed model never breaks a deployment; raising `VERSION` drops all
  entries at once.
- The cache fails open. A connection error or timeout skips the cache for
  `FAILURE_COOLDOWN` seconds. `get_or_set` and `@cached` then compute
  values, and also do so when the server refuses a command. The plain
  operations raise `CacheUnavailable` (unreachable) or `CacheError`
  (refused), so callers that need the cache can tell. Timeouts are short (1
  s) with one retry, and `/ready` checks a cache only when its `READINESS`
  says so.
- Stampedes: concurrent misses on one key share one computation per process
  and event loop. Entries written by `get_or_set` record their expiry and
  how long they took to compute, so XFetch can refresh hot entries early
  (`EARLY_RECOMPUTE`, XFetch's beta, default 1). There are no distributed
  locks.
- Each event loop gets its own `Cache` objects and Redis clients; memory
  stores are shared by the whole process.
- `@cached` keys entries by function and a SHA-256 of the JSON arguments,
  and types them by the return annotation. `invalidate_on` deletes entries
  when a domain event is published, which units of work do after commit.
- In staging and production, a network Redis needs `rediss://`, certificate
  checks, and a password or client certificate.

## Consequences

- Only JSON-friendly values can be cached: models, dataclasses, and plain
  data, not arbitrary objects. That also keeps aggregates, which belong to
  their unit of work, out of caches.
- A Redis outage makes the service slower, not unavailable, and shows up in
  logs and `cache_errors_total`. Code calling `get` or `set` directly must
  handle `CacheUnavailable`.
- Several replicas may each recompute a hot entry once; there is no
  cluster-wide single computation. Projects that need one can add a lock
  around their factory.
- Memory caches are per process: invalidation does not reach other
  replicas, so their entries may be stale for one TTL.
- Redis Cluster and Sentinel are not supported. Multi-key operations would
  have to stay within one hash slot, and clearing would have to scan every
  node; either can be added later behind the same `Store` port.
- Rate limits in Redis use these caches; see
  [0013](0013-shared-rate-limits-in-redis.md).
