# Keys: encrypted fields under a KMS, and crypto-shredding

A card number, a national id, or an API secret stored in clear is read by
anyone who reads the database, a replica, a backup, or a dump.
`domainkit.contrib.keys` stores such fields sealed instead: encrypted with
AES-256-GCM under *data keys* that are themselves wrapped by a
*key-encryption key* (KEK) held in a key management service (AWS KMS or
HashiCorp Vault), and bound to the row they belong to. A field about a
person can be sealed under that person's own key, which erasing them
destroys: every copy of what it sealed, backups included, is then
unreadable. The decisions are in
[ADR 0119](../adr/0119-wrap-data-keys-under-a-kms-key-through-a-port-and-name-the-key-in-every-sealed-value.md)
(keys and the envelope) and
[ADR 0120](../adr/0120-seal-encrypted-fields-in-the-orm-bound-to-their-row-and-shred-a-person-by-destroying-their-key.md)
(fields, blind indexes, crypto-shredding).

```python
INSTALLED_DOMAINS = [..., "domainkit.contrib.keys"]

KEYS = {
    "PROVIDER": "aws-kms",
    "AWS_KMS": {
        "KEY_ID": "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-...",
        "REGION": "eu-west-1",
    },
    "DATA_KEYS": {"2026-10": env.str("KEYS_DATA_KEY_2026_10")},  # keys generate
}
```

The cloud providers call through `domainkit.http.client`
(`pip install "domainkit[httpclient]"`); AES-GCM needs `cryptography`
(`domainkit[mfa]`).

## Encrypting a field

Mark it `Encrypted`; the field holds text or bytes, or a value object of
one:

```python
from typing import Annotated

from domainkit.ddd import AggregateRoot, Encrypted, Personal


class Card(AggregateRoot[CardId]):
    holder_id: CustomerId
    number: Annotated[str | None, Encrypted(subject="holder_id", blind_index=True)]
    holder_name: Annotated[str | None, Personal(of="holder_id"), Encrypted()] = None
    issuer_secret: Annotated[bytes | None, Encrypted()] = None
```

`table_for` stores each in a text column (`EncryptedType`), and the ORM
seals a value as its row is written and opens it as the row loads: the
aggregate holds `"4111111111111111"`, the database
`dk1:s.6f1c...:VQ7u...`. A sealed value names the key that sealed it and
is bound to its table, column, and primary key: copied to another row or
column, it raises `DecryptionFailed` when loaded. A hand-written table
uses `*EncryptedColumns("number", subject="holder_id", blind_index=True)`.

- `subject` names the field that identifies the person the value is
  about; a field also marked `Personal(of=...)` takes that marker's
  person. The value is sealed under the person's own key (below), so the
  field must allow None: that is what it reads as once the key is
  destroyed. Without either, the value is sealed under the current data
  key.
- The row needs its primary key before it is written: an aggregate with
  encrypted fields cannot be numbered by the database.
- `blind_index=True` adds a `<name>_index` column, so the field can be
  looked up by equality (below).

## Querying encrypted fields

Equal values seal to different text, so the database can compare an
encrypted column with nothing but NULL. Queries (`domainkit.ddd.query`,
filters, the admin API) may ask `F("number").isnull()` or
`F("number") == None`; text lookups, ranges, and ordering are refused
(`InvalidQuery`), in the in-memory repositories too.

With a blind index, `F("number") == "4111111111111111"` and
`F("number").in_([...])` match through `number_index`, which holds
`<data key id>:<32 hex digits>`: an HMAC-SHA256 of the value, its table and
its column, under a key derived from the data key. A lookup hashes the
value under every data key of the ring, so rows not yet moved to the
current key are found. A raw SQL comparison of the column with a value is
refused when it runs.

**What a blind index leaks.** Which rows hold equal values, and so how
often each value occurs; and, to whoever holds the data keys, whether a
guessed value is stored: a PAN has about 10^9 candidates per issuer
prefix, few enough to try them all. Index only fields that are looked up
by equality, normalize them first (strip spaces in the value object), and
keep the data keys in the KMS.

## Writing encrypted fields

The ORM seals values as it writes each row, so change encrypted fields
through the aggregate. A repository's `update_rows` that sets an
encrypted field, or the field naming its subject, writes through the ORM
instead of one statement, so the value is sealed again for its new
subject and its blind index follows (set to None, the index goes too).
An ORM `update()` of such a model that sets them is refused
(`InvalidQuery`); Core SQL on the table is not seen at all, and must
seal what it writes. A value not loaded (`load_only`, `defer`) is read
from its row when its subject changes, and sealed again.

**What is encrypted is the column, and nothing else.** A value the code
copies elsewhere is stored there as it was copied, in clear: a domain
event's fields (and so the outbox's payloads, the audit trail's details,
and webhook bodies), a command's arguments kept by
`domainkit.contrib.approvals`, a response kept by
`domainkit.contrib.idempotency`, a log line, an error message. Keep
encrypted values out of events and responses, or mark them `Personal` so
the audit trail keeps pseudonyms and erasure finds the copies. An
aggregate's `repr` shows `<encrypted>` for each encrypted value.

## What the database, a dump, and a backup hold

Sealed text and blind indexes, never a value. `manage.py dumpdata` writes
them as stored, so a fixture holds no card number and loads back only
where the same keys open it, into the same rows. `dumpdata --decrypt`
writes the values, as `{"plaintext": "4111..."}` (bytes in base64); it is
audited (`operator.dumpdata`, with `decrypted_tables`) and needs a named
operator where deployed. `loaddata` seals such values for the rows it
writes, under its database's current key. The subjects' keys are never in
a fixture.

## Data keys and the KEK

`DATA_KEYS` maps ids to data keys wrapped by the provider; the process
unwraps each once, at startup (a KMS that refuses stops it), and keeps
them in memory. `CURRENT_KEY` seals new values; the others are retired,
and still open what they sealed.

`manage.py keys generate [--id ID]` asks the provider for a new data key
and prints it wrapped, as a `DATA_KEYS` entry (its id defaults to the
date). The wrapped key is safe to keep in configuration: only the KMS
opens it. Without `DATA_KEYS`, the local provider's ring has one
development key, `dev`, derived from `SECRET_KEY`, and from each of
`SECRET_KEY_FALLBACKS` to open what it sealed before a rotation. Once
`DATA_KEYS` is set, `dev` stays in the ring as a retired key (with the
local provider; `KEEP_DEVELOPMENT_KEY` says otherwise), so `keys.W001`
warns and `keys reencrypt` moves what it sealed; then set
`KEEP_DEVELOPMENT_KEY = False`.

### Providers

- `local`: the KEK is derived from `SECRET_KEY` (and each of
  `SECRET_KEY_FALLBACKS` still unwraps). Whoever reads the settings opens
  every field, so staging and production refuse it (`keys.E001`) unless
  `ALLOW_LOCAL_KEK` accepts that.
- `aws-kms`: `GenerateDataKey`, `Decrypt`, and `ReEncrypt` on the key
  `AWS_KMS.KEY_ID`, each signed with SigV4 and carrying the encryption
  context `{"domainkit": "data-key"}`. Credentials come from the
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` variables (and
  `AWS_SESSION_TOKEN`), else a web identity (`AWS_ROLE_ARN` and
  `AWS_WEB_IDENTITY_TOKEN_FILE`, as EKS sets them), else the container's
  endpoint (ECS, EKS Pod Identity). As the AWS SDKs' container credential
  provider allows, `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` must be a path
  on `169.254.170.2`, and `AWS_CONTAINER_CREDENTIALS_FULL_URI` must use
  `https`, or `http` to a loopback address or an agent's
  (`169.254.170.2`, `169.254.170.23`, `fd00:ec2::23`): the pod's token is
  sent nowhere else. The role needs `kms:GenerateDataKey`,
  `kms:Decrypt`, and `kms:ReEncrypt*` on the key, best with the condition
  `kms:EncryptionContext:domainkit = data-key`.
- `vault`: the transit engine's `datakey/plaintext`, `decrypt`, and
  `rewrap` on `VAULT.KEY`; the token needs `update` on those three paths.
  Staging and production refuse `VAULT.URL` over plain `http://`
  (`keys.E002`): the token and every data key would cross in clear.

A project can pass any `domainkit.keys.KeyProvider` to its own
`Keyring`; `domainkit.keys.aws.AwsKmsKeyProvider` also takes a
`credentials` callable (an instance profile, say).

### Rotating

- **The KEK** rotates in its service (AWS's automatic yearly rotation, or
  `vault write -f transit/keys/<key>/rotate`). `manage.py keys rewrap`
  then prints `DATA_KEYS` wrapped under its newest version; deploy them.
  The data keys themselves do not change, so nothing is re-encrypted.
  In Vault, raise the key's `min_decryption_version` once no data key is
  wrapped under an older one.
- **A data key**: `keys generate --id 2027-01`, add it to `DATA_KEYS` in
  every process, then make it `CURRENT_KEY`. `check` warns (`keys.W001`)
  while a retired key stays in `DATA_KEYS`. `manage.py keys reencrypt`
  seals again every value under a retired key, remakes blind indexes
  made under one, and wraps every subject's key again, in batches
  (`--batch-size`, 500 rows a transaction) that each commit on their own;
  a row changed meanwhile is left for the next run, and a value that does
  not open is counted and left alone (the command then fails). It is
  audited (`operator.keys_reencrypt`). `manage.py keys status` counts, per
  column, what each key seals; `--check` fails while a retired key still
  seals anything. Then remove the retired key.

## Erasing a person: crypto-shredding

A subject's key is 32 random bytes, made when their first value is
sealed, stored in `domainkit_keys_subject_keys` (wrapped under the current
data key) in the same database and transaction as the row. The app
registers on `erase_subject`, so `manage.py privacy erase --subject <id>`
([privacy.md](../privacy.md)) erases the person's `Personal` fields, then
destroys their keys in every database that hosts domains
(`shred_subject`), and records how many (`privacy.copies_erased`,
`{"subject_keys": 1}`). From then on, every value sealed under those keys
reads as None, in the database and in every backup or dump of it.

An erasure runs in transactions of its own, so a transaction making the
person's first key at the same time is not seen. The erasure therefore
leaves a tombstone, `domainkit_keys_erasures` (the subject and when, kept
a day), under the subject's lock; a transaction that made keys checks, as
it commits and under the same locks, whether their subjects were erased
after the keys were made, and if so destroys them, with the blind indexes
it wrote for them.

What remains:

- **The ciphertext**, where it was: nothing can open it.
- **Values about the person sealed under a data key**: fields with no
  `subject` (and not `Personal`), or whose subject field was None when
  they were written. Erase or delete those through the domain.
- **Blind indexes** of the person's values are cleared in the rows whose
  subject column names them; a blind index of a field without a subject,
  or in another table, stays, and still tells whoever holds the data keys
  whether a guessed value is there.
- **The audit trail's pseudonyms**, which `erase_subject` makes
  unlinkable in the same erasure by forgetting their own keys
  ([audit](audit.md)).
- **Evidence**: its keys hold an HMAC of the subject under
  `EVIDENCE.SUBJECT_KEY`, not the person's key; keep personal data out of
  evidence ([evidence](evidence.md)).

A transaction that read a person's key before the erasure may still write
with it; what it writes reads as None afterwards.

## Settings

| `KEYS` key | Default | Meaning |
| --- | --- | --- |
| `PROVIDER` | `"local"` | Where the KEK is: `"local"` (derived from `SECRET_KEY`), `"aws-kms"` (`AWS_KMS`), or `"vault"` (`VAULT`). |
| `ALLOW_LOCAL_KEK` | `False` | Let staging and production use the local KEK, which `keys.E001` refuses otherwise. |
| `AWS_KMS` | `None` | The KMS key, below; required with `"aws-kms"`. |
| `VAULT` | `None` | The transit key, below; required with `"vault"`. |
| `DATA_KEYS` | `{}` | Data keys by id (1 to 32 letters, digits, `_`, `-`), each wrapped by `PROVIDER`; at most 16. Required with a key service. |
| `CURRENT_KEY` | `None` | The data key new values are sealed under; may be left out when `DATA_KEYS` holds one key. |
| `KEEP_DEVELOPMENT_KEY` | `None` | Keep `dev` beside `DATA_KEYS`, retired, so what it sealed still opens and `keys reencrypt` moves it: `None` keeps it with the local provider only. |
| `TIMEOUT` | `5.0` | Seconds each call to the key service may take, retries included; at most 60. |

| `KEYS["AWS_KMS"]` key | Default | Meaning |
| --- | --- | --- |
| `KEY_ID` | required | The KMS key's ARN, id, or `alias/<name>`; may not be empty, at most 2048 characters. |
| `REGION` | required | Its region, such as `eu-west-1`. |
| `ENDPOINT` | `None` | Another HTTPS endpoint: a VPC endpoint, or FIPS (`https://kms-fips.<region>.amazonaws.com`). |

| `KEYS["VAULT"]` key | Default | Meaning |
| --- | --- | --- |
| `URL` | required | Vault's address, such as `https://vault.internal:8200`. |
| `KEY` | required | The transit key's name; may not be empty, at most 128 characters. |
| `TOKEN` | required | A token allowed `update` on `datakey`, `decrypt`, and `rewrap` of the key; may not be empty. |
| `MOUNT` | `"transit"` | Where the transit engine is mounted. |
| `NAMESPACE` | `None` | Vault Enterprise's namespace, sent as `X-Vault-Namespace`; may not be empty, at most 255 characters. |

## Checks and commands

- `keys.E001` (error, staging and production): the local KEK without
  `ALLOW_LOCAL_KEK`.
- `keys.E002` (error, staging and production): `VAULT.URL` over plain
  `http://`.
- `keys.W001` (warning): `DATA_KEYS` keeps retired keys; `keys status
  --check` fails while values, indexes, or subjects' keys still use one.
- `manage.py keys generate | rewrap | status [--check] | reencrypt
  [--batch-size N]` (see above).
