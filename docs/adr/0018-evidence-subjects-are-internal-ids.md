# 0018. Evidence subjects are internal ids, keyed by a secret digest

- Status: Accepted
- Date: 2026-10-18
- Amends: [0011](0011-immudb-ledger-for-evidence.md)

## Context

[0011](0011-immudb-ledger-for-evidence.md) keys evidence by
`<namespace>/<subject digest>/<evidence id>`, where the digest is a plain
SHA-256 of the subject. Each record also stores the subject as written.

A ledger never forgets. If a project uses an email address, a phone number,
or a national id as the subject, that personal data is kept forever, and an
erasure request (GDPR article 17) cannot be met. The docs warned against
personal data in facts, but not in the subject. The key does not help
either: anyone who can list keys can hash a guessed email address and find
its records, since a plain hash of a guessable value is no secret.

Options considered:

- **Document it only.** Cheap, but nothing stops the mistake.
- **Encrypt the subject.** The record would need a key to read, and the
  ledger could still not forget it.
- **Refuse subjects that look like personal data, and key them by a secret
  digest.** No check can recognise every kind of personal data, but an
  allow-list of id characters refuses the common mistakes.

## Decision

- Records get format 2. Format 1 records stay readable and keep their keys.
- In format 2, a subject is an internal id: letters, digits, and
  `_ . : / -`, starting with a letter or digit, up to 255 characters. This
  refuses email addresses (`@`), names (spaces, non-ASCII letters), and
  international phone numbers (`+`). The error never echoes the subject.
- Format 2 keys use HMAC-SHA-256 of the subject under a new secret,
  `EVIDENCE.SUBJECT_KEY` (32 characters or more), truncated to 32 hex
  digits as before.
- `SUBJECT_KEY` is required in staging and production. Elsewhere a public
  development key is used, so tests and local runs need no setup.
- Keys follow the record's format. A format 1 record, such as one queued in
  the outbox before an upgrade, is written under the plain-digest key that
  `defer_evidence` returned for it.
- `for_subject` scans the subject's format 2 prefix, then its format 1
  prefix, so records from before the change are still listed.

## Consequences

- Upgrading a deployment needs `EVIDENCE.SUBJECT_KEY`; startup names it if
  missing. Projects whose subjects are not ids must map them to ids first.
- The key cannot be rotated without hiding earlier records from
  `for_subject`. Keys stored with receipts keep working, since they are
  whole keys. If rotation is ever needed, `for_subject` can scan the
  prefixes of earlier keys too.
- A leaked `SUBJECT_KEY` brings back the guessing problem for keys, but no
  more: subjects are ids, not personal data.
- `for_subject` costs one more prefix scan for the format 1 records. It can
  be dropped in a later format once no format 1 records are expected.
- `subject_digest`, `evidence_key`, and `LedgerEvidenceStore` take the
  subject key. Code that called them directly must pass it.
