# 0028. Sign with one SECRET_KEY, and derive a key per purpose from it

- Status: Accepted
- Date: 2026-10-19

## Context

Password reset links, email verification links, CSRF tokens for browser
sessions, and the short-lived challenge between a password and a one-time
code are all values the service hands out and must recognize when they
come back. Until now each feature that needed a secret brought its own:
`AUTH.JWT.SIGNING_KEY`, `EVIDENCE.SUBJECT_KEY`, per-subject audit keys in
the database. Adding one setting per new feature would multiply the keys
operators must generate, store, rotate, and check, and invite reuse of one
value for several of them.

Django has one `SECRET_KEY`, a `signing` module on top of it, and
`SECRET_KEY_FALLBACKS` for rotation. Its signatures are HMACs keyed by a
hash of the salt and the key. Encryption at rest (TOTP secrets, ADR 0030)
also needs a key, which must not be the signing key itself.

The options were: (a) a key per feature in each app's settings; (b) one
key, used directly by every feature; (c) one key, from which each purpose
derives its own.

## Decision

`SECRET_KEY` (and `SECRET_KEY_FALLBACKS`) are core settings, `SecretStr`,
hidden in `repr` and dumps like other secrets. `domainkit.signing` follows
Django's API: `Signer`, `TimestampSigner`, `dumps`, `loads`, `BadSignature`,
and `SignatureExpired`.

- Every purpose gets its own key, derived with HKDF-SHA256 (RFC 5869) from
  the secret, with `domainkit:<purpose>` as the info. A signer's purpose is
  its kind (plain or timestamped) and its salt, so a value signed for one
  purpose, or by the other kind, never verifies for another.
- Signatures are HMAC-SHA256 in URL-safe base64 without padding, compared
  in constant time against the derived key of `SECRET_KEY` and of each
  fallback. Only `SECRET_KEY` signs.
- `derive_key` and `derive_keys` give other uses, such as encryption, keys
  of their own.
- Staging and production refuse a missing key, one with fewer than 50
  characters or fewer than 5 different ones, and one that starts with a
  development prefix (`domainkit-insecure-`, `django-insecure-`); the same
  for every fallback. Errors never quote a key. Elsewhere a project starts
  without a key, and fails the first time something signs.
- Existing purpose-specific keys stay as they are: JWTs may be checked by
  other services with a key they share, and evidence subjects must keep
  their digests for good.

## Consequences

- Operators manage one secret for everything the framework signs, and
  rotate it without logging anyone out: the new key signs, the old one
  verifies until what it signed has expired.
- Changing `SECRET_KEY` without a fallback ends every outstanding reset
  link, verification link, CSRF token, and MFA challenge, and makes stored
  TOTP secrets unreadable (ADR 0030): the deployment documentation says so.
- Deployed projects must now set `SECRET_KEY`; the project template reads
  it from the environment, and the change is called out in the changelog.
- The derivation is part of the stored format of encrypted values and of
  every outstanding signature: changing it later needs a version marker,
  which encrypted values carry and signatures do not.
