# 0105. Fingerprint keyed bodies as they arrive, and spool them past a threshold

- Status: Accepted
- Date: 2026-10-28

## Context

PERF-11 of the 0.9.0 audit: a request with an `Idempotency-Key` had its
whole body read, joined into one value, and held until the request
ended; a response the key keeps was held in parts, then joined to be
stored. Both are bounded only by `MAX_REQUEST_BODY_SIZE` and
`IDEMPOTENCY.MAX_RESPONSE_BYTES` (1 MiB each by default), which a project
that takes uploads or serves exports through keyed routes raises.

Measured at 0.11.1 with tracemalloc, the middleware in front of an
application that reads the body and drops it and streams its response in
64 KiB parts, keys on SQLite:

| Body in / response out | Peak, first request | Peak, replay |
| --- | --- | --- |
| 8 MiB / none | 16.0 MiB | 16.2 MiB |
| none / 8 MiB | 16.2 MiB | 8.2 MiB |
| 8 MiB / 8 MiB | 24.2 MiB | 16.2 MiB |
| 32 MiB / 32 MiB | 96.2 MiB | 64.2 MiB |

What constrains a fix:

- The key's fingerprint must be known before the request runs: the claim
  stores it, and a retry with another body is refused 422. So the whole
  body is read before the application sees any of it; it can be hashed as
  it comes, but must be kept to hand on.
- The fingerprint is a SHA-256 of the method, path, query, and body, each
  after its length. A change of format would make a retry that reaches a
  replica still on 0.11, during a rolling upgrade, or a key claimed before
  it, a 422 "used for a different request". The body's length comes
  before the body, so hashing as it arrives needs the length first.
- The stored response is one value the driver binds whole: at least one
  copy is held when it is stored.
- Starlette spools multipart uploads past 1 MiB to a temporary file; a
  keyed upload held them in memory whole, twice.

The options:

- Hash the body after the fact from what was kept (no format change),
  without joining it: halves the peak, holds it all.
- Change the fingerprint to a hash of hashes, so any body streams: breaks
  keys across the upgrade, as above.
- Keep what arrives in memory up to a threshold and in a temporary file
  past it, for the body and the response alike; hash as it arrives when
  `Content-Length` gives the length first, from what was kept otherwise.

## Decision

- **A spool, in memory up to `IDEMPOTENCY.SPOOL_BYTES`** (default 1 MiB,
  at most 1 GiB, `0` for none), past it in a `tempfile.TemporaryFile`:
  unlinked from the start, so only this process can read it, and gone
  when closed, which every path through the middleware does. It is
  written and read on a thread, so the loop never waits on the disk. The
  default is the request and response limits' own, so a project that
  keeps them never writes to the disk. Where no temporary file can be
  made (a read-only root without a writable temporary directory), the
  body stays in memory, as in 0.11, with a warning.
- **The fingerprint is 0.11's.** With `Content-Length`, the length is
  hashed first and the body as it arrives; without it, or if the body was
  not that long, the body is hashed from the spool once read.
- **The body is handed on as kept**: held in memory, in one message, as
  0.11 sent it, so an application that reads one message still gets it
  all; from a file, in 64 KiB parts, as a server sends a large body, each
  let go once handed over.
- **A response being kept goes through a spool too**, up to
  `MAX_RESPONSE_BYTES` as before, and is read back once to be stored.
  What is kept, the replay, the lease and the tombstones are unchanged.

Measured the same way (`SPOOL_BYTES` 1 MiB):

| Body in / response out | Peak, first request | Peak, replay |
| --- | --- | --- |
| 8 MiB / none | 1.1 MiB | 1.3 MiB |
| none / 8 MiB | 8.2 MiB | 8.3 MiB |
| 8 MiB / 8 MiB | 8.3 MiB | 8.3 MiB |
| 32 MiB / 32 MiB | 32.3 MiB | 32.3 MiB |

What is left is the stored value, which the driver binds whole (asyncpg
encodes it again into a buffer of its own, which the tests do not count
as the middleware's). Every replay was byte-identical. The keyed-write
rung, a small body held in memory, costs 16 calls more per request
(3,623 to 3,639, every extra installed) and 2 KB more transient memory
(70.8 to 72.9 KB).

## Consequences

- A keyed body or kept response past `SPOOL_BYTES` is written to the
  temporary directory (`TMPDIR`), with what it holds: a card number in a
  keyed POST reaches the disk where it did not, unless the directory is a
  `tmpfs`. Projects that must not write request bodies to disk set
  `SPOOL_BYTES` at or above their limits.
- A large keyed body costs thread hops and disk writes in place of
  memory; small ones cost what they did, give or take the 16 calls.
- The fingerprint's format stays fixed: changing it needs a release that
  accepts both for a key's lifetime.
