# Evidence: records that provably never change

Evidence is a fact that you may have to prove later, exactly as it was:
a verification result, a signed consent, a report sent to a regulator.
The evidence app writes each fact to a ledger (an immudb database,
[ADR 0011](../adr/0011-immudb-ledger-for-evidence.md)), not to
PostgreSQL. A ledger keeps every revision of every key, so nobody can
change a record without the change showing. The [audit
trail](audit.md) says who did what; evidence keeps the facts and the
digests of the documents behind them.

On this page you learn how to:

- record a fact and keep its receipt;
- read a record back and prove that it did not change;
- record a fact only when a [use case](../learning.md#words) commits,
  with `defer_evidence`;
- keep amounts exact and personal data out of the ledger;
- set, and later rotate, the subject key that deployments need;
- list and verify records from the command line.

## Install

Add the app to `INSTALLED_DOMAINS`, and add a ledger under the alias
`evidence`. Install messaging too if you want to record facts after a
commit:

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # only for defer_evidence
    "domainkit.contrib.evidence",
]
LEDGERS = {"evidence": {"URL": env.str("EVIDENCE_LEDGER_URL")}}  # immudb://...
EVIDENCE = {"SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY")}  # in deployments
```

In tests, use `{"URL": "memory://"}`: a ledger in memory that behaves
the same way. The app has no tables, so it needs no migration.
[Ledgers (immudb)](../databases.md#ledgers-immudb) shows how to prepare
an immudb server.

## A first example

A customer's passport was checked. You record the result and the digest
of the scanned document, then prove the record later:

```python
from decimal import Decimal

from domainkit.contrib.evidence import Evidence, evidence_store, sha256

document_bytes = b"%PDF-1.7 passport scan"
store = evidence_store()

evidence = Evidence(
    kind="kyc.document_verified",
    subject="customer:42",
    facts={"result": "match", "model": "v3", "fee": Decimal("10.00")},
    artifacts={"passport": sha256(document_bytes)},
)
receipt = await store.record(evidence)  # keep it with your data
recorded = await store.verify(receipt)  # raises EvidenceTampered
print(receipt.key)
print(recorded.intact, recorded.evidence.facts)
```

```text
evidence/77e0b3b900b3f6eea06236db14ba86a3/9a2ca952-d664-4aab-adad-136c6ac9e202
True {'fee': '10.00', 'model': 'v3', 'result': 'match'}
```

`record` writes the fact once and returns an `EvidenceReceipt`. Keep the
receipt, or at least its `key`, next to the change that it proves.
`verify` reads the record back and raises `EvidenceTampered` if it is not
the one the receipt describes.

## What a record holds

An `Evidence` is a frozen dataclass. You give its kind, its subject, and
its facts. domainkit fills in the rest:

| Field | What it holds |
| --- | --- |
| `kind` | A dotted name in lower case, such as `kyc.document_verified`; at most 100 characters. |
| `subject` | The internal id of what the record is about, such as `customer:42`; 1 to 255 characters. |
| `facts` | JSON values: results, scores, versions, references. |
| `artifacts` | Names of outside documents, each mapped to its `sha256:<64 hex digits>` digest. |
| `id` | A new UUID. Recording is idempotent by this id. |
| `recorded_at` | The time now; it must have a time zone. |
| `actor`, `request_id` | The actor and the id of the current request, when there is one. |

The ledger stores the record as canonical JSON: sorted keys, no spaces,
UTF-8, and a `format` number. The same record always gives the same
bytes, so its SHA-256 digest is stable:

```python
print(evidence.canonical().decode())
```

```text
{"actor":null,"artifacts":{"passport":"sha256:f0c002302408fd961f2caa137daa9dca8993f944817e5fc1db139f06ead386d9"},"facts":{"fee":"10.00","model":"v3","result":"match"},"format":1,"id":"9a2ca952-d664-4aab-adad-136c6ac9e202","kind":"kyc.document_verified","recorded_at":"2026-11-04T18:17:23.908350+00:00","request_id":null,"subject":"customer:42"}
```

`sha256(data)` returns `sha256:<hex>` of some bytes. Use it for the
digests in `artifacts`. Keep the documents themselves in a file store:
a record may not be larger than `MAX_BYTES`.

## Exact amounts

Amounts must be exact. A `Decimal` in the facts is written as a
fixed-point string, so `Decimal("10.00")` is stored as `"10.00"` and
read back as that string. A float is refused when the record is written,
because most decimal amounts have no exact float, and the receipt would
prove a rounded value:

```python
Evidence(kind="payment.sent", subject="payment:1", facts={"amount": 10.5}).canonical()
```

```text
ValueError: Evidence facts may not hold floats (facts.amount): use Decimal or str, so amounts stay exact.
```

The message names the place in the facts, never the value. `NaN` and
infinite decimals are refused too. A record written before floats were
refused still reads and verifies. If it waits in the outbox, it is
recorded with the bytes it had.

## The subject: an internal id

The subject is an internal id, such as `customer:42` or a UUID. It may
hold letters, digits, and `_ . : / -`, and it starts with a letter or a
digit. An email address, a name, or a phone number with `+` is refused
with a `ValueError` that does not repeat the value:

```python
Evidence(kind="kyc.checked", subject="jane@example.com")
```

```text
ValueError: Evidence subject must be an internal id such as 'customer:42' (letters, digits, and _ . : / -), never personal data such as an email address, a name, or a phone number.
```

The check cannot tell a phone number without `+` from a numeric id. Give
every id a type, such as `customer:42`, and the question does not come
up.

!!! warning
    Never put personal data in the subject or the facts. A ledger cannot
    forget, so you could not meet an erasure request. Store references
    or digests, or encrypt personal fields with keys that you can
    destroy ([keys](keys.md)).

## Receipts and keys

An `EvidenceReceipt` holds four values:

| Field | What it holds |
| --- | --- |
| `id` | The evidence id. |
| `key` | Where the record is on the ledger: `<NAMESPACE>/<subject digest>/<evidence id>`. |
| `digest` | `sha256:` of the canonical record. |
| `transaction` | The ledger transaction that stored it. |

The subject digest is an HMAC-SHA-256 of the subject under the subject
key (`SUBJECT_KEY`), cut to 32 hex digits. So a key never shows its
subject, and a person without the subject key cannot find a subject's
records by hashing guesses. You can compute both parts yourself:

```python
from domainkit.contrib.evidence import evidence_key, subject_digest

subject_digest("customer:42", b"a-secret-of-32-characters-or-more")
evidence_key("evidence", evidence, b"a-secret-of-32-characters-or-more")
```

A receipt is what ties a record to your data. A person with the ledger's
write credentials can add records, but never change one without the
change showing. So keep each receipt (or its key) with the change that
it proves. `for_subject` is a convenience listing, not the proof.

## Recording the same fact twice

Recording is idempotent by evidence id. A retry with the same record
returns the same receipt. A different record with the same id raises
`EvidenceConflict`, which the API answers with 409:

```python
from dataclasses import replace

assert await store.record(evidence) == receipt
await store.record(replace(evidence, facts={"result": "no match"}))
```

```text
domainkit.contrib.evidence.models.EvidenceConflict: Evidence 9a2ca952-d664-4aab-adad-136c6ac9e202 already holds another record.
```

The ledger writes a key only if the key is new. So writers that race to
record the same fact leave one revision, and each gets its receipt.

## Reading and verifying

An `EvidenceStore` has three ways to read:

| Method | What it returns |
| --- | --- |
| `get(key)` | The `RecordedEvidence` under `key`, or `EvidenceNotFound` (404). |
| `verify(receipt)` | The same, after it proves that the record is the receipt's and was written once. Otherwise `EvidenceTampered`. |
| `for_subject(subject, limit=100)` | The subject's records, at most `limit`, found by a scan of the key prefix. |

A `RecordedEvidence` holds the `evidence`, its `receipt`, and
`revisions`: the number of writes to its key. Its `intact` property is
true when the key was written exactly once.

Reads always return revision 1, the original. Each of these raises
`EvidenceTampered`:

- the key was written again (`verify`; `get` returns it with `intact`
  false);
- the record under a key does not end that key with its own id: a real
  record copied to another place;
- `for_subject` finds a record about another subject under the
  subject's prefix;
- the ledger's proof fails. With immudb, every read and write is checked
  against a Merkle proof and, in deployments, the server's signature.

`get` and `verify` read only keys of the form
`<namespace>/<32 hex digits>/<id>` under `NAMESPACE` or one of
`PREVIOUS_NAMESPACES`. Any other key is `EvidenceNotFound`:

```python
await store.get("other/abc/def")
```

```text
domainkit.contrib.evidence.models.EvidenceNotFound: No evidence under other/abc/def.
```

So a caller who sends a key cannot read another project's records on a
shared ledger, or anything else on it.

`evidence_store()` returns a `LedgerEvidenceStore` built from the
settings. You can build one yourself, for a test or a second ledger:

```python
from domainkit.contrib.evidence import LedgerEvidenceStore
from domainkit.ledger import MemoryLedger

store = LedgerEvidenceStore(
    MemoryLedger(),
    namespace="evidence",
    max_bytes=4096,
    subject_key=b"subject-key-for-the-evidence-tests",
)
```

`EvidenceStore` is the port: write your own class for another kind of
store, with `record`, `get`, and `for_subject`. `verify` comes with the
base class.

## Recording after a commit: `defer_evidence`

Inside a use case, record the fact only if the change commits. Call
`defer_evidence(uow, evidence)`. It returns the key at once, so you can
store the key with the change. The record goes through the
[outbox](messaging.md), so `domainkit.contrib.messaging` must be
installed:

```python
from domainkit.contrib.evidence import Evidence

async with uow:
    consent = Evidence(
        kind="consent.given", subject="customer:42", facts={"version": "2026-10"}
    )
    key = defer_evidence(uow, consent)
    await uow.commit()
```

After the commit, the worker records it, at least once (`python
manage.py runworker`). Because recording is idempotent, a retry is
harmless. If the unit of work rolls back, nothing is recorded.

While it waits in the outbox, the record is signed with `SECRET_KEY`.
The worker refuses a record whose signature does not hold, logs it on
the security logger, and raises `EvidenceTampered`. So a person who can
write the outbox table cannot change the facts before they reach the
ledger.

## The subject key in deployments

Staging and production refuse to start without `SUBJECT_KEY` (or
`SUBJECT_KEYS`): the check is `security.E019`. Without a key, domainkit
uses a public development key, which hides nothing. Make a value once,
keep it in your secret store, and read it from the environment:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```python
EVIDENCE = {"SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY")}
```

`env.str` reads `<PREFIX>EVIDENCE_SUBJECT_KEY`, or a mounted secret file
named by `<PREFIX>EVIDENCE_SUBJECT_KEY_FILE`. Back the key up with the
ledger's credentials. The keys in your receipts always work, but
`for_subject` finds a record only with the key it was written with.

## Rotating the subject key

The subject keys form a ring. Add the new key under an id in
`SUBJECT_KEYS`, name it in `SUBJECT_KEY_ID`, and keep the old one.
`SUBJECT_KEY` has the id `default`:

```python
EVIDENCE = {
    "SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY"),  # the old key: "default"
    "SUBJECT_KEYS": {"2027": env.str("EVIDENCE_SUBJECT_KEY_2027")},
    "SUBJECT_KEY_ID": "2027",
}
```

New records' keys use the current key. `for_subject` scans the
subject's prefix under each key of the ring, the current one first,
until it has `limit` records. A record is never written again, so its
key keeps the digest it was made with. `get` and `verify` do not check
the digest, so every receipt keeps proving its record. Remove an old key
only when you no longer need to list a subject's older records.

The settings refuse a ring that does not name its current key:

- `SUBJECT_KEYS` may not use the id `default`;
- with more than one key, `SUBJECT_KEY_ID` is required;
- `SUBJECT_KEY_ID` must name a key of the ring.

## Namespaces

`NAMESPACE` is the first part of every key, so several projects or
environments can share one ledger. If you change it, list the old value
in `PREVIOUS_NAMESPACES`. Otherwise `get` and `verify` no longer find the
records written before:

```python
EVIDENCE = {"NAMESPACE": "shop_prod", "PREVIOUS_NAMESPACES": ("evidence",)}
```

`for_subject` lists only the current namespace.

## From the command line

`evidence list SUBJECT` lists a subject's records, with `--limit`
(default 50). `evidence verify KEY` proves one record:

```bash
python manage.py evidence list customer:42
python manage.py evidence verify evidence/77e0b3b900b3f6eea06236db14ba86a3/005219ba-2377-4970-b4ee-4b552bd44b8b
```

```text
evidence/77e0b3b900b3f6eea06236db14ba86a3/005219ba-2377-4970-b4ee-4b552bd44b8b  consent.given  2026-11-04 18:17:43  intact
Intact: consent.given about customer:42 at 2026-11-04 18:17:43, sha256:2e71316c952b2c50cfd62b392082709267dbed271cb67e9717a63d2c7b2155a7, transaction 1.
```

A record whose key was written again shows `WRITTEN AGAIN` in the list.
`verify` then fails with a command error that says so.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `EVIDENCE` key | Default | Meaning |
| --- | --- | --- |
| `LEDGER` | `"evidence"` | The alias in `LEDGERS` (`immudb://`; `memory://` in tests). |
| `NAMESPACE` | `"evidence"` | The first part of every key, so projects or environments can share one ledger. A lower-case letter, then up to 31 letters, digits, `_`, or `-`. |
| `PREVIOUS_NAMESPACES` | `()` | Namespaces used before `NAMESPACE`, whose records `get` and `verify` still read; at most 8, each written as `NAMESPACE` is. |
| `MAX_BYTES` | `262144` | The largest record, in bytes (1,024 to 4,000,000). Keep documents elsewhere and record their digests. |
| `SUBJECT_KEY` | `None` | The secret for the subject digests in keys, 32 characters or more; its id in the ring is `default`. Required in staging and production (or `SUBJECT_KEYS`); elsewhere a public development key is used. Keep it for as long as the evidence, or in the ring: a key removed hides its records from `for_subject`, though the keys in their receipts still work. |
| `SUBJECT_KEYS` | `{}` | More subject keys by id (1 to 32 letters, digits, `_`, `-`; not `default`), each 32 characters or more; at most 8. The current one makes the keys of new records; the others still find theirs. |
| `SUBJECT_KEY_ID` | `None` | The id of the current subject key; required when the ring holds more than one key. |

## Checks

| Id | When | What to do |
| --- | --- | --- |
| `security.E019` | Startup in staging or production with no `SUBJECT_KEY` and no `SUBJECT_KEYS`. | Set a random secret of 32 characters or more. |
| (startup) | `LEDGER` names no alias in `LEDGERS`: `ImproperlyConfigured: LEDGERS has no 'evidence' alias.` | Add the ledger, or set `LEDGER` to an alias that you have. |
| (deployment warning) | Staging or production without `domainkit.contrib.audit`. | Install the audit app. It keeps the state the ledger last proved in `AUDIT.DATABASE`; otherwise that state lives in memory, and a process that starts after the ledger was rolled back trusts it. |

## Errors and refusals

| Error | When | HTTP |
| --- | --- | --- |
| `ValueError` | The kind, subject, artifacts, or `recorded_at` are not allowed; a float or a non-finite number in the facts; a record larger than `MAX_BYTES`. | 500, a bug in your code |
| `EvidenceConflict` | Another record already uses this evidence id. | 409, `evidence_conflict` |
| `EvidenceNotFound` | No record under the key, or the key is not an evidence key of your namespaces. | 404, `evidence_not_found` |
| `EvidenceTampered` | The record does not match its receipt, its key was written again, it sits under another record's key, the ledger's proof failed, or a deferred record's signature does not hold. | 500; investigate at once |

## On PostgreSQL and SQLite

The records live on the ledger, so the SQL database does not matter.
`defer_evidence` writes one outbox row in the same transaction as your
change, on PostgreSQL and on SQLite alike.

## Upgrading

Deferred records are signed since the release after 1.6, and the worker
refuses an unsigned one. Before you deploy it, let the worker run until
no `domainkit.evidence.record` message is pending. A record left
unsigned is refused and dies; the change that made it defers its
evidence again ([upgrading.md](../upgrading.md#let-the-evidence-queue-drain-first)).

## Common mistakes

- **Putting an email address or a name in the subject.** It is refused.
  Use an internal id with a type, such as `customer:42`.
- **Writing an amount as a float.** It is refused when recorded. Use
  `Decimal` or a string.
- **Keeping only `for_subject`, not the receipts.** Store each receipt's
  key with the change it proves. A listing finds records; a receipt
  proves them.
- **Changing `SUBJECT_KEY` without keeping the old one.** `for_subject`
  no longer finds older records. Add the new key to the ring instead.
- **Changing `NAMESPACE` without `PREVIOUS_NAMESPACES`.** Older receipts
  then read as `EvidenceNotFound`.

## See also

- [Ledgers (immudb)](../databases.md#ledgers-immudb): the ledger server
  and its settings.
- [Audit](audit.md): who did what; it also keeps the ledger's trusted
  state.
- [Messaging](messaging.md): the outbox and the worker that
  `defer_evidence` uses.
- [Keys](keys.md): encrypted fields that you can erase.
- [Checks](../reference/checks.md): every check id.
