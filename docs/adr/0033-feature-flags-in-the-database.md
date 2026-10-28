# 0033. Keep feature flags and dynamic settings in the database, cached whole

- Status: Accepted; amended 2026-10-28 (at the end)
- Date: 2026-10-19

## Context

Turning a feature on for some users, or changing a limit, should not need a
deployment, and who changed it should be on record. Hosted services
(LaunchDarkly, Unleash) do this well but add a dependency, a network call
or a sidecar per decision, and a second place where personal data
(user ids) goes. Settings modules need a deployment. Django projects
mostly use django-waffle and django-constance: rows in the database, read
through a cache.

Checking a flag must be cheap, since it happens in requests, possibly
several times. A rollout must keep each user in or out as long as the
percentage stays, in every process, and keep them in as it grows.

## Decision

- `domainkit.contrib.flags` stores flags (`domainkit_flags`) and the values
  of dynamic settings (`domainkit_dynamic_settings`) in its routing
  target, which must be one database. A flag is on for everyone, for no
  one, or for listed users, members of listed groups, and a percentage of
  the rest, by SHA-256 of the flag's name and the user's id into 100
  buckets. Anonymous callers get a flag only when it is on for everyone.
- Dynamic settings are declared in code with a type and a default, and
  validated by pydantic on every write and read; a stored value the type
  refuses reads as the default, with a warning.
- Every flag and stored value is read in one query and cached as one JSON
  entry in `FLAGS.CACHE` for `FLAGS.CACHE_TTL` (30 seconds). Flags and
  settings are aggregates changed through `FlagsUnitOfWork`, so each
  change is a domain event: the audit trail records it with its actor, and
  an in-process handler drops the cached entry once it commits.
- `RequireFlag` answers 404 while a flag is off for the caller, so a hidden
  feature looks absent; it is not an access rule, and does not satisfy
  deny by default.

## Consequences

A flag check costs one cache read, and nothing else is needed to run them.
With a Redis cache every process sees a change at once; with memory caches,
other processes see it within the TTL, which is the price of not
broadcasting changes. The one entry grows with the number of flags and
settings, which stays small in practice. Values of dynamic settings are
audited in clear, so they must never hold secrets. Rollouts are by user:
without a user there is no stable bucket, so anonymous rollouts would need
a key of their own, which this does not offer.

## Amendment (2026-10-28, 0.12): a change replaces the cached entry instead of deleting it

PERF-17 of the 0.9.0 audit: a change deleted the one entry every process
reads, so with a Redis cache the next read missed in every process at
once, and each read the tables. Twenty replicas, each a `Cache` of its
own over one store, reading as a change landed: 20 loads.

The options:

- A versioned entry key (`domainkit.flags:<version>`) and a counter:
  a change bumps the counter, and readers read it first. Every flag
  check then costs a second round trip, to save one load per replica per
  change, and the replicas still all miss the new key at once.
- The process that made the change loads the flags and replaces the
  entry, so readers are served the old one until the new one lands.
  Two changes at once could lose one: a reload that read before the
  other committed may store after the other's reload.

Decision: the second, with a counter. A change increments
`domainkit.flags.version` in `FLAGS.CACHE`, then loads the flags and
replaces the entry with `Cache.refresh`, which stores as `get_or_set`
does (its early recomputation still applies) and joins no computation
already running. If the counter moved meanwhile, another change may have
been overwritten, so it loads again; after three such races, or a load
that fails, it deletes the entry, as before, and the next read loads.
Measured the same way: one load, the writer's; every replica was served
the old rules until the new ones landed, then the new ones. Two racing
changes, the first reading before the second committed and storing after
it: both kept, in three loads.

What it does not change: processes with their own memory cache see a
change within `CACHE_TTL`, as do changes made behind the store's back,
and a reader's load that began before a change committed and stored after
its reload (a race the deletion had too).

## Amendment (2026-10-28, 0.12, SEC-12b-1): the entry is kept under the count of changes

The 0.12 security review found what the amendment above left to
`CACHE_TTL`: a process that missed the entry and read the tables just
before a change stored what it read after the change's reload, over it.
The counter caught only two changes racing. Every process sharing the
cache was served the flags as they were before the change for up to
`CACHE_TTL` (30 seconds): a kill switch turned off stayed on.

The options:

- Tag each loaded entry with the count read before its load, and treat
  an entry older than the count as a miss. The count must be read with
  every flag read, and once a change is counted, every reader in every
  process finds the entry older than it and loads at once: the stampede
  this record set out to end, on every change.
- Make the store conditional on the count being unchanged: no such write
  in `Cache`, nor in a memory store.
- Keep the entry under the count (`domainkit.flags:<count>`). Readers read
  the count, then its entry. A change reads the count, loads the next
  count's entry, then counts itself, so readers move to an entry that is
  already there.

Decision: the last. A load that read before a change was started by a
reader that read an earlier count, so it stores under that count, which
no one reads once the change is counted. If another change was counted
between this change's read of the count and its own count, the count it
gets is not the one it loaded: it loads that count's entry again, after
both changes committed. If its load fails, the change is still counted,
and readers load the new entry themselves. While the cache cannot say the
count, reads use 0, and `get_or_set` reads the database, as on any outage.

A flag read costs two cache reads where it cost one: the count, then its
entry. Entries under earlier counts are never read again, and expire
after `CACHE_TTL`. With the 20 replicas of the amendment above, a change
still loads the flags once; the review's case, a miss that reads before a
kill switch is turned off and stores after, now leaves the switch off
(`test_sec12b_1_flag_stale_load_outlives_refresh.py`).

