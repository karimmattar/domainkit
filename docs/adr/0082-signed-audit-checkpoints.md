# 0082. Prune the audit chain below signed checkpoints, in a fixed format

- Status: Accepted
- Date: 2026-10-26

## Context

The audit trail is a hash chain (each seal links the one before), anchored
on a ledger. Two things it could not do (N4, N6, DATA-05):

- **Retention.** A trail kept for good grows for good, and regulations
  set how long to keep it, not forever. Deleting old rows broke the chain:
  `verify` reported every pruned row as missing.
- **A witness outside the operator.** Whoever controls both the database
  and the ledger can rewrite both. Only a party that kept something from
  earlier can tell, and it needs something it can check without the
  operator's database, ledger, or credentials.

Both need the same thing: a statement "the chain of database D had link L
at seal S", which someone other than the database vouches for. Options:

- **HMAC under `SECRET_KEY`.** No new key, but a verifier needs the secret:
  no third party can check it.
- **The ledger's own proof.** immudb signs its states, and an anchor is in
  the ledger. Checking that offline means shipping immudb's proof
  verification (inclusion and dual proofs, its encodings) to every
  verifier, and it ties the format to one ledger.
- **An Ed25519 signature by a key of the project (chosen).** Any library
  checks it with the public key alone; the key stays out of the database,
  so writing to the tables cannot forge one. It needs `cryptography` (the
  `audit` extra), already an extra of webhooks and MFA.

How to delete rows the `protect()` triggers refuse to delete, measured on
PostgreSQL 16 with a writer inserting a row every millisecond while 20,000
rows were pruned in batches of 1,000:

| Way | Needs | Prune | Writer's median, worst wait |
| --- | --- | --- | --- |
| `ALTER TABLE ... DISABLE TRIGGER` in each batch's transaction | the table's owner | 100 ms | 2.5 ms, 6.8 ms |
| `SET LOCAL session_replication_role = replica` | a superuser (or a grant of that setting) | 58 ms | 1.1 ms, 4.1 ms |

## Decision

- **The checkpoint format** (`domainkit.contrib.audit.checkpoints`) is a
  JSON object written canonically: keys sorted, no spaces, ASCII:

  ```json
  {"database":"default","format":"domainkit.audit.checkpoint/1","key_id":"<16 hex>","kind":"prune","link":"<64 hex>","made_at":"<ISO 8601>","namespace":"acme-prod","previous":{"link":"<64 hex>","seq":1000},"sealed_at":"<ISO 8601>","seq":1234}
  ```

  `kind` is what it was signed for: `"prune"` (stored by `auditprune`) or
  `"export"` (an export's head, which leaves the system and so must vouch
  for no deletion); `seq` and `link` are the seal's; `sealed_at` when it
  was sealed;
  `previous` the checkpoint before it in the chain (`null` for the first);
  `key_id` the first 16 hex digits of the SHA-256 of the raw public key.
  The signature is Ed25519 over the document's exact UTF-8 bytes. A
  document that is not canonical is refused, so there is one byte string
  per checkpoint. A new field or meaning is a new `format`.
- **An export** (`auditverify --export`) is JSON: `format`
  (`domainkit.audit.export/1`), `exported_at`, `namespace`, `key_id`,
  `public_key` (PEM, for convenience: verifiers pin their own copy), and
  `databases`, each with `checkpoints`, a list of `{"document": "<the
  exact text>", "signature": "<base64>"}`, oldest first: the stored
  checkpoints, then one of the head. A verifier checks each signature
  under the key it pinned, that each names that key, its database and
  namespace, and that each one's `previous` is the one before; only a
  `"prune"` checkpoint is followed by another.
- **Pruning** (`auditprune`) cuts at the newest seal sealed before the
  retention date, never one younger than `AUDIT.MIN_RETENTION` (with a
  ledger, the newest one anchored that long ago; never the head),
  verifies the chain up to it, and refuses if anything is wrong. It
  records the checkpoint on the ledger (the SHA-256 of its document, under
  `domainkit/audit-pruned/<namespace>/<database>`), stores it
  (`domainkit_audit_checkpoints`), then deletes rows and
  seals at and below the cut, newest first, so a run cut short leaves a
  gap the checkpoint ends; the next run checks again and finishes.
- **The keys.** The application verifies with the public key
  (`AUDIT.CHECKPOINT_PUBLIC_KEY`), checked at startup. Only `auditprune`
  and `auditverify --export` read the private key (`AUDIT.CHECKPOINT_KEY`),
  and refuse one whose public half is not the configured public key, so
  the private key can live where those commands run alone.
- **`verify`** accepts a gap in the chain only where a checkpoint ends
  it that verifies under `AUDIT.CHECKPOINT_PUBLIC_KEY`, for this database
  and namespace, was signed for pruning, follows the one before it that
  does (`previous`; none for the first), and was made `MIN_RETENTION`
  after its seal and before `verify` runs less `MIN_RETENTION`; with a
  ledger, the ledger records it, and first anchored its seal that long
  before (an anchor's time is the latest any anchor before it in the
  ledger names, so one appended later and dated back moves nothing). It
  ends it: the seal after it must link from its link. A checkpoint at a
  seal still present must name that seal's link. One that does not verify
  is reported and starts nothing. Anchors of pruned seals are not checked,
  except one at a checkpoint, which must be its link.
- **Triggers** are disabled per batch with `ALTER TABLE`, as the tables'
  owner (`MIGRATION_URL`): owner rights are what migrations need already,
  where the other way needs a superuser for the application. Writers wait
  for one batch, a few milliseconds.

Revised before release, after a security review (SEC-10b-2, SEC-10b-3):
without `kind`, an export's head checkpoint, stored in the table by
whoever can write it, covered deleting the whole trail; and with the
private key in every process's settings, whoever held them could sign a
checkpoint over anchored rows.

## Consequences

- Whoever can write the tables cannot cover a deletion: they have no key,
  and what they can copy (an export) is not for pruning. Whoever also
  holds the private key and the ledger's credentials can prune, as
  `auditprune` does, but not rows younger than `MIN_RETENTION`, by the
  ledger's account; without a ledger, by the checkpoint's own dates,
  which the key's holder chooses. Raising `MIN_RETENTION` reports the
  prunes made below it before.

- A trail can be kept for a period and verified as before; the
  checkpoints, and the exports, outlive the release, which is why the
  format is fixed here and versioned.
- Losing `AUDIT.CHECKPOINT_KEY` leaves stored checkpoints unverifiable:
  `auditverify` fails until the key is back. Rotation (several public
  keys by `key_id`) is left to the next minor's work on retention, with
  timestamps from a third party (RFC 3161) and legal holds.
- The checkpoints table needs no triggers: a row changed, added, or
  removed shows as a signature that does not verify, or as a gap nothing
  ends.
- An export is a witness only if someone outside keeps it. The checks
  compare checkpoints with each other and with the key, not with rows:
  exporting the rows themselves (segments) is also left to the next minor.
