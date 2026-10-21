# 0052. Keep immudb's SDK, on threads, rather than an async-native client

- Status: Accepted
- Date: 2026-10-20

## Context

immudb-py is synchronous, and it keeps the client's trusted state (the
last verified transaction) inside the client. ADR 0011 runs each ledger's
client on one thread, so calls never block the event loop and never race
on that state. The re-audit found what one thread costs (ASYNC-13) and
asked whether an async-native client should replace it (R14):

- a readiness probe queued behind writes, so a slow write made the ledger
  look unready;
- `close()` queued its disconnect behind a stuck call, and a shutdown that
  cut it short left the thread and the client behind;
- a call given up on after `TIMEOUT` cannot be interrupted, so a write may
  land after its caller saw an error; an audit anchor retried then could
  be written twice.

An async-native client would use `grpc.aio` with immudb's protos, which
immudb-py ships. But the verification is the point of the ledger, and it
lives in immudb-py's handlers (`verifiedSet`, `verifiedGet`, the state
signature), which call the stub synchronously and update the trusted state
as they go. Going async means re-implementing inclusion, consistency, and
linear proofs and the signature check, and keeping them in step with the
server's versions: a bug there either trusts a tampered ledger or raises
tamper alarms on a sound one. Only a real immudb can test it; the
framework's tests reach one in CI's integration job, not on a laptop.
Cancelling a `grpc.aio` call would not make late writes impossible either:
the server may apply a write whose client went away.

## Decision

- Keep immudb-py's verified operations, on threads.
- Give each ledger two lanes, each a thread with its own client: one for
  the work, in order, and one for readiness probes, which log in on their
  own and check the server's signed state without waiting behind writes.
- `close()` hands each client's disconnect to its thread, shielded from
  the caller's cancellation, lets the thread go, and waits at most
  `TIMEOUT`: a busy client closes when its call returns.
- Make late and retried writes harmless where they matter: `create`
  (`keyMustNotExist`) for evidence, and a new `append(...,
  unchanged_since=...)` (`keyNotModifiedAfterTX`) for audit anchors, both
  checked and written in one step by the server.

## Consequences

- Each ledger keeps a second immudb session, for its probes.
- Calls still cost a thread hop, and one process writes to a ledger one
  call at a time: about 70 verified writes a second on a laptop, which
  immudb's synced commits limit anyway.
- grpcio has no free-threaded build yet, so the immudb extra is left out of
  the 3.14t CI job; an async client would not change that.
- What would change this: an immudb SDK with asyncio support and its own
  verification, or evidence volumes beyond what one thread per process can
  verify. Then the `Ledger` port stays as it is, and only the adapter
  changes.
