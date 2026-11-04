# Operations

This page is for the people who deploy and run a domainkit service. It
says what runs, how to configure it from the environment, how to change
the schema without downtime, what to watch, and how to back the service
up. It puts in order what the reference pages say, and links them for the
detail.

On this page you learn how to:

- configure each process from environment variables and secret files;
- run the web processes, the workers, and the migration job;
- point health checks at `/health` and `/ready`, and scrape `/metrics`;
- size processes and database pools;
- change the schema in a rollout, and roll back safely;
- run management commands in production as a named operator;
- read logs, metrics, traces, and request ids, and alert on them;
- back up and restore the databases and the ledger.

[runbook.md](runbook.md) says what to do when an alarm fires.
[security.md](security.md) is the hardening checklist, and
[deploying.md](deploying.md) the checklist from a passing project to one
in production. [upgrading.md](upgrading.md) moves a project to a new
version.

## A first example

A service reads every address and secret from the environment. The
project's settings make an `Env`, which reads variables with the
project's prefix, and reads a variable's file when `<NAME>_FILE` is set:

```python
import os

from domainkit.conf import Env

os.environ["SHOP_SECRET_KEY_FILE"] = "secrets/secret_key"
os.environ["SHOP_ALLOWED_HOSTS"] = "api.example.com,shop.example.com"

env = Env(prefix="SHOP_")
print(env.str("SECRET_KEY"))
print(env.list("ALLOWED_HOSTS"))
print(env.int("WEB_WORKERS", 4))
env.str("DATABASE_URL")
```

With `secrets/secret_key` holding `k3y-from-a-file`, it prints three
lines, then the last line raises:

```text
k3y-from-a-file
['api.example.com', 'shop.example.com']
4
Traceback (most recent call last):
  ...
domainkit.exceptions.ImproperlyConfigured: Set the SHOP_DATABASE_URL environment variable.
```

- `SECRET_KEY` came from the file that `SHOP_SECRET_KEY_FILE` names. The
  file's last line break is dropped.
- `ALLOWED_HOSTS` is a list, split at the commas.
- `WEB_WORKERS` is not set, so the default, 4, is used.
- `DATABASE_URL` has no default and is not set, so the process stops and
  names the variable.

In a project, you do not set the prefix: `Env()` takes it from the
project's name in `pyproject.toml` (`init bankAPI` reads `BANK_API_*`).

## What runs

A service is three kinds of process, from one image and one settings
module:

| Process | Command | How many |
| --- | --- | --- |
| Web | `uvicorn config.asgi:application ...` or Granian ([Running the web processes](#running-the-web-processes)) | One worker process per core, and as many replicas as the load needs |
| Worker | `python manage.py runworker` | At least one per outbox database, more for throughput ([The worker](#the-worker)) |
| Migrations | `python manage.py migrate` | One job per rollout, before the new code serves ([Migrations in a rollout](#migrations-in-a-rollout)) |

Projects with `domainkit.contrib.messaging` installed need the
[worker](learning.md#words). It runs durable event handlers, background
tasks, schedules, webhook deliveries, and the audit trail's sealing.
Without a worker, those wait in the outbox: nothing is lost, but nothing
happens.

## Configuration

The deployment sets `<PREFIX>SETTINGS_MODULE`. For `init bankAPI`, that
is `BANK_API_SETTINGS_MODULE=config.settings.production`. The `.env` file
is for one machine, and is never deployed.

The production settings that `init` writes read every address and
secret from `<PREFIX>*` variables:

| Variable | Required | What it sets |
| --- | --- | --- |
| `ALLOWED_HOSTS` | yes | The hosts callers use, comma-separated. |
| `DATABASE_URL` | yes | The default database, with `SSLMODE = "verify-full"`. |
| `REDIS_URL` | yes | The shared cache (`rediss://`, with a password). It counts requests, and wrong passwords and codes, once for every process. |
| `SECRET_KEY` | yes | 50 random characters or more. Better as `SECRET_KEY_FILE`. |
| `TRUSTED_PROXIES` | yes | The networks of the load balancers, comma-separated, or empty when callers connect directly. |
| `SECRET_KEY_FALLBACKS` | no | Old keys, while what they signed expires. |
| `API_DOCS` | no | `False` unless set: the API documentation is off. |
| `SECURE_HSTS_SECONDS` | no | `31536000` (a year) unless set. |
| `RATE_LIMIT` | no | `300/minute` unless set. |
| `LOG_LEVEL` | no | `INFO` unless set. |
| `ACCESS_LOG_SAMPLE_RATE` | no | `1.0` unless set: every access line. |

Startup fails without a required variable, and names it. While Redis is
down, logins are refused
([runbook.md](runbook.md#logins-are-refused-while-redis-is-down)).

**Secrets as files.** Any variable may be a file instead:
`<NAME>_FILE=/run/secrets/<name>` reads the value from the file a secret
store mounts. The secret is then in no environment listing, crash
report, or `docker inspect`. Setting both `<NAME>` and `<NAME>_FILE` is
refused: `Set either SHOP_SECRET_KEY or SHOP_SECRET_KEY_FILE, not both.`

**Replacing the key.** `SECRET_KEY_FALLBACKS` holds the old key while
what it signed expires, so you replace a key without logging everyone
out ([reference/signing.md](reference/signing.md)).

**Where the settings come from.** Three more variables help where the
project is not run from its own directory:

- `DOMAINKIT_PROJECT_ROOT`: the project's directory, when the working
  directory is not inside it. The image sets it to `/app`.
- `DOMAINKIT_ENV_PREFIX`: the prefix, when the project's
  `pyproject.toml` is not shipped (a wheel).
- `<PREFIX>ENV_FILE`: another env file, or an empty value to read none.
  The process environment always wins over the file.

**The deployment checks.** `ENVIRONMENT = "production"` (or
`"staging"`) turns on the deployment [checks](learning.md#words).
Startup refuses `DEBUG`, wildcard hosts, weak keys, a database without
verified TLS, and the rest of the list in
[reference/checks.md](reference/checks.md). `check` warns about choices
that are valid but weak. Run `python manage.py check --deploy` with the
production settings in CI, against the real variable names, so a refusal
shows up before a rollout.

## Running the web processes

Run uvicorn or Granian with one worker process per core, behind a
reverse proxy or load balancer that limits header sizes and slow
clients. The image that `init` writes runs uvicorn, and
`WEB_CONCURRENCY` sets its worker processes (one unless set). The whole
command, with each limit, is on [reference/servers.md](reference/servers.md).
The two settings that matter most:

- **Shutdown.** The server's graceful timeout
  (`--timeout-graceful-shutdown`) plus `SHUTDOWN_TIMEOUT` (15 seconds)
  must fit in the orchestrator's grace period (30 seconds on
  Kubernetes). Otherwise a stop ends in `SIGKILL`
  ([runtime.md](runtime.md#shutting-down-within-a-budget)).
- **Proxies.** Run uvicorn with `--no-proxy-headers`, and list the
  proxies in `TRUSTED_PROXIES`, so that domainkit, not uvicorn, decides
  each caller's address. Rate limits, audit records, and logs depend on
  it. Have the proxy set `X-Request-ID` too ([request ids](#request-ids)).

## Health checks

The project's root router includes `domainkit.http.health.router`, which
serves two probes. For `/metrics`, install `domainkit[metrics]`, list
`MetricsMiddleware` in `MIDDLEWARE`, and include the `router` that
`domainkit.http.metrics` defines in the root router:

| Path | Answers | Use it for |
| --- | --- | --- |
| `/health` | `200 {"status": "ok"}` while the process serves | Liveness: restart a process that stops answering |
| `/ready` | `200 {"status": "ready"}` when the databases, ledgers, caches marked `READINESS`, and domains answer; else `503 {"status": "unavailable"}` | Readiness: take a replica out of rotation |
| `/metrics` | Prometheus text (`domainkit[metrics]`) | Scraping |

- None of the three is counted against the concurrency cap
  (`CONCURRENCY_EXEMPT_PATHS`), so a busy replica is not taken for a
  dead one.
- `/health` checks nothing but the process. Do not point liveness at
  `/ready`: a database outage would then restart every replica.
- `/ready` runs its checks at most once per `READINESS_CACHE_SECONDS`,
  each within `READINESS_TIMEOUT`
  ([runtime.md](runtime.md#readiness)). With `READINESS_DETAILS = True`,
  a 503 lists the failing checks. Failing checks are always logged.
- A probe must send a `Host` that `ALLOWED_HOSTS` lists. A request with
  any other host, a probe's included, is answered 400.
- In staging and production, `/metrics` needs
  `Authorization: Bearer <METRICS_TOKEN>`. Without `METRICS_TOKEN`, it
  answers `403 metrics_disabled`. A wrong token gets
  `401 unauthorized`.

On Kubernetes:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
    httpHeaders: [{name: Host, value: api.example.com}]
readinessProbe:
  httpGet:
    path: /ready
    port: 8000
    httpHeaders: [{name: Host, value: api.example.com}]
```

## Sizing

Each process works on at most `MAX_CONCURRENT_REQUESTS` requests at once
(1,000), and queues up to `MAX_QUEUED_REQUESTS` (100) for up to
`REQUEST_QUEUE_TIMEOUT` seconds (1). The rest get `503 overloaded` at
once ([runtime.md](runtime.md#load-shedding)). A request that needs the
database waits for a pooled connection for up to `POOL_TIMEOUT` (10
seconds), then gets `503 database_busy`.

- **Set the cap from what a process can finish.** With a pool of
  `POOL_SIZE` plus `MAX_OVERFLOW` connections, requests past a few times
  that number only wait for connections. A lower cap sheds them sooner
  and at less cost.
- **PostgreSQL must accept every pool at once:** web processes ×
  replicas × (`POOL_SIZE` + `MAX_OVERFLOW`), plus the workers' pools,
  plus `migrate`, plus your own sessions, below `max_connections`. Past a
  few hundred connections, put PgBouncer in front, in transaction
  pooling, and set `PGBOUNCER = True`
  ([runtime.md](runtime.md#database-timeouts-and-pgbouncer)).
- **Reads without a transaction** (`Reads.STATEMENTS`) open a second
  pool per alias, `<alias>:reads`. Count it too.
- [performance.md](performance.md) gives what a request costs, and how
  many cores a load needs.

Each process opens up to `POOL_SIZE` + `MAX_OVERFLOW` connections to each
network database (5 + 10 by default), and admits up to
`MAX_CONCURRENT_REQUESTS` requests at once (1,000). Past about four
requests per connection, requests wait for a connection where the
shedding queue does not see them, up to `POOL_TIMEOUT`, then fail with
503. `check` warns about both in staging and production:

- `deploy.W004`: a network database without `POOL_SIZE`;
- `deploy.W006`: a pool that holds fewer than a quarter of
  `MAX_CONCURRENT_REQUESTS`.

The formula is on [reference/backends.md](reference/backends.md#sizing-pools).
The defaults stay as they are: with a pool of 15 and 20 ms per request,
a burst of 1,000 requests was served within 3.3 seconds at a cap of
1,000, where a cap sized to the pool (60) refused 840 of them.

## Migrations in a rollout

`migrate` applies every revision to every database of every target, then
creates partitions. It takes no lock of its own. So run it once per
rollout, as a job, not in each web process's start:

1. `python manage.py migrate --plan` lists what will run, and `--sql`
   prints the SQL, for review. Neither touches the schema.
2. The migration job runs `python manage.py migrate` with the new code,
   while the old code still serves.
3. The new code rolls out. With messaging, roll the workers out first
   when the release adds tasks or durable handlers, and the web
   processes first when it removes them. A worker that does not know a
   message's task gives the message back, and dead-letters it after
   `MESSAGING.UNKNOWN_TARGET_GRACE`
   ([messaging.md](contrib/messaging.md)).

Between steps 2 and 3, the old code runs against the new schema. Keep
each release's schema change one that the old code survives. Spread a
breaking change over two releases (expand, then contract):

| Change | Release N (expand) | Release N+1 (contract) |
| --- | --- | --- |
| Add a column | Nullable, or with a `server_default` (part 2 of the [tutorial](tutorial/2-rules.md) shows one) | Drop the default if it was only for the backfill |
| Rename a column | Add the new column; write both; backfill | Read the new one; drop the old |
| Drop a column | Stop reading and writing it | Drop it |
| Add a `NOT NULL` constraint | Write the value everywhere; backfill | Add the constraint |

**Locks.** A migration statement is exempt from `STATEMENT_TIMEOUT` and
`LOCK_TIMEOUT`, so one that waits for a lock waits for as long as it
takes. On a busy table, take the lock briefly: build indexes with
`postgresql_concurrently=True` outside a transaction, and split
backfills into batches.

**The schema owner.** `DATABASES[alias]["MIGRATION_URL"]` lets `migrate`
connect as the schema owner, while the application keeps a role that
cannot change the schema.

**Tenants.** A database of `TENANCY.SCHEMA_DATABASES` keeps a schema per
[tenant](learning.md#words). `migrate` skips it and says so; migrate it
with `migrate --tenant NAME` (repeat for several), which makes the schema
if it is missing ([tenancy.md](tenancy.md)).

**When the database cannot be reached** (the server is down, the host
unknown, the login refused), `migrate`, `makemigrations`, and
`showmigrations` say so on one line and exit with status 1. The line
names the alias, its driver, host, port, and database (never a user, a
password, or the URL's query), and the driver's reason:

```text
Database 'default' cannot be reached at postgresql+asyncpg://db:5432/app (ConnectionRefusedError: ...). Is the server running, and are its URL (or MIGRATION_URL) and credentials right?
```

A job that retries can wait for the database on that status.

### Rolling back a migration

Prefer to roll forward: write a new revision that undoes the change. A
downgrade can drop data, so domainkit guards it:

- **In staging and production, `migrate --downgrade` needs
  `--allow-data-loss`.** This holds for a tenant's schema too
  (`--tenant`), and for `--fake`. Without it, the command stops:
  `Downgrading in production can drop data; repeat with
  --allow-data-loss after taking a backup.` `--plan` and `--sql` need no
  flag, because they change nothing.
- **In staging and production, each downgrade is recorded in the audit
  trail before it runs**, as
  `operator.migrate.downgrade`, with the database, the revision, and the
  tenant. The record is written first, so a downgrade that drops the
  trail's own tables is still on record. A record that cannot be written
  stops the downgrade. Nothing is recorded without
  `domainkit.contrib.audit`.
- **The operator is named.** `migrate` takes `--operator NAME`, or reads
  `DOMAINKIT_OPERATOR` ([Commands in production](#commands-in-production)).

```bash
python manage.py migrate --plan --downgrade 3f2a9c1d
python manage.py migrate --downgrade 3f2a9c1d --allow-data-loss --operator alice
```

Take a backup first, and keep the old code ready: the code must match
the schema you go back to.

## Commands in production

Commands that change or erase stored data, or credentials, run as a
named operator, and leave a row in the audit trail (with
`domainkit.contrib.audit` installed). The operator is `--operator NAME`,
else `DOMAINKIT_OPERATOR`, else the login name. The command runs as the
actor `operator:<name>`, so what it writes to the trail names that
person. [reference/commands.md](reference/commands.md#operators) lists
every such command.

Three commands that a person runs by hand take `--operator` too:

| Command | What is recorded |
| --- | --- |
| `shell` | What it changes is audited as the operator. In staging and production, the console is recorded as it opens (`operator.shell`), before the first line runs. |
| `dbshell` | In staging and production, its opening is recorded (`operator.dbshell`), with the alias and whether it connected as the owner. What the client does is beyond the audit trail. In production it also needs `--allow-production`. |
| `seed` | What it writes is audited as the operator, and the run is recorded (`operator.seed`) with the domains it seeded. |

```bash
DOMAINKIT_OPERATOR=alice python manage.py shell
python manage.py dbshell --operator alice --allow-production -- -c 'SELECT 1'
python manage.py seed --operator alice
```

`shell` and `dbshell` are recorded before they open because the operator
may close them with the terminal, and leave nothing to record after. A
record that cannot be written stops the command.

`loaddata` and `flush` need an operator named on purpose in staging and
production: `--operator` or `DOMAINKIT_OPERATOR`, not the login name. So
does `dumpdata --include-guarded`. The name is what the operator says: it
attributes an action, it does not authenticate anyone. Keep who can run
`manage.py` against production as narrow as who can reach its database.

## The worker

`python manage.py runworker` claims the outbox's due messages, and runs
at most `MESSAGING.CONCURRENCY` of them at once. It retries failures with
a growing delay until `MAX_ATTEMPTS`, then keeps them as dead letters.
Messages run at least once: a worker that dies mid-message leaves it to
another worker once its lease expires. So handlers must be idempotent,
or use the inbox ([messaging.md](contrib/messaging.md#once-in-the-database-the-inbox)).

- **More throughput.** Add worker processes: they share the outbox
  safely. Give a busy queue workers of its own with `--queue NAME`
  (repeat for several).
- **One database.** `--database` runs the outboxes of one target or
  alias only. By default, a worker runs every database that hosts
  domains.
- **Shutdown.** A stopping worker gives running messages
  `MESSAGING.DRAIN_TIMEOUT` (20 seconds), then shuts down within
  `SHUTDOWN_TIMEOUT`. Give worker pods a grace period of 45 seconds
  ([runtime.md](runtime.md#workers-and-the-outbox)).
- **Metrics.** `runworker --metrics-port 9100` serves the worker's
  Prometheus metrics, on `127.0.0.1` unless `--metrics-address` says
  otherwise.
- **Without a long-running worker.** `runworker --once` runs what is due
  and exits, for cron. Messages then wait for the next run.
- **RabbitMQ.** With `MESSAGING = {"BROKER": ...}`, commits publish to
  the broker and workers consume from it. The outbox stays the source of
  truth, and relays publish what a commit could not.
  `runworker --no-relay` only consumes
  ([messaging.md](contrib/messaging.md#workers-on-rabbitmq)).

`python manage.py messages` inspects the outbox and acts on it:

```bash
python manage.py messages status            # counts by status, and each queue's lag
python manage.py messages list pending      # also: held, locked, dead
python manage.py messages retry 42          # or: retry all
python manage.py messages cancel 42
python manage.py messages release           # unblock keys with held messages and none pending
```

`retry`, `cancel`, and `release` take `--operator`. With
`domainkit.contrib.audit` installed, each one is recorded in the audit
trail with the operator's name.

## Sealing the audit trail

With `domainkit.contrib.audit`, the trail is tamper-evident only with
four things in place
([audit.md](contrib/audit.md#in-staging-and-production)):

1. a ledger (`AUDIT.LEDGER`: immudb with TLS and the server's signing
   key);
2. a namespace on it (`AUDIT.NAMESPACE`);
3. something that seals on a schedule;
4. the `protect()` triggers on both audit tables.

How sealing runs:

- The worker seals every `AUDIT.SEAL_EVERY` (5 minutes), and anchors the
  chain's head on the ledger at most every `AUDIT.ANCHOR_EVERY` (an
  hour). Without the messaging app, run `python manage.py audit seal`
  from cron, and set `AUDIT.SEALED_EXTERNALLY = True`.
- Run `python manage.py audit verify` daily, and alert when it fails. It
  checks every row against its seal, every link, and every anchored
  head, and reports rows older than a day that were never sealed.
- `audit_unsealed_rows` is the backlog. The
  [runbook](runbook.md#the-seal-backlog-grows) says what to do when it
  grows.

**The ledger's state.** With `AUDIT.LEDGER`, each ledger's last verified
state is kept in `AUDIT.DATABASE` (`domainkit_ledger_states`), shared by
every process and replica, and kept across restarts. A ledger whose
state goes back (restored from an older copy) is refused with
`LedgerRolledBack`, and one that forked with `LedgerTampered`. Every
call then fails, and `/ready` answers 503 until someone looks
([runbook.md](runbook.md#the-ledgers-state-goes-back)).

**Exports and pruning.**

- `audit verify --export FILE` writes signed checkpoints of every trail
  for an auditor (Ed25519, `AUDIT.CHECKPOINT_KEY`). Set the private key
  only where `audit verify --export` and `audit prune` run. The
  application verifies with `AUDIT.CHECKPOINT_PUBLIC_KEY`.
- `audit check-export FILE --public-key PEM` checks such a file offline,
  without the databases.
- `audit prune --keep-days DAYS` removes rows sealed before then (at
  least `AUDIT.MIN_RETENTION`). It verifies the chain up to the newest
  anchored seal before the cut, stores a signed checkpoint, then deletes
  the oldest rows, newest first. So a run cut short still verifies, and
  the next run finishes it. `audit verify` starts from that checkpoint.

Keep the checkpoint key out of the database, and its public key where
auditors can find it.

## Backups and restores

The databases hold everything a service knows. The ledger holds the
evidence that the audit trail was not changed. Back up both, separately,
and test restores.

- **PostgreSQL.** Continuous archiving (a base backup and the WAL, as
  pgBackRest, Barman, or a managed service keep them) restores to any
  point in time. `pg_dump` alone loses everything since the dump. Keep
  the outbox's database in the same backup as the domains' tables when
  they are one database. Then a restore never holds a change without its
  messages, or messages without their change.
- **The key service.** With `domainkit.contrib.keys`, a backup opens only
  with the data keys that sealed it. Keep `KEYS.DATA_KEYS` (wrapped) with
  the configuration's history, and never destroy a KEK version in the key
  service while a backup still needs it. A subject's key is in the
  backup, in the database it seals: crypto-shredding reaches a backup
  only when its erasures are applied to it (below).
- **The ledger.** Back up immudb as its documentation says. Its state
  must never go back: a restored ledger that is older than what the
  service verified before is what a rollback attack looks like.
- **After a restore.** Restoring a database to a point before the newest
  anchored head leaves the ledger holding seals the database lacks.
  `audit verify` reports them as rewritten anchors, by number. That is
  the record of what the restore lost: keep its output with the
  incident, and do not try to make it go away. Messages that ran after
  the restore point run again (handlers are idempotent), and messages
  written after it are lost with their changes.
- **Erasures after a restore.** A backup from before an erasure still
  holds the person's keys and their values (crypto-shredding reaches
  only copies made afterwards). Before you restore, list the erasures
  made since the backup's time
  (`manage.py audit log --action privacy.erase`), and keep the list
  outside the databases being restored. Once restored, run
  `manage.py privacy erase --subject <id>` for each
  ([keys.md](contrib/keys.md#backups-taken-before-an-erasure)).
- **SQLite** is for development and tests: one file, on one host.
- **Fixtures are not backups.** `loaddata` only adds rows (`--replace`
  replaces stored ones). It writes no guarded table (users, permissions,
  grants, tokens, second factors, dynamic settings) without
  `--include-guarded`, and `flush` keeps those tables. In staging and
  production both need `--force` and an operator named with `--operator`
  or `DOMAINKIT_OPERATOR`. Each run is audited with the tables it
  touched and, for a load, the fixture's SHA-256
  ([reference/commands.md](reference/commands.md#data-fixtures-flush-and-seeds)).

## Observability

A service tells you what it does through four channels: logs, error
reports, metrics, and traces. Each one carries the request id.

### Logs

Logs are JSON lines on stdout, at `LOG_LEVEL`, with a `request_id`, and
with `trace_id` and `span_id` under a span. A thread writes them, never
the event loop ([runtime.md](runtime.md#logging-and-error-reports)).

- Query strings, bodies, and SQL values are never logged.
- Paths show the route's template (`/accounts/{account_id}`), not the
  path the caller sent ([runtime.md](runtime.md#paths-in-logs)).
- `ACCESS_LOG_SAMPLE_RATE` samples successful requests. Every answer
  from 400 up, and every slow one, is always written
  ([runtime.md](runtime.md#sampling-the-access-log)).
- Refusals (bad hosts, bodies, credentials) are throttled per caller:
  the first line of each caller, then a count every 10 seconds.
- Security events go to the `domainkit.security` logger. Ship them where
  you keep evidence.

### Errors

Every unexpected error (a 500, a dead-lettered message) goes to
`ERROR_REPORTERS`, such as Sentry's SDK. Reporters run on a thread of
their own, in the context of the request that failed
([runtime.md](runtime.md#error-reports)).

### Tracing

Run the service under `opentelemetry-instrument`, or configure the
OpenTelemetry SDK and its exporter yourself (`domainkit[tracing]`).
domainkit makes spans for its own work (commits, worker messages), and
stamps log records with the trace and span ids. Outbox messages carry
the W3C trace context of the commit that queued them, so a worker's span
continues the request's trace
([reference/observability.md](reference/observability.md)).

`domainkit.tracing` gives your own code the same tools. Without
OpenTelemetry installed, each one does nothing:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from domainkit.tracing import remote_parent, span, trace_headers

trace.set_tracer_provider(TracerProvider())

incoming = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
with remote_parent(incoming), span("shop.send_invoice", invoice="A-1001"):
    print(trace_headers())
```

```text
{'traceparent': '00-4bf92f3577b34da6a3ce929d0e0e4736-f4d9761f7fa35670-01'}
```

- `span(name, **attributes)` runs a block inside a span.
- `remote_parent(headers)` makes the trace context in `headers` the
  parent of the spans opened inside. Use it where work arrives from
  another process. Headers that do not follow the W3C format are
  ignored.
- `trace_headers()` returns the current span's `traceparent` (and
  `tracestate`), to send with work that leaves the process. The trace id
  stays the incoming one; the span id is new on each run.

`HttpClient` sends the trace context with every call
([runtime.md](runtime.md#outgoing-http-calls)).

### Request ids

Each request has an id. It appears in the request's log lines, error
reports, and audit rows, in the work it queues, in its outgoing calls
(`X-Request-ID`), and in its answer's `X-Request-ID` header and error
bodies.

- The id is the `X-Request-ID` that a trusted proxy sent
  (`TRUSTED_PROXIES`, or a unix socket's peer with `"unix"`), when it is
  up to 128 letters, digits, `.`, `_`, `:`, or `-`. Otherwise the request
  gets a new id: 32 hex digits.
- Of several header lines, the last is read: the nearest proxy's.
- A caller's own header is dropped. So nobody outside can give its
  requests another request's id, or one id to many requests.

A proxy forwards a client's headers as they came unless it sets them, so
set the id at the proxy. Both ways work: overwrite the client's line
(nginx), or add the proxy's own line after it (HAProxy, Apache's
`RequestHeader add`, Caddy's `header_up +X-Request-ID`):

```nginx
proxy_set_header X-Request-ID $request_id;
```

```text
unique-id-format %{+X}o\ %ci:%cp_%fi:%fp_%Ts_%rt:%pid
unique-id-header X-Request-ID
```

`ACCEPT_REQUEST_ID` chooses whose ids are taken:

| `ACCEPT_REQUEST_ID` | Whose id is taken |
| --- | --- |
| `"proxies"` (default) | A trusted proxy's only. |
| `"never"` | Nobody's: each request gets a new id. Use it when a proxy does not set the header, and would pass a client's on. |
| `"anyone"` | Any caller's, as up to 0.10. For callers inside a private network who correlate their own calls. A caller's id then names its lines in the logs and the trail. |

### Metrics to alert on

Install `domainkit[metrics]`. Every metric and label is on
[reference/observability.md](reference/observability.md). The thresholds
below are starting points: tune them to the service.

| Metric | Alert when | Runbook |
| --- | --- | --- |
| `http_requests_shed_total` | it grows for more than a few minutes | [Requests are shed](runbook.md#requests-are-shed) |
| `http_request_deadlines_exceeded_total` | its rate is more than 1% of requests | [Requests are shed](runbook.md#requests-are-shed) |
| `http_requests_total{status="503"}` | more than 1% of requests | [Requests are shed](runbook.md#requests-are-shed) |
| `messaging_outbox_lag_seconds` | above a few `POLL_INTERVAL`s for 10 minutes | [The outbox backs up](runbook.md#the-outbox-backs-up) |
| `messaging_outbox_messages{status="dead"}` | above zero | [Messages are dead-lettered](runbook.md#messages-are-dead-lettered) |
| `messaging_publisher_breaker_open` | 1 for more than `BREAKER_COOLDOWN` | [A circuit breaker opens](runbook.md#a-circuit-breaker-opens) |
| `audit_unsealed_rows` | above `AUDIT.SEAL_BACKLOG_WARNING`, or growing for an hour | [The seal backlog grows](runbook.md#the-seal-backlog-grows) |
| `cache_errors_total` | it grows | [A cache fails](runbook.md#a-cache-fails) |
| `db_pool_connections{state="checked_out"}` | at the pool's size for minutes | [Requests are shed](runbook.md#requests-are-shed) |
| `tasks_restarted_total` | it grows | [A background task restarts](runbook.md#a-background-task-restarts) |
| `messaging_outbox_messages{status="held"}` | it does not fall for an hour while pending messages run | [A key's messages stall](runbook.md#a-keys-messages-stall) |

These are alarms too:

- `audit verify` fails;
- `/ready` answers 503 on every replica;
- a process fails to start with `KeyProviderError`
  ([The key service is unreachable](runbook.md#the-key-service-is-unreachable));
- `manage.py accounting verify`, run daily where the ledger is
  installed, exits with status 1
  ([The ledger does not verify](runbook.md#the-ledger-does-not-verify)).

## Upgrades

Read [upgrading.md](upgrading.md) for the version, and follow its steps:

- Within 1.x, a minor release breaks nothing. Each deprecated name works,
  with a warning, until 2.0.
- From 0.12, the "0.12 to 1.0" section of upgrading.md is one upgrade,
  applied in the order it gives.
- Find the deprecated names a project still uses with the tests and
  `-W error::domainkit.deprecation.DomainkitDeprecationWarning`.
- Run `makemigrations` and `migrate` (a contrib app's tables may have
  changed) before the tests.
- Run `check --deploy` with the production settings before the rollout.

## Options

Each setting has its full row on [reference/settings.md](reference/settings.md).

| Setting | Default | What it does |
| --- | --- | --- |
| `ENVIRONMENT` | `"local"` | `"staging"` and `"production"` turn on the deployment checks. |
| `SILENCED_CHECKS` | `()` | Warnings that `check --deploy` no longer fails on. |
| `READINESS_TIMEOUT` | `5.0` | Seconds each readiness check may take. |
| `READINESS_CACHE_SECONDS` | `1.0` | Seconds a round of readiness checks is reused. |
| `READINESS_DETAILS` | `False` | List failing checks in `/ready`'s 503. |
| `METRICS_TOKEN` | `None` | The bearer token `/metrics` needs in staging and production. |
| `ACCEPT_REQUEST_ID` | `"proxies"` | Whose `X-Request-ID` is taken: `"proxies"`, `"never"`, or `"anyone"`. |
| `TRUSTED_PROXIES` | `()` | The proxies whose forwarded headers are believed. |
| `MAX_CONCURRENT_REQUESTS` | `1000` | Requests each process works on at once. |
| `SHUTDOWN_TIMEOUT` | `15.0` | Seconds a shutdown may take. |

The database keys that matter here (`POOL_SIZE`, `MAX_OVERFLOW`,
`POOL_TIMEOUT`, `MIGRATION_URL`, `PGBOUNCER`, `SSLMODE`) are on
[reference/backends.md](reference/backends.md#databases-and-migrations).

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `ImproperlyConfigured: Set the <PREFIX>DATABASE_URL environment variable.` | A required variable is missing. | Set it, or its `_FILE`. |
| `Set either <NAME> or <NAME>_FILE, not both.` | Both the variable and its file are set. | Keep one. |
| `CheckFailed` at startup, with ids such as `security.E007` | A setting breaks the deployment baseline. | Fix each listed setting ([reference/checks.md](reference/checks.md)). |
| `Database 'default' cannot be reached at ...`, status 1 | `migrate` could not connect. | Retry the job once the database answers. |
| `Downgrading in production can drop data; repeat with --allow-data-loss after taking a backup.` | A downgrade in staging or production without the flag. | Roll forward instead, or take a backup and add the flag. |
| `Name the operator who runs loaddata in production: ...` | `loaddata` or `flush` without a named operator. | Pass `--operator NAME`, or set `DOMAINKIT_OPERATOR`. |
| `dbshell gives full access to the production database; repeat with --allow-production if that is meant.` | `dbshell` in production. | Add `--allow-production` if you mean it. |
| `400 invalid_host` on probes | The probe's `Host` is not in `ALLOWED_HOSTS`. | Send a listed host. |
| `403 metrics_disabled` on `/metrics` | No `METRICS_TOKEN` in staging or production. | Set the token, and send it as a bearer token. |

## Common mistakes

- **Running `migrate` in each web process's start.** `migrate` takes no
  lock, so several processes would migrate at once. Run it once, as a
  job, before the new code serves.
- **Pointing liveness at `/ready`.** A database outage then restarts
  every replica. Liveness is `/health`.
- **Secrets in environment variables.** They leak through process
  listings and crash reports. Use `<NAME>_FILE` and a secret store.
- **Downgrading to undo a release.** A downgrade can drop data. Write a
  new revision that rolls forward.
- **Shared operator names.** A container that runs as `root` or `app`
  names nobody. Set `DOMAINKIT_OPERATOR` from your access tooling, or
  pass `--operator`.

## See also

- [Deploying](deploying.md): the checklist from a passing project to one
  in production.
- [Runbook](runbook.md): what to do when an alarm fires.
- [Security](security.md): the hardening checklist.
- [Runtime](runtime.md): shutdown, deadlines, shedding, and logs in
  detail.
- [Commands](reference/commands.md#operators): operators and the commands
  they run.
- [Observability](reference/observability.md): every metric and label.
