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

No evidence has been recorded with domainkit yet, so the change needs no
second record format and no way to read old keys.

## Decision

- A subject is an internal id: letters, digits, and `_ . : / -`, starting
  with a letter or digit, up to 255 characters. This refuses email
  addresses (`@`), names (spaces, non-ASCII letters), and international
  phone numbers (`+`). The error never echoes the subject.
- Keys use HMAC-SHA-256 of the subject under a new secret,
  `EVIDENCE.SUBJECT_KEY` (32 characters or more), truncated to 32 hex
  digits as before.
- `SUBJECT_KEY` is required in staging and production. Elsewhere a public
  development key is used, so tests and local runs need no setup.
- The record layout is unchanged and stays format 1.

## Consequences

- Deployments need `EVIDENCE.SUBJECT_KEY`; startup names it if missing.
- The key cannot be rotated without hiding earlier records from
  `for_subject`. Keys stored with receipts keep working, since they are
  whole keys. If rotation is ever needed, `for_subject` can scan the
  prefixes of earlier keys too.
- A leaked `SUBJECT_KEY` brings back the guessing problem for keys, but no
  more: subjects are ids, not personal data.
- `subject_digest`, `evidence_key`, and `LedgerEvidenceStore` take the
  subject key. Code that called them directly must pass it.
