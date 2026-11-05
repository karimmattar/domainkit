# 0127. Ping a pooled connection only after it rested

- Status: Accepted
- Date: 2026-11-05

## Context

[ADR 0061](0061-read-without-a-transaction-and-ping-once.md) cut the
asyncpg pre-ping to one round trip, and kept it on every checkout: turning
it off would make every writable path fail once per stale connection after
a failover or a restart.

Measured again for 1.8, one read at a time against FastAPI's SQLAlchemy
Core app, the ping was the largest part of the gap that the unit of work
did not explain:

| Data access of one read, no HTTP | ms |
|---|---:|
| SQLAlchemy Core, a bare engine | 0.64–0.74 |
| SQLAlchemy Core, this framework's engine (the ping) | 0.93–0.98 |
| The same, with `POOL_PRE_PING = False` | 0.75 |

An authenticated read takes two connections (the token's lookup, then the
unit of work), so it pinged twice.

What the ping finds, on asyncpg outside PgBouncer:

- **The server closed the connection** (a restart, a failover,
  `pg_terminate_backend`, `idle_session_timeout`). asyncpg reads the
  server's goodbye, or the socket's end, as soon as the event loop runs,
  and the connection reports itself closed without a round trip. The pool
  already replaces such a connection as it hands it out when it does not
  ping (`_replace_closed`).
- **The network dropped it silently** (a NAT or a firewall forgetting an
  idle flow, a host that vanished). Only a round trip finds that. A flow
  is forgotten after it rests, not while it is busy. A connection that is
  busy when its host vanishes is not saved by a ping either: the ping
  waits as long as the statement would.

## Decision

On asyncpg outside PgBouncer, with `POOL_PRE_PING` (the default), the pool
does not ping at every checkout. As a connection leaves the pool:

1. One that asyncpg knows is closed is replaced, as before.
2. One back in the pool for `PING_IDLE` (1 second) or longer is pinged,
   with SQLAlchemy's own test through the dialect, so its judgement of an
   error stays SQLAlchemy's. A connection that is gone is replaced.
3. One back for less, or just made, is handed out as it is.

Through PgBouncer, and on other drivers, SQLAlchemy pings at every
checkout, as before. `POOL_PRE_PING = False` still turns the test off,
keeping step 1.

## Consequences

- A busy process does not ping: the benchmark ladder's database rungs went
  from 1 ping a request (3 for a keyed write) to none, and from 2 to 4%
  fewer Python calls. Over a network, each ping saved is a round trip.
- A connection that rests for a second or more is tested as before, so
  what a silently dropped flow does is unchanged.
- A connection whose server closed it in the last second, without asyncpg
  noticing yet, is not tested. asyncpg notices before the next statement
  is sent on it whenever the event loop has run in between, which it has
  for any connection that went back to the pool.
- The window is a constant, not a setting. Lowering it to 0 restores a
  ping at every checkout; nobody has needed that.
