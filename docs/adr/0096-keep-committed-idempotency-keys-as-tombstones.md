# 0096. Keep committed idempotency keys as tombstones past their TTL

- Status: Accepted
- Date: 2026-10-26
- Amends: [0019](0019-idempotency-keys-commit-with-changes.md), [0020](0020-lease-idempotency-keys.md), [0062](0062-claim-idempotency-keys-in-one-statement.md)

## Context

A key's row lived for `IDEMPOTENCY.TTL_SECONDS` (24 hours by default).
Once that passed, the hourly purge deleted it, or the next claim of the key
replaced it, whatever its request had done. A retry with the same key and
the same body was then a new request: a payment that committed ran again,
and the client was charged twice (audit 0.9.0, FIN-84). A test that pays,
moves past the TTL, purges or not, and retries showed it on SQLite and
PostgreSQL: two payments where one was made.

What a retry past the TTL needs is not the response but the fact that the
request ran: a small record of completion that outlives the response.
Options considered:

- **Raise `TTL_SECONDS`.** It keeps every response, up to
  `MAX_RESPONSE_BYTES` each, for as long, and kept responses hold personal
  data, which erasure must find (DATA-02). The two needs have different
  sizes and different lifetimes.
- **Tombstones in a table of their own,** made by the purge as keys expire.
  The claim must then consult that table too: a second statement on every
  first keyed write, or one claim, `INSERT … SELECT … WHERE NOT EXISTS
  (tombstone) ON CONFLICT …`. That claim races the purge: its snapshot is
  taken before the purge's move commits, so it sees no tombstone; its
  insert then waits on the key's row, which the purge is deleting, and
  succeeds once the purge commits. The request runs again. The read of a
  held key would need both tables as well.
- **Tombstones in the keys table.** One row per key, so the claim's upsert
  stays the only arbiter: under `READ COMMITTED`, PostgreSQL checks its
  `WHERE` again on the newest version of a row it waited for (ADR 0062),
  so a claim never replaces a row the purge has just made a tombstone.
- **Write the record when the request commits,** in the statement of the
  commit mark (ADR 0019). The record's expiry must then be computed from
  the row's on SQLite too, which keeps times as text; and the purge visits
  each expiring row anyway.

Measured on PostgreSQL 18, on a machine shared with other work (one run;
the script is a PoC kept out of the tree), with the columns of each
design and their indexes:

| | In the keys table | A table of its own |
| --- | --- | --- |
| A million tombstones, with indexes | 369 MiB (387 bytes a key) | 298 MiB (313 bytes a key) |
| Making 100,000 tombstones of rows with 200-byte responses | 5.3 s | 5.8 s |
| A purge with nothing due, a million tombstones kept | 4.9 ms | 0.9 ms |
| The same, had tombstones kept their old `expires_at` | 483 ms | |
| Deleting 100,000 tombstones that expired | 312 ms | 355 ms |
| The keyed write of the ladder | 4 statements, 3 transactions, 3 checkouts, as before | one statement more, or the racing claim |

## Decision

- An expired key whose request committed, or pinned it for a commit on
  another database, becomes a tombstone in its own row: a new column,
  `tombstone`. The purge (hourly with messaging; otherwise the claim's
  sweep, once a minute) drops its response, headers and `subjects`, sets
  `tombstone`, and moves `expires_at` to `TOMBSTONE_SECONDS` from now, on
  the database's clock (ADR 0062's amendment of 2026-10-25). Moving the
  expiry keeps the purge reading only the rows due, however many
  tombstones wait (4.9 ms above, not 483). It deletes the rows nothing
  committed or pinned, as before, and the tombstones that expired.
- The claim replaces an expired row only if nothing committed or pinned
  it, or if it is a tombstone that expired in turn. The keyed write path
  is unchanged: the same statements, with new conditions in the claim and
  one more column read with a held key's row, whether it `expired`.
- A retry of a tombstone, or of an expired key that committed or was
  pinned and that the purge has not reached yet, gets 409
  `idempotency_response_unavailable`, with a fixed message the catalogs
  translate: "already ran, and its response is no longer kept; look up the
  result instead of retrying." The response is not replayed past its TTL
  even while it is still stored. Another request with the key gets 422
  `idempotency_key_reused`, as within the TTL: the fingerprint is checked
  first.
- 409, not 422: the draft "The Idempotency-Key HTTP Header Field" uses 422
  for a key reused with another payload, a fault of the request, and 409
  for a key whose first request is still being processed, a conflict with
  the key's state. A key that ran and kept no response is the second kind,
  and the state ADR 0019's 409 already names, with the same code: clients
  that handle it ("it ran; look up the result") need no change.
- `IDEMPOTENCY.TOMBSTONE_SECONDS`, 7 days by default, from 0 to 90 days.
  A week covers the retry windows clients plausibly have once a day has
  passed: a queue replayed after an outage over a long weekend, a
  broker's default retention (Kafka keeps 7 days, SQS 4), and
  messaging's own 7 days of finished messages (`RETENTION_SECONDS`). The
  framework's own retries end within the TTL: messaging's within an hour,
  webhooks' within a day. Past 90 days a retry is no longer a retry: a
  duplicate that old is for the domain's own unique columns to refuse.
  0 builds 0.10's claim and purge, so a project that reuses keys on
  purpose can keep that while it changes.
- A tombstone keeps the hash of the caller and key (SHA-256, as every key
  row), the request's fingerprint (a hash), its status, and its times. It
  names no one: erasure finds kept responses by `subjects`, which the
  tombstone no longer has, and the hash cannot be found from a subject
  without the key, which the server never stores. It is what an erased key
  keeps (DATA-02), so `privacy erase` and `privacy export` have nothing of
  it to reach. A column naming the caller, for erasure to find, was
  considered: it would add the very link the tombstone otherwise lacks.

## Consequences

- A committed or pinned key never runs again for `TTL_SECONDS` and
  `TOMBSTONE_SECONDS` together (8 days by default), at least: a tombstone's
  week starts when the purge makes it, and a purge that comes late leaves
  the key unreplaced meanwhile. Past that the key is forgotten; keys are
  still not a record of what was ever done.
- The table holds a week of tombstones more: about 390 bytes a key, so
  some 2.6 GB at a million keyed commits a day, by default.
- Without messaging, a sweep that is due makes two statements, a delete
  then the tombstones, still last on the claim's connection and still
  skipping locked rows; a whole batch of either sweeps again at the next
  claim.
- Clients that reuse keys on purpose past a day get 409, or 422 with
  another body, for a week. `TOMBSTONE_SECONDS = 0` keeps 0.10's
  behaviour.
- Projects run `makemigrations` and `migrate` for the column, which
  defaults to false on the server, so rows from before the upgrade are
  kept as they were and buried when they expire.
