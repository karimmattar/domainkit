# 0100. Guard credentials and grants from `loaddata` and `flush`, and load new rows only

- Status: Accepted
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
- **Named operators where deployed.** In staging and production both
  commands need `--operator` or `DOMAINKIT_OPERATOR`, beside `--force`;
  the login name (`operator_source` `system`) is refused. Other operator
  commands keep the rule of ADR 0070's amendment: they run unattended.
- **The record.** `operator.loaddata` holds the fixture's name, the
  SHA-256 of the bytes the command read (the file is read once), the
  database, the totals, each table with its rows and replaced rows, and
  whether `--replace` and `--include-guarded` were given;
  `operator.flush` the rows emptied per table, the tables kept, and
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
