# Signing

`domainkit.signing` is Django's `signing`: values the service hands out and
takes back unchanged, such as a link in an email.

```python
from datetime import timedelta

from domainkit import signing

signer = signing.Signer(salt="newsletter.unsubscribe")
value = signer.sign("42")  # "42:<signature>"
signer.unsign(value)  # "42", or BadSignature if anything changed

token = signing.dumps({"invite": 7}, salt="invites")  # timestamped JSON
signing.loads(token, salt="invites", max_age=timedelta(days=7))
```

| Name | What it does |
| --- | --- |
| `Signer(salt=, key=, fallback_keys=)` | `sign`, `unsign`, `sign_object`, `unsign_object`. |
| `TimestampSigner` | The same, with the time signed: `unsign(value, max_age=)` raises `SignatureExpired` past it. |
| `dumps(obj, salt=)`, `loads(value, salt=, max_age=)` | Compact, URL-safe, signed, timestamped JSON. |
| `BadSignature`, `SignatureExpired` | Refusals; the second is a kind of the first. Neither quotes the value. |
| `derive_key(purpose)`, `derive_keys(purpose)` | A key of its own for `purpose` (HKDF-SHA256), from `SECRET_KEY` and then each fallback. |
| `constant_time_compare(a, b)` | Compare secrets without telling how close they came. |

A signature is HMAC-SHA256, compared in constant time, under a key
derived from `SECRET_KEY` for its salt, so a value signed for one purpose
never verifies for another. Signed values are readable by anyone: sign
ids, not secrets. To replace `SECRET_KEY`, put the old key first in
`SECRET_KEY_FALLBACKS` and the new one in `SECRET_KEY`; drop the old one
once what it signed has expired. A project without `SECRET_KEY` starts
locally, and fails the first time something signs. See
[ADR 0028](../adr/0028-one-secret-key-signs-and-derives.md).

## Encryption

Secrets the database must give back, such as TOTP secrets or a signing
secret shared with another service, are kept encrypted with
`domainkit.encryption.SecretBox` (it needs `cryptography`: the `mfa`
extra):

```python
from domainkit.encryption import SecretBox

box = SecretBox("billing.webhook_secret")  # a purpose of its own
sealed = box.encrypt(secret, associated=subscription_id.bytes)  # bytes to store
secret, current = box.decrypt(sealed, associated=subscription_id.bytes)
```

It is AES-256-GCM under a key derived from `SECRET_KEY` for the purpose,
with a random nonce per value. `associated` binds the value to its owner:
a value copied to another row does not decrypt. Values encrypted under a
key now in `SECRET_KEY_FALLBACKS` still decrypt, with `current` False:
encrypt them again then. Anything else raises `DecryptionFailed`, which
quotes nothing. See [ADR 0030](../adr/0030-encrypt-totp-secrets-under-a-derived-key.md).
