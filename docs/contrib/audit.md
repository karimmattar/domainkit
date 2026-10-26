# Audit: who did what, when, from where

Every committed domain event becomes a row in `domainkit_audit_log`, written
in the same transaction as the change. A row holds:

- `occurred_at`, `actor`, `request_id`, and `client_ip`, from the request.
  The actor is `anonymous` for callers who did not authenticate, and
  `system` outside requests, as in periodic tasks and other work no request
  queued; work a request queued keeps its actor.
- `action`, named `<domain label>.<event in snake case>` (`catalog.product_created`)
- `outcome`, `target`, `event_id`, and the event's fields as `details`

Record what no event describes, such as refused logins or reads of sensitive
data, in its own transaction:

```python
from domainkit.contrib.audit import Outcome, record

await record("auth.login", Outcome.DENIED, reason="mfa")
```

`python manage.py auditlog --actor alice --action catalog.product_created` reads the
trail. Events are audited in the database where they commit, so with several
databases, `auditlog --database <alias>` reads another one.

## Personal data

An append-only trail cannot delete what it holds, so personal data never
goes into it in clear. Each personal value is stored as a pseudonym,
`pseudonym:` and 32 hex digits: an HMAC-SHA256 of the field's name and the
value, keyed with a random key of the person it is about, its subject.
Subjects' keys live in `domainkit_audit_keys`, which is not append-only.

- **The client's address** is the actor's personal data. Without an actor,
  it belongs to that day's anonymous callers (subject `anonymous:2026-10-18`,
  in UTC).
- **Event fields** are personal when marked with `Personal`, naming the field
  that identifies the person. Auth marks the username of `UserCreated` and
  `UserDeleted`:

  ```python
  from typing import Annotated
  from uuid import UUID

  from domainkit.ddd import DomainEvent, Personal


  class MemberJoined(DomainEvent):
      member_id: UUID
      email: Annotated[str, Personal(of="member_id")]
      phone: Annotated[str, Personal(of="member_id")] | None = None
  ```

  The whole field becomes one pseudonym, also when it is optional. Mark
  the field itself: a marker inside it (`list[Annotated[str, Personal(...)]]`,
  or a value object's own marked fields) raises `TypeError`, since it would
  be ignored.

- **Explicit records** take personal details apart, under the actor (or the
  day's anonymous callers) unless a `subject` is named:
  `record("auth.login", Outcome.DENIED, personal={"username": username})`.
- **`actor` and `target`** are not pseudonymised. The actor is a user's id;
  give targets ids, not names or emails.
- **Lists that grow long or name people** can be kept as their size: a
  field marked `Counted()` (`users: Annotated[tuple[str, ...], Counted()]`)
  is audited as its number of items, while the event itself keeps them.
  Flags count the users of `FlagChanged` this way.

A value and its subject always give the same pseudonym, so the trail still
answers "did this happen to this person?":
`await pseudonym_of(str(user.id), "username", "alice")` returns the pseudonym
to look for, or None once the person is forgotten. Each database keeps its
own keys, so look in the one that recorded the rows (`database=`, by
default `AUDIT.DATABASE`). A row whose personal value is None stores None.
The same markers on aggregate fields let `domainkit.contrib.privacy`
export and erase what the domains' own tables hold about a person
([privacy.md](../privacy.md)).

To honour an erasure request, delete the person's key, in every database
that holds an audit trail:

```shell
python manage.py auditforget 4b0c6f0e-...            # or: await forget(str(user.id))
python manage.py auditforget --anonymous-days 30     # anonymous callers' days before that
```

Each run is recorded as `operator.auditforget`, with the operator who ran
it and how many subjects and keys, not which subjects (see
[operators](../reference.md#operators)). Their pseudonyms stay in the trail,
unchanged, but can no longer be linked to them by anyone: the key was
random and is gone. A later event about them
gets a new key, unlinkable to the old pseudonyms. Run `--anonymous-days` on
a schedule to keep anonymous callers' addresses only as long as you need
them. Rows written before this release keep their values in clear; the
append-only protection keeps them as they are.

A transaction reads the keys of all the subjects it writes about with one
query, and makes a new subject's key with an insert that returns it, so
each key is read once per transaction. No key is kept past its
transaction: `forget` reaches every process through the database, for
every transaction that starts after it, and drops at once the keys that
open transactions of its own process hold. A lookup that read a key
before `forget` began may still use it for the rows it is writing, as it
would had it run a moment earlier, but keeps nothing it read. A subject's
first key is made under a lock of that subject, which `forget` takes too
(a transaction advisory lock on PostgreSQL, one more statement for a new
subject only; SQLite runs one writer at a time): `forget` waits for a
writer making a key, then deletes that key as well.

The application only inserts. To make sure a compromised API cannot rewrite
history, make the table append-only in the database itself. Add a revision
that calls `domainkit.contrib.audit.protection.protect(op)` in `upgrade()`
and `unprotect(op)` in `downgrade()`. On PostgreSQL this installs triggers
that refuse `UPDATE`, `DELETE`, and `TRUNCATE` for every role. Only the table
owner (the migration role) can drop them, for example to apply a retention
policy:

```python
from alembic import op

from domainkit.contrib.audit.protection import protect, unprotect


def upgrade() -> None:
    protect(op)


def downgrade() -> None:
    unprotect(op)
```

## Tamper evidence

Triggers stop the application, but not the tables' owner, who can drop
them, rewrite rows, and put them back. So the trail is also sealed into a
hash chain, and the chain's head is anchored on a ledger the owner cannot
rewrite.

- **Sealing.** Each row not sealed yet gets the next number in
  `domainkit_audit_seals`, with its digest (the SHA-256 of the row's
  canonical JSON) and its link (the SHA-256 of the previous link and the
  digest). The worker seals every `AUDIT.SEAL_EVERY` when
  `domainkit.contrib.messaging` is installed; otherwise run
  `python manage.py auditseal` on a schedule. Each database's trail is its
  own chain. The newest sealed row is a high-water mark: a run looks for
  rows from `AUDIT.SEAL_LATE_WINDOW` before it (rows that committed late),
  so its cost follows what is new, not how long the trail is. The mark
  counts as now at most, by the database's clock on PostgreSQL (the
  host's on SQLite), which also dates the seals. A run seals
  `AUDIT.SEAL_BATCHES` batches at most and leaves the rest for the next;
  the `audit_unsealed_rows` gauge says how many it left, and a warning is
  logged past `AUDIT.SEAL_BACKLOG_WARNING`. Sealing also makes the keys of
  today's and tomorrow's anonymous callers ahead of time, so at midnight
  (UTC) audited requests do not queue behind one insert of the new day's
  key.
- **Sealing late rows.** A row that commits later than the window after
  the mark is out of a run's reach, and `auditverify` reports it as
  unsealed once it is a day old. Look into why (a transaction that ran
  that long, a host whose clock runs behind), then run
  `python manage.py auditseal --late`: it seals such rows into the same
  chain, each seal flagged `late`, and `auditverify` counts them
  afterwards ("rows sealed late"). They were open to change unnoticed for
  longer than the others. Finding them reads the whole trail, so the
  worker never does it on its own.
- **Anchoring.** With `AUDIT.LEDGER` naming a ledger in `LEDGERS` (immudb),
  sealing also writes each chain's head (its number and link) there, at
  most once per `AUDIT.ANCHOR_EVERY`, as a new revision of
  `domainkit/audit/<AUDIT.NAMESPACE>/<alias>`. immudb keeps every revision
  and verifies what it returns. Give each project and environment that
  shares a ledger a namespace of its own (`"shop-production"`): without
  one, anchors go under `domainkit/audit/<alias>`, the key of 0.9.0, which
  every project on the ledger shares, so one project's anchors hold
  another's back and read as rewritten anchors in its `auditverify`.
  Setting a namespace on a trail anchored before keeps it verified:
  `auditverify` also reads the heads anchored under the old key while
  `AUDIT.PREVIOUS_ANCHORS` is on. If other projects wrote that key too,
  their heads read as rewritten anchors there; set `PREVIOUS_ANCHORS` to
  `False` once your namespace holds an anchor of its own.
- **Verifying.** `python manage.py auditverify` checks every row against
  its seal, every link against the one before, and every anchored head
  against the chain. It prints what it found and fails on changed or
  missing rows, broken links, rewritten anchors, seals that no anchor
  covers a day past `AUDIT.ANCHOR_EVERY` (anchoring stopped), and rows
  older than a day that were never sealed.

Every row can be sealed, whatever its strings hold (a lone surrogate, which
JSON can escape, included). A host whose clock runs ahead cannot stop
sealing or anchoring: a row or an anchor dated in the future counts as
dated now, and on PostgreSQL "now" is the database's, so a sealer whose
own clock runs ahead cannot leave other hosts' rows behind.

A row can be changed unnoticed only until it is sealed and its head
anchored, so anchor as often as you need: the default is hourly. Erasing a
person (`auditforget`) deletes a key, not a row, so the chain stays intact.

**Upgrading.** The seals table has a new `late` column: run
`makemigrations` and `migrate` in each project that installs audit.

## Retention that keeps the chain

The trail is append-only, so `privacy purge` never touches it. To keep
it for a period and no longer, prune it below a signed checkpoint
(`pip install "domainkit[audit]"`, for the signatures):

```bash
openssl genpkey -algorithm ed25519 -out audit-checkpoint.pem   # once, kept secret
openssl pkey -in audit-checkpoint.pem -pubout -out audit-checkpoint.pub
```

```python
# Every process: the public key, to verify.
AUDIT = {..., "CHECKPOINT_PUBLIC_KEY": env.str("AUDIT_CHECKPOINT_PUBLIC_KEY")}
```

```bash
# Only where auditprune (and auditverify --export) run: the private key too.
AUDIT_CHECKPOINT_KEY="$(cat audit-checkpoint.pem)" \
  python manage.py auditprune --keep-days 2555   # keep seven years
```

where those commands' settings read `"CHECKPOINT_KEY":
env.str("AUDIT_CHECKPOINT_KEY", None)`. The application never reads the
private key: whoever holds its settings cannot sign a checkpoint.

`--keep-days` is at least `AUDIT.MIN_RETENTION` (30 days by default):
a shorter one is refused. For each audited database, `auditprune` finds
the newest seal sealed more than `--keep-days` ago (with `AUDIT.LEDGER`,
the newest one anchored `MIN_RETENTION` ago, so the ledger vouches for it
too; never the chain's head), checks the chain up to it as `auditverify`
does, and refuses if anything there is wrong: pruning would hide it. It
then signs a checkpoint of that seal, its number and link, with
`AUDIT.CHECKPOINT_KEY` (Ed25519), records it on the ledger
(`domainkit/audit-pruned/<NAMESPACE>/<alias>`), stores it in
`domainkit_audit_checkpoints`, and deletes the rows and seals at and
below it, the newest first, 1,000 a transaction. `auditverify` starts the
chain from the checkpoint, so a pruned trail still verifies (it prints
how many seals were pruned), and one tampered with still fails: a row
changed, a seal removed, the checkpoint removed or changed, or a
checkpoint stored to cover a deletion shows as a changed row, a broken
link, or a checkpoint that does not verify. A checkpoint covers a gap
only if it verifies under `AUDIT.CHECKPOINT_PUBLIC_KEY`, was signed for
pruning (the head checkpoint of an export says `"kind": "export"`, and
covers nothing), follows the checkpoint before it, was made
`MIN_RETENTION` after its seal and, with a ledger, is recorded there,
its seal first anchored `MIN_RETENTION` before. So whoever writes the
tables cannot delete the trail with a checkpoint handed out in an
export, and whoever also stole the key and the ledger's credentials
still cannot delete rows younger than `MIN_RETENTION` unseen
(SEC-10b-2, SEC-10b-3). Raising `MIN_RETENTION` reports the prunes made
below it before. A run cut short leaves a gap the checkpoint ends, which
verifies; the next run finishes it. Each prune is recorded, with the seal
and the rows deleted (`audit.pruned`), and so is the command
(`operator.auditprune`).

On PostgreSQL the `protect()` triggers refuse every delete, so each batch
disables them in its own transaction, as the tables' owner: set
`MIGRATION_URL` to the owner's URL where the application's role is not
it. Writers of the trail wait for that one batch (under 10 ms in our
measurements). Run `auditprune` from cron, not more than daily.

**Exports for a third party.** `python manage.py auditverify --export
checkpoints.json` verifies every trail, then writes a new file (mode
`0600`) with each database's stored checkpoints and one of its current
head, each signed, and the public key's id. Whoever holds the public key
(an auditor, a regulator) checks it without the database, the ledger, or
your credentials:

```bash
python manage.py auditverify --check-export checkpoints.json --public-key audit-checkpoint.pub
```

or with any Ed25519 library: the format is in
[ADR 0082](../adr/0082-signed-audit-checkpoints.md). A later export of the
same trail extends the earlier one, checkpoint by checkpoint, so an
operator who rewrote the trail and the ledger together cannot make the
exports agree.

**Upgrading.** The checkpoints table, and the ledger states table
([databases.md](../databases.md#ledgers-immudb)), are new: run
`makemigrations` and `migrate`.

**Two sealers at once.** One sealer seals a database at a time: on
PostgreSQL each batch takes a transaction-scoped advisory lock. A sealer
that finds it taken (`auditseal` run by hand while the worker seals, or
two outboxes' workers sealing one database) stops at once, logs that
another sealer is at work, and leaves it the rest; what it sealed before
stays sealed. Until 0.9.2 it waited for the lock, and failed after
`LOCK_TIMEOUT`. A run that gives way still counts what waits
(`audit_unsealed_rows`) and warns past `AUDIT.SEAL_BACKLOG_WARNING`, and
it warns, whatever the count, once the oldest row waiting is older than
`AUDIT.SEAL_EVERY` and `AUDIT.SEAL_LATE_WINDOW` together: a sealer at
work leaves no row that long, so the lock is held by something that does
not seal (a stuck session, or anyone's `pg_advisory_lock`). Find it in
`pg_locks` and end it.

## In staging and production

The trail is tamper-evident only with four things in place, and each is
easy to leave out: a ledger (`AUDIT.LEDGER`), a namespace on it
(`AUDIT.NAMESPACE`), something that seals on a schedule (the worker, with
`domainkit.contrib.messaging` installed, or `auditseal` from cron, said
with `AUDIT.SEALED_EXTERNALLY = True`), and the `protect()` triggers on
both tables. In staging and production, `manage.py check` warns about each
setting that is missing, and the application, when it starts, asks each
audited PostgreSQL database whether `domainkit_audit_log` and
`domainkit_audit_seals` have their triggers as `protect()` makes them, and
logs a warning for each database without them, or that did not answer
within 10 seconds. A trigger that is disabled, or fires only in replica
sessions (`ENABLE REPLICA TRIGGER`), on other events, under a `WHEN`
condition, or for some columns only, or whose function is not the one
`protect()` writes (its body is compared, whitespace aside), counts as
missing. The probe cannot see a session that sets
`session_replication_role = replica`, which only a superuser may do, nor
stop a table owner from dropping the triggers between two starts:
`auditverify` checks what they protect.

Since 0.10 they refuse: `AUDIT.REQUIRE_BASELINE` is `True` by default, so
staging and production do not start without the four, `check` fails, and
each missing part is an error (`audit.E001` to `audit.E005`) that
`SILENCED_CHECKS` cannot name. `AUDIT.REQUIRE_BASELINE = False` keeps
0.9's warnings (`audit.W001` to `audit.W005`).

Protect the seals like the trail, in the revision that creates them:

```python
from domainkit.contrib.audit.tables import audit_seals


def upgrade() -> None:
    ...  # The revision's create_table.
    protect(op, audit_seals)


def downgrade() -> None:
    unprotect(op, audit_seals)
    ...
```

| `AUDIT` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Where `record()` writes, and what `auditlog` reads by default. |
| `EVENTS` | `True` | Record every committed domain event. |
| `EXCLUDE` | `()` | Actions to skip, as shell patterns (`catalog.*`, `*.viewed`). |
| `LEDGER` | `None` | `LEDGERS` alias to anchor each chain's head on; `None` seals without anchoring. |
| `NAMESPACE` | `None` | Part of the anchors' ledger key, `domainkit/audit/<NAMESPACE>/<alias>`: a lower-case letter, then up to 31 letters, digits, `_`, or `-`. `None` keeps the key of 0.9.0, which every project on the ledger shares; staging and production warn. |
| `PREVIOUS_ANCHORS` | `True` | With `NAMESPACE`, also verify the heads anchored under `domainkit/audit/<alias>` before it was set. Turn it off when other projects anchored there too. |
| `ANCHOR_EVERY` | 1 hour | Anchor a new head at most this often. |
| `SEAL_EVERY` | 5 minutes | How often the worker seals new rows. |
| `SEALED_EXTERNALLY` | `False` | `auditseal` runs from a schedule of your own (cron), not in the worker: staging and production then do not warn that nothing seals. |
| `REQUIRE_BASELINE` | `True` | Refuse to start in staging and production without a ledger, a namespace, a sealing schedule, and the triggers (the default since 0.10). `False` only warns, as 0.9 did. |
| `SEAL_LATE_WINDOW` | 15 minutes | How far before the newest sealed row a run looks for rows that committed late, up to a day. A row later than this stays unsealed until `auditseal --late`, and `auditverify` reports it once it is a day old: keep it above your longest transaction plus the clock skew between hosts. |
| `SEAL_BATCHES` | `100` | Batches of 1,000 rows one run seals at most (1 to 10,000); the rest wait for the next run. |
| `SEAL_BACKLOG_WARNING` | `100000` | Rows a run leaves unsealed past which it logs a warning. |
| `CHECKPOINT_KEY` | `None` | The Ed25519 private key, in PEM (at most 1,024 characters), that signs checkpoints: those `auditprune` prunes below, and those `auditverify --export` writes. Read by those two commands alone, so set it only where they run, not in the application's settings. `None`: neither runs. |
| `CHECKPOINT_PUBLIC_KEY` | `None` | The public half of `CHECKPOINT_KEY`, in PEM (at most 1,024 characters): what `auditverify` checks stored checkpoints with. Checked when the application starts; signing refuses a private key it is not the half of. `None`: no stored checkpoint verifies, so a pruned trail does not. |
| `MIN_RETENTION` | `30 days` | How old a seal must be before `auditprune` may cut there (1 day to 3,650 days): a shorter `--keep-days` is refused, and a checkpoint of a younger seal starts nothing in `auditverify`. |

## History

A record's history is the rows about it, newest first, from every audited
database (Django admin's "History"). Rows name what they are about in two
ways:

- an explicit record has a `target`: `record("report.viewed", target="r-1")`;
- a committed event holds its aggregate's id in a field of `details`, such
  as `product_id`; give events the id of the aggregate they are about, as
  `startdomain` does.

```python
from domainkit.contrib.audit.history import history

entries = await history(target="r-1")
entries = await history(aggregate=("product_id", str(product.id)), limit=20)
older = await history(target="r-1", before=entries[-1].position)
```

Each entry lists its `changes`, where the details allow. An explicit record
can say what changed, with `before` and `after` mappings:
`record("report.renamed", target="r-1", before={"name": "Q1"}, after={"name": "Q2"})`.
They are kept as written, so never put personal data in them; pass it as
`personal=`. Otherwise each field is compared with the newest older entry of the
history that has it, on the same page or the one entry fetched past it, so
events read best when they carry the fields they change
(`ProductRenamed(product_id=..., name=...)`). A field nothing older has is
new (`before` is None).

Personal values stay the pseudonyms the trail holds, also for people who
were forgotten: reading a history reads no keys. An aggregate's id is
matched as text, so query by ids, never by personal fields (they are
pseudonyms).

From the command line, each change under its entry:

```shell
python manage.py auditlog --target r-1
python manage.py auditlog --aggregate product_id=6f1c7e6a-...
```

Staff read histories over HTTP, with `audit.view_history` (a permission
`migrate` creates for the trail's `AuditEntry` model) and `is_staff`, or as
superusers. Mount the router next to the admin API:

```python
from domainkit.contrib.audit.api import router as audit_router

router.include_router(audit_router, prefix="/admin/audit")
```

`GET /admin/audit/history?target=r-1` and
`GET /admin/audit/history?field=product_id&id=...` answer pages of up to
`limit` (50, at most 200) entries, with `next_cursor`. Entries leave out
the client's address. The permission opens every record's history, and
querying by a field reads the whole trail: grant it as you would read
access to the trail.

**Upgrading.** The trail gained an index on `target`, for histories. Run
`makemigrations` and `migrate` in each project that installs audit.

