# 0058. Keep principals in each process, and stamp revocations in a shared cache

- Status: Accepted
- Date: 2026-10-21
- Amends: [0016](0016-authenticating-requests.md), [0056](0056-reauthorize-open-connections.md)

## Context

Authenticating a request read its token, then the user with the password
hash, groups, and permissions (`selectin`): four or five `SELECT`s, a
`BEGIN`, a `ROLLBACK`, and a pre-ping, in a transaction of its own. The
re-audit measured it at about 3.1 to 3.3 ms of CPU in a 4.5 ms
authenticated read, about 70% (PERF-01, ASYNC-R13, FX-11). A request over
its rate limit paid the same before its 429, since authentication runs
first so that limits count users (N8).

Reading the table on every request also makes revocation exact: a token
deleted, a user deactivated, or a permission taken back fails the next
request, in every process. Keeping principals trades some of that for
speed. For a fintech service the trade must be bounded, short by default,
and exact in the process that made the change.

Options considered:

- **Keep reading, in fewer queries.** Two queries instead of four or five
  (ADR 0059) still cost a pool checkout, a pre-ping, and a transaction per
  request: about half the saving.
- **Keep principals in Redis.** One round trip per request, shared by every
  process, but users' personal data and permissions leave the process, the
  values must be encoded, and a Redis outage removes the saving and the
  cache at once.
- **Keep them in each process, for a time-to-live.** No round trip at all.
  A change made in another process is seen when the entry expires.
- **The same, with revocation stamps in a shared cache.** Each request
  checks a few small keys in Redis (one round trip, far cheaper than two
  queries and a transaction), so a change made anywhere is seen at once;
  the time-to-live bounds the rest.
- **Push invalidations through a broker.** No cost per request, but a lost
  or late message leaves a principal stale until it expires, and the
  request path gains a dependency on the broker.
- **Rate-limit before authenticating (N8).** A 429 would cost nothing, but
  limits could no longer count users, only addresses.

## Decision

- **A cache per process** (`domainkit.contrib.auth.principals`), shared by
  its event loops under one lock, on by default (`AUTH.PRINCIPAL_CACHE`).
  Entries are keyed by the authentication class's scheme and the
  credential's digest: the token's SHA-256, a certificate's, a JSON Web
  Token's login and user. Never by a raw value.
- **What an entry holds**: a snapshot of the user, their groups and
  permissions (ADR 0059), and what the token table says of the credential:
  kind, expiry, scopes, login. Never a second factor's secret, recovery
  codes, or the token. The password hash only until 0.10, for the
  deprecated `request.user.password` (ADR 0059). Only credentials their class
  accepted are kept; refusals never are, so guesses cannot fill the cache.
- **Checked on every request, kept or not**: a token's expiry against
  `domainkit.clock` (refused from memory as from the table), a cookie's
  CSRF checks, a JSON Web Token's signature and times. A kept login past
  its last token's expiry is read again (a refresh may have renewed it).
- **Bounds**: `MAX_ENTRIES` (10,000; least recently used first, at most
  100,000) and `TTL`, 5 seconds by default and 5 minutes at most. Five
  seconds is shorter than an operator takes to revoke anything after
  noticing it, and still removes most lookups: a client sending one
  request a second is read once in five, a busy one once in fifty or more.
  The ceiling is the default lifetime of a JSON Web Token's access token,
  the window domainkit already accepts for a credential it signed.
- **In the process that makes a change, at once.** The change is marked
  on its session and applied by the session's `after_commit`, never
  before the commit, so no request can read the old rows again in
  between: the token repository marks what it deletes (`DELETE ...
  RETURNING`), `revoke_user` the user, `record_login` the user (it
  changes `last_login`); the ORM marks users, groups, permissions, and
  tokens it updates or deletes (mapper events), which covers the API, the
  admin API, the management commands, erasure, and projects' own units of
  work. A group, permission, or content type changed drops every
  principal. Bulk statements fire no mapper event: a session's
  `do_orm_execute` sees each `UPDATE` or `DELETE` (and link `INSERT`) it
  executes on the tables principals are read from (`update_rows`,
  `delete_rows`, `update(User)`, Core statements) and drops every
  principal at the commit; the auth repositories' own writes, which mark
  what they change or change nothing principals hold, say so with an
  execution option. SQLAlchemy reports a savepoint's release as a commit
  too: it is ignored, so marks wait for the transaction's commit. A read
  that overlaps an invalidation of what it rests on is served but not kept
  (each read takes a mark before its queries; an invalidation after the
  mark refuses the entry).
- **In other processes**: within `TTL`, or at once with `STAMPS`, the
  alias of a Redis cache every process shares (with one `KEY_PREFIX`). A
  commit writes a new random stamp for each user, credential, and login
  it changed (and one for everything, for groups and permissions), for 10
  minutes, twice: before the commit, and after it in an owned task that a
  cancelled request does not stop, bounded by 5 seconds. Any session's
  commit stamps, a unit of work's or a bare one's. An entry records its
  stamps, read before its rows (`ALL`'s and the token's before the unit
  of work opens, the user's after the token, which names the user; the
  user's read then checks the token is still stored), and each request
  compares them in one `MGET`. A stamp absent when an entry is read is
  given one first (`SET NX`, once the credential proved real), so an entry
  never records an absent stamp: a stamp the cache later loses reads as a
  change. Random rather than counted, so a stamp that expired and came
  back never matches an old one.
- **Failures fail closed to the database.** Stamps that cannot be read
  confirm nothing: the principal is read from the database and not kept.
  A stamp that cannot be written is logged, and that change reaches other
  processes within `TTL`. Nothing is served past its `TTL`.
- **Deployment checks**: `STAMPS` must name a Redis cache of `CACHES`,
  everywhere. In staging and production, `check` warns about a `TTL`
  above 5 seconds without `STAMPS`, since one process per core is the
  documented setup.
- **The middleware order stays**: authentication, then the rate limit.
  With the cache a 429 for a kept principal costs no query, and limits
  still count users.
- **Open connections** (ADR 0056) are re-checked through the cache, so they
  see a change exactly when requests do.
- `auth_principal_cache_total{authentication, event}` counts hits, misses,
  invalidations, and evictions, labelled by class only.

## Consequences

- A request whose principal is kept makes no query to authenticate, and a
  429 for it none either; a miss makes two. `docs/performance.md` has the
  measurements.
- Without `STAMPS`, a change reaches the other processes within the `TTL`,
  5 seconds by default; `check` warns when it is longer. With `STAMPS`,
  each request makes one cache round trip.
- SQL run on a connection outside any session (`connections.engine()`,
  fixtures, snapshot restores), or another service writing the tables
  without these stamps, is seen within the `TTL`, unless the code calls
  `principals.invalidate(...)`. Bulk statements through a session drop
  every principal: a burst of misses.
- A database outage no longer refuses requests whose principal is kept,
  for up to `TTL`; open connections then close at their first re-check
  past it (ADR 0056 said at once).
- Changing a group drops every principal at once: a burst of misses after
  an admin action.
- Memory is bounded by `MAX_ENTRIES` times a principal's size. Grants are
  shared between principals (a user's credentials, a group's users), so
  a principal costs a few kilobytes whatever its role holds (5.6 kB per
  entry for a user with 1,000 permissions, measured; it was 368 kB before
  grants were shared). `AUTH.MAX_LIVE_TOKENS` bounds the personal tokens
  one user can make.
- Whoever can write the shared cache can put an old stamp back and delay a
  revocation in other processes, never past the `TTL`. Deleting stamps
  (or Redis losing them: a restart without persistence, an eviction,
  `FLUSHDB`) causes misses, never matches, since no entry records an
  absent stamp; `maxmemory-policy noeviction` avoids the misses. Its
  server deserves the protection the deployment checks already ask of
  caches (TLS, a password).
- A commit's stamps are written before it and after it. If the process
  ends between the commit and the second write, principals read before
  the transaction still differ from the first stamp; one read during the
  commit itself may stay for the `TTL`.
- On a miss with `STAMPS`, `ALL`'s stamp (given one if absent) and the
  token's (if it exists) are read before the connection is taken; the
  user's, and an absent token's (given one once the token proved real),
  between the two queries, while the read holds its connection: a slow
  cache lengthens that by its `SOCKET_TIMEOUT` at most, and a failing one
  not at all once its `FAILURE_COOLDOWN` starts.
