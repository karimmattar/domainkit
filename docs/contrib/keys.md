# Keys: encrypted fields under a KMS, and crypto-shredding

A card number, a national id, or an API secret stored in clear is read
by anyone who reads the database, a replica, a backup, or a dump. The
keys app stores such fields sealed instead. Each value is encrypted with
AES-256-GCM under a *data key*. The data keys are wrapped by a
*key-encryption key* (KEK) held in a key management service (KMS), such
as AWS KMS or HashiCorp Vault. Each sealed value is bound to its row. A
value about a person can be sealed under that person's own key, and
erasing the person destroys the key: the value can then never be read
again.

On this page you learn how to:

- mark a field `Encrypted`, and find rows by it with a blind index;
- choose a key service, and make data keys;
- rotate the KEK and the data keys;
- erase a person by destroying their key (crypto-shredding);
- plan backups so that an erasure reaches them.

The decisions are in
[ADR 0119](../adr/0119-wrap-data-keys-under-a-kms-key-through-a-port-and-name-the-key-in-every-sealed-value.md)
(keys and the envelope) and
[ADR 0120](../adr/0120-seal-encrypted-fields-in-the-orm-bound-to-their-row-and-shred-a-person-by-destroying-their-key.md)
(fields, blind indexes, crypto-shredding).

## Install

Install the extra, which brings `cryptography` for AES-GCM and the HTTP
client that the cloud providers call through:

```bash
pip install "domainkit[keys]"
```

```python
INSTALLED_DOMAINS = ["domainkit.contrib.keys"]

KEYS = {
    "PROVIDER": "aws-kms",
    "AWS_KMS": {
        "KEY_ID": "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
        "REGION": "eu-west-1",
    },
    "DATA_KEYS": {"2026-10": env.str("KEYS_DATA_KEY_2026_10")},  # keys generate
}
```

```bash
python manage.py makemigrations -m "keys"
python manage.py migrate
```

The app brings two tables, `domainkit_keys_subject_keys` and
`domainkit_keys_erasures`, in every database that hosts a domain. They
sit beside the rows that the keys seal (`domainkit.contrib.keys.tables`
exports them as `subject_keys` and `erasures`).

Without `KEYS`, the provider is `local`: a KEK derived from `SECRET_KEY`,
for development and tests.

## A first example

A card holder has a card number, sealed under the holder's own key and
found by a blind index, and an API secret, sealed under the data key:

```python
from typing import Annotated
from uuid import UUID

from domainkit.ddd import AggregateRoot, Encrypted, Personal


class Cardholder(AggregateRoot[UUID]):
    name: Annotated[str, Personal(of="id")]
    card_number: Annotated[str | None, Encrypted(subject="id", blind_index=True)] = None
    api_secret: Annotated[bytes | None, Encrypted()] = None
```

You save it as any aggregate. The aggregate holds the values; the
database holds sealed text:

```text
card_number       = dk1:s.94a8ddf7917142938ba9b705f026051e:E9mtnNa6STgsKJWq2wZCFpZ5iywaJ63mCa7GS-iHbePS1mTlnK8Sh29rssg
card_number_index = dev:1cc7664b3f28d55a200e3c8f0535413b
api_secret        = dk1:k.dev:e4USRz46AAu8rz8dfl5bPK6BisO_uzPv6NewWt0b2Q93HA
```

`dk1:s.<id>` names a subject's key; `dk1:k.dev` names the data key
`dev`. Find the holder by the card number, then erase the holder's key:

```python
from domainkit.contrib.keys import shred_subject
from domainkit.ddd import F

async with CardsUnitOfWork() as uow:
    found = await uow.cardholders.filter(F("card_number") == "4111111111111111")
    print(found[0])

print(await shred_subject(str(ann)))

async with CardsUnitOfWork() as uow:
    holder = await uow.cardholders.get(ann)
    print(holder.card_number, holder.api_secret)
```

```text
Cardholder(id=UUID('00000000-0000-4000-8000-00000000000a'), version=1, name='Ann', passport=None, card_number=<encrypted>, api_secret=<encrypted>)
1
None b's3cr3t'
```

An aggregate's `repr` shows `<encrypted>` for each encrypted value.
After the erasure, the card number reads as `None`. The API secret was
sealed under the data key, not the person's key, so it still reads.

## Encrypting a field

Mark a field `Encrypted`. It holds text or bytes, or a value object of
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

`table_for` stores each one in a text column (`EncryptedType`). The ORM
seals a value as its row is written, and opens it as the row loads. A
sealed value names the key that sealed it, and is bound to its table,
column, and primary key. Copied to another row or column, it raises
`DecryptionFailed` when loaded. A table that you write by hand uses
`*EncryptedColumns("number", subject="holder_id", blind_index=True)`.

| Argument | What it does |
| --- | --- |
| `subject` | Names the field that identifies the person the value is about. The value is sealed under that person's own key. A field also marked `Personal(of=...)` takes that marker's person. |
| `blind_index` | `True` adds a `<name>_index` column, so the field can be found by equality. |

A field sealed for a person must allow `None`: that is what it reads as
once the key is destroyed. Without `subject` or `Personal`, the value is
sealed under the current data key.

The row needs its primary key before it is written. So an aggregate with
encrypted fields cannot be numbered by the database.

## Querying encrypted fields

Equal values seal to different text, so the database can compare an
encrypted column with nothing but NULL. Queries (`domainkit.ddd.query`,
filters, the admin API) may ask `F("number").isnull()` or
`F("number") == None`. Text lookups, ranges, and ordering are refused
with `InvalidQuery`, in the in-memory repositories too.

With a blind index, `F("number") == "4111111111111111"` and
`F("number").in_([...])` match through `number_index`. That column holds
`<data key id>:<32 hex digits>`: an HMAC-SHA256 of the value, its table,
and its column, under a key derived from the data key. A lookup hashes
the value under every data key of the ring, so rows not yet moved to the
current key are found too. A raw SQL comparison of the column with a
value is refused when it runs.

!!! warning
    A blind index shows which rows hold equal values, and so how often
    each value occurs. To a person who holds the data keys, it also shows
    whether a guessed value is stored. A card number has about 10^9
    candidates per issuer prefix, few enough to try them all. Index only
    fields that you find by equality, normalize them first (remove spaces
    in the value object), and keep the data keys in the KMS.

## Writing encrypted fields

The ORM seals values as it writes each row, so change encrypted fields
through the aggregate. A repository's `update_rows` that sets an
encrypted field, or the field that names its subject, writes through the
ORM instead of one statement. So the value is sealed again for its new
subject, and its blind index follows (set to `None`, the index goes
too). An ORM `update()` of such a model that sets them is refused
(`InvalidQuery`). Core SQL on the table is not seen at all, and must seal
what it writes. A value that was not loaded (`load_only`, `defer`) is
read from its row when its subject changes, and sealed again.

**Only the column is encrypted.** A value that your code copies
elsewhere is stored there as it was copied, in clear: a domain event's
fields (and so the outbox's payloads, the audit trail's details, and
webhook bodies), a command's arguments kept by
[approvals](approvals.md), a response kept by
[idempotency](idempotency.md), a log line, an error message. Keep
encrypted values out of events and responses, or mark them `Personal`,
so that the audit trail keeps pseudonyms and erasure finds the copies.

## What the database, a dump, and a backup hold

Sealed text and blind indexes, never a value. `manage.py dumpdata` writes
them as stored. So a fixture holds no card number, and loads back only
where the same keys open it, into the same rows. `dumpdata --decrypt`
writes the values, as `{"plaintext": "4111..."}` (bytes in base64). It is
audited (`operator.dumpdata`, with `decrypted_tables`) and needs a named
operator where deployed. `loaddata` seals such values for the rows it
writes, under its database's current key. The subjects' keys are never
in a fixture.

## Data keys and the KEK

`DATA_KEYS` maps ids to data keys wrapped by the provider. The process
unwraps each one once, at startup, and keeps them in memory. A KMS that
refuses stops the startup. `CURRENT_KEY` seals new values. The others
are retired: they still open what they sealed.

`manage.py keys generate [--id ID]` asks the provider for a new data key,
and prints it wrapped, as a `DATA_KEYS` entry. Its id is today's date by
default. The wrapped key is safe to keep in configuration: only the KMS
opens it.

```bash
python manage.py keys generate --id 2026-10
```

```text
{"2026-10": "local:60d77f42:PtC16fTW_tAPeXYdmFoh77wwpi8pdUmw06fEQBMyMaHGmGJ2wrjFrCJBD6SRJvOVpvg6p20FGLVEIniI"}
Add it to KEYS['DATA_KEYS'] in every process, then set KEYS['CURRENT_KEY'] = '2026-10' and run 'manage.py keys reencrypt'.
```

Each wrapped key starts with its provider's prefix (`local:`,
`aws-kms:`, `vault:`, `gcp-kms:`, `azure-kv:`, `huawei-kms:`, `pkcs11:`,
or a custom provider's `name`). The settings refuse a key that another
provider wrapped.

Without `DATA_KEYS`, the local provider's ring has one development key,
`dev`, derived from `SECRET_KEY`, and from each of
`SECRET_KEY_FALLBACKS` to open what it sealed before a rotation. Once
`DATA_KEYS` is set, `dev` stays in the ring as a retired key (with the
local provider; `KEEP_DEVELOPMENT_KEY` says otherwise). So `keys.W001`
warns, and `keys reencrypt` moves what it sealed. Then set
`KEEP_DEVELOPMENT_KEY = False`.

### Providers

- `local`: the KEK is derived from `SECRET_KEY` (and each of
  `SECRET_KEY_FALLBACKS` still unwraps). A person who reads the settings
  opens every field, so staging and production refuse it (`keys.E001`)
  unless `ALLOW_LOCAL_KEK` accepts that.
- `aws-kms`: `GenerateDataKey`, `Decrypt`, and `ReEncrypt` on the key
  `AWS_KMS.KEY_ID`, each signed with SigV4 and carrying the encryption
  context `{"domainkit": "data-key"}`. Credentials come from the
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` variables (and
  `AWS_SESSION_TOKEN`), else a web identity (`AWS_ROLE_ARN` and
  `AWS_WEB_IDENTITY_TOKEN_FILE`, as EKS sets them), else the container's
  endpoint (ECS, EKS Pod Identity). As the AWS SDKs allow,
  `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` must be a path on
  `169.254.170.2`, and `AWS_CONTAINER_CREDENTIALS_FULL_URI` must use
  `https`, or `http` to a loopback address or an agent's
  (`169.254.170.2`, `169.254.170.23`, `fd00:ec2::23`): the pod's token is
  sent nowhere else. The role needs `kms:GenerateDataKey`,
  `kms:Decrypt`, and `kms:ReEncrypt*` on the key, best with the condition
  `kms:EncryptionContext:domainkit = data-key`.
- `vault`: the transit engine's `datakey/plaintext`, `decrypt`, and
  `rewrap` on `VAULT.KEY`. The token needs `update` on those three paths.
  Staging and production refuse `VAULT.URL` over plain `http://`
  (`keys.E002`): the token and every data key would cross the network in
  clear.
- `gcp-kms`: Cloud KMS makes no data keys. Each one is 32 random bytes
  made here, wrapped by `encrypt` on `GCP_KMS.KEY_NAME` (its primary
  version) and unwrapped by `decrypt`, both with `domainkit.keys` as
  additional authenticated data. `rewrap` unwraps and wraps under the
  version that is now primary. The caller is the service account in
  `CREDENTIALS_FILE`, or the metadata server's, with
  `roles/cloudkms.cryptoKeyEncrypterDecrypter`.
- `azure-key-vault`: `wrapkey` and `unwrapkey` on
  `AZURE_KEY_VAULT.KEY_NAME` (`RSA-OAEP-256`, or `A256KW` for a Managed
  HSM's AES key). The key's current version is read every 5 minutes and
  named in each wrapped key, which unwraps under its own version after a
  rotation. The caller is an Entra application (`TENANT_ID`,
  `CLIENT_ID`, `CLIENT_SECRET`) or the managed identity (a user-assigned
  one by `CLIENT_ID`), with the `wrapKey` and `unwrapKey` permissions.
- `huawei-kms`: `create-datakey`, `decrypt-datakey` (its digest of the
  key checked), and `encrypt-datakey` on `HUAWEI_KMS.KEY_ID`, signed with
  the access key (`SDK-HMAC-SHA256`).
- `pkcs11`: an AES-256 key inside an HSM, found by `PKCS11.KEY_LABEL` on
  the token `TOKEN_LABEL`, encrypts each data key with AES-GCM (the label
  as additional data). The key never leaves the HSM. To rotate, make a
  new key in the HSM, name it `KEY_LABEL` and the old one in
  `RETIRED_LABELS`, then run `keys rewrap`. The library is called on a
  worker thread, one session at a time (the `pkcs11` extra; tested
  against SoftHSM).
- `custom`: a `domainkit.keys.KeyProvider` of your own, `CUSTOM.CLASS`,
  made by its `from_options(CUSTOM.OPTIONS, timeout=...)` (by default,
  the class called with the options). Its `name` starts the keys it
  wraps, and `DATA_KEYS` must start with it.

A project can also pass any `KeyProvider` to its own `Keyring`.
`domainkit.keys.aws.AwsKmsKeyProvider` also takes a `credentials`
callable (an instance profile, for example).

### Rotating

**The KEK** rotates in its service: AWS's automatic yearly rotation, or
`vault write -f transit/keys/<key>/rotate`. Then `manage.py keys rewrap`
prints `DATA_KEYS` wrapped under the KEK's newest version; deploy them.
The data keys themselves do not change, so nothing is encrypted again.
In Vault, raise the key's `min_decryption_version` once no data key is
wrapped under an older version.

**A data key** rotates in four steps:

1. `keys generate --id 2027-01`, and add the key to `DATA_KEYS` in every
   process.
2. Make it `CURRENT_KEY`. `check` warns (`keys.W001`) while a retired
   key stays in `DATA_KEYS`.
3. `manage.py keys reencrypt` seals again every value under a retired
   key, makes again the blind indexes made under one, and wraps every
   subject's key again.
4. `manage.py keys status --check` passes once a retired key seals
   nothing. Then remove the retired key.

`reencrypt` works in batches (`--batch-size`, 500 rows a transaction)
that each commit on their own, so it can stop and start again. A batch's
rows are locked as they are read (on SQLite, the database takes its
write lock), and written back in one statement per kind of change, so a
write to them waits for the batch. A value that does not open is counted
and left alone, and the command then fails. The run is audited
(`operator.keys_reencrypt`).

`keys status` counts, per column, what each key seals:

```bash
python manage.py keys status
```

```text
Current key: dev; retired: none.
default cards_cardholders.passport: dev: 0; subjects' keys: 0
default cards_cardholders.card_number: dev: 0; subjects' keys: 1
default cards_cardholders.api_secret: dev: 1; subjects' keys: 0
default cards_payments.pan: dev: 0; subjects' keys: 0
default cards_payments.note: dev: 0; subjects' keys: 0
default subjects' keys wrapped under none
```

A line also says `under no key held: N` for values that no key of the
ring opens, and `indexes to remake: N` for blind indexes made under a
retired key.

## Erasing a person: crypto-shredding

A subject's key is 32 random bytes. It is made when the subject's first
value is sealed, and stored in `domainkit_keys_subject_keys` (wrapped
under the current data key), in the same database and transaction as the
row. When a query loads rows, it reads the keys of their subjects
together, in one statement (500 keys a statement), and its transaction
holds them from then on. A streamed query (`yield_per`) reads them row by
row.

The app registers on `erase_subject`. So `manage.py privacy erase
--subject <id>` ([privacy.md](../privacy.md)) erases the person's
`Personal` fields, then destroys their keys (`shred_subject`), and
records how many (`privacy.copies_erased`, `{"subject_keys": 1}`). From
then on, every value sealed under those keys reads as `None`: in the
database, its replicas, and every backup or dump taken after the
erasure.

`shred_subject` visits every database that hosts domains, and in each one
its own schema and every [tenant](../learning.md#words) schema
([tenancy](../tenancy.md)). A tenant schema that does not have the keys
tables yet is skipped. `keys status` and `keys reencrypt` visit the same
schemas.

An erasure runs in transactions of its own, so it cannot see a
transaction that makes the person's first key at the same moment. So the
erasure leaves a tombstone in `domainkit_keys_erasures` (the subject and
when, kept a day), under the subject's lock. A transaction that made keys
checks, as it commits and under the same locks, whether their subjects
were erased after the keys were made. If so, it destroys the keys, with
the blind indexes that it wrote for them. A transaction that sealed a
value with a key the subject already had checks too: if an erasure
destroyed that key in the meantime, it clears the blind indexes that it
wrote as it commits, because the erasure could not see them.

What remains:

- **The ciphertext**, where it was: nothing can open it.
- **Values about the person sealed under a data key**: fields with no
  `subject` (and not `Personal`), or whose subject field was `None` when
  they were written. Erase or delete those through the domain.
- **Blind indexes** of the person's values are cleared in the rows whose
  subject column names them. A blind index of a field without a subject,
  or in another table, stays, and still tells a person who holds the data
  keys whether a guessed value is there.
- **The audit trail's pseudonyms**, which `erase_subject` makes
  unlinkable in the same erasure by forgetting their own keys
  ([audit](audit.md)).
- **Evidence**: its keys hold an HMAC of the subject under
  `EVIDENCE.SUBJECT_KEY`, not the person's key. Keep personal data out of
  evidence ([evidence](evidence.md)).

A transaction that read a person's key before the erasure may still
write with it. What it writes reads as `None` afterwards.

### Backups taken before an erasure

A subject's key is stored in the database where it seals values, wrapped
under a data key that the service keeps (DOCS-14). A backup, a
point-in-time recovery, or a dump taken *before* the erasure holds that
key. Restored, or read with the service's data keys, it opens the
person's values again. Erasure reaches such a copy only once it has
expired, or once it has been restored and the erasure applied to it
again. So:

- **Limit how long backups are kept**, and name that retention in your
  records of processing. Until a backup taken before an erasure expires,
  the person's data is in it. If you need a shorter limit for people's
  keys than for the rest of the data, put the tables with
  `Encrypted(subject=...)` fields, and their keys, in a database of their
  own (`DATABASE_ROUTING`; the keys app keeps a subject's keys in each
  database that hosts domains, beside the rows they seal), and keep that
  database's backups for less time.
- **Apply recorded erasures again after every restore.** Each erasure is
  recorded in the audit trail (`privacy.erase`, its subject as the
  target). Before restoring, list those made since the backup's time with
  `manage.py audit log --action privacy.erase --limit N`, and keep the
  list outside the database that you restore. Once it is restored, run
  `manage.py privacy erase --subject <id>` for each one again. Erasing is
  idempotent. Keep the audit trail in a database of its own
  (`AUDIT.DATABASE`), with its own backups, so the list survives the
  restore of the others
  ([operations.md](../operations.md#backups-and-restores)).

## From Python

The commands call functions that you can call too:

| Name | What it does |
| --- | --- |
| `ring_for(config)` | The `Keyring` that a `KeysSettings` describes, its subjects' keys in their table. |
| `key_status(ring)` | Counts, in every hosting database and schema, what each data key seals: a `KeyStatus`. |
| `reencrypt(ring, batch_size=500)` | Moves everything under a retired key to the current one: a `Reencrypted`. |
| `shred_subject(subject)` | Destroys a subject's keys everywhere; returns how many there were. |

```python
from domainkit.apps import registry
from domainkit.contrib.keys import KeysSettings, key_status, reencrypt, ring_for

ring = ring_for(registry.settings(KeysSettings))
await ring.load()
status = await key_status(ring)
print(status.current, status.retired, status.on_retired, status.unreadable)
done = await reencrypt(ring, batch_size=200)
print(done.values, done.indexes, done.subject_keys, done.failed)
```

A `KeyStatus` holds `current`, `retired`, `columns` (one `ColumnStatus`
per encrypted column: `database`, `table`, `column`, `by_key`,
`subjects`, `other`, `stale_indexes`), and `subject_keys` (by database,
then by wrapping key). Its `on_retired` counts what is still under a
retired key, and `unreadable` the values that no key opens. A
`Reencrypted` counts the `values`, `indexes`, and `subject_keys` it
moved, and the values that `failed` to open.

`TableSubjectKeyStore` reads and adds subjects' keys in the connection's
own transaction. Two writers that make a subject's first key at once both
keep the one stored first. If another writer's key is erased each time
between its insert and the read, `add` gives up after 3 attempts with
`KeyStoreConflict`.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `KEYS` key | Default | Meaning |
| --- | --- | --- |
| `PROVIDER` | `"local"` | Where the KEK is: `"local"` (derived from `SECRET_KEY`), `"aws-kms"` (`AWS_KMS`), `"vault"` (`VAULT`), `"gcp-kms"` (`GCP_KMS`), `"azure-key-vault"` (`AZURE_KEY_VAULT`), `"huawei-kms"` (`HUAWEI_KMS`), `"pkcs11"` (`PKCS11`), or `"custom"` (`CUSTOM`). |
| `ALLOW_LOCAL_KEK` | `False` | Let staging and production use the local KEK, which `keys.E001` refuses otherwise. |
| `AWS_KMS` | `None` | The KMS key, below; required with `"aws-kms"`. |
| `VAULT` | `None` | The transit key, below; required with `"vault"`. |
| `GCP_KMS` | `None` | The Cloud KMS key, below; required with `"gcp-kms"`. |
| `AZURE_KEY_VAULT` | `None` | The Key Vault key, below; required with `"azure-key-vault"`. |
| `HUAWEI_KMS` | `None` | The master key, below; required with `"huawei-kms"`. |
| `PKCS11` | `None` | The HSM and its key, below; required with `"pkcs11"`. |
| `CUSTOM` | `None` | Your own provider, below; required with `"custom"`. |
| `DATA_KEYS` | `{}` | Data keys by id (1 to 32 letters, digits, `_`, `-`), each wrapped by `PROVIDER`; at most 16. Required with a key service. |
| `CURRENT_KEY` | `None` | The data key that new values are sealed under; may be left out when `DATA_KEYS` holds one key. |
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

| `KEYS["GCP_KMS"]` key | Default | Meaning |
| --- | --- | --- |
| `KEY_NAME` | required | `projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>`. |
| `CREDENTIALS_FILE` | `None` | A service account's key file; `None`: the metadata server's account. |

| `KEYS["AZURE_KEY_VAULT"]` key | Default | Meaning |
| --- | --- | --- |
| `VAULT_URL` | required | `https://<vault>.vault.azure.net`, or a Managed HSM's URL. |
| `KEY_NAME` | required | The key's name: letters, digits, `-`; at most 127 characters. |
| `ALGORITHM` | `"RSA-OAEP-256"` | `RSA-OAEP-256`, `RSA-OAEP`, or `A256KW`. |
| `TENANT_ID`, `CLIENT_SECRET` | `None` | Together, with `CLIENT_ID`: an Entra application. |
| `CLIENT_ID` | `None` | Alone: a user-assigned managed identity; none: the system-assigned one. |

| `KEYS["HUAWEI_KMS"]` key | Default | Meaning |
| --- | --- | --- |
| `KEY_ID` | required | The master key's id; at most 64 characters. |
| `REGION` | required | Such as `ap-southeast-3`. |
| `PROJECT_ID` | required | The project's id (hex); at most 64 characters. |
| `ACCESS_KEY`, `SECRET_KEY` | required | The access key that signs the calls. |
| `ENDPOINT` | `None` | Another HTTPS endpoint; `None`: `https://kms.<REGION>.myhuaweicloud.com`. |

| `KEYS["PKCS11"]` key | Default | Meaning |
| --- | --- | --- |
| `LIBRARY` | required | The vendor's PKCS#11 library. |
| `TOKEN_LABEL` | required | The token's label; at most 32 characters. |
| `PIN` | required | The user PIN; hidden in reprs. |
| `KEY_LABEL` | required | The AES-256 key that new data keys are wrapped under; at most 64 characters, without `:`. |
| `RETIRED_LABELS` | `()` | Older keys that still unwrap, until `keys rewrap` moves their data keys. |

| `KEYS["CUSTOM"]` key | Default | Meaning |
| --- | --- | --- |
| `CLASS` | required | Dotted path to a `KeyProvider`. |
| `OPTIONS` | `{}` | What its `from_options` reads. |

The settings refuse, when they are read: a provider without its
settings; a key service without `DATA_KEYS`; a data key that another
provider wrapped; a `CURRENT_KEY` that is not in `DATA_KEYS`; several
data keys and no `CURRENT_KEY`; and, for Azure, a `TENANT_ID` without a
`CLIENT_SECRET` and `CLIENT_ID`.

## Checks and commands

| Id | When | What to do |
| --- | --- | --- |
| `keys.E001` | Staging or production with the local KEK and no `ALLOW_LOCAL_KEK`. | Use a key service. |
| `keys.E002` | Staging or production with `VAULT.URL` over plain `http://`. | Serve Vault over TLS and give an `https://` URL. |
| `keys.W001` | `DATA_KEYS` keeps retired keys. | Run `keys reencrypt`, then `keys status --check`; remove the keys once nothing uses them. |

| Action | What it does |
| --- | --- |
| `keys generate [--id ID]` | Print a new data key, wrapped, as a `DATA_KEYS` entry. |
| `keys rewrap` | Print `DATA_KEYS` wrapped under the KEK's newest version. |
| `keys status [--check]` | Count what each key seals; `--check` fails while a retired key seals anything. |
| `keys reencrypt [--batch-size N]` | Seal everything under a retired key again; audited. |

Every action takes `--operator` ([operators](../reference/commands.md#operators)).
A key service's refusal is reported as the command's error.

## On PostgreSQL and SQLite

On PostgreSQL, an erasure and a commit that made keys take each
subject's advisory lock (a stable 64-bit hash of the subject), in one
order, so two transactions never wait for each other. Keys and
tombstones are dated by the database's clock (`clock_timestamp()`), so a
host whose clock runs ahead never forgets a tombstone too early
(DATA-22). Tenant schemas are visited one by one, with `SET LOCAL
search_path`.

SQLite runs one writer at a time and needs no subject lock; the
process's clock dates keys and tombstones. `keys reencrypt` takes the
database's write lock for each batch.

## Common mistakes

- **Copying an encrypted value into an event.** The event, and the
  outbox, the audit trail, and webhooks after it, hold it in clear. Leave
  it out, or mark it `Personal`.
- **A subject field that may not be `None`.** After an erasure the value
  must read as `None`. Write `str | None`.
- **Keeping backups longer than you can explain.** An erasure does not
  reach a backup taken before it. Limit retention, and apply erasures
  again after a restore.
- **Removing a retired key before `keys status --check` passes.** Values
  sealed under it can then never be opened.
- **Using the local provider in production.** `SECRET_KEY` then opens
  every field. Use a key service.

## See also

- [Privacy](../privacy.md): erasure and export.
- [Audit](audit.md): pseudonyms and the audit trail.
- [Operations: backups and restores](../operations.md#backups-and-restores).
- [Tenancy](../tenancy.md): tenant schemas.
- [Checks](../reference/checks.md): every check id.
