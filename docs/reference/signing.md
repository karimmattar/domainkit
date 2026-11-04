# Signing

A service often hands out a value and takes it back later unchanged: an
unsubscribe link in an email, an invitation, a cursor. `domainkit.signing`
signs such values, so the service can prove that it made them and that
nobody changed them. `domainkit.encryption` keeps secrets that the
database must give back, such as a webhook's signing secret, encrypted.
Both derive their keys from `SECRET_KEY`.

On this page you find:

- how to sign a value, and check it when it comes back;
- how to sign JSON with a time, and refuse it once it is too old;
- how keys are derived, and how to replace `SECRET_KEY`;
- how to encrypt a secret with `SecretBox`.

## A first example

`Signer` signs text with a salt, which names the purpose of the value:

```python
from datetime import timedelta

from domainkit import signing

signer = signing.Signer(salt="newsletter.unsubscribe")
value = signer.sign("42")
print(value)
print(signer.unsign(value))

token = signing.dumps({"invite": 7}, salt="invites")  # timestamped JSON
print(token)
print(signing.loads(token, salt="invites", max_age=timedelta(days=7)))
```

```text
42:cUqAIiZfS_dvHbhObDEwLxs5cv_u-kDZUc4HLoeub_o
42
eyJpbnZpdGUiOjd9:1xOfqc:4JOFoP9EHXjlrjWXSN5lSKPVIvBDNiSWRTk5MvVtMA0
{'invite': 7}
```

A value that was changed, or signed for another salt, is refused:

```python
signing.loads(token, salt="other")
```

```text
domainkit.signing.BadSignature: The signature does not match.
```

## The names

| Name | What it does |
| --- | --- |
| `Signer(*, salt=, key=, fallback_keys=)` | `sign(value)`, `unsign(value)`, `sign_object(obj)`, `unsign_object(value)`. It signs with `key` (default: `SECRET_KEY`, which brings `SECRET_KEY_FALLBACKS` along), and also accepts `fallback_keys`. |
| `TimestampSigner` | The same, with the time signed too: `unsign(value, max_age=)` raises `SignatureExpired` past it. |
| `dumps(obj, *, salt=, key=)`, `loads(value, *, salt=, key=, fallback_keys=, max_age=)` | Compact, URL-safe, signed, timestamped JSON. `max_age` is seconds or a `timedelta`. |
| `BadSignature`, `SignatureExpired` | The refusals; the second is a kind of the first. Neither quotes the value. |
| `derive_key(purpose, *, key=None, length=32)`, `derive_keys(purpose, *, length=32)` | A key of its own for `purpose` (HKDF-SHA256), from `SECRET_KEY`; `derive_keys` gives one from `SECRET_KEY`, then one from each fallback. |
| `constant_time_compare(a, b)` | Compares secrets without telling how close they came. |
| `Key` | The type of a key: `str` or `bytes`. |

## How a signature works

A signature is HMAC-SHA256, compared in constant time, under a key
derived from `SECRET_KEY` for its salt. So a value signed for one purpose
never verifies for another. Plain and timestamped values never mix
either: each kind derives its own keys.

!!! warning
    Anyone can read a signed value: the signature only proves that it was
    not changed. Sign ids, never secrets. To keep a secret, encrypt it
    with `SecretBox`.

A project without `SECRET_KEY` starts locally, and fails the first time
something signs. Staging and production refuse to start without one
(`security.E016`).

## Replacing `SECRET_KEY`

You can replace the key without logging anyone out:

1. Put the old key first in `SECRET_KEY_FALLBACKS`, and the new one in
   `SECRET_KEY`. New values are signed with the new key; old ones still
   verify.
2. With `domainkit.contrib.auth`, run `python manage.py resealmfa`, so no
   TOTP secret stays under the old key.
3. Drop the old key once what it signed has expired.

```python
SECRET_KEY = env.str("SECRET_KEY")
SECRET_KEY_FALLBACKS = env.list("SECRET_KEY_FALLBACKS", [])
```

See [ADR 0028](../adr/0028-one-secret-key-signs-and-derives.md).

## Encryption

Some secrets must come back from the database as they were: TOTP
secrets, or a signing secret shared with another service. Keep them
encrypted with `domainkit.encryption.SecretBox`. It needs `cryptography`
(the `mfa` extra):

```python
from uuid import UUID

from domainkit.encryption import SecretBox

box = SecretBox("billing.webhook_secret")  # a purpose of its own
owner = UUID("6f1c7e6a-0d2b-4c55-9f0e-1f2a3b4c5d6e")
sealed = box.encrypt(b"whsec_abc123", associated=owner.bytes)  # bytes to store
print(box.decrypt(sealed, associated=owner.bytes))
box.decrypt(sealed, associated=b"another row")
```

```text
(b'whsec_abc123', True)
domainkit.encryption.DecryptionFailed: No key decrypts the value.
```

| Name | What it does |
| --- | --- |
| `SecretBox(purpose)` | A box with keys derived from `SECRET_KEY` and its fallbacks, for `purpose` alone. |
| `encrypt(plaintext, *, associated)` | The plaintext (bytes), encrypted under the current key, and bound to `associated`. |
| `decrypt(value, *, associated)` | The plaintext, and `True` when `SECRET_KEY` sealed it (`False` for a fallback). |
| `DecryptionFailed` | Raised when no key decrypts the value. It quotes nothing. |

The box uses AES-256-GCM under a key derived from `SECRET_KEY` for the
purpose, with a random nonce per value. `associated` binds the value to
its owner, so a value copied to another row does not decrypt. A value
encrypted under a key that is now in `SECRET_KEY_FALLBACKS` still
decrypts, with `False` as the second item: encrypt it again then. See
[ADR 0030](../adr/0030-encrypt-totp-secrets-under-a-derived-key.md).

For fields of your aggregates, and keys held by a key service, use
`domainkit.contrib.keys` instead ([keys.md](../contrib/keys.md)).

## See also

- [Settings](settings.md#core): `SECRET_KEY` and `SECRET_KEY_FALLBACKS`.
- [Keys](../contrib/keys.md): encrypted fields under a key service.
- [Commands](commands.md#resealmfa-encrypt-second-factors-under-the-current-key):
  `resealmfa`.
