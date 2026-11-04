# Audit: who did what, when, from where

The audit trail answers "who did what, when, and from where?". The
audit app writes one row for each committed [domain
event](../learning.md#words), in the same transaction as the change.
You can also record actions that no event describes, such as a refused
login. The trail is append-only, keeps personal data only as
pseudonyms, and is sealed into a hash chain anchored on a ledger. So
nobody can change it without the change showing, not even the owner of
its tables.

On this page you learn how to:

- install the trail and read it;
- record actions yourself, with `record`;
- keep personal data out of it, and forget a person;
- seal, anchor, and verify the trail;
- prune it below a signed checkpoint, and export checkpoints for an
  auditor;
- read a record's history, in code, from the command line, and over
  HTTP;
- meet the checks that staging and production run.

## Install

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",  # seals the trail on a schedule
    "domainkit.contrib.audit",
]
LEDGERS = {"audit": {"URL": env.str("AUDIT_LEDGER_URL")}}  # immudb://...
AUDIT = {"LEDGER": "audit", "NAMESPACE": "shop-production"}
```

```bash
pip install "domainkit[audit]"  # for signed checkpoints
python manage.py makemigrations -m "audit"
python manage.py migrate
```

The app brings five tables:

| Table | What it holds | Protection on PostgreSQL |
| --- | --- | --- |
| `domainkit_audit_log` | The trail: one row per action. | Append-only. |
| `domainkit_audit_seals` | Each row's place in the hash chain. | Append-only. |
| `domainkit_audit_checkpoints` | Signed checkpoints that pruning leaves. | Append-only. |
| `domainkit_audit_keys` | The keys of the pseudonyms, one per person. | None: `forget` deletes keys. |
| `domainkit_ledger_states` | The state each ledger last proved. | Moves forward only. |

The trail lives in every database that hosts a domain, because events
are audited where they commit. `record()` writes to `AUDIT.DATABASE`.

## A first example

Committed events are recorded on their own. Record a refused login
yourself, then read the trail:

```python
from domainkit.contrib.audit import Outcome, record
from domainkit.contrib.audit.testing import audit_entries

await record("auth.login", Outcome.DENIED, reason="mfa")

for entry in await audit_entries(action="auth.login"):
    print(entry.actor, entry.action, entry.outcome, dict(entry.details))
```

```text
system auth.login denied {'reason': 'mfa'}
```

The actor is `system` here, because no request was running. From the
command line:

```bash
python manage.py audit log --limit 3
```

```text
2026-11-04 18:33:08  system  auth.login  denied  -  -
2026-11-04 18:33:08  system  report.renamed  success  r-1  -
2026-11-04 18:33:08  system  report.viewed  success  r-1  -
```

Each line is the time, the actor, the action, the outcome, the target,
and the request id.

## What a row holds

- `occurred_at`, `actor`, `request_id`, and `client_ip`, from the
  request. The request id is a trusted proxy's `X-Request-ID`, or a new
  one; never a caller's own ([operations.md](../operations.md#request-ids)).
  The actor is `anonymous` for callers who did not authenticate, and
  `system` outside requests, as in periodic tasks and other work that no
  request queued. Work that a request queued keeps its actor.
- `action`, named `<domain label>.<event in snake case>`
  (`catalog.product_created`). `action_for(EventClass)` gives the name.
- `outcome`: an `Outcome`, one of `success`, `failure` (attempted, and
  it failed), or `denied` (a policy refused it).
- `target`, `event_id`, and the event's fields as `details`.

## Recording actions yourself: `record`

`record(action, outcome=Outcome.SUCCESS, *, target=None, personal=None,
subject=None, **details)` records an action in its own transaction. Use
it for what no event describes: refused logins, reads of sensitive data,
failures.

```python
await record("report.viewed", target="r-1")
await record("auth.login", Outcome.DENIED, personal={"username": username})
```

`personal` details are stored as pseudonyms (next section). `target`
names the record that the action is about, for its history.

`AUDIT.EVENTS = False` stops recording events; `AUDIT.EXCLUDE` skips
some actions by shell pattern (`catalog.*`, `*.viewed`).

Operators' commands add their own rows
([operators](../reference/commands.md#operators)). `operator.loaddata`
names the fixture file, its SHA-256, and each table with how many rows it
got and replaced. `operator.flush` names each table it emptied, with its
rows, and the tables it kept. Both say whether `--replace` or
`--include-guarded` was given. Neither loads nor empties the trail's own
tables.

`recorded(connection, action, per=500)` streams the `details` of every
row of one action, `per` rows at a time. `accounting verify` compares
the books with it.

## Personal data

An append-only trail cannot delete what it holds, so personal data never
goes into it in clear. Each personal value is stored as a pseudonym:
`pseudonym:` and 32 hex digits. A pseudonym is an HMAC-SHA256 of the
field's name and the value, keyed with a random key of the person the
value is about, its subject. The subjects' keys live in
`domainkit_audit_keys`, which is not append-only.

- **The client's address** is the actor's personal data. Without an
  actor, it belongs to that day's anonymous callers (subject
  `anonymous:2026-10-18`, in UTC).
- **Event fields** are personal when marked with `Personal`, naming the
  field that identifies the person. Auth marks the username of
  `UserCreated` and `UserDeleted`:

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
  the field itself: a marker inside it
  (`list[Annotated[str, Personal(...)]]`, or a value object's own marked
  fields) raises `TypeError`, because it would be ignored.

- **Explicit records** take personal details apart, under the actor (or
  the day's anonymous callers) unless you name a `subject`:
  `record("auth.login", Outcome.DENIED, personal={"username": username})`.
- **`actor` and `target`** are not pseudonymized. The actor is a user's
  id; give targets ids, not names or email addresses.
- **Lists that grow long or name people** can be kept as their size: a
  field marked `Counted()` (`users: Annotated[tuple[str, ...], Counted()]`)
  is audited as its number of items, while the event itself keeps them.
  Flags count the users of `FlagChanged` this way.

A value and its subject always give the same pseudonym, so the trail
still answers "did this happen to this person?". `pseudonym_of(subject,
field, value)` returns the pseudonym to look for, or `None` once the
person is forgotten:

```python
from domainkit.contrib.audit import pseudonym_of

await record(
    "auth.login", Outcome.DENIED, personal={"username": "alice"}, subject="user-7"
)
print(await pseudonym_of("user-7", "username", "alice"))
```

```text
pseudonym:9ea8cbe297962e12334f3befe19d0f4d
```

Each database keeps its own keys, so look in the one that recorded the
rows (`database=`, by default `AUDIT.DATABASE`). A row whose personal
value is `None` stores `None`. The same markers on aggregate fields let
`domainkit.contrib.privacy` export and erase what the domains' own tables
hold about a person ([privacy.md](../privacy.md)).

### Forgetting a person

To honour an erasure request, delete the person's key, in every database
that holds an audit trail:

```bash
python manage.py audit forget 4b0c6f0e-6a43-4e58-9a59-5f1e3c3bd2a1   # or: await forget(str(user.id))
python manage.py audit forget --anonymous-days 30     # anonymous callers' days before that
```

```text
Forgot 1 key.
```

`forget(*subjects)` and `forget_anonymous(before)` do the same from code,
and return how many keys they deleted. Each run of the command is
recorded as `operator.auditforget`, with the operator who ran it and how
many subjects and keys, not which subjects. The pseudonyms stay in the
trail, unchanged, but nobody can link them to the person any more: the
key was random and is gone. A later event about the person gets a new
key, which cannot be linked to the old pseudonyms. Run
`--anonymous-days` on a schedule to keep anonymous callers' addresses
only as long as you need them. Rows written before pseudonyms existed
keep their values in clear; the append-only protection keeps them as
they are.

A transaction reads the keys of all the subjects it writes about with
one query, and makes a new subject's key with an insert that returns it.
So each key is read once per transaction. No key is kept past its
transaction: `forget` reaches every process through the database, for
every transaction that starts after it, and drops at once the keys that
open transactions of its own process hold. A lookup that read a key
before `forget` began may still use it for the rows it is writing, but
keeps nothing it read. A subject's first key is made under a lock of
that subject, which `forget` takes too (a transaction advisory lock on
PostgreSQL; SQLite runs one writer at a time). So `forget` waits for a
writer making a key, then deletes that key as well.

## Append-only tables

The application only inserts. To make sure that a compromised API cannot
rewrite history, the tables are protected in the database itself. On
PostgreSQL, `migrate` installs triggers that refuse `UPDATE`, `DELETE`,
and `TRUNCATE` of `domainkit_audit_log`, `domainkit_audit_seals`, and
`domainkit_audit_checkpoints`, for every role. `domainkit_ledger_states`
gets triggers too: a ledger's saved state may only be updated to the same
ledger at the same or a later transaction, never deleted or moved back,
because a state rolled back would reset the rollback detection it exists
for.

`migrate` makes the triggers afresh after every run, so a trigger
dropped, disabled, or changed is put back
(`domainkit.contrib.audit.protection.protect_migrated`, registered on
`on_migrated`). No revision of yours needs to call them. Only the table
owner (the migration role) can drop them, for example to apply a
retention policy: `unprotect(op)` in a revision does, until the next
`migrate`. A revision that calls `protect(op)` itself, as 0.12 asked,
still works: protecting replaces what is there.

```python
from domainkit.contrib.audit.protection import protect, unprotect


def upgrade() -> None:
    protect(op)


def downgrade() -> None:
    unprotect(op)
```

On other databases, both do nothing.

### Tenant schemas

On a database that keeps a schema per [tenant](../learning.md#words)
(`TENANCY.SCHEMA_DATABASES`), each tenant's schema holds an audit trail
of its own: a unit of work run as the tenant writes its rows there
([tenancy.md](../tenancy.md)). `migrate --tenant` protects that trail
with the same triggers, because the `on_migrated` hooks run in the
tenant's schema. Each tenant trail is a chain of its own:

- `audit seal` seals and anchors it, under the ledger key
  `domainkit/audit/<NAMESPACE>/<alias>/<schema>`;
- `audit verify` verifies it, and prints a line for it, such as
  `default/tenant_acme`;
- `audit forget` and `privacy erase` delete a person's keys there too.

`audit prune` and the startup check of the triggers cover only the
database's own schema.

## Tamper evidence

Triggers stop the application, but not the tables' owner, who can drop
them, rewrite rows, and put the triggers back. So the trail is also
sealed into a hash chain, and the chain's head is anchored on a ledger
that the owner cannot rewrite.

- **Sealing.** Each row not sealed yet gets the next number in
  `domainkit_audit_seals`, with its digest (the SHA-256 of the row's
  canonical JSON) and its link (the SHA-256 of the previous link and the
  digest). The worker seals every `AUDIT.SEAL_EVERY` when
  `domainkit.contrib.messaging` is installed. Otherwise, run `python
  manage.py audit seal` on a schedule. Each database's trail is its own
  chain. The newest sealed row is a high-water mark: a run looks for rows
  from `AUDIT.SEAL_LATE_WINDOW` before it (rows that committed late), so
  its cost follows what is new, not how long the trail is. The mark counts
  as now at most, by the database's clock on PostgreSQL (the host's on
  SQLite), which also dates the seals. A run seals `AUDIT.SEAL_BATCHES`
  batches at most and leaves the rest for the next. The
  `audit_unsealed_rows` gauge says how many it left, and a warning is
  logged past `AUDIT.SEAL_BACKLOG_WARNING`. Sealing also makes the keys
  of today's and tomorrow's anonymous callers ahead of time. So at
  midnight (UTC), audited requests do not wait for one insert of the new
  day's key.
- **Sealing late rows.** A row that commits later than the window after
  the mark is out of a run's reach. `audit verify` reports it as unsealed
  once it is a day old. Find out why (a transaction that ran that long, a
  host whose clock runs behind), then run `python manage.py audit seal
  --late`. It seals such rows into the same chain, each seal flagged
  `late`, and `audit verify` counts them afterwards ("rows sealed late").
  They were open to change unnoticed for longer than the others. Finding
  them reads the whole trail, so the worker never does it on its own.
- **Anchoring.** With `AUDIT.LEDGER` naming a ledger in `LEDGERS`
  (immudb), sealing also writes each chain's head (its number and link)
  there, at most once per `AUDIT.ANCHOR_EVERY`, as a new revision of
  `domainkit/audit/<AUDIT.NAMESPACE>/<alias>`. immudb keeps every
  revision and verifies what it returns. Give each project and
  environment that shares a ledger a namespace of its own
  (`"shop-production"`). Without one, anchors go under
  `domainkit/audit/<alias>`, the key of 0.9.0, which every project on the
  ledger shares. Then one project's anchors hold another's back, and read
  as rewritten anchors in its `audit verify`. Setting a namespace on a
  trail anchored before keeps it verified: `audit verify` also reads the
  heads anchored under the old key while `AUDIT.PREVIOUS_ANCHORS` is on.
  If other projects wrote that key too, their heads read as rewritten
  anchors there; set `PREVIOUS_ANCHORS` to `False` once your namespace
  holds an anchor of its own.
- **Verifying.** `python manage.py audit verify` checks every row against
  its seal, every link against the one before, and every anchored head
  against the chain. It prints what it found. It fails on changed or
  missing rows, broken links, rewritten anchors, seals that no anchor
  covers a day past `AUDIT.ANCHOR_EVERY` (anchoring stopped), and rows
  older than a day that were never sealed.

```bash
python manage.py audit seal
python manage.py audit verify
```

```text
default: sealed 4 rows, anchored #4.
default: 4 seals, 1 anchors: intact.
```

When something is wrong, the line says `NOT INTACT.`, and more lines
follow: `changed rows`, `missing rows`, `broken links at`, `rewritten
anchors at`, `checkpoints that do not verify at`, `seals left
unanchored`, `unsealed rows`, `rows sealed late`, and `seals pruned
below checkpoints`.

From code, `seal(database)` seals one trail and returns how many rows,
`seal_all()` seals and anchors every trail, and `verify(database,
ledger)` returns a report whose `intact` is true when nothing is wrong.

Every row can be sealed, whatever its strings hold (a lone surrogate,
which JSON can escape, included). A host whose clock runs ahead cannot
stop sealing or anchoring: a row or an anchor dated in the future counts
as dated now. On PostgreSQL "now" is the database's, so a sealer whose
own clock runs ahead cannot leave other hosts' rows behind.

A row can be changed unnoticed only until it is sealed and its head
anchored, so anchor as often as you need: the default is hourly.
Forgetting a person deletes a key, not a row, so the chain stays intact.

**Two sealers at once.** One sealer seals a database at a time: on
PostgreSQL, each batch takes a transaction-scoped advisory lock. A sealer
that finds it taken (`audit seal` run by hand while the worker seals, or
two outboxes' workers sealing one database) stops at once, logs that
another sealer is at work, and leaves it the rest. What it sealed before
stays sealed. Until 0.9.2 it waited for the lock, and failed after
`LOCK_TIMEOUT`. A run that gives way still counts what waits
(`audit_unsealed_rows`) and warns past `AUDIT.SEAL_BACKLOG_WARNING`. It
also warns, whatever the count, once the oldest row waiting is older
than `AUDIT.SEAL_EVERY` and `AUDIT.SEAL_LATE_WINDOW` together. A sealer
at work leaves no row that long, so the lock is held by something that
does not seal (a stuck session, or anyone's `pg_advisory_lock`). Find it
in `pg_locks` and end it.

### Ledger states

A ledger client checks a proof with every read and write, against the
state the ledger last proved. With the audit app installed, that state
is kept in `domainkit_ledger_states` on `AUDIT.DATABASE`
(`DatabaseStateStore`), not only in memory. So a new process, a replaced
container, or another replica refuses a ledger rolled back below what any
of them verified. A save replaces only the state the saver expects, in
one statement, so no replica overwrites a state that another proved
since (SEC-10b-5). The [evidence](evidence.md) app relies on this too.

## Retention that keeps the chain

The trail is append-only, so `privacy purge` never touches it. To keep
it for a period and no longer, prune it below a signed checkpoint. The
signatures need the `audit` extra (`pip install "domainkit[audit]"`):

```bash
openssl genpkey -algorithm ed25519 -out audit-checkpoint.pem   # once, kept secret
openssl pkey -in audit-checkpoint.pem -pubout -out audit-checkpoint.pub
```

```python
# Every process: the public key, to verify.
AUDIT = {
    "CHECKPOINT_PUBLIC_KEY": env.str("AUDIT_CHECKPOINT_PUBLIC_KEY"),
    # ... and the other keys
}
```

```bash
# Only where audit prune (and audit verify --export) run: the private key too.
AUDIT_CHECKPOINT_KEY="$(cat audit-checkpoint.pem)" \
  python manage.py audit prune --keep-days 2555   # keep seven years
```

There, the commands' settings read `"CHECKPOINT_KEY":
env.str("AUDIT_CHECKPOINT_KEY", None)`. The application never reads the
private key, so a person who holds its settings cannot sign a
checkpoint.

`--keep-days` is at least `AUDIT.MIN_RETENTION` (30 days by default); a
shorter one is refused. For each audited database, `audit prune`:

1. finds the newest seal sealed more than `--keep-days` ago (with
   `AUDIT.LEDGER`, the newest one anchored `MIN_RETENTION` ago, so the
   ledger vouches for it too; never the chain's head);
2. checks the chain up to it as `audit verify` does, and refuses if
   anything there is wrong, because pruning would hide it;
3. signs a checkpoint of that seal, its number and link, with
   `AUDIT.CHECKPOINT_KEY` (Ed25519);
4. records the checkpoint on the ledger
   (`domainkit/audit-pruned/<NAMESPACE>/<alias>`), and stores it in
   `domainkit_audit_checkpoints`;
5. deletes the rows and seals at and below it, the newest first, 1,000 a
   transaction.

```text
default: pruned 1200 rows below seal #1200.
```

`audit verify` starts the chain from the checkpoint, so a pruned trail
still verifies (it prints how many seals were pruned), and one tampered
with still fails. A row changed, a seal removed, the checkpoint removed
or changed, or a checkpoint stored to cover a deletion shows as a changed
row, a broken link, or a checkpoint that does not verify.

A checkpoint covers a gap only if all of these hold: it verifies under
`AUDIT.CHECKPOINT_PUBLIC_KEY`; it was signed for pruning (the head
checkpoint of an export says `"kind": "export"`, and covers nothing); it
follows the checkpoint before it; it was made `MIN_RETENTION` after its
seal; and, with a ledger, it is recorded there, its seal first anchored
`MIN_RETENTION` before. So a person who writes the tables cannot delete
the trail with a checkpoint handed out in an export. A person who also
stole the key and the ledger's credentials still cannot delete rows
younger than `MIN_RETENTION` unseen (SEC-10b-2, SEC-10b-3). Raising
`MIN_RETENTION` reports the prunes made below it before. A run cut short
leaves a gap that the checkpoint ends, which verifies; the next run
finishes it. Each prune is recorded, with the seal and the rows deleted
(`audit.pruned`), and so is the command (`operator.auditprune`).

On PostgreSQL the `protect()` triggers refuse every delete, so each batch
disables them in its own transaction, as the tables' owner. Set
`MIGRATION_URL` to the owner's URL where the application's role is not
the owner. Writers of the trail wait for that one batch (under 10 ms in
our measurements). Run `audit prune` from cron, not more than daily.

From code, `prune(database, before=..., ledger=...)` returns a `Pruned`
(`database`, `checkpoint`, `rows`), or raises `PruneRefused`, having
deleted nothing.

**Exports for a third party.** `python manage.py audit verify --export
checkpoints.json` verifies every trail. Then it writes a new file (mode
`0600`) with each database's stored checkpoints and one of its current
head, each signed, and the public key's id. A person who holds the
public key (an auditor, a regulator) checks it without the database, the
ledger, or your credentials:

```bash
python manage.py audit check-export checkpoints.json --public-key audit-checkpoint.pub
```

```text
default: 2 checkpoints verified, newest at seal #4800.
```

Any Ed25519 library can check it too: the format is in
[ADR 0082](../adr/0082-signed-audit-checkpoints.md). A later export of
the same trail extends the earlier one, checkpoint by checkpoint. So an
operator who rewrote the trail and the ledger together cannot make the
exports agree.

From code, `export(ledger)` returns the document, or raises
`ExportRefused` when a trail does not verify.
`check_export(document_bytes, public_key_bytes)` returns each database's
`Checkpoint`s, or raises `CheckpointInvalid`: never trust such a file.

## In staging and production

The trail is tamper-evident only with four things in place, and each is
easy to forget:

1. a ledger (`AUDIT.LEDGER`);
2. a namespace on it (`AUDIT.NAMESPACE`);
3. something that seals on a schedule: the worker, with
   `domainkit.contrib.messaging` installed, or `audit seal` from cron,
   said with `AUDIT.SEALED_EXTERNALLY = True`;
4. the `protect()` triggers on the trail and its seals.

In staging and production, `manage.py check` reports each setting that
is missing. When the application starts, it asks each audited PostgreSQL
database whether `domainkit_audit_log` and `domainkit_audit_seals` have
their triggers as `protect()` makes them. A database without them, or
that did not answer within 10 seconds, is reported. A trigger counts as
missing when it is disabled, fires only in replica sessions (`ENABLE
REPLICA TRIGGER`), fires on other events, under a `WHEN` condition, or
for some columns only, or runs a function that is not the one `protect()`
writes (its body is compared, whitespace aside). The probe cannot see a
session that sets `session_replication_role = replica`, which only a
superuser may do. It cannot stop a table owner from dropping the
triggers between two starts either: `audit verify` checks what they
protect. The probe looks at the database's own schema only, not at
tenant schemas.

Since 0.10 these checks refuse. `AUDIT.REQUIRE_BASELINE` is `True` by
default, so staging and production do not start without the four parts,
`check` fails, and each missing part is an error that `SILENCED_CHECKS`
cannot name. `AUDIT.REQUIRE_BASELINE = False` keeps 0.9's warnings.

| Error | Warning | What is missing |
| --- | --- | --- |
| `audit.E001` | `audit.W001` | `AUDIT.LEDGER`: the chain is anchored nowhere. |
| `audit.E002` | `audit.W002` | `AUDIT.NAMESPACE`: anchors go under a key every project on the ledger shares. |
| `audit.E003` | `audit.W003` | Something that seals on a schedule. |
| `audit.E004` | `audit.W004` | At startup: a database whose triggers could not be checked. |
| `audit.E005` | `audit.W005` | At startup: a database whose triggers are missing, disabled, or changed. Run `migrate`. |

The application also refuses to start when `AUDIT.CHECKPOINT_PUBLIC_KEY`
is not an Ed25519 public key in PEM, or cryptography is missing, and when
`AUDIT.DATABASE` is part of a shard group.

## History

A record's history is the rows about it, newest first, from every
audited database (Django admin calls it "History"). Rows name what they
are about in two ways:

- an explicit record has a `target`: `record("report.viewed",
  target="r-1")`;
- a committed event holds its aggregate's id in a field of `details`,
  such as `product_id`. Give events the id of the aggregate they are
  about, as `startdomain` does.

```python
from domainkit.contrib.audit.history import history

entries = await history(target="r-1")
entries = await history(aggregate=("product_id", str(product.id)), limit=20)
older = await history(target="r-1", before=entries[-1].position)
```

Each entry lists its `changes`, where the details allow. An explicit
record can say what changed, with `before` and `after` mappings:

```python
await record(
    "report.renamed", target="r-1", before={"name": "Q1"}, after={"name": "Q2"}
)
print((await history(target="r-1"))[0].changes)
```

```text
(FieldChange(field='name', before='Q1', after='Q2'),)
```

`before` and `after` are kept as written, so never put personal data in
them; pass it as `personal=`. Otherwise each field is compared with the
newest older entry of the history that has it, on the same page or the
one entry fetched past it. So events read best when they carry the fields
they change (`ProductRenamed(product_id=..., name=...)`). A field that
nothing older has is new (`before` is `None`).

Personal values stay the pseudonyms that the trail holds, also for
people who were forgotten: reading a history reads no keys. An
aggregate's id is matched as text, so query by ids, never by personal
fields (they are pseudonyms).

From the command line, each change is shown under its entry:

```bash
python manage.py audit history --target r-1
python manage.py audit history --aggregate product_id=6f1c7e6a-0d55-4a6e-9a3f-1b2c3d4e5f60
```

```text
2026-11-04 18:33:08  system  report.renamed  success  -
    name: 'Q1' -> 'Q2'
2026-11-04 18:33:08  system  report.viewed  success  -
```

**Reading by aggregate at scale.** A history by `target` reads its
index. A history by an aggregate's field reads the rows whose
`details[field]` is the id. Without an index of that field, each page
scans the whole trail (555 ms a page at 500,000 rows, and growing: every
committed event adds a row). Name the fields that you read histories by
in `AUDIT.HISTORY_FIELDS`. On PostgreSQL, `migrate` then keeps an index
`ix_domainkit_audit_log_history_<field>` of each,
`((details ->> 'field'), occurred_at, id)`, and drops those of fields no
longer named. Revisions (`makemigrations`) leave these indexes alone.
Building one holds writes to the trail until it is built. On a large
trail, make it first with `CREATE INDEX CONCURRENTLY` under the same name
and columns, and `migrate` keeps it. SQLite gets none (PERF-29).

```python
AUDIT = {"HISTORY_FIELDS": ["account_id", "entry_id", "request_id"]}
```

Staff read histories over HTTP, with `audit.view_history` (a permission
that `migrate` creates for the trail's `AuditEntry` model) and
`is_staff`, or as superusers. Mount the router next to the admin API:

```python
from domainkit.contrib.audit.api import router as audit_router

router.include_router(audit_router, prefix="/admin/audit")
```

`GET /admin/audit/history?target=r-1` and
`GET /admin/audit/history?field=product_id&id=...` answer pages of up to
`limit` (50, at most 200) entries, with `next_cursor`. Entries leave out
the client's address. The permission opens every record's history, and
querying by a field reads the whole trail: grant it as you would grant
read access to the trail.

## From the command line

| Action | What it does |
| --- | --- |
| `audit log [--actor A] [--action X] [--limit N] [--database D]` | Recent entries, newest first. Events are audited in the database where they commit, so `--database` reads another one. |
| `audit history --target T` or `--aggregate FIELD=ID` | One record's history, with what each entry changed. |
| `audit seal [--late]` | Seal new rows into each chain, and anchor each head. |
| `audit verify [--database D] [--export FILE]` | Check rows, seals, links, and anchors; with `--export`, write signed checkpoints (needs `CHECKPOINT_KEY`). |
| `audit check-export FILE --public-key PEM` | Check an export offline. |
| `audit prune --keep-days DAYS [--database D]` | Delete rows sealed more than `DAYS` ago, below a signed checkpoint. |
| `audit forget [SUBJECT ...] [--anonymous-days DAYS]` | Delete subjects' keys everywhere. |

`prune` and `forget` take `--operator` and are recorded as the
operator's actions ([operators](../reference/commands.md#operators)).

## In tests

`domainkit.contrib.audit.testing.audit_entries` reads the trail as
records, oldest first, so a test checks who did what from the trail
itself:

```python
from domainkit.contrib.audit.testing import audit_entries


async def test_the_maker_is_named(maker) -> None:
    ...
    [made] = await audit_entries(action="payments.transfer_made")
    assert made.actor == str(maker.id)
```

It filters by `action`, `actor`, and `target`, and reads `database` (by
default `AUDIT.DATABASE`). Each `RecordedEntry` has `occurred_at`,
`actor`, `action`, `outcome`, `target`, `request_id`, and `details`.
`domainkit.contrib.audit.tables` exports `audit_seals`, for tests and
tools that read the chain.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `AUDIT` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Where `record()` writes, what `audit log` reads by default, and where ledger states are kept. |
| `EVENTS` | `True` | Record every committed domain event. |
| `EXCLUDE` | `()` | Actions to skip, as shell patterns (`catalog.*`, `*.viewed`). |
| `HISTORY_FIELDS` | `()` | Fields of `details` whose histories are read by aggregate (`account_id`, `request_id`): on PostgreSQL, `migrate` keeps an index of each and drops those of fields no longer named ([History](#history)). Each is a letter or `_`, then up to 63 letters, digits, or `_`. |
| `LEDGER` | `None` | `LEDGERS` alias to anchor each chain's head on; `None` seals without anchoring. |
| `NAMESPACE` | `None` | Part of the anchors' ledger key, `domainkit/audit/<NAMESPACE>/<alias>`: a lower-case letter, then up to 31 letters, digits, `_`, or `-`. `None` keeps the key of 0.9.0, which every project on the ledger shares; staging and production refuse it. |
| `PREVIOUS_ANCHORS` | `True` | With `NAMESPACE`, also verify the heads anchored under `domainkit/audit/<alias>` before it was set. Turn it off when other projects anchored there too. |
| `ANCHOR_EVERY` | 1 hour | Anchor a new head at most this often. |
| `SEAL_EVERY` | 5 minutes | How often the worker seals new rows. |
| `SEALED_EXTERNALLY` | `False` | `audit seal` runs from a schedule of your own (cron), not in the worker: staging and production then do not report that nothing seals. |
| `REQUIRE_BASELINE` | `True` | Refuse to start in staging and production without a ledger, a namespace, a sealing schedule, and the triggers (the default since 0.10). `False` only warns, as 0.9 did. |
| `SEAL_LATE_WINDOW` | 15 minutes | How far before the newest sealed row a run looks for rows that committed late, up to a day. A row later than this stays unsealed until `audit seal --late`, and `audit verify` reports it once it is a day old: keep it above your longest transaction plus the clock skew between hosts. |
| `SEAL_BATCHES` | `100` | Batches of 1,000 rows one run seals at most (1 to 10,000); the rest wait for the next run. |
| `SEAL_BACKLOG_WARNING` | `100000` | Rows a run leaves unsealed past which it logs a warning. |
| `CHECKPOINT_KEY` | `None` | The Ed25519 private key, in PEM (at most 1,024 characters), that signs checkpoints: those `audit prune` prunes below, and those `audit verify --export` writes. Read by those two commands alone, so set it only where they run, not in the application's settings. `None`: neither runs. |
| `CHECKPOINT_PUBLIC_KEY` | `None` | The public half of `CHECKPOINT_KEY`, in PEM (at most 1,024 characters): what `audit verify` checks stored checkpoints with. Checked when the application starts; signing refuses a private key it is not the half of. `None`: no stored checkpoint verifies, so a pruned trail does not. |
| `MIN_RETENTION` | `30 days` | How old a seal must be before `audit prune` may cut there (1 day to 3,650 days): a shorter `--keep-days` is refused, and a checkpoint of a younger seal starts nothing in `audit verify`. Under `AUTH.PRESET = "pci-dss-4"` it is 365 days at least, whatever it says (PCI DSS v4.0 10.5.1); `--profile fintech` sets 365 days. |

## On PostgreSQL and SQLite

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Append-only tables | Triggers, made by `migrate` and checked at startup. | None: the application only inserts, and the chain shows changes. |
| Sealing | One sealer at a time (an advisory lock); dated by the database's clock. | One writer at a time; dated by the host's clock. |
| A subject's first key | Under a transaction advisory lock of the subject. | Under the database's write lock. |
| History indexes | One per field of `HISTORY_FIELDS`. | None. |
| Tenant schemas | One trail per tenant schema. | None. |

## Upgrading

Each of these added a table, a column, or an index. Run
`makemigrations` and `migrate` in each project that installs audit:

- the seals table's `late` column;
- the checkpoints table, and the ledger states table
  ([databases.md](../databases.md#ledgers-immudb));
- the trail's index on `target`, for histories.

## Common mistakes

- **Putting a name or an email address in `target` or `actor`.** They
  are not pseudonymized. Use ids, and pass personal values as
  `personal=`.
- **Leaving `AUDIT.NAMESPACE` unset on a shared ledger.** Projects then
  hold one another's anchors back. Set a namespace per project and
  environment.
- **Running `audit seal` by hand while the worker seals.** One of them
  gives way; nothing is lost, but you see a warning. Choose one.
- **Keeping `CHECKPOINT_KEY` in the application's settings.** A person
  who reads them could sign a checkpoint. Set it only where `audit prune`
  runs.
- **Putting personal data in `before` and `after`.** They are kept as
  written.

## See also

- [Privacy](../privacy.md): export, erase, and purge.
- [Ledgers (immudb)](../databases.md#ledgers-immudb): the ledger that
  anchors the chain.
- [Tenancy](../tenancy.md): tenant schemas.
- [Commands: operators](../reference/commands.md#operators): who ran a
  command.
- [Checks](../reference/checks.md): every check id.
