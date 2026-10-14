# 0011. Keep evidence in an immudb ledger, verified by the client

- Status: Accepted
- Date: 2026-10-14

## Context

Some records must stay exactly as written, and anyone relying on them later
must be able to prove it: verification results, signed consents,
regulatory submissions. We call these **evidence**. The audit trail is
different. It records who did what, lives in PostgreSQL next to the data,
and is append-only by triggers that its owner can drop.

immudb is an open-source database built for this: every write is a new
revision, history is kept, and each transaction extends a Merkle tree. A
client keeps the last state it trusts and checks a cryptographic proof with
every verified read or write. If the server rewrites history, the next proof
fails. The server can also sign its states.

Integration options, tested against immudb 1.11.2 on Python 3.13:

- **The PostgreSQL wire protocol** (asyncpg works). It skips client-side
  verification, which is the point of the ledger.
- **The Python SDK, immudb-py 1.5.0** (gRPC). Verified set and get, reads
  of a given revision, history, and signed-state verification all work. The
  SDK is synchronous, not thread-safe, its last release was in July 2024,
  it pulls in about twenty dependencies, and it only opens plain-text
  channels.

## Decision

- Core gets a `Ledger` port with two adapters: `ImmudbLedger` (the optional
  `immudb` extra) and `MemoryLedger` (tests and development).
- A ledger is a `LEDGERS` entry with an `immudb://user:password@host:port/
  database` URL ([0008](0008-multiple-databases-and-routing.md)).
- `ImmudbLedger` uses the SDK's verified operations only. It runs them on
  one dedicated thread per ledger, so calls never block the event loop or
  race on the client's state. It adds TLS with server certificate
  verification (`TLS_CA`) by giving the SDK a secure gRPC channel, and
  checks signed states when `SERVER_SIGNING_KEY` is set.
- In staging and production, ledgers must use TLS and a server signing key,
  and `memory://` is refused.
- `domainkit.contrib.evidence` records evidence on a ledger:
  - Each record is canonical JSON. Its SHA-256 digest is the receipt anyone
    can check a copy against.
  - Keys are `<namespace>/<subject digest>/<evidence id>`, so no personal
    data appears in keys, and listing by subject is a prefix scan.
  - Writes are idempotent by evidence id: an existing record with the same
    digest returns its receipt.
  - Reads verify revision 1. A key with more than one revision means someone
    wrote it again, and verification reports it as tampered.
  - Durable recording goes through the outbox (`defer_evidence`), so
    evidence is written exactly when the business change commits, at least
    once.
- Large artifacts (documents, images) stay outside the ledger. Evidence
  holds their SHA-256 digests.

## Consequences

- Evidence and business data cannot commit atomically, since immudb takes
  no part in PostgreSQL transactions. Through the outbox, evidence follows
  the commit; written directly, a failed business change leaves evidence of
  the attempt.
- The trusted state lives in process memory. A restarted process trusts the
  server's current state first, so the server signing key is what protects
  across restarts.
- Throughput per process is bounded by one serialized SDK client and by
  immudb's synced commits. Measured on a laptop (immudb 1.11.2 in Docker,
  synced, with TLS and signed states): about 70 verified writes and 900
  verified reads per second; plain unverified writes reached about 100.
  That suits evidence, not bulk data. Batching several records into one
  transaction is the way to go faster.
- Nothing written can be erased. Do not put personal data in facts (store
  digests or references), or encrypt it with per-subject keys that can be
  destroyed (crypto-shredding), so erasure requests remain possible.
- The SDK depends on `ecdsa`, which has a timing attack on signing with no
  planned fix (CVE-2024-23342). The SDK only verifies signatures, which the
  advisory says is unaffected, so CI's dependency audit ignores that one
  advisory (see `SECURITY.md`).
- The SDK is pinned (`>=1.5,<1.6`), and its private channel setup is covered
  by integration tests against a TLS-enabled immudb in CI. If it stays
  unmaintained, the `Ledger` port lets us replace it with our own gRPC
  client without touching evidence code.
