# 0100. Guard credentials and grants from `loaddata` and `flush`, and load new rows only

- Status: Accepted; amended twice on 2026-10-28 (at the end)
- Date: 2026-10-26

## Context

`loaddata` and `flush` write the tables directly, as the schema owner,
behind the aggregates: no version check, no rule, no domain event. Up to
0.10 (audit 0.9.0, DATA-12):

- `loaddata` put each row whose primary key was stored in place of the
  stored one, in every table a fixture may carry. The auth tables were
  among them: a fixture of `domainkit_users_permissions`,
  `domainkit_users_groups`, `domainkit_groups`, and
  `domainkit_group_permissions` rows gave a user every permission, and no
  `auth` event, principal stamp, or audit row named the grant. A fixture
  of `domainkit_users` rows was stopped only by an accident: the password
  column's type cannot bind the text a fixture holds, and the command died
  with a `StatementError` traceback.
- `flush` emptied every table but the append-only ones: users, tokens,
  TOTP authenticators, and recovery codes went with the data, so a flush
  turned off every second factor along with the accounts.
- Both were recorded (`operator.loaddata`, `operator.flush`, since 0.9.1,
  ADR 0070), but the row held only totals and the file's name: not which
  tables, nor what the file held when it ran. In staging and production
  `--force` was enough; the operator could be whatever login name the
  process had.

What tests and docs relied on, measured at 0.10: of the 30 tests of the
data commands, 16 run `loaddata`, and 4 of those rely on a load replacing
stored rows (`test_loading_replaces_rows_and_says_so_first`,
`test_loading_updates_stored_rows`, `test_loading_works_in_chunks`, and
the second load of `test_tables_without_other_columns_or_keys`); one more
writes content types. No test outside them, no tutorial page, and no
walkthrough runs `loaddata` or `flush`. The reference documents
replacement in one sentence.

The options:

- Refuse guarded tables per row kind (a new superuser, a changed hash):
  a list of what is dangerous, which every custom user model and new
  table would need to extend.
- Skip guarded tables silently in `loaddata`: a load that half happens.
- A table marker read by the core (`info={"guarded": True}`), as
  `fixtures`, `flush`, and `append_only` already are, set by each app on
  its tables; an option that names the intent to reach them; and loads
  that only insert by default, so a stored row is never replaced unless
  asked.
- For `flush`, refuse the whole command while a guarded table has rows,
  or keep the guarded tables and empty the rest.

## Decision

- **Guarded tables.** `info={"guarded": True}` marks a table whose rows
  are logins, privileges, or secrets. domainkit marks auth's users (the
  default table, and the `AUTH.USER_TABLE` a project names, when auth
  links it), groups, permissions, group permissions, the user-group and
  user-permission links, tokens (logins, sessions, personal tokens),
  TOTP authenticators, and recovery codes; content types, since
  permissions hang from them (replacing one re-points each permission on
  it, deleting one cascades into them); object grants
  (`objectpermissions`); and dynamic settings, which may hold a secret
  such as a provider's key. Not guarded: pending signups (a signup opens
  nothing without the signed link), feature flags (they gate features,
  not access), webhook subscriptions and deliveries, the outbox, and
  idempotency keys (state: never loaded, and emptying them grants
  nothing). The audit trail stays outside both commands whatever the
  options, as before.
- **`loaddata`** refuses, before writing anything, a fixture that writes
  a guarded table, unless `--include-guarded`: an added row there is a
  new login or grant, so inserts are refused too, not only replacements.
- **Loads only insert.** A row whose primary key is stored refuses the
  fixture, before anything is written, unless `--replace`. The statement
  is a plain `INSERT`, so a row stored after the plan was counted fails
  the load on the database's constraint, in its one transaction. With
  `--replace`, rows replace stored ones, as in 0.10.
- **`flush`** keeps the guarded tables, and every table they reference
  (deleting a parent would fail, or cascade into the kept rows), unless
  `--include-guarded`. It lists them as `kept (guarded)`. Keeping, not
  refusing: a development reset keeps working, and the default leaves
  every login and second factor in place.
- **What goes with a flushed object goes.** A grant goes with its object
  when the ORM deletes it (ADR 0026), so an object stored later with the
  same id starts bare; `flush` deletes with Core statements, behind that
  hook, and ids are handed out again (sequences restart, SQLite reuses
  row ids, fixtures and seeds bring their own). A kept table whose rows
  are about other tables' objects says which column names their model
  (`info={"about_model": "content_type"}`, on object grants); `flush`
  deletes its rows about the models whose tables it empties, in the same
  transaction, and says how many in the plan (`kept (guarded); delete N
  rows about emptied tables`) and in its record. Keeping the objects'
  tables instead, whenever grants on them are kept, would have made
  `flush` keep the data it exists to delete. The security review of 0.11
  found the gap (SEC-11a-4): before this, a flush kept Ada's grant on
  her ticket 1 for Bob's next ticket 1.
- **Named operators where deployed.** In staging and production both
  commands need `--operator` or `DOMAINKIT_OPERATOR`, beside `--force`;
  the login name (`operator_source` `system`) is refused. Other operator
  commands keep the rule of ADR 0070's amendment: they run unattended.
- **The record.** `operator.loaddata` holds the fixture's name, the
  SHA-256 of the bytes the command read (the file is read once), the
  database, the totals, each table with its rows and replaced rows, and
  whether `--replace` and `--include-guarded` were given;
  `operator.flush` the rows emptied per table, the tables kept, the rows
  of kept tables deleted with their objects (`about`), and
  `--include-guarded`. Table names and counts only: no key, value, or
  subject. The core writes them through `OperatorRecorder`, as before
  (ADR 0080): wherever `domainkit.contrib.audit` is installed, in every
  environment.

## Consequences

- Restoring users, permissions, or grants from a fixture, or emptying
  them, is a deliberate step: `--include-guarded`, recorded as such. A
  project marks its own tables of credentials (API keys, its own grants)
  with `info={"guarded": True}`.
- A fixture that overlaps stored rows fails where it loaded before;
  `--replace` restores 0.10's behaviour. Development scripts that load
  the same seed fixture twice need `--replace` or a `flush` first.
- `flush` in development keeps the superuser, and the content types and
  permissions `migrate` wrote; `flush --include-guarded` empties them as
  0.10 did.
- Deployment runs of both commands that relied on the login name stop
  with "Name the operator", and need `DOMAINKIT_OPERATOR` or
  `--operator`.
- A fixture's content can be matched to the trail later: its SHA-256 is
  in the row.
- What would change this: a domain-level import (fixtures through units of
  work, with events), which would make the guard a rule of each
  aggregate rather than a table marker.
- Left as found: a user row in a fixture still cannot be loaded, even
  with `--include-guarded` (the password column's type takes a
  `PasswordHash`, not the text a fixture holds, and `dumpdata` writes it
  as `{"value": ...}`). That is a bug of the fixture format, not a guard.

## Amendment (2026-10-28, 0.12): `dumpdata`, the count a load records, and users in fixtures

Three gaps 0.11 left, each proved by a test that failed on 0.11.1
(`test_data_guards.py`):

- **`dumpdata` copied guarded tables out unrecorded.** Any operator could
  write users with their password hashes, dynamic settings (which may be
  secret), and grants to a file or a terminal, and no row said so.
  `dumpdata` now treats them as `loaddata` does: a guarded table, named
  or reached by its domain's label, is refused without
  `--include-guarded`; a dump of every table leaves them out and says so
  on standard error, rather than failing, since that is how it already
  left out state, and a project with auth would otherwise have no bare
  `dumpdata` at all. With the option, the dump is recorded as
  `operator.dumpdata` (the tables, their row counts, the output's name,
  and the SHA-256 of the text written: no row), and needs a named
  operator in staging and production. A dump without guarded tables is
  not recorded, as reads are not: it holds nothing a load of it could not
  write back without the option either.
- **`loaddata --replace` recorded the plan's count.** The counts came
  from `plan_load`, read before the load's own transaction: a row stored
  in between was replaced but recorded as added (0 replaced, where 1
  was). The load now takes its tables' write lock first (`LOCK TABLE ...
  IN SHARE ROW EXCLUSIVE MODE` on PostgreSQL, which lets readers in and
  keeps other writers, another load among them, out; `BEGIN IMMEDIATE` on
  SQLite), counts the stored keys inside that transaction, and the record
  holds those counts. The plan is still printed first, as advice.
  Counting from `RETURNING` (`xmax <> 0` on PostgreSQL) was the other
  candidate: no lock, but nothing on SQLite tells an insert from an
  update, and it rests on PostgreSQL's storage internals.
- **A user row did not round-trip.** `dumpdata` wrote the password hash
  as the value object's JSON (`{"value": ...}`), which `loaddata`
  refused, even with `--include-guarded`. The bug was in the fixture
  format, not the guard: any single-field value object's column
  (`ValueObjectType`, a project's `ProductName` too) had it. Such columns
  now hold the field's value in a fixture, as the database stores it, and
  loading makes the object from it, so the object's checks still apply: a
  raw password in place of a hash is refused (`PasswordHash` takes only
  hashes). `PasswordHashType` became a `ValueObjectType`, keeping its
  constructor and repr (`PasswordHashType(length=255)`), so migrations
  that name it are unchanged. Fixtures 0.11 wrote, with `{"value": ...}`,
  load too. The guard is not opened: loading a user still needs
  `--include-guarded`.

## Amendment (2026-10-28, 0.12): password hashes no hasher makes, and dumps of personal data

The security review of 0.12 found two gaps in the first amendment, each
with a proof that failed on the merged branch (`test_sec12a_3_*`,
`test_sec12a_7_*`):

- **A fixture's password hash was checked for its shape only**
  (SEC-12a-3). `PasswordHash` takes any `<algorithm>$...`, so under
  production's hashers `loaddata --include-guarded` stored
  `plaintext$hunter2`, a password in clear, and the test hasher's
  one-iteration hash, a login a copy of the table gives up at once (it is
  made again only at that user's next login). `ValueObjectType` gains
  `loaded()`, the object a fixture's field makes, which a column type may
  make stricter; `PasswordHashType`'s refuses a hash no hasher of
  `AUTH.PASSWORD_HASHERS` identifies, or one it would make again more
  strongly (`must_update`), saying why but never the hash. Unusable
  passwords load as before. Re-hashing on load was the other option: it
  needs the password, which a fixture does not hold.
- **A dump of personal data was a read, and reads were not recorded**
  (SEC-12a-7). The first amendment recorded only dumps of guarded tables,
  "as reads are not"; but `privacy export` of one person, a read, is
  recorded, and `dumpdata patients_patients` copied every patient out
  with no one named. Where deployed, a dump of a table whose model has
  fields marked `Personal` (`domainkit.ddd.personal`, as `privacy` reads
  them) now needs a named operator and is recorded as `operator.dumpdata`,
  with those tables (`personal_tables`). It is not refused, and needs no
  `--include-guarded`: personal data is what an operator dumps to move or
  inspect it, and the record is what was missing. In development and
  tests nothing changes, as with the other operator commands.


## Amendment (2026-10-29, 0.12.1): rows of one table that name different columns

A load wrote each table's rows as one `executemany` per `CHUNK`, which
takes its columns from the batch's first row. A hand-written fixture
whose first row left out a column, and a later row gave it, lost that
value: the load said it wrote the row, and the column was null. The other
way round, the load failed with SQLAlchemy's `InvalidRequestError`.
`dumpdata` writes every column, so only hand-written fixtures met it.

Two fixes were tried against the fixtures the test suite loads, all of
which name the same columns in every row of a table:

- **Refuse rows that name different columns.** A clear error instead of
  a lost value, but a hand-written fixture that leaves out what it has no
  value for, a use `--replace`'s tests already make, would have to spell
  out every column as null.
- **Write each run of rows naming the same columns as its own batch.**
  Every fixture that loaded still loads, the two that did not now do, and
  a fixture `dumpdata` wrote is written in as many statements as before.

Loading takes the second. Runs are consecutive, not one batch per set of
columns, so rows keep their order: in a table that references itself, a
row is written before the rows that reference it, as the fixture gives
them. A fixture that alternates its columns row by row costs one
statement per row. The counts a load prints and records are unchanged:
they are of rows, not statements.
