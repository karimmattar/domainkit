# Performance

## Database round trips: reads, keyed writes, and audit keys

A round trip to PostgreSQL costs the server work and the request its
latency, however little it carries. The performance branch cut them on
the paths every service takes:

- **Pre-ping, one round trip.** A pooled asyncpg connection is tested with
  SQLAlchemy's own `;`, outside a transaction, where SQLAlchemy wraps it
  in `BEGIN` and `ROLLBACK`. Every checkout saves two round trips. Through
  PgBouncer, which needs the transaction, the ping is as before
  ([databases.md](databases.md#connection-pools)).
- **Reads without a transaction.** `reads=Reads.STATEMENTS` runs each
  statement of a read-only unit of work on its own, on a pool held
  read-only by PostgreSQL. `reads=Reads.SNAPSHOT` reads one snapshot
  ([queries.md](queries.md#how-read-only-work-reads)).
- **Keyed writes.** An `Idempotency-Key` is claimed with one upsert, which
  also replaces an expired key, and the response is stored with one
  statement. Both commit as they run
  ([contrib.md](contrib.md#idempotency-safe-retries-of-unsafe-requests)).
- **Audit keys.** A new subject's key comes back from its insert, and a
  transaction reads each key once.

### What it saves

Round trips per request on the wire, counted by a proxy that reads
PostgreSQL's protocol. These are the re-audit's scenarios: 16 requests in
flight, and the old 10-middleware stack (proxy, metrics, security, CORS,
context, hosts, token authentication, rate limit, body limit,
idempotency) for the writes.

| Request | 0.8.0 | Now | Transactions | Checkouts |
| --- | ---: | ---: | --- | --- |
| Read by id through a unit of work, no middleware | 6 | 4 | 1 → 1 | 1 → 1 |
| … with `reads=Reads.STATEMENTS` | 6 | **1** | 1 → 0 | 1 → 1 |
| Write through a unit of work, with a token | 15 | 11 | 2 → 2 | 2 → 2 |
| Keyed write (`Idempotency-Key`), first request | 34 | **16** | 5 → 2 | 5 → 4 |
| Keyed write, replay | 27 | **10** | 4 → 1 | 4 → 2 |
| Audited write, known subject | 17 | 13 | 2 → 2 | 2 → 2 |
| Audited write, new subject | 19 | 14 | 2 → 2 | 2 → 2 |

- Every checkout's ping went from 3 round trips to 1. A read through a
  unit of work is then `;`, `BEGIN`, the `SELECT`, `ROLLBACK`.
- A `STATEMENTS` read is the `SELECT` alone.
- Of the keyed write's 18 saved round trips, the ping saves 10 and the
  claim and store 8. Of its 9 statements, the `DELETE` of the key's
  expired row is gone, since the upsert replaces that row, and the claim
  and the store each run alone.
- A new audit subject costs 2 statements for its keys instead of 3, the
  key coming back from the insert. A known subject still costs 1 `SELECT`
  for the keys of every subject the commit writes about.

The counts are the same with SQLAlchemy 2.0.54 and with 2.1.4 and asyncpg
0.32.0.

The benchmark ladder counts on SQLAlchemy's side, with a project of its
own. Its `keyed-write` rung went from 10 statements, 5 transactions, 5
checkouts and 5 pings to 9, 4, 4 and 4, with either SQLAlchemy. It counts
a transaction for each connection SQLAlchemy begins, the claim's and the
store's included, which send no `BEGIN`; the table above counts the
transactions the server runs.

CPU of the Python process per request, the best of three runs, each in a
fresh process, old and new interleaved (16 in flight, 5 × 1,600 requests
after 320 to warm up), with SQLAlchemy 2.0.54. The machine was shared,
with a load average of 2.6 to 12, so read differences under about 5% as
noise. Each figure is the best run, with the range of all three:

| Request | 0.8.0 µs | Now µs |
| --- | ---: | ---: |
| Read by id through a unit of work | 1,257 (1,257–1,323) | 1,214 (1,214–1,248) |
| … with `reads=Reads.STATEMENTS` | | 995 (995–1,032) |
| Write through a unit of work | 5,049 (5,049–5,372) | 4,903 (4,903–5,148) |
| Keyed write, first request | 8,997 (8,997–9,237) | 8,386 (8,386–8,489) |
| Keyed write, replay | 7,105 (7,105–7,216) | 5,759 (5,759–5,986) |
| Audited write, known subject | 6,630 (6,630–6,946) | 6,277 (6,277–6,634) |
| Audited write, new subject | 7,689 (7,689–7,865) | 7,104 (7,104–7,319) |

What the CPU table shows:

- A replay costs 19% less.
- A `STATEMENTS` read costs 21% less than a read through a unit of work
  did.
- A first keyed write costs about 7% less: every run after the change was
  cheaper than every run before it.
- The rest moved within the noise.
- Round trips cost latency as well, which the client's CPU does not
  show: on a database across a network, each saved round trip is one
  network round trip less per request.

### What it does not change

- **Authentication** still makes a transaction and four `SELECT`s per
  request without the principal cache. With `reads=Reads.STATEMENTS` it
  would drop `BEGIN`, `ROLLBACK` and the ping.
- **Core for hot reads** was measured and left out. A repository `get` by
  id through the ORM took 760 Python calls per read, and a Core `SELECT` of
  the same row 591. Keeping the ORM's identity map and version tracking
  costs those calls back (ADR
  [0061](adr/0061-read-without-a-transaction-and-ping-once.md)).
- **A second pool.** `STATEMENTS` reads open a pool of their own per
  alias, `<alias>:reads`. Count it when sizing `max_connections`.
