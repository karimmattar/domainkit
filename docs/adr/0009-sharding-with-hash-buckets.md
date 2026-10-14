# 0009. Shard by key over fixed hash buckets

- Status: Accepted
- Date: 2026-10-14

## Context

Some domains outgrow one PostgreSQL server: many tenants, or tenants that
need isolation. Options considered:

- **A distributed database** (Citus, managed sharded PostgreSQL). Routing
  and cross-shard queries happen inside the database, at the cost of an
  extension or a vendor, and less control over where data lives.
- **Application-level sharding.** The application routes each unit of work
  to one of several ordinary PostgreSQL databases with the same schema. With
  `hash(key) % databases`, adding a database moves most keys. Consistent
  hashing or fixed buckets bound that movement. A directory (key to
  database) allows pinning, but becomes a lookup on every request.

## Decision

- `SHARDS` defines shard groups. A group lists its SQL aliases, a fixed
  number of `BUCKETS` (default 4096), and optionally the bucket ranges each
  alias owns (default: equal contiguous ranges, in list order) and keys
  pinned to an alias.
- A shard key (`str`, `int`, or `UUID`) maps to a bucket by a stable hash
  (BLAKE2b, 8 bytes, modulo `BUCKETS`), and the bucket to the alias that
  owns it. Pinned keys skip the hash.
- A unit of work on a group needs a shard key:
  `AccountsUnitOfWork(shard_key=tenant_id)`. It then works on exactly one
  database. `for_each_shard(group, fn)` runs a coroutine on every database
  of a group, for reports and maintenance.
- All databases of a group share one migration history. `migrate` applies
  it to each, and `makemigrations` compares against the first.
- Settings validation rejects aliases in two groups, gaps or overlaps in
  bucket ranges, and pins to aliases outside the group.

## Consequences

- Adding a database means moving whole bucket ranges. The operator copies
  the rows of those buckets (dual writes, then backfill, then switch the
  ranges), and the framework provides `bucket_for(key)` and each alias's
  ranges to do it. `BUCKETS` can never change once data exists.
- Cross-shard queries and transactions are not supported. Choose a key that
  keeps each use case on one shard (tenant, customer).
- Each shard has its own outbox and audit trail ([0008](0008-multiple-databases-and-routing.md)),
  so a worker must run for each group.
- Citus stays an option: a group of one alias pointing at a Citus
  coordinator needs no framework changes.
