# Commands

A management command is a task you run from a terminal: you create a
project, apply migrations, make a user, or read the audit trail. You run
each one as `python manage.py <command>`. This page lists every command
that domainkit and its optional apps bring, with every option and an
example.

On this page you find:

- how to run a command, and how to write one of your own;
- every command, grouped by what it works on, with its options;
- how commands that change stored data name their operator;
- how fixtures, `flush`, and `seed` treat your data;
- how `squashmigrations` joins revisions.

## Running a command

Run a command from the project's directory, where `manage.py` is:

```bash
python manage.py check
```

```text
System check identified no issues. Domains: messaging, contenttypes, auth.
```

Every command answers `--help` with its options. A command of several
actions also answers `--help` after an action:

```bash
python manage.py migrate --help
python manage.py messages list --help
```

`init` is the one command that runs before a project exists. You run it
as `domainkit init`, or as `python -m domainkit init` where the
`domainkit` script is not on the `PATH`.

A command that fails prints `Error: <message>` on standard error and
exits with status 1. A command that checks something (`check --deploy`,
`makemigrations --check`, `openapi --diff`, `audit verify`) also exits
with status 1 when it finds a problem, so a CI job can stop on it.

## Writing a command of your own

A [domain](../learning.md#words) adds a command in
`management/commands/<name>.py`. The module holds a class `Command`, a
subclass of `BaseCommand`. Its `handle` method does the work, and may be
`async`:

```python
from argparse import ArgumentParser

from domainkit.management import BaseCommand


class Command(BaseCommand):
    help = "Count the products of the catalog."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--active", action="store_true")

    async def handle(self, *, active: bool) -> None:
        self.write(f"Counting {'active' if active else 'all'} products.")
```

A domain command with the name of a built-in command replaces the
built-in one.

A command that does more than one thing has one shape:
`manage.py <command> <action> [options]`. Subclass `ActionCommand`, and
declare each action as a method with `@action`. Each action has its own
options, and `--help` lists them:

```python
from domainkit.management import ActionCommand, action, option


class Command(ActionCommand):
    help = "Manage the catalog's prices."

    @action(
        help="raise every price by a percentage",
        options=[option("percent", type=int), option("--dry-run", action="store_true")],
    )
    async def raise_prices(self, *, percent: int, dry_run: bool) -> None: ...

    @action(help="list the prices")
    async def show(self) -> None: ...
```

`python manage.py prices raise-prices 5 --dry-run` runs
`raise_prices(percent=5, dry_run=True)`. An action takes the name of its
method, with `_` written as `-`, unless `@action(name=...)` gives another
name. These are the other parts of an action command:

| Name | What it does |
| --- | --- |
| `@action(name=None, help=..., options=(), operator=False)` | Marks a method as an action. `operator=True` makes the action take `--operator` and run as that operator. |
| `option(*names, **settings)` | One option, as `ArgumentParser.add_argument(*names, **settings)` takes it. |
| `ActionCommand.common_options` | Options that every action takes after its own, such as `--database`. |
| `ActionCommand.default_action` | The action that runs when the command line names none. `None` (the default) makes the action required. |
| `BaseCommand.operator_actions` | `True` makes every action, or the whole command, take `--operator` ([Operators](#operators)). |
| `BaseCommand.requires_setup` | `True` (the default) loads the settings and the installed domains before `handle` runs. |
| `CommandError(message, returncode=1)` | Raise it to stop the command with a message and an exit status. |
| `call_command(name, *args)` | Runs a command from Python, as a test does. |

A command with `operator_actions` takes `--operator` after any action:
`python manage.py prices raise-prices 5 --operator alice`.

## Projects and code

### `init`: create a project

```bash
domainkit init billingAPI billing   # into ./billing
domainkit init billingAPI .         # into the current directory
```

`init` writes a new project: settings for local work, tests, and
production, its routes and ASGI application, `manage.py`, `.env`, a first
test, a `Dockerfile`, and a CI workflow (`.github/workflows/ci.yml`).

| Option | What it does |
| --- | --- |
| `name` | The project's name, as in `[project].name`. It sets the prefix of environment variables: `billingAPI` gives `BILLING_API_`. |
| `[directory]` | Where to write the project (default: `./<name>`); `.` is the current directory. The directory may already exist and hold other files, such as a fresh clone's `.git`, `README.md`, or `.github`: `init` adds its files next to them. It refuses, and writes nothing, when a file it would write is already there, and names those files. |
| `--profile fintech` | Writes a bank's controls on and wired: a guarded sample transfer and its tests, which also pass `mypy --strict`, and production settings that pass `check --deploy` once their secrets are set ([fintech.md](../fintech.md), ADR 0124). |

What `init` writes passes the project's own `ruff check` and
`ruff format --check`. Its development group installs ruff, which also
formats what the generators write.

### `startdomain`: create a domain

```bash
python manage.py startdomain catalog --aggregate Product
```

```text
Created src/domains/catalog and tests/domains/catalog.
Added 'domains.catalog' to INSTALLED_DOMAINS in src/config/settings/base.py.
Mounted its router at "/api/v1/catalog" in src/config/urls.py.
Next steps:
  1. python manage.py makemigrations -m "create catalog"
  2. python manage.py migrate
  ...
```

`startdomain` writes a [bounded context](../learning.md#words) and its
tests ([resources.md](../resources.md#generators)). Without options it
writes four files, one per layer: `domain.py`, `controllers.py`,
`models.py`, and `routes.py`. In a project laid out as `init` makes it,
it also adds the domain to `INSTALLED_DOMAINS` and mounts its router. Its
next steps end with a login and a first `curl` of the resource, at
`/api/v1/<name>/<plural>`.

| Option | What it does |
| --- | --- |
| `name` | The domain's package name, in snake_case: `billing`. |
| `--aggregate` | The aggregate root's class name (default: the domain's name in CamelCase, in the singular). |
| `--plural` | The aggregate's plural in snake_case, for tables and URLs. |
| `--parent` | The package that holds the domains (default: `domains`). |
| `--tests-dir` | Where the tests go (default: `tests/<parent path>`). |
| `--layered` | A package per layer (`domain/`, `application/`, `infrastructure/`, `api/`), with notes, an owner, and events. `addcommand`, `addquery`, and `addevent` grow it. |
| `--minimal` | With `--layered` (it implies it): a smaller layered domain, with an aggregate with a name, create and rename, a table, a resource, and two tests. |
| `--user` | The project's own user model (default `User`) and its table, named in `AUTH`. Run it before the first `migrate`. It does not go with `--layered` or `--minimal`. |
| `--manual` | Neither installs the domain nor mounts its router; prints the steps instead. A layout that `startdomain` does not recognise gets the same steps. |

### `startaggregate`: add an aggregate

```bash
python manage.py startdomain inventory --layered
python manage.py startaggregate inventory Supplier --field name:str:120 --field rating:int?
```

`startaggregate` adds an aggregate to a layered domain (one made with
`startdomain --layered`): the model, events, commands, use cases, table,
repository, resource, fakes, and tests. A four-file domain refuses it:
add the aggregate by hand, or with `addcommand`.

| Option | What it does |
| --- | --- |
| `domain` | The domain's package name: `inventory`. |
| `aggregate` | The aggregate's class: `Supplier`. |
| `--field name:kind[:options][?]` | A field, repeated. `?` lets the field be `None`. The kinds are `bool`, `date`, `datetime`, `decimal` (precision, scale), `float`, `int`, `money` (an amount and a currency), `ref` (another aggregate's id), `str` (length), `text`, and `uuid`. `--help` lists them. |
| `--plural`, `--parent`, `--tests-dir` | As for `startdomain`. |

### `addcommand`: add a use case that changes an aggregate

```bash
python manage.py addcommand catalog Product PublishProduct
```

```text
Added PublishProduct to domains.catalog:
  tests/domains/catalog/test_publish_product.py
  src/domains/catalog/domain.py
  src/domains/catalog/controllers.py
  src/domains/catalog/routes.py
```

`addcommand` adds the aggregate's method and its event, the command, its
use case, its route, and tests. In a four-file domain, the method and the
event go in `domain.py`, a `@use_case` in `controllers.py`, the route in
`routes.py`, and a test beside them.

| Option | What it does |
| --- | --- |
| `domain`, `aggregate`, `command` | The domain, the aggregate's class, and the command's class: `PublishProduct`. |
| `--field` | A field of the command, as for `startaggregate`, repeated. |
| `--event` | The event's class (default: from the command, as `ProductPublished`). |
| `--plural`, `--parent`, `--tests-dir` | As for `startdomain`. |

### `addquery`: add a query

```bash
python manage.py addquery catalog Product ProductsByName --by name
```

```text
Added ProductsByName to domains.catalog:
  tests/domains/catalog/test_products_by_name.py
  src/domains/catalog/controllers.py
```

`addquery` adds a query that finds aggregates by fields: the message and
its handler, its route, and tests. A four-file domain gets them in
`controllers.py`; a layered one in `application/queries.py`.

| Option | What it does |
| --- | --- |
| `domain`, `aggregate`, `query` | The domain, the aggregate's class, and the query's class. |
| `--by` | A field of the aggregate that must be equal to the query's value, repeated. |
| `--path` | The route's path (default: from the query, as `/by-owner`). |
| `--plural`, `--parent`, `--tests-dir` | As for `startdomain`. |

### `addevent`: add a domain event

```bash
python manage.py addevent catalog Product ProductRestocked --field quantity:int
```

`addevent` adds a [domain event](../learning.md#words), and a durable
handler of it, subscribed when the domain starts, with its test.

| Option | What it does |
| --- | --- |
| `domain`, `aggregate`, `event` | The domain, the aggregate's class, and the event's class. |
| `--field` | A field of the event, repeated. |
| `--no-handler` | Adds the event only, without a handler. |
| `--plural`, `--parent`, `--tests-dir` | As for `startdomain`. |

### `check`: check the project

```bash
python manage.py check
```

```text
System check identified no issues. Domains: messaging, contenttypes, auth.
```

`check` validates the settings, the installed domains, the routes and
their access rules, the middleware, the layer rules between a domain's
layers, and the migration history (one head per target). It also warns
about:

- route handlers and dependencies of your domains written as `def`, which
  run on FastAPI's thread pool ([runtime.md](../runtime.md#finding-blocking-code));
- event handlers and tasks written as `def`, which block the event loop;
- in staging and production, [weak choices](checks.md), and where HTTPS
  is checked (`security.I001`).

| Option | What it does |
| --- | --- |
| `--deploy` | Fails on warnings too, unless `SILENCED_CHECKS` lists them. It runs only with `ENVIRONMENT` set to `staging` or `production`. |

Every finding has an id, listed on [checks.md](checks.md). In a local
project, `--deploy` refuses to run:

```text
Error: check --deploy checks a deployed configuration; run it with ENVIRONMENT set to staging or production, not 'local'.
```

### `diffsettings`: show the settings

```bash
python manage.py diffsettings --all
```

`diffsettings` prints the settings that differ from the defaults.

| Option | What it does |
| --- | --- |
| `--all` | Prints every setting, and marks the defaults with `###`. |

Secrets stay hidden, as in `repr(settings)`. So do the entries of
free-form settings named like secrets (`password`, `token`, `key`, ...),
and passwords in URLs. A class or function inside a setting, as in
`LOGGING`, is shown by its dotted path.

In a new project, `python manage.py diffsettings` prints (long lines cut):

```text
# config.settings.local: settings that differ from defaults
ASGI_APPLICATION = 'config.asgi:application'
AUTH = '**********'
DATABASES = {'default': {'URL': '**********', 'MIGRATION_URL': None, ...
DEBUG = True
INSTALLED_DOMAINS = ['domainkit.contrib.messaging', 'domainkit.contrib.contenttypes', 'domainkit.contrib.auth']
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'filters': {'context': {'()': 'domainkit.log.ContextFilter'}}, ...
MIDDLEWARE = ['domainkit.http.middleware.ProxyHeadersMiddleware', 'domainkit.http.locale.LocaleMiddleware', ...
PROJECT_NAME = 'shop'
ROOT_ROUTER = 'config.urls.router'
SECRET_KEY = '**********'
```

### `openapi`: print the API's schema

```bash
python manage.py openapi --output openapi.json
python manage.py openapi --diff openapi.json
```

`openapi` prints the OpenAPI schema as JSON, or writes it to a file.

| Option | What it does |
| --- | --- |
| `-o`, `--output` | Writes the schema to this file, not to standard output. |
| `--indent` | Spaces per level (default: 2). |
| `--diff OLD` | Lists the changes since `OLD` that break clients, and exits 1 if there are any ([The API surface](responses.md#the-api-surface)). |

### `inspectdb`: write aggregates for existing tables

```bash
python manage.py inspectdb domainkit_groups
```

```text
"""Aggregates read from the database by inspectdb: check each one."""

from typing import Annotated

from domainkit.db import table_for
from domainkit.ddd import AggregateRoot, MaxLength


class DomainkitGroup(AggregateRoot[Annotated[str, MaxLength(32)]]):
    """The table domainkit_groups."""

    name: Annotated[str, MaxLength(150)]


domainkit_groups = table_for(DomainkitGroup, "domainkit_groups")
```

`inspectdb` prints aggregates for the tables of an existing database, as
a start. Each column becomes a field with its type hint (`MaxLength`,
`Digits`, `| None`), and each table gets its `table_for` call. A table
without a single `id` key says what it needs. Nothing is written to the
database.

| Option | What it does |
| --- | --- |
| `[TABLE ...]` | Only these tables (default: all). |
| `--database` | The alias to read (default: `default`). |

### `makemessages`: find the messages to translate

```bash
python manage.py makemessages -l ar
```

`makemessages` finds the messages that each domain's code marks for
translation. It updates `<domain>/locale/<locale>/LC_MESSAGES/messages.po`:
new messages are added empty, translations are kept, and messages no
longer used are kept as obsolete ([i18n.md](../i18n.md#catalogs)).

| Option | What it does |
| --- | --- |
| `[DOMAIN ...]` | Only these domains, by label (default: every domain). |
| `-l`, `--locale` | A locale to update or start, such as `ar` or `pt_BR`; repeat it for more. |
| `--all` | Updates every locale a domain already has. |
| `--check` | Writes nothing, and fails if a catalog is behind its code. |

### `compilemessages`: compile translations

```bash
python manage.py compilemessages --check
```

`compilemessages` compiles translation catalogs (`.po`) to `.mo` files: in
the directories named, or in `LOCALE_PATHS` and each installed domain's
`locale` directory ([i18n.md](../i18n.md)).

| Option | What it does |
| --- | --- |
| `[DIRECTORY ...]` | Locale directories (default: `LOCALE_PATHS` and the domains'). |
| `-l`, `--locale` | Only this locale; repeat it for more. |
| `--check` | Writes nothing, and fails if a `.mo` file is missing or out of date. |

### `runserver`: serve the project while you work

```bash
python manage.py runserver 0.0.0.0:8000 --server granian
```

`runserver` starts a development server for `ASGI_APPLICATION`, on
uvicorn or Granian (the `granian` extra). It restarts when your code
changes. In production you run the server yourself
([Server limits](servers.md)).

| Option | What it does |
| --- | --- |
| `[address]` | A port, or `address:port` (default: `127.0.0.1:8000`). |
| `--noreload` | Does not restart when the code changes. |
| `--server` | `uvicorn` (the default) or `granian`. |

### `shell`: a Python console with the project loaded

```bash
python manage.py shell -c "print(settings.ENVIRONMENT)"
```

```text
local
```

`shell` opens an asyncio Python console, where `await` works at the top
level. It holds `settings`, `registry`, `connections`, `asyncio`, and
every installed domain's models, repositories, and units of work by name.

| Option | What it does |
| --- | --- |
| `-c`, `--command` | Runs this code, then exits. |
| `--no-imports` | Imports no domain classes. |
| `--operator` | Who runs the console ([Operators](#operators)). |

What you change in the console is audited as its operator's work. In
staging and production, the opening of the console is recorded
(`operator.shell`) before it starts.

### `dbshell`: the database's own console

```bash
python manage.py dbshell --database default -- -c "SELECT 1"
```

`dbshell` opens `psql` or `sqlite3` on an alias, as the user of its
`URL`. The connection and the password reach `psql` in libpq's
environment variables, never on its command line. TLS follows `SSLMODE`
and its files.

| Option | What it does |
| --- | --- |
| `--database` | The alias to open (default: `default`). |
| `--owner` | Connects as the schema owner, with `MIGRATION_URL`. |
| `--allow-production` | Required when `ENVIRONMENT` is `production`. |
| `--operator` | Who opens it ([Operators](#operators)). |
| `-- [ARG ...]` | Arguments for the client, after `--`. |

What the client does is outside the audit trail. So in staging and
production, the opening is recorded (`operator.dbshell`) with the
operator, the database, and whether `--owner` was given.

## Migrations

### `makemigrations`: write migrations

```bash
python manage.py makemigrations -m "create catalog"
```

```text
Created migrations/0002_1825_create_catalog.py.
```

`makemigrations` compares your tables with the database and writes a
revision for each target (a SQL alias or a shard group) that has changes.
Without `--database`, it looks at every target.

| Option | What it does |
| --- | --- |
| `-m`, `--message` | The revision's name: one line, without backslashes or triple quotes (default: `auto`). |
| `--database` | One target (default: every target). |
| `--check` | Writes nothing, and fails if a migration is missing. |
| `--merge` | Joins the two heads that two branches left. |
| `--empty` | Writes a revision with nothing in it, to fill in by hand (a data migration). With several targets, it needs `--database`. |

### `migrate`: apply migrations

```bash
python manage.py migrate
python manage.py migrate 0001_b769 --downgrade --plan
```

```text
Database 'default' is at head.
Database 'default':
  revert 0002_1825  Create catalog.
```

`migrate` applies revisions (or reverts down to one) on every database of
every target. After an upgrade it creates the partitions of partitioned
tables, and runs the `on_migrated` hooks. It connects with
`MIGRATION_URL` when it is set.

| Option | What it does |
| --- | --- |
| `[revision]` | The revision to reach (default: `head`). A revision id needs `--database` when there are several targets. `base` with `--downgrade` reverts everything. |
| `--database` | A target or an alias (default: all of them). |
| `--downgrade` | Reverts down to the revision. |
| `--allow-data-loss` | Required to downgrade in staging and production, after a backup. |
| `--plan` | Lists the revisions that would run. |
| `--sql` | Prints their SQL, rendered offline by Alembic, without running it. |
| `--from` | With `--sql`: the revision to start from (default: the database's). |
| `--fake` | Records the revision as applied without running anything, for a schema made another way. |
| `--tenant NAME` | Migrates this tenant's schema on `TENANCY.SCHEMA_DATABASES`, and makes the schema if it is missing. Repeat it for several tenants. It does not go with `--plan`, `--sql`, or `--fake`. |
| `--operator` | Who runs it ([Operators](#operators)). |

`--plan`, `--sql`, and `--fake` do not change the schema. But
`--fake --downgrade` still needs `--allow-data-loss` in staging and
production. A database of `TENANCY.SCHEMA_DATABASES` keeps a schema per
tenant, so `migrate` without `--tenant` skips it and says so:

```text
Database 'tenants' keeps a schema per tenant: use --tenant.
```

```bash
python manage.py migrate --tenant acme --tenant globex
```

!!! warning
    A downgrade can drop data. In staging and production, each downgrade
    that runs, a tenant's and a `--fake` one included, is recorded in the
    audit trail
    (`operator.migrate.downgrade`, with the database, the revision, and
    the tenant) **before** it runs. So a downgrade that drops the trail's
    own tables is still on record. If the record cannot be written, the
    downgrade does not run.

### `showmigrations`: see what is applied

```bash
python manage.py showmigrations
```

```text
[X] 0001_b769  Auth.
[X] 0002_1825  Create catalog.
```

`showmigrations` lists the revisions, oldest first, and marks those that
are applied.

| Option | What it does |
| --- | --- |
| `--database` | A target or an alias (default: `default`). A shard group lists each of its databases. |

### `squashmigrations`: join revisions into one

```bash
python manage.py squashmigrations 0007_a1b2 --start 0001_b769 -m "initial"
```

`squashmigrations` replaces a run of a target's revisions with one. See
[Squashing migrations](#squashing-migrations) for what it keeps and what
it refuses.

| Option | What it does |
| --- | --- |
| `[end]` | The newest revision to squash (default: the head). |
| `--start` | The oldest revision to squash (default: the first). |
| `--database` | The target whose history to squash (default: `default`). |
| `-m`, `--message` | The squashed revision's name (default: `squashed`). |

### `partitions`: create and drop partitions

```bash
python manage.py partitions --dry-run
python manage.py partitions --prune --operator alice
```

`partitions` creates the current and the next partitions of partitioned
tables ([databases.md](../databases.md)).

| Option | What it does |
| --- | --- |
| `--database` | A target or an alias (default: all of them). |
| `--prune` | Detaches and drops the partitions older than each table's `retain`. It is audited (`operator.partitions.prune`). |
| `--dry-run` | Prints the SQL instead of running it. |
| `--operator` | Who runs it ([Operators](#operators)). |

## Data

These commands load, save, and delete rows. Read
[Data: fixtures, flush, and seeds](#data-fixtures-flush-and-seeds) before
you use them on a database you care about.

### `dumpdata`: save rows to a fixture

```bash
python manage.py dumpdata catalog --output catalog.json
```

```json
{
  "format": "domainkit.fixture",
  "version": 1,
  "tables": [
    {
      "table": "catalog_products",
      "rows": []
    }
  ]
}
```

`dumpdata` writes rows as a JSON fixture: every table, or the tables and
domains you name.

| Option | What it does |
| --- | --- |
| `[TABLE_OR_DOMAIN ...]` | Tables or domain labels (default: every table of the database). |
| `--database` | The alias to read (default: `default`). |
| `-o`, `--output` | A new file to write, readable only by its owner (mode `0600`). Default: standard output. |
| `--indent` | Spaces per level (default: 2). |
| `--include-guarded` | Dumps guarded tables too: users and their password hashes, permissions, grants, dynamic settings, ... |
| `--decrypt` | Writes the values of encrypted columns, not their ciphertext. |
| `--operator` | Who runs it ([Operators](#operators)). |

A guarded table named without `--include-guarded` is refused:

```text
Error: Guarded tables (domainkit_content_types) hold logins, password hashes, permissions, and secrets. Nothing was written; repeat with --include-guarded if that is what you mean.
```

### `loaddata`: load rows from fixtures

```bash
python manage.py loaddata catalog.json --dry-run
```

`loaddata` loads fixtures. It first says what each table gets, and how
many of its rows are stored already.

| Option | What it does |
| --- | --- |
| `FIXTURE ...` | The fixture files, one or more. |
| `--database` | The alias to write (default: `default`). |
| `--dry-run` | Only says what would be written. |
| `--replace` | Replaces stored rows with the same primary key. Without it, such a row refuses the load. |
| `--include-guarded` | Writes guarded tables: users, permissions, grants, tokens, ... |
| `--force` | Required in staging and production. |
| `--operator` | Who runs it ([Operators](#operators)). |

Staging and production refuse a load without `--force` and a named
operator. Each load is audited with its tables and the fixture's SHA-256.

### `flush`: delete rows

```bash
python manage.py flush --dry-run
```

```text
default: domainkit_inbox: delete 0 rows
default: domainkit_messages: delete 0 rows
default: domainkit_content_types: kept (guarded)
default: domainkit_users: kept (guarded)
...
```

`flush` deletes every row but the audit trail's and, without
`--include-guarded`, the guarded tables'. It says first what goes. Then
it runs the `on_migrated` hooks again (content types, permissions).

| Option | What it does |
| --- | --- |
| `--database` | The alias to empty (default: `default`). |
| `--dry-run` | Only says what would be deleted. |
| `--include-guarded` | Empties the guarded tables too: users, permissions, grants, tokens, ... |
| `--force` | Required in staging and production. |
| `--operator` | Who runs it ([Operators](#operators)). |

Staging and production refuse `flush` without `--force` and a named
operator. Each run is audited, with its tables.

### `seed`: add sample or default data

```bash
python manage.py seed catalog
```

`seed` runs the `on_seed` hooks of the installed domains, in
`INSTALLED_DOMAINS` order, as the operator. The run is recorded
(`operator.seed`, with the domains seeded). A project with no hook prints
`No domain has seed data.`

| Option | What it does |
| --- | --- |
| `[LABEL ...]` | Only these domains (default: all). |
| `--operator` | Who runs it ([Operators](#operators)). |

## Caches and brokers

### `cache`: check or clear caches

```bash
python manage.py cache check
python manage.py cache clear --all
```

```text
default: ok
default: cleared 0 key(s)
```

| Action and option | What it does |
| --- | --- |
| `check [ALIAS ...]` | Pings caches (default: all of them). |
| `clear [ALIAS ...]` | Deletes every key of the named caches, every version, in their own namespace only. |
| `clear --all` | Clears every cache. `clear` needs aliases or `--all`. |

### `broker`: check brokers, or declare queues

```bash
python manage.py broker check
python manage.py broker declare
```

| Action | What it does |
| --- | --- |
| `check [ALIAS ...]` | Connects to brokers (default: all of them). |
| `declare` | Declares the exchange and every queue that the worker's tasks use on `MESSAGING.BROKER`. |

## Messaging

These commands come with `domainkit.contrib.messaging`
([messaging](../contrib/messaging.md)).

### `runworker`: run background work

```bash
python manage.py runworker --metrics-port 9100
python manage.py runworker --once
```

`runworker` runs [outbox](../learning.md#words) messages: tasks and
durable event handlers. It runs one worker per outbox, on every database
that hosts domains. With `MESSAGING.BROKER`, it relays messages to the
broker and runs what the broker delivers. Whatever it serves, the
process runs at most `MESSAGING.CONCURRENCY` messages at once.

| Option | What it does |
| --- | --- |
| `--once` | Runs the messages that are due now, then exits (cron, tests). It always works from the outbox. |
| `--metrics-port` | Serves Prometheus metrics on this port (needs `domainkit[metrics]`). |
| `--metrics-address` | The address for `--metrics-port` (default: `127.0.0.1`). |
| `--database` | A target or an alias whose outboxes to run (default: every database that hosts domains). |
| `--queue` | Runs only this queue's messages; repeat it for more (default: every queue). |
| `--no-relay` | With a broker: only consumes, and never reads the outbox. |

### `messages`: see and act on the outbox

```bash
python manage.py messages status
python manage.py messages list dead --limit 50
python manage.py messages retry all --operator alice
```

```text
pending  0
held     0
done     0
dead     0
cancelled 0
```

| Action and option | What it does |
| --- | --- |
| `status` | Counts the messages by status, and shows each queue's lag. |
| `list pending\|held\|locked\|dead` | Lists messages in a state. `pending` lists them in the order workers take them; `held`, those waiting for an earlier message of their ordering key; `locked`, those running or published. |
| `list --queue Q`, `list --limit N` | Only one queue's messages; at most N (default: 20). |
| `retry ID\|all` | Queues a dead message, or all of them, again. The dead letters of `sensitive` tasks are not retried, because their arguments were blanked. A message with an ordering key goes back in its place. |
| `cancel ID` | Cancels a message that no worker runs. |
| `release` | Runs the next message of each ordering key that has held messages and none pending. |
| `--database` | After any action: the outbox's alias (default: `MESSAGING.DATABASE`). |
| `--operator` | After `retry`, `cancel`, and `release`, which are audited ([Operators](#operators)). |

## Users and logins

These commands come with `domainkit.contrib.auth` ([auth.md](../auth.md)).
Each one takes `--operator` and is audited.

### `createsuperuser`: make a user who may do everything

```bash
python manage.py createsuperuser --username admin --email admin@example.com
```

```text
Superuser created successfully.
```

`createsuperuser` asks for what the options leave out. It has one option
per field: `--<USERNAME_FIELD>`, `--<field>` for each field of
`AUTH.IDENTIFIERS`, and one for each of `REQUIRED_FIELDS` (with the
default user model, `--username` and `--email`, and `--mobile_number`
where numbers identify users). Where the username does not identify
users, it is optional, and the user needs one of the identifiers:

```bash
python manage.py createsuperuser --noinput --email admin@example.com
```

| Option | What it does |
| --- | --- |
| `--noinput`, `--no-input` | Asks nothing. It reads the missing values from `<PREFIX>SUPERUSER_<FIELD>`, the password from `<PREFIX>SUPERUSER_PASSWORD`. |
| `--temporary`, `--no-temporary` | Whether the user must change the password at the first login. By default: with `--noinput`, under `AUTH.PASSWORD_CHANGE_ON_FIRST_USE`. |
| `--operator` | Who runs it. |

A password that the validators refuse is kept on request only without
`AUTH.PRESET`, and outside staging and production.

### `changepassword`: set a user's password

```bash
python manage.py changepassword ann --temporary
```

`changepassword` asks for the new password twice, and validates it. The
user's tokens end.

| Option | What it does |
| --- | --- |
| `[username]` | Whose password (default: your login name). |
| `--temporary`, `--no-temporary` | Whether the user must change it at the first login. By default: for a user you name, under `AUTH.PASSWORD_CHANGE_ON_FIRST_USE`. |
| `--operator` | Who runs it. |

### `createtoken`: make a token for scripts

```bash
python manage.py createtoken admin --name deploy --days 30 --scope catalog.view_product
```

```text
dkt_S9lnwPecSWV9t8AeRuXQOkKG9PhwY7c7TGO-zqtqFxU
```

`createtoken` prints a new token for a user's scripts. It shows the token
once; the database keeps only its hash.

| Option | What it does |
| --- | --- |
| `username` | Whose token. |
| `--name` | What it is for (default: `cli`). `login` is refused: it is the name of logins' tokens. |
| `--days` | Days it works (default: 90). |
| `--no-expiry` | Works until it is revoked (`cleartokens --user`). |
| `--scope` | Limits the token to this permission key; repeat it for more. The user must hold each one. |
| `--operator` | Who runs it. |

### `cleartokens`: delete tokens

```bash
python manage.py cleartokens
python manage.py cleartokens --user ann
```

```text
Deleted 0 expired token(s).
```

| Option | What it does |
| --- | --- |
| `--user` | Deletes every token of this user, and the client certificates `addcertificate` gave them. Without it, only expired tokens go. |
| `--operator` | Who runs it. |

### `resetmfa`: turn off a user's second factor

```bash
python manage.py resetmfa ann
```

`resetmfa` turns a user's second factor off, after a lost authenticator
and lost recovery codes. Every session of the user ends. Check who is
asking before you run it.

| Option | What it does |
| --- | --- |
| `username` | Whose second factor. |
| `--operator` | Who runs it. |

### `resealmfa`: encrypt second factors under the current key

```bash
python manage.py resealmfa --check
python manage.py resealmfa
```

`resealmfa` encrypts every TOTP secret again under `SECRET_KEY`, so you
can drop a key of `SECRET_KEY_FALLBACKS`. Run it before you drop a
fallback ([Two-factor logins](../auth/logging-in.md#two-factor-logins)).

| Option | What it does |
| --- | --- |
| `--check` | Changes nothing. Fails while a secret is under a fallback, or under no key held. |
| `--batch-size` | Authenticators per transaction (default: 500). |
| `--operator` | Who runs it. |

### `addcertificate`: let a client certificate log in

```bash
python manage.py addcertificate billing-service "$(cat billing.pem)" --name billing
```

`addcertificate` lets a client certificate (PEM) log in as a user, by its
SHA-256, with `AUTH.CLIENT_CERT.MATCH = "fingerprint"`.
`cleartokens --user` removes it.

| Option | What it does |
| --- | --- |
| `username` | The user the certificate logs in as. |
| `certificate` | The certificate, in PEM. |
| `--name` | What it is for (default: `certificate`). |
| `--operator` | Who runs it. |

### `unbinddevices`: unbind a lost phone

```bash
python manage.py unbinddevices ann
```

`unbinddevices` unbinds every device of a user who lost their phone,
forgets its push tokens, and ends every session of the user. With
`CIBA.APPROVE_LOGINS`, the user then logs in with their other factors and
binds the new phone. Check who is asking before you run it.

| Option | What it does |
| --- | --- |
| `username` | Whose devices. |
| `--operator` | Who runs it. |

### `ciba`: register the clients that ask users' phones

```bash
openssl rand -base64 32 > acme.secret
python manage.py ciba add "Acme Bank" --scope payments.add_payment < acme.secret
python manage.py ciba add "Kiosk" --method private_key_jwt --jwks kiosk-jwks.json
python manage.py ciba list
openssl rand -base64 32 > acme.secret
python manage.py ciba rotate ciba_2kQ... < acme.secret
python manage.py ciba remove ciba_2kQ...
```

`ciba` manages the OpenID Connect clients of the CIBA routes
([Sign-in approvals](../auth/ciba.md#oidc-clients)). `add` prints the new
`client_id`. A client's secret, for the secret methods, is read from the
first line of the standard input: at least 32 characters, never printed,
stored as its SHA-256. Generate it, and give it to the client yourself.
`list` (the default) shows each client's id, name, method, and scopes.
`rotate` gives a client the secret read the same way; the old one stops
working. `remove` deletes
a client: it can start and poll no more, and tokens it holds run out.

| Option | What it does |
| --- | --- |
| `name` | What the user's phone shows (`add`). |
| `--scope` | The permission keys its tokens may carry (`add`). |
| `--method` | `client_secret_basic` (the default), `client_secret_post`, or `private_key_jwt` (`add`). |
| `--jwks` | A file of the client's public JWK Set, for `private_key_jwt` (`add`). |
| `client_id` | The client (`rotate`, `remove`). |
| `--operator` | Who runs it (`add`, `rotate`, `remove`). |

## Audit, evidence, privacy, and keys

### `audit`: read and protect the audit trail

```bash
python manage.py audit log --actor alice --action catalog.product_created
python manage.py audit history --aggregate product_id=6f1c7e6a-0d2b-4c55-9f0e-1f2a3b4c5d6e
python manage.py audit seal
python manage.py audit verify --export checkpoints.json
python manage.py audit check-export checkpoints.json --public-key audit-checkpoint.pub
python manage.py audit prune --keep-days 2555 --operator alice
python manage.py audit forget 4b0c6f0e-7d4b-4c55-9f0e-1f2a3b4c5d6e --operator alice
```

`audit` comes with `domainkit.contrib.audit`
([audit](../contrib/audit.md)).

| Action and option | What it does |
| --- | --- |
| `log` | Shows recent entries, newest first. `--actor`, `--action`: only those of one actor or action. `--limit` (default: 50). `--database` (default: `AUDIT.DATABASE`): events are audited in the database where they commit. |
| `history` | One record's history, with what each entry changed ([history](../contrib/audit.md#history)). `--target` names the record; `--aggregate FIELD=ID` names an aggregate, by the rows whose `details[FIELD]` is ID. `--limit` (default: 50). `--database` (default: every audited one). |
| `seal` | Seals new rows into each database's hash chain, and anchors its head on `AUDIT.LEDGER`. `--late` also seals rows that committed later than `AUDIT.SEAL_LATE_WINDOW`, flagged as sealed late. |
| `verify` | Checks the trail against its seals, checkpoints, and anchors, and fails if anything changed. `--database` (default: every audited one). `--export FILE` then writes signed checkpoints of every trail for a third party, in a new file only its owner can read (needs `AUDIT.CHECKPOINT_KEY`, which this and `audit prune` alone read). |
| `check-export FILE` | Checks an export offline, with the Ed25519 public key alone (`--public-key PEM`, required). |
| `prune` | Deletes rows sealed more than `--keep-days` ago (at least `AUDIT.MIN_RETENTION`), below a signed checkpoint recorded on the ledger. `audit verify` then starts the chain from that checkpoint. It is refused if the chain up to it does not verify. `--database` (default: every audited one). Audited. |
| `forget subject ...` | Makes people's pseudonyms in the trail unlinkable, in every audited database. `--anonymous-days DAYS` keeps anonymous callers' addresses for that many days (UTC) and forgets older ones. Audited. |
| `--operator` | After `prune` and `forget` ([Operators](#operators)). |

### `evidence`: list and verify evidence

```bash
python manage.py evidence list customer:42 --limit 20
python manage.py evidence verify evidence/77e0b3b900b3f6eea06236db14ba86a3/005219ba-2377-4970-b4ee-4b552bd44b8b
```

`evidence` comes with `domainkit.contrib.evidence`
([evidence](../contrib/evidence.md)).

| Action and option | What it does |
| --- | --- |
| `list SUBJECT` | Lists the evidence about a subject. `--limit` (default: 50). |
| `verify KEY` | Verifies the record under a key. |

### `privacy`: export or erase a person's data

```bash
python manage.py privacy export --subject 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e --output ann.json --operator alice
python manage.py privacy erase --subject 3f2a9c1e-0d2b-4c55-9f0e-1f2a3b4c5d6e --reason "ticket 812" --operator alice
python manage.py privacy purge --dry-run
```

`privacy` exports or erases what every installed domain holds about a
person, from the `Personal` markers on aggregates. It also applies the
domains' retention policies ([privacy.md](../privacy.md)). Each action is
audited as the operator's.

| Action and option | What it does |
| --- | --- |
| `export --subject ID` | Writes a person's data as JSON. `--output FILE`: a new file, mode `0600` (default: standard output). |
| `erase --subject ID` | Erases a person's data. `--reason` is recorded with each erasure (default: `request`), such as a ticket. `--noinput` does not ask for confirmation. |
| `purge` | Applies every domain's retention. `--dry-run` only counts what would go. |
| `--operator` | After any action ([Operators](#operators)). |

### `keys`: manage data keys

```bash
python manage.py keys generate --id 2026-11 --operator alice
python manage.py keys status --check
python manage.py keys reencrypt --batch-size 1000 --operator alice
```

`keys` comes with `domainkit.contrib.keys`
([keys.md](../contrib/keys.md#rotating)). Every action takes `--operator`.

| Action and option | What it does |
| --- | --- |
| `generate` | Prints a new data key, wrapped by `KEYS.PROVIDER`. `--id`: its id in `DATA_KEYS` (default: today's date). |
| `rewrap` | Prints `KEYS.DATA_KEYS` wrapped under the newest version of the key-encryption key. |
| `status` | Counts what each data key seals. `--check` fails while anything is sealed under a retired key (`keys.W001`). |
| `reencrypt` | Seals everything that is under a retired key again, under `CURRENT_KEY`, in batches. `--batch-size`: rows per transaction (default: 500). Audited (`operator.keys_reencrypt`). |

## Feature apps

### `flags`: feature flags and dynamic settings

```bash
python manage.py flags list
python manage.py flags set beta --percentage 10 --groups staff --note "new checkout" --operator alice
python manage.py flags setting checkout.max_items 25 --operator alice
python manage.py flags setting checkout.max_items --reset --operator alice
python manage.py flags delete beta --operator alice
```

`flags` comes with `domainkit.contrib.flags`
([flags](../contrib/flags.md#flags-feature-flags-and-dynamic-settings)).
Without an action it runs `list`.

| Action and option | What it does |
| --- | --- |
| `list` | Every flag and its rules. |
| `set NAME` | Creates a flag or changes its rules: `--everyone {on,off,rules}`, `--percentage N`, `--users USER_ID ...`, `--groups GROUP ...`, `--note TEXT`. |
| `delete NAME` | Deletes a flag, which is then off for everyone. |
| `settings` | Every dynamic setting and its value. |
| `setting KEY [VALUE]` | Sets a dynamic setting, its value as JSON. `--reset` goes back to the default. |
| `--operator` | After `set`, `delete`, and `setting`, which are audited. |

### `webhooks`: subscriptions and deliveries

```bash
python manage.py webhooks subscriptions
python manage.py webhooks deliveries --status failed --limit 50
python manage.py webhooks replay --failed --subscription 7c1d --operator alice
```

`webhooks` comes with `domainkit.contrib.webhooks`
([webhooks](../contrib/webhooks.md)).

| Action and option | What it does |
| --- | --- |
| `subscriptions` | Lists the subscriptions, without their URLs' paths or secrets. |
| `deliveries` | Lists the newest deliveries. `--status` is `pending`, `succeeded`, `failed`, or `cancelled`; `--subscription` keeps to one subscription. |
| `replay [ID ...]` | Sends finished deliveries again: those named, or with `--failed`, the most recent failed ones. `--subscription` keeps to one subscription. Audited. |
| `--limit` | After any action: at most this many (default: 20). |
| `--operator` | After `replay` ([Operators](#operators)). |

### `approvals`: approval requests

```bash
python manage.py approvals list --status pending --limit 20
python manage.py approvals show 9b2e7c1a-0d2b-4c55-9f0e-1f2a3b4c5d6e
python manage.py approvals expire --operator alice
```

`approvals` comes with `domainkit.contrib.approvals`
([approvals.md](../contrib/approvals.md#from-the-command-line)).

| Action and option | What it does |
| --- | --- |
| `list` | Lists the requests in a status, oldest first. `--status` is `pending` (the default), `approved`, `executed`, `rejected`, `cancelled`, `expired`, or `failed`. `--limit`: 1 to 1,000 (default: 50). |
| `show ID` | Shows a request, its command, and its decisions. |
| `expire` | Expires the requests whose time is up. Audited. |
| `--operator` | After `expire` ([Operators](#operators)). |

### `accounting`: reports, holds, and statements

```bash
python manage.py accounting trialbalance 2026-10 --format json --output october.json
python manage.py accounting verify
python manage.py accounting import 1000 october.csv --operator alice
python manage.py accounting reconcile 1000 --operator alice
python manage.py accounting unmatched 1000
```

`accounting` comes with `domainkit.contrib.accounting`
([accounting.md](../contrib/accounting.md#from-the-command-line)). A
period is named by its name or id; an account by its code or id.

| Action and option | What it does |
| --- | --- |
| `trialbalance PERIOD` | Writes a period's trial balance. `--format csv\|json` (default: `csv`); `--output FILE` (default: standard output). |
| `ledger PERIOD` | Writes a period's general ledger, with the same options. |
| `verify` | Checks that every entry balances and every balance sums its postings. Fails otherwise. |
| `expireholds` | Releases the holds whose time is up. Audited. |
| `import ACCOUNT FILE` | Imports a bank statement (CSV) into an account. Audited. |
| `reconcile ACCOUNT` | Reconciles an account's statement lines. Audited. |
| `unmatched ACCOUNT` | Lists an account's unmatched lines. |
| `--operator` | After `expireholds`, `import`, and `reconcile` ([Operators](#operators)). |

### `limits`: transaction limits

```bash
python manage.py limits list --kind holder
python manage.py limits usage holder 6f1c7e6a-0d2b-4c55-9f0e-1f2a3b4c5d6e --currency EGP
python manage.py limits set daily-out --kind holder --currency EGP --window day --max-amount 50000.00 --operator alice
python manage.py limits purge --operator alice
```

`limits` comes with `domainkit.contrib.limits`
([limits.md](../contrib/limits.md#from-the-command-line)). Without an
action it runs `list`.

| Action and option | What it does |
| --- | --- |
| `list` | Every limit, stored or from `LIMITS.DEFAULTS`. `--kind K` keeps to one kind of subject. |
| `usage KIND SUBJECT` | What a subject used of each window. `--currency C` keeps to one currency. |
| `set CODE --kind K --currency C` | Makes a limit, or changes the bounds given: `--subject S` (default: every subject), `--direction in\|out` (default: `out`), `--per-transaction A`, `--window` (`day`, `month`, `rolling_day`, `rolling_month`, or `none`), `--max-amount A`, `--max-count N`, `--time-zone Z`, `--note T`, `--enable` or `--disable`. `none` clears a bound. Audited. |
| `purge` | Deletes usage older than `LIMITS.KEEP_USAGE`. Audited. |
| `--operator` | After `set` and `purge` ([Operators](#operators)). |

## Operators

Some commands change or erase stored data as a person, not as the
application. Each one says who runs it, and leaves a row in the audit
trail (with `domainkit.contrib.audit` installed). These are:

- data: `loaddata`, `flush`, `partitions --prune`, `migrate` (its
  downgrades), `seed`, and `dumpdata --include-guarded` (it copies
  credentials out) or `dumpdata --decrypt` (it copies encrypted values
  out in clear);
- consoles: `shell` and `dbshell` (their openings in staging and
  production);
- the outbox and the trail: `messages retry`, `cancel`, and `release`,
  `audit forget`, and `audit prune`;
- privacy and keys: `privacy`, and every action of `keys`;
- credentials: `resetmfa`, `resealmfa`, `createtoken`, `changepassword`,
  `createsuperuser`, `addcertificate`, and `cleartokens`;
- flags, settings, and deliveries: `flags set`, `delete`, and `setting`,
  `webhooks replay`, `accounting import`, `reconcile`, and
  `expireholds`, `approvals expire`, and `limits set` and `purge`.

A command of several actions takes `--operator` after the action, and
only on the actions that change something. Its listings and checks
(`flags list`, `accounting verify`, `approvals list`, `messages status`,
`audit log`, `evidence`, ...) only read, and take none. `privacy` and
`keys` take it on every action.

The operator is, in this order:

1. `--operator NAME`, after the action of a command of several
   (`privacy purge --operator alice`, `flags set beta --everyone on
   --operator alice`);
2. the `DOMAINKIT_OPERATOR` environment variable;
3. the login name that `getpass.getuser()` reports: the `LOGNAME`,
   `USER`, `LNAME`, or `USERNAME` environment variable, else the name of
   the process's user id. Whoever runs the command can set any of these,
   so this source names the operator no better than the first two;
4. when there is no login name that an operator may have (a container
   under an arbitrary uid, with no passwd entry; a Windows domain
   account; a name with a space): `uid-<n>`, the process's user id, or
   `unnamed` where there are no user ids. Commands run from cron or an
   entrypoint keep running, and the name they had is neither kept nor
   shown.

A name is 1 to 64 letters, digits, `.`, `_`, `@`, `+`, or `-`, and starts
with a letter or a digit. A bad `--operator` or `DOMAINKIT_OPERATOR` is
refused before the command runs, because the operator typed it.

The last two sources are recorded as `operator_source` `system`, so the
trail shows that nobody gave a name. In a container that runs as `root`
or `app`, or under a uid, they name nobody: set `DOMAINKIT_OPERATOR` from
your access tooling, or pass `--operator`. In staging and production,
`loaddata` and `flush` need a name from one of the first two sources
(0.11), and so does `dumpdata --include-guarded` (0.12). The other
commands keep running unattended.

!!! note
    The name is what the operator says. It attributes an action; it does
    not authenticate anyone. Keep the group of people who can run
    `manage.py` against production as small as the group who can reach
    its database. Prefer a staff id or a handle to an email address: the
    trail keeps what it is given for good.

The command runs as the actor `operator:<name>`. So what the command
records itself (`privacy.erase`, `privacy.purge`, `auth.mfa_disabled`,
`auth.token_created`, `auth.password_changed`, `auth.user_created`,
`auth.user_logged_out`, `flags.flag_changed`, `flags.setting_changed`,
`limits.limit_set`) names the operator instead of `system`.

An action that records nothing itself is recorded once it ends, as
`operator.<command>`: `operator.messages.retry`,
`operator.messages.cancel`, `operator.messages.release`,
`operator.auditforget`, `operator.auditprune`, `operator.loaddata`,
`operator.flush`, `operator.partitions.prune`, `operator.seed`,
`operator.webhooks.replay`, `operator.accounting.import`,
`operator.accounting.reconcile`, `operator.accounting.expireholds`,
`operator.approvals.expire`, `operator.limits.purge`, and
`operator.keys_reencrypt`. The row holds the outcome (`success`, or
`failure` with the error's class), what the action acted on, its counts,
and `operator_source`: `option`, `environment`, or `system`.

- `operator.loaddata` names the fixture file, its SHA-256, and each table
  with its rows and how many it replaced.
- `operator.flush` names each table emptied with its rows, the tables
  kept, and the rows of kept tables it deleted with their objects
  (`about`: object grants).
- Both say whether `--replace` and `--include-guarded` were given.

Three actions are recorded **before** they run, because after them there
may be nothing left to record in, all three in staging and production
only: `operator.migrate.downgrade` (a `--fake` downgrade included),
`operator.shell`, and `operator.dbshell`.

Dry runs, listings, and refusals before the action (no `--force`) are
not recorded. A successful action whose row cannot be written fails the
command. A command stopped part way (Ctrl-C) is recorded as a failure,
because what it did may have been committed.

## Data: fixtures, flush, and seeds

A fixture is one JSON document. It lists its tables, parents first:

```json
{"format": "domainkit.fixture", "version": 1, "tables": [
  {"table": "shop_products", "rows": [{"id": "6f1c7e6a-...", "name": "Lamp", "version": 1}]}
]}
```

### How values are written

- Numbers and text are written as they are.
- Decimals, times, durations, ids, and enums are written as text.
- Bytes are written as base64, and JSON columns as JSON.
- A value object with a single field (a password hash, a project's
  `ProductName`) is written as its field. Loading makes the object again,
  so the object's own checks refuse what it would not hold.
- A value object around another (`Code(sku: Sku)`) is written as the
  innermost field, and each object is made again from the inside out.
- Fixtures from 0.11, which wrote such an object as `{"value": ...}`, and
  from 0.12, which wrote the inner object of two that way, load the same.

A password hash loads only if a hasher of `AUTH.PASSWORD_HASHERS` made
it, and would not make it again more strongly. So a password in clear,
or the test hasher's single iteration under production's hashers, is
refused.

### How a load works

`loaddata` converts each value back by its column's type. It refuses a
value that does not fit, without quoting it. A number with a fraction
reaches a decimal column with every digit as written
(`1234567890123456.78`), never through a float; other columns get it as
a float. The rows load in one transaction. On PostgreSQL, serial ids then
continue after the highest one loaded. Rows of one table may name
different columns: a hand-written fixture leaves out what it has no value
for, and each row is written with the columns it names.

A load only adds rows. A row whose primary key is stored refuses the
whole fixture, before anything is written, unless you give `--replace`;
then it replaces the stored row. The load holds its tables' write lock
while it counts and writes (`SHARE ROW EXCLUSIVE` on PostgreSQL,
`BEGIN IMMEDIATE` on SQLite). So its record (`operator.loaddata`) holds
the rows it replaced, a row stored after the plan was printed included.

!!! warning
    A row that replaces another bypasses the aggregate's version, its
    rules, and its events. Keep `--replace` for development data.

### Guarded tables

Guarded tables hold logins, grants, and secrets:

- users (the `AUTH.USER_TABLE` too), groups, permissions and their links;
- tokens, second factors, and recovery codes;
- content types (permissions hang from them) and object grants;
- dynamic settings, which may be secret;
- any table with `info={"guarded": True}`.

A row added to one of them is a login or a privilege. So:

- `loaddata` refuses a fixture that writes one, unless you give
  `--include-guarded`;
- `flush` keeps them, and every table they reference, unless you give
  `--include-guarded`;
- `dumpdata` leaves them out of a dump of every table, and says so on
  standard error. It refuses a table or a domain you name that holds one,
  unless you give `--include-guarded`: a dump of users holds their
  password hashes.

A dump with guarded tables is recorded (`operator.dumpdata`: the tables,
their row counts, and the SHA-256 of what was written). In staging and
production it needs an operator named by `--operator` or
`DOMAINKIT_OPERATOR`. So does, in staging and production, a dump of a
table whose model has fields marked `Personal`, recorded with those
tables (`personal_tables`), as `privacy export` of one person is
recorded: it copies everyone out.

Encrypted columns ([keys.md](../contrib/keys.md)) are dumped sealed, as
stored. `--decrypt` writes their values (`{"plaintext": ...}`). It is
always recorded, with the tables (`decrypted_tables`), and needs a named
operator in staging and production too. `loaddata` seals such values for
the rows it writes.

The audit trail is never loaded or flushed, guarded or not.

A kept table whose rows are about other tables' objects names the column
of their model key (`info={"about_model": "content_type"}`, as object
grants do). `flush` deletes its rows about the models whose tables it
empties. So a grant goes with its object, as with a repository's
`remove`, and an object given a flushed one's id starts with no grants.

### What is never dumped or loaded

State is never dumped or loaded: the audit trail (a copy of its keys
would outlive `audit forget`), tokens, the outbox, idempotency keys,
webhook subscriptions (they hold signing secrets: set them up in each
environment) and deliveries, and any table with
`info={"fixtures": False}`.

A fixture can still hold personal data and password hashes: keep it as
safe as the database. `dumpdata --output` writes a new file that only its
owner can read (mode `0600`). It refuses a path that exists, a symbolic
link too, so a link planted in a shared directory cannot redirect it.
Delete an old fixture before you dump again.

### What `flush` keeps

`flush` keeps append-only tables: those with `info={"append_only": True}`
or, on PostgreSQL, with triggers of their own. It also keeps those with
`info={"flush": False}`, so the audit trail survives it. It keeps guarded
tables too, listed as `kept (guarded)`, unless you give
`--include-guarded`. But it deletes the object grants on the rows it
deletes; the plan says `kept (guarded); delete N rows about emptied
tables`. Serial ids start again on PostgreSQL.

Each of these commands works on one alias (`--database`, default
`default`), as the schema owner (`MIGRATION_URL` when it is set).

### Seeds

`seed` runs the `async` hooks that installed domains register with
`domainkit.apps.hooks.on_seed` in `ready()`: sample data for development,
or defaults that a deployment starts with. Write rows through the
domain's [units of work](../learning.md#words), and make a second run
harmless:

```python
from domainkit.apps import DomainConfig
from domainkit.apps.hooks import on_seed


async def seed_catalog() -> None:
    """Add the sample products once; a second run adds nothing."""


class CatalogConfig(DomainConfig):
    def ready(self) -> None:
        on_seed(seed_catalog)
```

## Squashing migrations

`squashmigrations` writes one revision that does what a run of a target's
revisions did. Its `upgrade` runs each one's `upgrade`, oldest first, and
its `downgrade` runs each one's `downgrade`, newest first. Each body is
copied as written, comments included, into a function of its own, and
the imports are merged. Then the run's files are deleted.

The new revision keeps the id of the newest revision it replaces, and the
`down_revision` of the oldest. So databases at or past the newest
revision, and revisions written after it, need nothing. A database at a
revision inside the run would be at a revision that no longer exists:

- The command refuses while a database in `DATABASES` is there.
- It cannot see other environments. Deploy the squash only once every
  database (staging, production, developers' copies) is at or past the
  newest revision of the run.

It also refuses what it cannot copy and keep correct:

- a merge, a branch, a branch label, or `depends_on` in the run;
- code outside `upgrade` and `downgrade` other than imports (move helpers
  into them first), or two revisions that import different things under
  one name;
- a `downgrade` that raises: the squashed revision could not be reverted
  either.

Comments outside the two functions are not kept. Check the result as you
check any revision: the test database migrates up, down, and up again.

## See also

- [Checks](checks.md): every id that `check` reports.
- [Settings](settings.md): the settings the commands read.
- [Databases](../databases.md): migrations, partitions, and routing.
- [Learning domainkit](../learning.md#a-day-of-work): the commands of a
  day of work.
