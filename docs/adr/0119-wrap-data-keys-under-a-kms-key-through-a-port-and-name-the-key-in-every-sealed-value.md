# 0119. Wrap data keys under a KMS key through a port, and name the key in every sealed value

- Status: Accepted
- Date: 2026-10-31

## Context

Until 0.15 every secret domainkit read back was sealed by `SecretBox`
(ADR 0030): AES-256-GCM under a key derived from `SECRET_KEY`. A bank's
controls ask more of keys than that (the fintech audit's FIN-23 and
FIN-38): the key that protects stored data lives in a key management
service (an HSM-backed KMS or Vault), never in the application's
settings; using it leaves a trail in that service; it rotates without
re-encrypting everything at once; and an operator can tell which key
protects which data. ADR 0030 left the seam for this: a new version byte
and a box that calls a KMS.

Calling the KMS for every value is not an option: a call costs a network
round trip (milliseconds), and a page of 50 rows would cost 50 of them.
Envelope encryption is the standard answer: a few random *data keys*
encrypt the values in the process, and only the data keys are encrypted
(*wrapped*) by the *key-encryption key* (KEK) in the KMS. Unwrapping a
data key is one call; the process keeps it in memory.

How the adapters reach their service was chosen by measurement:

- **Vault's transit engine** is plain HTTPS and JSON (`POST
  /v1/transit/datakey/plaintext/<key>`, `decrypt`, `rewrap`), so
  `domainkit.http.client` (`domainkit[httpclient]`, already used for
  outgoing calls) reaches it with no new package.
- **AWS KMS** is HTTPS and JSON too (`X-Amz-Target:
  TrentService.GenerateDataKey`), but every request must be signed with
  Signature Version 4. Measured against the two SDK options (`pip install
  --dry-run --report`, October 2026):

  | | New packages | Size | Async |
  | --- | --- | --- | --- |
  | `boto3` as an extra | 7 (`botocore` alone is a 16 MB wheel) | 16.3 MB | no: every call on a thread |
  | `aiobotocore` as an extra | 17, `aiohttp` among them: a second HTTP stack beside httpx2 | larger still | yes |
  | SigV4 signed here | 0 | about 100 lines | yes, through the HTTP client |

  The signature is small and fully specified, and the implementation
  reproduces AWS's own published example (the IAM `ListUsers` request
  of 2015-08-30, its signing key and its signature) exactly, which the
  tests pin. What the SDK adds beyond signing is the credential chain;
  the cases a bank deploys on are few: the `AWS_*` variables, a web
  identity (EKS's IAM roles for service accounts, through STS's unsigned
  `AssumeRoleWithWebIdentity`), and the container's endpoint (ECS and EKS
  Pod Identity). Anything else plugs in as an async callable.

## Decision

- **`domainkit.keys.KeyProvider`**, the port: `generate_data_key()` (a
  `DataKey`: 32 random bytes and their wrapped form), `unwrap(wrapped)`,
  and `rewrap(wrapped)` (the same data key under the KEK's newest version,
  after the KEK rotated, without its plaintext leaving the service).
  A wrapped key is text that starts with its provider's name (`local:`,
  `aws-kms:`, `vault:`), so one provider is never handed another's.
  Failures are `KeyProviderError` (`UnknownKey` for a key it cannot
  unwrap), whose messages name the provider, the operation and the
  service's error class or status, never a key or the answer's text.
- **Three adapters.** `LocalKeyProvider`: a KEK derived from `SECRET_KEY`
  (HKDF, purpose `keys.kek`), its fallbacks unwrapping too, for
  development and tests. `domainkit.keys.aws.AwsKmsKeyProvider`:
  `GenerateDataKey`, `Decrypt` (naming the key, so KMS refuses another
  key's blob), `ReEncrypt`, each carrying the encryption context
  `{"domainkit": "data-key"}`, signed by `domainkit.keys.aws.sign`.
  `domainkit.keys.vault.VaultTransitKeyProvider`: `datakey/plaintext`,
  `decrypt`, `rewrap`, with a token (or an async callable for a renewed
  one) and Vault Enterprise's namespace. The cloud adapters call through
  `HttpClient` (`propagate=False`), which logs a call's method, origin and
  path only, and bounds it by `KEYS.TIMEOUT` and the request's deadline.
  No adapter logs or `repr`s key material or credentials. They hold no
  state a concurrent task could tear: credentials are swapped whole.
- **`domainkit.keys.ring.Keyring`** holds the data keys of
  `KEYS.DATA_KEYS` (id to wrapped key), unwrapped once (`load()`, at
  startup, so a KMS that refuses stops the process early), and the id of
  the current one. Inside SQLAlchemy's async session, whose events run in
  a greenlet, a ring used before startup loads itself (`await_only`);
  elsewhere it says to call `load()` (`KeysNotLoaded`); a local provider's
  ring loads anywhere.
- **The envelope.** A sealed value is text: `dk1:k.<data key id>:` or
  `dk1:s.<subject key id>:` (ADR 0120), then a random 96-bit nonce and the
  AES-256-GCM ciphertext and tag, in URL-safe base64. `dk1` is the format;
  the format, the key's reference, and the caller's context are the
  associated data, so a value that claims another key, or is opened for
  another context, does not open. Text, not bytes, so dumps, fixtures and
  `LIKE` prefixes (which count the values under each key) read it as it is.
- **Rotation, in two layers.** The KEK rotates in its service (AWS's
  yearly rotation, `vault write -f transit/keys/<key>/rotate`); `manage.py
  keys rewrap` prints `DATA_KEYS` wrapped under its newest version, the
  data keys unchanged. A data key rotates by adding one (`manage.py keys
  generate`) and making it `CURRENT_KEY`; the others are retired: they
  still open what they sealed until `manage.py keys reencrypt` moves it
  (ADR 0120), and `keys.W001` warns while any is kept.
- **The local KEK is refused where deployed** (`keys.E001`) unless
  `KEYS.ALLOW_LOCAL_KEK` says the project accepts that whoever reads the
  settings can open every encrypted field.

## Consequences

- A bank's KMS holds the only key that opens the data keys; each process
  makes one call per data key at startup, then none. The KMS's audit log
  (CloudTrail, Vault's audit device) shows every unwrap.
- domainkit maintains a SigV4 signer and a credential chain of three
  sources. Both are pinned by tests: AWS's published vector, and a fake
  KMS that recomputes each request's signature from the request as sent.
  An instance profile (IMDS) is not built in: pass a credentials callable.
- `SecretBox` (ADR 0030) is unchanged: TOTP secrets and webhook secrets
  stay under `SECRET_KEY`. Moving them onto the ring is a later change.
- A data key unwrapped lives in the process's memory for its lifetime,
  like `SECRET_KEY`; a memory dump of a running process reveals it.
