# 0030. Encrypt TOTP secrets with AES-GCM under a key derived from SECRET_KEY

- Status: Accepted
- Date: 2026-10-19

## Context

A second factor with an authenticator app (TOTP, RFC 6238) shares a secret
between the app and the service. Unlike a password, the service must read
the secret back to compute the expected code, so it cannot store a slow
hash of it. A secret stored in clear lets anyone who reads the database,
or a backup of it, compute every user's codes, which defeats the second
factor exactly when the first may be leaking too.

The options were:

- store the secret in clear, as many libraries do;
- encrypt it with a key of its own in a new setting (`AUTH.MFA.KEY`);
- encrypt it with a key derived from `SECRET_KEY` (ADR 0028);
- hand it to a key management service (KMS) or an HSM.

For the cipher: AES-GCM (authenticated, a standard, hardware-accelerated)
or ChaCha20-Poly1305, both in `cryptography`; the standard library has no
authenticated cipher. Fintech projects will later need key rings, key
ids, and a KMS.

## Decision

- TOTP secrets are encrypted with AES-256-GCM under a key derived from
  `SECRET_KEY` with HKDF-SHA256 for the purpose `encryption:domainkit.auth.totp`
  (`domainkit.signing.derive_keys`). The key is never stored, and differs
  from every signing key.
- A stored value is a version byte, a random 96-bit nonce, and the
  ciphertext with its tag. The version byte and the user's id are
  associated data, so a value copied to another user's row does not
  decrypt, and the format can change later without guessing.
- Values encrypted under a key that moved to `SECRET_KEY_FALLBACKS` still
  decrypt; the next code a user enters encrypts the secret again under the
  current key, so rotation completes as users log in.
- `cryptography` is an optional extra, `domainkit[mfa]`, like `jwt` and
  `argon2`; the rest of auth needs nothing new. It is already a dependency
  of `pyjwt[crypto]`.
- `encryption.SecretBox(purpose)` is the seam: `encrypt(plaintext,
  associated=)` and `decrypt(value, associated=)`. A key ring or a KMS
  later implements the same two calls behind a new version byte.

## Consequences

- A database dump, or read access to it, reveals no TOTP secret without
  `SECRET_KEY`. Recovery codes, which are never read back, are stored as
  SHA-256 digests bound to their user (80 random bits each).
- Losing `SECRET_KEY`, or rotating it without keeping the old key as a
  fallback until every user has logged in, makes the secrets unreadable:
  those users can no longer log in until staff run `resetmfa`. The
  documentation says so where it explains rotation.
- `SECRET_KEY` now protects confidentiality, not only integrity. Deployed
  environments already require a strong one (ADR 0028).
- Moving to a KMS later means a new version byte and a `SecretBox` that
  calls it; values of version 1 keep decrypting until re-encrypted.
