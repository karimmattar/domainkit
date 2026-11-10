# 0135. Keep each signed request, not the last time

- Status: Accepted
- Date: 2026-11-10

## Context

A bound device signs its requests ([Passkeys and bound devices](../auth/devices.md#bound-devices)):
the signature covers the method, the target, the body, and a time. To
stop a signed request from working twice, each device's row kept the time
of its last request, and a request passed only if its time was later.

An audit of 2.2.1 found two faults in that rule:

- **Requests sent at once were refused.** An app that sends two requests
  together signs them a millisecond apart. When the later one arrives
  first, the earlier one is `401 device_signature_refused`. Mobile
  networks and HTTP/2 reorder requests often.
- **Every signed request wrote the same row.** The update of the
  device's row, and its commit, ran with each request, and all the
  requests of one phone waited on that row. In the strict phone mode
  every request of the app is signed.

What others do:

- **RFC 9449 (DPoP), 11.1:** the server keeps each proof's `jti` for the
  time window in which the proof is accepted, and refuses one it has
  seen. Order plays no part.
- **RFC 9421 (HTTP Message Signatures):** `created` bounds the window,
  and `nonce` makes a signature unique within it.
- **The framework itself** keeps the `jti` of each CIBA client assertion
  in a table until it expires (`domainkit_ciba_assertions`).

An ECDSA signature has a twin: `(r, n - s)` signs what `(r, s)` signs.
Keeping the signature's bytes would let the twin through.

## Decision

### A signed request works once, whatever its order

`DeviceSigned` keeps each request a device sent, by the SHA-256 of the
message it signed (the time, method, target, and body), while its time is
within `DEVICES.SIGNATURE_WINDOW`. A request passes when:

- one of the user's devices signed it;
- its time is within the window;
- the same request was not kept before.

Two requests of the same millisecond pass if they differ. The same
request with another valid signature is a replay.

### Kept in the database, or in a shared cache

`DEVICES.SIGNATURE_STORE`:

| Value | Where a request is kept |
| --- | --- |
| `"database"` (default) | A row in `domainkit_device_signatures`, in the devices' database. Rows do not wait on each other. |
| `"cache"` | A key in the default cache, for twice the window. A signed request then writes to no database. |

With `"cache"`:

- the cache must be one every process shares. `setup()` refuses
  `dummy://` anywhere, and `memory://` in staging and production;
- while the cache is down, no signed request passes. A request that
  nothing can remember is refused, and the outage is logged.

### The device's last use is written once a minute

`last_used_at` was written with every request. It is now written when it
is more than a minute old, and the device's kept requests older than the
window are deleted then. With messaging installed, an hourly task
(`domainkit.devices.purge_signatures`) deletes those of devices that
stopped. Unbinding a device, and deleting a user, delete theirs.

## Consequences

- **Apps may send signed requests together.** Each still needs its own
  time or its own content.
- **A new table:** `makemigrations`, then `migrate`.
- **`last_used_at` is up to a minute behind.**
- **`DeviceStore.advance` is gone;** `use` and `touch` replace it. The
  `last_time` column stays, as the latest time noted.
- **With `"cache"`, signed requests depend on the cache being up.**
