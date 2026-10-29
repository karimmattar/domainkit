# Commands

Run as `python manage.py <command>`; `init` runs before a project exists, as
`domainkit init` (or `python -m domainkit init`, where the script is not on
the `PATH`). Domains add their own in `management/commands/<name>.py`
with a `Command(BaseCommand)` class; `handle` may be `async`.

| Command | Options | Purpose |
| --- | --- | --- |
| `init` | `name`, `[directory]` | Create a project: base, local, test, and production settings, routes, `manage.py`, `.env`, and a first test. The name sets the environment prefix; the directory (default `./<name>`) must be empty or missing. What it writes passes the project's own `ruff check` and `ruff format --check`; its dev group installs ruff, which formats what the generators write. |
| `check` | `--deploy` | Settings, domains, routes and their access rules, middleware, layer rules, and a single migration head. It warns about route handlers and dependencies of the installed domains written as `def`, which run on FastAPI's thread pool ([runtime.md](../runtime.md#finding-blocking-code)); in staging and production it also lists [weak choices](checks.md) as warnings. Every finding has an id; `--deploy` fails on warnings not in `SILENCED_CHECKS` too, and runs only in staging and production. |
| `broker` | `check\|declare`, `[ALIAS ...]` | Connect to brokers (all by default), or declare the exchange and every queue the worker's tasks use on `MESSAGING.BROKER`. |
| `compilemessages` | `[DIRECTORY ...]`, `-l/--locale`, `--check` | Compile translation catalogs (`.po`) to `.mo` files: in the directories named, or in `LOCALE_PATHS` and each installed domain's `locale` directory. `--check` writes nothing and fails if one is missing or out of date ([i18n.md](../i18n.md)). |
| `cache` | `check\|clear`, `[ALIAS ...]`, `--all` | Ping caches (all by default), or delete every key of the named caches, all versions, in their namespace only. Clearing needs aliases or `--all`. |
| `startdomain` | `name`, `--aggregate`, `--plural`, `--parent`, `--tests-dir`, `--manual`, `--minimal` | Generate a bounded context and its tests ([resources.md](../resources.md#generators)); `--minimal`, a smaller one: an aggregate with a name, create and rename, a table, a resource, and two tests. In a project laid out as `init` makes it, it also adds the domain to `INSTALLED_DOMAINS` and mounts its router; `--manual`, or a layout it does not recognise, prints those steps instead. Its next steps end with a login and a first `curl` of the resource, at `/api/v1/<name>/<plural>`. |
| `startaggregate` | `domain`, `aggregate`, `--field name:kind[:options][?]`, `--plural`, `--parent`, `--tests-dir` | Add an aggregate to a domain: model, events, commands, use cases, table, repository, resource, fakes, and tests. `--help` lists the field kinds. |
| `addcommand` | `domain`, `aggregate`, `command`, `--field`, `--event`, `--plural`, `--parent`, `--tests-dir` | Add a task command: the aggregate's method and event, the command, its use case, its route, and tests. |
| `addquery` | `domain`, `aggregate`, `query`, `--by`, `--path`, `--plural`, `--parent`, `--tests-dir` | Add a query that finds aggregates by fields: the message and handler (`application/queries.py`), its route, and tests. |
| `addevent` | `domain`, `aggregate`, `event`, `--field`, `--no-handler`, `--plural`, `--parent`, `--tests-dir` | Add a domain event, and a durable handler subscribed in `apps.py`, with its test. |
| `makemigrations` | `-m/--message`, `--check`, `--merge`, `--empty`, `--database` | Write a revision per target with changes; `--check` fails if one is missing; `--merge` joins two heads; `--empty` writes a revision with nothing in it, to fill in by hand (a data migration; it needs `--database` when there are several targets). Without `--database`, every target. The message is one line, without backslashes or triple quotes. |
| `migrate` | `[revision]`, `--downgrade`, `--allow-data-loss`, `--database`, `--plan`, `--sql`, `--from`, `--fake` | Apply revisions (or revert down to one) on every database of every target, then create partitions. A revision id needs `--database` when there are several targets. `--plan` lists the revisions that would run; `--sql` prints their SQL, rendered offline by Alembic, from the database's revision or `--from`; `--fake` records the revision without running anything, for a schema made by other means. None of the three changes the schema, but `--fake --downgrade` still needs `--allow-data-loss` in staging and production. |
| `squashmigrations` | `[end]`, `--start`, `--database`, `-m/--message` | Replace a target's revisions, `--start` (default: the first) to `end` (default: the head), with one; see [Squashing migrations](#squashing-migrations). |
| `showmigrations` | `--database` | Applied and pending revisions; a shard group lists each database. |
| `partitions` | `--database`, `--prune`, `--dry-run`, `--operator` | Create the current and upcoming partitions of partitioned tables; `--prune` drops those past `retain`, and is audited ([operators](#operators)). |
| `runserver` | `[addr:port]`, `--noreload`, `--server uvicorn\|granian` | Development server, on uvicorn or Granian (the `granian` extra). Production runs the server directly ([Server limits](servers.md)). |
| `shell` | `-c/--command`, `--no-imports` | An asyncio Python console, where `await` works at the top level, with settings, the registry, connections, `asyncio`, and every installed domain's models, repositories, and units of work by name (unless `--no-imports`). `-c` runs code and exits. |
| `dbshell` | `--database`, `--owner`, `--allow-production`, `-- [ARG ...]` | Open `psql` or `sqlite3` on an alias, as its `URL`'s user (`--owner`: `MIGRATION_URL`'s). The connection and password reach `psql` in libpq's environment variables, never on its command line; TLS follows `SSLMODE` and its files. Production refuses without `--allow-production`. Arguments after `--` go to the client. |
| `diffsettings` | `--all` | The settings that differ from the defaults; `--all` lists every one, defaults marked `###`. Secrets stay hidden as in `repr(settings)`, and so do entries of free-form settings named like secrets (`password`, `token`, `key`, ...) and passwords in URLs. |
| `dumpdata` | `[TABLE_OR_DOMAIN ...]`, `--database`, `--output`, `--indent`, `--include-guarded`, `--operator` | Write rows as a JSON fixture; see [Data: fixtures, flush, and seeds](#data-fixtures-flush-and-seeds). |
| `loaddata` | `FIXTURE ...`, `--database`, `--dry-run`, `--force`, `--replace`, `--include-guarded`, `--operator` | Load fixtures, saying first what each table gets and how many of its rows are stored already. A row whose primary key is stored is refused without `--replace`; a fixture that writes a guarded table (users, permissions, grants, ...) without `--include-guarded` ([guarded tables](#data-fixtures-flush-and-seeds)). Staging and production refuse without `--force` and a named operator. Each load is audited with its tables and the fixture's SHA-256 ([operators](#operators)). |
| `flush` | `--database`, `--dry-run`, `--force`, `--include-guarded`, `--operator` | Delete every row but the audit trail's and, without `--include-guarded`, the guarded tables', saying first what goes, then rerun the `migrated` hooks (content types, permissions). Staging and production refuse without `--force` and a named operator. Audited, with its tables ([operators](#operators)). |
| `seed` | `[LABEL ...]` | Run installed domains' `on_seed` hooks, in `INSTALLED_DOMAINS` order. |
| `openapi` | `--output`, `--indent`, `--diff OLD` | Print or write the OpenAPI schema; `--diff` lists breaking changes since `OLD` and exits 1 if any (see [The API surface](responses.md#the-api-surface)). |
| `runworker` | `--once`, `--metrics-port`, `--metrics-address`, `--database`, `--queue`, `--no-relay` | Run outbox messages (messaging): one worker per outbox, every hosting database by default; `--queue` (repeatable) limits them to some queues. With `MESSAGING.BROKER`, relay to the broker and run what it delivers; `--no-relay` only consumes. `--once` always works from the outbox. Whatever it serves, the process runs at most `MESSAGING.CONCURRENCY` messages at once. |
| `messages` | `--pending\|--locked\|--dead`, `--queue`, `--limit`, `--retry ID\|all`, `--cancel ID`, `--database`, `--operator` | Count an outbox's messages and show each queue's lag, list them, requeue dead letters (not those of `sensitive` tasks, whose arguments were blanked), or cancel a message no worker runs; retries and cancels are audited (messaging; [operators](#operators)). |
| `webhooks` | `subscriptions\|deliveries\|replay`, `[ID ...]`, `--failed`, `--status`, `--subscription`, `--limit` | List webhook subscriptions or deliveries, or send finished deliveries again (webhooks). |
| `auditlog` | `--actor`, `--action`, `--limit`, `--database`, `--target`, `--aggregate FIELD=ID` | Read the audit trail, or one record's history with what each entry changed (audit; see [contrib.md](../contrib/audit.md#history)). |
| `auditseal` | `--late` | Seal new audit rows into each database's hash chain and anchor its head on `AUDIT.LEDGER`; `--late` also seals rows that committed later than `AUDIT.SEAL_LATE_WINDOW`, flagged as sealed late (audit). |
| `auditverify` | `--database`, `--export FILE`, `--check-export FILE --public-key PEM` | Check the audit trail against its seals, checkpoints, and anchors; fails if anything changed. `--export` then writes signed checkpoints of every trail for a third party (`AUDIT.CHECKPOINT_KEY`, read by this and `auditprune` alone); `--check-export` checks such a file offline, with the public key alone (audit). |
| `auditprune` | `--keep-days`, `--database`, `--operator` | Delete audit rows sealed more than `--keep-days` (at least `AUDIT.MIN_RETENTION`) ago below a signed checkpoint recorded on the ledger, which `auditverify` then starts the chain from; refused if the chain up to it does not verify. Each run is audited (audit; [operators](#operators)). |
| `auditforget` | `subject ...`, `--anonymous-days`, `--operator` | Make people's pseudonyms in the audit trail unlinkable, in every audited database; each run is audited (audit; [operators](#operators)). |
| `privacy` | `[--operator NAME]`, then `export --subject ID [--output FILE]`, `erase --subject ID [--reason] [--noinput]`, `purge [--dry-run]` | Export or erase what every installed domain holds about a person, from `Personal` markers on aggregates, or apply the domains' retention policies; each is audited, as the operator (privacy; see [privacy.md](../privacy.md), [operators](#operators)). |
| `evidence` | `--subject`, `--verify KEY`, `--limit` | List a subject's evidence, or verify one record (evidence). |
| `createsuperuser` | `--<USERNAME_FIELD>`, `--<field>` per `REQUIRED_FIELDS`, `--noinput` | Create a superuser, asking for what the options leave out; `--noinput` reads `<PREFIX>SUPERUSER_<FIELD>` (auth; see [auth.md](../auth.md)). |
| `changepassword` | `[username]` | Set a user's password, asked twice and validated; the user's tokens end (auth). |
| `createtoken` | `username`, `--name`, `--days`, `--no-expiry`, `--scope` | Print a new token for a user's scripts, shown once, limited to the `--scope` permissions if given (auth). |
| `cleartokens` | `--user` | Delete expired tokens, or every token of one user (auth). |
| `resetmfa` | `username` | Turn a user's second factor off, and end their sessions (auth). |
| `addcertificate` | `username`, `certificate`, `--name` | Let a client certificate log in as a user, by its SHA-256 (auth). |
| `flags` | `list`, `set NAME [--everyone {on,off,rules}] [--percentage N] [--users USER_ID ...] [--groups GROUP ...] [--note TEXT]`, `delete NAME`, `settings`, `setting KEY [VALUE] [--reset]` | List and change feature flags and dynamic settings (flags; see [contrib.md](../contrib/flags.md#flags-feature-flags-and-dynamic-settings)). |

## Operators

Commands that change or erase stored data as a person, not as the
application, say who runs them and leave a row in the audit trail (with
`domainkit.contrib.audit` installed): `messages --retry` and `--cancel`,
`auditforget`, `auditprune`, `loaddata`, `flush`, `partitions --prune`, and `privacy`;
`dumpdata --include-guarded`, which copies credentials out (0.12);
and so do those that change credentials, flags, settings, and deliveries:
`resetmfa`, `createtoken`, `changepassword`, `createsuperuser`,
`addcertificate`, `cleartokens`, `flags`, and `webhooks`. Their listings
(`flags list`, `webhooks deliveries`, ...) take the operator too but record
nothing; `evidence`, `auditlog`, and `auditverify` only read and take none.
The operator is, in this order:

1. `--operator NAME` (before the action for `privacy`:
   `privacy --operator alice purge`, and for `flags`: `flags --operator
   alice set beta --everyone on`);
2. the `DOMAINKIT_OPERATOR` environment variable;
3. the login name `getpass.getuser()` reports: the `LOGNAME`, `USER`,
   `LNAME`, or `USERNAME` environment variable, else the name of the
   process's user id. Whoever runs the command can set any of these, so
   this names the operator no better than the other two;
4. without a login name an operator may have (a container under an
   arbitrary uid, with no passwd entry; a Windows domain account; a name
   with a space), `uid-<n>`, the process's user id, or `unnamed` where
   there are no user ids. Commands run from cron or an entrypoint keep
   running, as in 0.9.0, and the name they had is neither kept nor shown.

A name is 1 to 64 letters, digits, `.`, `_`, `@`, `+`, or `-`, starting
with a letter or digit. A bad `--operator` or `DOMAINKIT_OPERATOR` is
refused before the command runs, since the operator typed it. The last two
sources are recorded as `operator_source` `system`, so the trail shows
that nobody gave a name; and in a container that runs as `root` or `app`,
or under a uid, they name nobody: set `DOMAINKIT_OPERATOR` from your
access tooling, or pass `--operator`. In staging and production
`loaddata` and `flush` need a name from one of the first two (0.11), and
so does `dumpdata --include-guarded` (0.12); the other commands keep running unattended. Prefer a staff id or handle to an
email address: the trail keeps what it is given for good. The name is what
the operator says: it attributes an action, it does not authenticate
anyone, so keep who can run `manage.py` against production as narrow as
who can reach its database.

The command runs as the actor `operator:<name>`, so what it records itself
(`privacy.erase`, `privacy.purge`, `auth.mfa_disabled`,
`auth.token_created`, `auth.password_changed`, `auth.user_created`,
`auth.user_logged_out`, `flags.flag_changed`, `flags.setting_changed`)
names the operator instead of `system`. An action that records nothing
itself is recorded once it ends as `operator.<command>`
(`operator.messages.retry`, `operator.messages.cancel`,
`operator.auditforget`, `operator.auditprune`, `operator.loaddata`, `operator.flush`,
`operator.partitions.prune`, `operator.webhooks.replay`), `success` or `failure` (with the error's
class), with what it acted on and counts, and `operator_source`: `option`,
`environment`, or `system`. `operator.loaddata` names the fixture file,
its SHA-256, and each table with its rows and how many it replaced;
`operator.flush` each table emptied with its rows, the tables kept, and
the rows of kept tables it deleted with their objects (`about`: object
grants); both say whether `--replace` and `--include-guarded` were given. Dry runs,
listings, and refusals before the action (no `--force`) are not
recorded. A successful action whose row cannot be written fails the
command.

## Data: fixtures, flush, and seeds

A fixture is one JSON document, its tables parents first:

```json
{"format": "domainkit.fixture", "version": 1, "tables": [
  {"table": "shop_products", "rows": [{"id": "6f1c7e6a-...", "name": "Lamp", "version": 1}]}
]}
```

Numbers and text are written as they are; decimals, times, durations, ids,
and enums as text; bytes as base64; JSON columns as JSON; a single-field
value object (a password hash, a project's `ProductName`) as its field,
which loading makes into the object again, so the object's own checks
refuse what it would not hold; one around another (`Code(sku: Sku)`) as
the innermost field, made into each object from the inside out. A password hash loads only if a hasher of
`AUTH.PASSWORD_HASHERS` made it and would not make it again more strongly:
a password in clear, or the test hasher's single iteration under
production's hashers, is refused. Fixtures from 0.11, which wrote such an
object as `{"value": ...}`, and from 0.12, which wrote the inner object of
two that way, load the same. `loaddata`
converts each value back by its column's type, and refuses one that does
not fit without quoting it. A number with a fraction reaches a decimal
column with every digit, as written (`1234567890123456.78`), never through
a float; other columns get it as a float. Rows load in one transaction,
and on PostgreSQL serial ids then continue after the highest one loaded.

A load only adds rows: a row whose primary key is stored refuses the
whole fixture, before anything is written, unless `--replace` is given,
and then it replaces the stored row. The load holds its tables' write
lock while it counts and writes (`SHARE ROW EXCLUSIVE` on PostgreSQL,
`BEGIN IMMEDIATE` on SQLite), so its record (`operator.loaddata`) holds
the rows it replaced, a row stored since the plan was printed included. A row that replaces another bypasses
the aggregate's version, its rules, and its events: keep `--replace` for
development data.

Guarded tables hold logins, grants, and secrets: users (the
`AUTH.USER_TABLE` too), groups, permissions and their links, tokens,
second factors and recovery codes, content types (permissions hang from
them), object grants, and dynamic settings (which may be secret), and any
table with `info={"guarded": True}`. A row added there is a login or a
privilege, so `loaddata` refuses a fixture that writes one unless
`--include-guarded` is given, and `flush` keeps them, and every table they
reference, unless it is given. `dumpdata` leaves them out of a dump of
every table, and says so on standard error, and refuses a table or domain
named that holds one, unless `--include-guarded` is given: a dump of
users holds their password hashes. A dump with guarded tables is recorded
(`operator.dumpdata`: the tables, their row counts, and the SHA-256 of
what was written), and in staging and production needs an operator named
by `--operator` or `DOMAINKIT_OPERATOR`. So does, in staging and
production, a dump of a table whose model has fields marked `Personal`,
recorded with those tables (`personal_tables`), as `privacy export` of one
person is recorded: it copies everyone out. The audit trail is never loaded or
flushed, guarded or not. A kept table whose rows are about other tables'
objects names the column of their model key (`info={"about_model":
"content_type"}`, as object grants do): `flush` deletes its rows about
the models whose tables it empties, so a grant goes with its object, as
with a repository's `remove`, and an object given a flushed one's id
starts bare.

State is never dumped or loaded: the audit trail (a copy of its keys would
outlive `auditforget`), tokens, the outbox, idempotency keys, webhook
subscriptions (they hold signing secrets: set them up in each environment)
and deliveries, and any table with `info={"fixtures": False}`. A fixture can still hold personal data and
password hashes: keep it as safe as the database. `dumpdata --output`
writes a new file only its owner can read (mode `0600`), and refuses a
path that exists, a symbolic link too, so a link planted in a shared
directory cannot redirect it: delete an old fixture before dumping again.

`flush` keeps append-only tables, those with `info={"append_only": True}`
or, on PostgreSQL, triggers of their own, and those with
`info={"flush": False}`: the audit trail survives it. It keeps guarded
tables too, listed as `kept (guarded)`, unless `--include-guarded`, but
not the object grants on the rows it deletes: the plan says `kept
(guarded); delete N rows about emptied tables`.
Serial ids start again on PostgreSQL.

Each command works on one alias (`--database`, default `default`), as the
schema owner (`MIGRATION_URL` when set).

`seed` runs the `async` hooks installed domains registered with
`domainkit.apps.hooks.on_seed` in `ready()`: sample
data for development, or defaults a deployment starts with. Write rows
through the domain's units of work, and make a second run harmless.

## Squashing migrations

`squashmigrations` writes one revision that does what a run of a target's
revisions did: its `upgrade` runs each one's `upgrade`, oldest first, and
its `downgrade` each one's `downgrade`, newest first. Each body is copied
as written, comments included, into a function of its own, and imports are
merged. Then the run's files are deleted.

The new revision keeps the id of the newest revision it replaces, and the
`down_revision` of the oldest. So databases at or past the newest revision,
and revisions written after it, need nothing. A database at a revision
inside the run would be at one that no longer exists:

- The command refuses while a database in `DATABASES` is there.
- It cannot see other environments: deploy the squash only once every
  database (staging, production, developers' copies) is at or past the
  newest revision of the run.

It also refuses what it cannot copy and keep correct:

- a merge, a branch, a branch label, or `depends_on` in the run;
- code outside `upgrade` and `downgrade` other than imports (move helpers
  into them first), or two revisions importing different things under one
  name;
- a `downgrade` that raises: the squashed revision could not be reverted
  either.

Comments outside the two functions are not kept. Check the result as any
revision: the test database migrates up, down, and up again.
