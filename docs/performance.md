# Performance

## Authentication: the principal cache

An authenticated request used to read its principal (the token, the user
with the password hash, groups, the groups' permissions, and the user's
own permissions) in a transaction of its own: four or five `SELECT`s, a
`BEGIN`, a `ROLLBACK`, a pool checkout, and a pre-ping, about 70% of an
authenticated read. A 429 for a valid token paid the same, since
authentication runs before the rate limit.

Now a principal is kept per process for `AUTH.PRINCIPAL_CACHE.TTL`
(5 seconds), and read in two queries when it is not
([auth.md](auth.md#the-principal-cache); ADRs
[0058](adr/0058-cache-principals-and-stamp-revocations.md) and
[0059](adr/0059-read-principals-in-two-queries-without-the-hash.md)).

**What a request costs**, on PostgreSQL 16, through the `init` template's
middleware (proxy, security, context, hosts, auth, rate limit, body limit),
per request:

| Request | Before (0.8.0) | Kept (hit) | Read (miss) |
| --- | --- | --- | --- |
| `GET /me`, `IsAuthenticated`, a user in no group | 4 `SELECT`, `BEGIN`, `ROLLBACK`, pre-ping, checkout; 4,265 Python calls | nothing; 972 calls | 2 `SELECT`, `BEGIN`, `ROLLBACK`, pre-ping, checkout; 2,395 calls |
| `GET /guarded`, `HasPermissions`, a user with a permission and a group | 5 `SELECT`, the rest as above; 5,221 calls | nothing; 1,262 calls | 2 `SELECT`, the rest as above; 2,693 calls |
| `GET /guarded/items/{id}`, the same, and a read through a unit of work | 6 `SELECT`, 2 transactions, 2 pre-pings, 2 checkouts; 6,834 calls | the read's 1 `SELECT`, transaction, pre-ping, checkout; 2,876 calls | 3 `SELECT`, 2 of each; 4,307 calls |
| `GET /me` over the rate limit (429), a valid token | 4 `SELECT`, `BEGIN`, `ROLLBACK`, pre-ping, checkout; 4,049 calls | nothing; 752 calls | 2 `SELECT`, the rest as above; 2,175 calls |

Statements, transactions, pre-pings, and pool checkouts are counted with
SQLAlchemy's events over 400 requests, one at a time; "nothing" is none
after the first request, which reads the principal. Python calls are
cProfile's exact count over 500 requests. `last_used_at` adds an `UPDATE`
and a `COMMIT` once per token per five minutes, before and after.

**CPU per request**, 16 requests in flight, best of five runs of 1,600
requests each, old and new in fresh processes, interleaved, over three
rounds (in brackets, the best run of each round, lowest to highest):

| Request | Before (0.8.0) | Kept (hit) | Read (miss) |
| --- | --- | --- | --- |
| `GET /me` | 3,658 µs (3,658–3,758) | 248 µs (248–318), −93% | 1,556 µs (1,556–1,645), −57% |
| `GET /guarded` | 4,576 µs (4,576–4,820) | 339 µs (339–346), −93% | 1,602 µs (1,602–1,867), −65% |
| `GET /guarded/items/{id}` | 5,868 µs (5,868–6,221) | 1,748 µs (1,748–1,856), −70% | 2,918 µs (2,918–3,078), −50% |
| 429 for a valid token | 3,668 µs (3,668–3,725) | 219 µs (219–242), −94% | 1,458 µs (1,458–1,603), −60% |

Measured on 21 October 2026 on a shared 4-CPU machine (load average 6 to
9 during the runs), with the re-audit's harness (`bench.py`, requests fed
straight to the ASGI application), CPU time of the process per request.
Under that load, 0.8.0 also answered a few of the reads with 503 (the
pool's wait ran out), and the new code none.

- "Kept" is a hit: the principal was read by an earlier request of the
  same credential within the TTL. "Read" is a miss, forced by dropping
  every principal before each request.
- A hit makes no query. Its cost is the user built for the request (about
  16 µs for a user, and 16 µs more for each permission with its content
  type, measured with `timeit`; estimated in proportion for users with
  hundreds of permissions) and the rest of the stack.
- With `STAMPS`, a hit adds one `MGET` to the shared cache.
- Lending the password hash to `request.user` while `.password` is
  deprecated (until 0.10) adds no statement. It adds 6 Python calls to a
  hit and 13 to 18 to a miss, and one hash of about 100 characters to each kept
  principal. The CPU per request did not change measurably: best of three
  rounds, `/me` hit 293 µs before and 283 µs after, miss 1,852 µs and
  1,776 µs; `/guarded` hit 378 µs and 374 µs, miss 1,910 µs and 1,921 µs.
  Those were measured on 22 October 2026, at load average 6 to 9.
- Measure it yourself: `auth_principal_cache_total{event="hit"}` against
  `{event="miss"}` gives the hit rate. A low one means short-lived tokens,
  many processes per client, or a `TTL` shorter than the gap between a
  client's requests.

How to make it faster:

- Raise the `TTL` (up to 5 minutes) when `STAMPS` names a shared Redis
  cache: changes still reach every process at once, and fewer requests
  read their principal. Without `STAMPS`, the `TTL` is how long a
  revocation takes to reach the other processes; `check` warns above
  5 seconds.
- Keep groups and permissions per user to what they need: a user's
  principal is built for each request, in time proportional to its grants.
