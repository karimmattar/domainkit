# 0013. Share rate limits through Redis, with GCRA on the server's clock

- Status: Accepted; amended 2026-10-25 (at the end); amended by [0098](0098-share-login-throttles-and-fail-closed-where-deployed.md)
- Date: 2026-10-14

## Context

`RATE_LIMIT` counts requests per caller with GCRA (the generic cell rate
algorithm) in process memory. With ten replicas behind a load balancer, a
caller gets ten times the limit. The limit has to live in a store every
replica shares.

Options considered:

- **Fixed windows** (`INCR` plus `EXPIRE`): simple, but a caller can send
  twice the limit around a window boundary.
- **Sliding logs** (a sorted set of timestamps per caller): exact, but
  memory grows with the limit, and every request writes several entries.
- **The redis-cell module** (`CL.THROTTLE`): GCRA built into Redis, but
  managed services and Valkey do not offer it.
- **GCRA in a Lua script**: one key per caller holding one timestamp, read
  and updated atomically on the server. It behaves exactly like the memory
  backend.

Two pitfalls shaped the script. Replicas' clocks differ, so the time must
come from one place. And comparing sums of large clock readings in floating
point once refused new callers at a limit of 1 (see the memory backend's
comments).

## Decision

- `domainkit.http.ratelimit.RedisBackend` runs GCRA in one Lua script
  against the Redis cache `RATE_LIMIT_CACHE`.
- The script reads the time with `TIME`, in integer microseconds, so
  replicas' clocks never matter. It compares the debt (`arrival - now`) with
  the tolerance (`period - interval`), as the memory backend does, and
  writes the next arrival with full precision.
- Each caller's key, `<KEY_PREFIX>:ratelimit:<caller>`, expires when its
  quota is full again, so idle callers cost nothing.
- If Redis fails, requests are let through, and the cache is skipped for
  its `FAILURE_COOLDOWN` (the same handling as every cache operation; see
  [0012](0012-caches-json-values-fail-open.md)). The limit protects the
  service from overuse; an outage of the limiter must not take the service
  down with it.
- The middleware, headers, and `Backend` protocol are unchanged.

## Consequences

- All replicas share one limit per caller, with the same burst and refill
  behaviour as before.
- Every counted request costs one round trip to Redis, about a quarter of a
  millisecond on a laptop. Exempt paths cost nothing.
- While Redis is down, there is no limit at all. Projects that must refuse
  traffic then should also limit at the gateway.
- `cache clear` on the limiter's cache resets every caller's quota.
- The script needs `TIME`, `GET`, `SET`, `EVALSHA`, and `SCRIPT LOAD`, all
  in the least-privilege user `docs/cache.md` shows.

## Amendment (2026-10-25)

Backends may now give a counted request back (`refund`), and say whether
they counted one (`Decision.counted`), for the attempt throttles of
`domainkit.contrib.auth` ([0068](0068-count-failed-attempts-and-hold-back-only-guesses.md)).
`RedisBackend` gives back in a second Lua script, on the server's clock,
with the same commands. Requests still pass while Redis is down; only
password and code checks and mail requests may be refused then, with
`AUTH.THROTTLE_FAIL_CLOSED`.
