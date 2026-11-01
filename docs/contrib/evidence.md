# Evidence: records that provably never change

Evidence is what you may have to prove later, exactly as it was: a
verification result, a signed consent, a regulatory submission. It goes to a
ledger (an immudb database, [ADR 0011](../adr/0011-immudb-ledger-for-evidence.md)),
not to PostgreSQL. The audit trail records who did what; evidence records
facts and the digests of the documents behind them.

```python
from domainkit.contrib.evidence import Evidence, evidence_store, sha256

evidence = Evidence(
    kind="kyc.document_verified",
    subject="customer:42",
    facts={"result": "match", "model": "v3"},
    artifacts={"passport": sha256(document_bytes)},
)
receipt = await evidence_store().record(evidence)  # keep it with your data
recorded = await evidence_store().verify(receipt)  # raises EvidenceTampered
```

- A record is canonical JSON (sorted keys, no spaces, UTF-8) with the id,
  kind, subject, facts, artifact digests, time, actor, and request id. The
  receipt holds its key, its SHA-256 digest, and the ledger transaction.
- Amounts must be exact. A `Decimal` in the facts is written as a
  fixed-point string (`Decimal("10.00")` as `"10.00"`) and read back as that
  string. Floats are refused, with a `ValueError` naming where they are:
  most decimal amounts have no exact float, and the receipt would prove a
  rounded value. Records written before floats were refused still read,
  verify, and, if queued in the outbox, record as they were.
- The subject is an internal id, such as `customer:42` or a UUID: letters,
  digits, and `_ . : / -`. Email addresses, names, and phone numbers with a
  `+` are refused (`ValueError`, without echoing the value). A check cannot
  tell a bare phone number from a numeric id, so use ids with a type, such
  as `customer:42`.
- Keys are `<NAMESPACE>/<subject digest>/<evidence id>`. The digest is an
  HMAC-SHA-256 of the subject under `SUBJECT_KEY`, so keys show no subject
  and nobody without the key can find a subject's records by hashing
  guesses. `for_subject("customer:42")` is a prefix scan.
- `get` and `verify` read only keys of that form under `NAMESPACE` or one
  of `PREVIOUS_NAMESPACES`; any other key is `EvidenceNotFound`, so a
  caller who sends a key cannot read another project's or environment's
  records on a shared ledger, or anything else on it. Changing
  `NAMESPACE` hides the receipts written before until the old namespace
  is listed in `PREVIOUS_NAMESPACES` (in 0.9.0, `get` read any key).
- Recording is idempotent by evidence id: a retry returns the same receipt,
  and another record with that id is an `EvidenceConflict` (409). The ledger
  writes a key only if it is new, so writers racing to record the same
  evidence leave one revision and all get its receipt.
- Reads return revision 1, the original. If anything wrote the key again,
  `RecordedEvidence.intact` is false and `verify()` raises
  `EvidenceTampered`. So does a read of a record under a key that does
  not end with its own id, and a `for_subject` listing that finds a record
  about another subject under the prefix: real records copied elsewhere.
  With immudb, every read and write is also checked against a Merkle proof
  and, in deployments, the server's signature.
- Receipts are what tie evidence to your data: anyone holding the ledger's
  write credentials can add records, though never change one unnoticed. Keep
  each receipt (or key) with the change it proves; `for_subject` is a
  convenience listing.
- Inside a use case, `key = defer_evidence(uow, evidence)` records it
  through the outbox once the change commits (messaging must be
  installed). Store the key with the change.

**Never put personal data in the subject or facts**: a ledger cannot
forget, so an erasure request could not be met. Store references or
digests, or encrypt personal fields with keys you can destroy.

`python manage.py evidence --subject customer:42` lists a subject's records;
`evidence --verify <key>` proves one.

**Deployments need `SUBJECT_KEY`**: staging and production refuse to start
without it. Generate a value once, keep it in your secret store, and read
it from the environment:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```python
EVIDENCE = {"SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY")}
```

`env.str` reads `<PREFIX>EVIDENCE_SUBJECT_KEY`, or a mounted secret file
named by `<PREFIX>EVIDENCE_SUBJECT_KEY_FILE`. Back the key up with the
ledger's credentials. `for_subject` finds records only under a key of the
ring they were written with, though the keys kept with their receipts
always work.

**Rotating the subject key.** The subject keys are a ring: add a new key
under an id in `SUBJECT_KEYS` and name it in `SUBJECT_KEY_ID`, keeping the
old one (`SUBJECT_KEY` has the id `default`):

```python
EVIDENCE = {
    "SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY"),  # the old key: "default"
    "SUBJECT_KEYS": {"2027": env.str("EVIDENCE_SUBJECT_KEY_2027")},
    "SUBJECT_KEY_ID": "2027",
}
```

New records' keys use the current key; `for_subject` scans the subject's
prefix under each key of the ring, the current one first. A record is
never rewritten, so its key keeps the digest it was made with: `get` and
`verify` read keys without checking the digest, so every receipt keeps
proving its record. Drop an old key only when listing a subject's older
records by subject no longer matters.

| `EVIDENCE` key | Default | Meaning |
| --- | --- | --- |
| `LEDGER` | `"evidence"` | Alias in `LEDGERS` (`immudb://`; `memory://` in tests). |
| `NAMESPACE` | `"evidence"` | Key prefix, so projects or environments can share one ledger. |
| `PREVIOUS_NAMESPACES` | `()` | Namespaces used before `NAMESPACE`, whose records `get` and `verify` still read; at most 8, each a namespace as `NAMESPACE` is. |
| `MAX_BYTES` | `262144` | Largest record, in bytes (1,024 to 4,000,000); keep documents elsewhere and record their digests. |
| `SUBJECT_KEY` | `None` | Secret for the subject digests in keys, 32 characters or more; in the ring of subject keys its id is `default`. Required in staging and production (or `SUBJECT_KEYS`); elsewhere a public development key is used. Keep it for as long as the evidence, or in the ring: a key dropped hides the records it keyed from `for_subject`, though the keys in their receipts still work. |
| `SUBJECT_KEYS` | `{}` | More subject keys by id (1 to 32 letters, digits, `_`, `-`; not `default`), each 32 characters or more; at most 8. The current one keys new records; the others still find theirs. |
| `SUBJECT_KEY_ID` | `None` | The id of the current subject key: required when the ring holds more than one key. |

