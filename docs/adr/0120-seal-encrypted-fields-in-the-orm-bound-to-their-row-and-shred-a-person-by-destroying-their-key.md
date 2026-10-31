# 0120. Seal encrypted fields in the ORM, bound to their row, and shred a person by destroying their key

- Status: Accepted; amended 2026-10-31 (at the end)
- Date: 2026-10-31

## Context

A card number (PAN) stored in clear is readable by anyone who reads the
database, a replica, a backup, or a dump: PCI DSS 3.5 forbids it, and the
fintech audit asks for field encryption under a KMS (FIN-23) and for
crypto-shredding (FIN-24). `ddd.fields.Sensitive` keeps a value out of
schemas but stores it in clear.

Three things decide the design: where values are sealed, how one row's
value is kept from being moved to another, and how a person's values are
made unrecoverable when they are erased.

**Where to seal.** A SQLAlchemy `TypeDecorator` sees one value, never its
row: it cannot bind a value to its row's key, so a value copied to
another row would decrypt there (an attacker who can write the database
swaps two customers' card numbers, or a support script copies one by
mistake). A PoC (`orm_poc.py`, SQLite and PostgreSQL 16 through
aiosqlite and asyncpg) sealed in the mapper's `before_insert` and
`before_update` events, which see the object and its primary key and run
before any statement, and opened in the `load` and `refresh` events,
which see the loaded row. It covered inserts batched by the ORM, an
autoflush through a query, an update, `refresh` after `expire`, `get`
with `populate_existing`, and SQL run on the session's own connection
from inside the events (to read a person's key), all on both databases.
The column type then stays trivial: it passes through text already
sealed (`Ciphertext`) and refuses anything else, so a query that compares
the column with a value fails loudly instead of matching nothing.

**What a value costs**, measured: sealing a 16-digit PAN 7 µs, opening it
17 µs, a blind index 8 µs; a sealed PAN is 67 characters.

**Lookups.** Sealed values differ for equal plaintexts, so equality needs
something deterministic. A *blind index*, an HMAC of the value under a
key derived from the data key (HKDF, its own purpose), is the usual
answer. It leaks, by design: which rows hold equal values (and so how
often each value occurs), and, to whoever holds the data keys, whether a
guessed value is present. A PAN has little entropy (a known 6-digit BIN,
a Luhn digit: about 10^9 candidates per BIN), so the index of a PAN is
only as safe as the keys. It is opt-in, per field.

**Erasure.** Deleting or overwriting a value leaves its copies in
backups, replicas and dumps. Destroying the only key that opens them
erases those too (crypto-shredding), if each person has their own key.
A key per person derived from a master key could be derived again; the
key must be random and stored, wrapped, in one place.

## Decision

- **`Encrypted`** (`domainkit.ddd`), `Annotated` metadata on a field of
  text or bytes (or a value object of one): `table_for` stores it in an
  `EncryptedType` column (text), and `map_imperatively` seals and opens
  it in the mapper events above. A value is sealed under the ring's
  current data key (ADR 0119) for the context *table, column, primary
  key*: copied to another row or column it raises `DecryptionFailed` on
  load. Only changed values are sealed again; a change of the primary key
  or of the subject seals again too. The attribute holds the ciphertext
  only while its statement runs, and the value again after it, or after a
  failed flush. A row needs its key before it is written (an encrypted
  aggregate cannot be numbered by the database).
- **Queries**: an encrypted field is neither text nor orderable
  (`FieldInfo`), so the in-memory and SQL repositories alike refuse text
  lookups, ranges and ordering; `isnull` and `== None` read the column.
  With `Encrypted(blind_index=True)`, `==` and `in_` go through the
  `<name>_index` column (`<key id>:<HMAC-SHA256, 128 bits>`), matching
  the index under every data key of the ring, so rows not yet moved to the
  current key are found.
- **Subjects' keys.** `Encrypted(subject="customer_id")`, or a field also
  marked `Personal(of=...)`, is sealed under the key of the person that
  field names: 32 random bytes, made on first use, stored in
  `domainkit_keys_subject_keys` (one per subject, wrapped under the
  current data key, beside the rows it seals, in every database that hosts
  domains) and read on the writer's own connection, in its transaction.
  A transaction keeps the keys it read until it ends, never longer, so a
  key destroyed elsewhere is never used by a transaction that starts
  afterwards. Such a field must allow None: once its key is gone it reads
  as None. `domainkit.contrib.keys` registers on `erase_subject`, which
  privacy's erasure calls after erasing the person's aggregates: it
  deletes their keys in every hosting database and clears the blind
  indexes of their sealed values (`shred_subject`).
- **What remains after shredding**, documented on the page: the
  ciphertext, which nothing can open; values of the person sealed under a
  data key instead (fields with no subject, or whose subject was None);
  blind indexes of fields whose subject column does not name the person;
  the audit trail's pseudonyms, whose keys `erase_subject` forgets in the
  same erasure (ADR 0041); and evidence keys, whose subject digests are
  keyed by `EVIDENCE.SUBJECT_KEY`, not by the person.
- **Fixtures** see sealed text: `dumpdata` writes it as stored, so a dump
  holds no card number, and loads back only where the same keys open it,
  into the same rows. `dumpdata --decrypt` writes `{"plaintext": ...}`,
  audited and, where deployed, with a named operator; `loaddata` seals
  such values for the rows it writes. Subjects' keys are never in a
  fixture: a copy would outlive the erasure.
- **`manage.py keys reencrypt`** moves values sealed under a retired data
  key, blind indexes made under one, and subjects' keys wrapped under one,
  to the current key, in batches of `--batch-size` rows, each committed on
  its own, each update naming the value it read (a row changed meanwhile
  is left for the next run). Values sealed under subjects' keys are not
  touched: rewrapping the key moves them. `keys status` counts what each
  key seals; `--check` fails while a retired key still does.
- **Evidence's `SUBJECT_KEY` becomes a ring**: `SUBJECT_KEYS` by id and
  `SUBJECT_KEY_ID` for the current one (`SUBJECT_KEY` keeps the id
  `default`). New records' keys use the current key; `for_subject` scans
  under every key of the ring; `get` and `verify` never read the digest,
  so receipts keep proving their records across a rotation.

## Consequences

- A database dump, a replica or a backup shows no encrypted value; a
  person erased can no longer be read in any of them.
- Encrypted columns are sealed only through the ORM (or fixtures, or
  `seal_row`): hand-written SQL that writes one must seal it. Columns are
  text whatever the field's type; numbers and dates cannot be encrypted.
- Opening a subject's value costs one query for their key, once per
  transaction. Streaming results (`yield_per`) on a connection that must
  also read keys is not supported.
- Renaming a table, a column, or a primary key's values makes its sealed
  values unreadable: re-encrypt them under the new names first (export
  with `dumpdata --decrypt`, load into the new shape).
- Erasure concurrent with a write may leave the write's value sealed under
  the key being destroyed: it reads as None, as the erasure intends.

## Amendment (2026-10-31): the security review of 0.15

The review before release (SEC-15a) found three gaps here; each is fixed.

- **The context's format** (SEC-15a-5). It joined the primary key's parts
  with an unescaped `\x1f`, so two rows of a composite key could share it.
  It is now the JSON array `[table, column, [part, ...]]`, each part as it
  prints. This changes every sealed value's associated data; nothing was
  released under the old format, so no value sealed under it exists
  outside development databases, and none is migrated: re-create those.
- **Writes past the mapper's events** (SEC-15a-1). `update_rows` that sets
  an encrypted field, or the field naming its subject, loads the matching
  aggregates, sets the values, and flushes, so `before_update` seals them
  for their row and subject and clears the index of a value set to None;
  it still checks no invariant and records no event. An ORM `update()`
  that sets an encrypted field, its index, its subject, or the primary key
  is refused in the session's `do_orm_execute`; Core SQL is not seen and
  must seal what it writes. A value not loaded (`load_only`, `defer`) is
  read from its row, under its stored key, when its subject or key
  changes, and sealed again; a refresh during the flush leaves a value
  sealed for that flush as it is.
- **An erasure racing a first key** (SEC-15a-2). Measured on PostgreSQL:
  the erasure's `DELETE` neither sees nor waits for a key inserted by an
  open transaction. A per-subject lock held from the key's making to its
  commit would make the erasure wait on every writer, and deadlock a
  request that erases a person it is writing for (the proof's shape). So
  the erasure leaves a tombstone (`domainkit_keys_erasures`: the subject
  and the database's `clock_timestamp()`, kept a day) under a
  transaction-scoped advisory lock of the subject; a transaction that made
  keys takes the same locks as it commits (the connection's `commit`
  event) and destroys each key made before its subject's tombstone, with
  the blind indexes it wrote for that subject. Either the erasure finished
  first and left its tombstone, or it starts after the commit and sees the
  key. The tombstone keeps the subject's id for a day, as the audit trail
  keeps it in the erasure's own record.
