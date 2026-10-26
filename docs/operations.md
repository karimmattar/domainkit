# Running domainkit in production

This page is for the people who deploy and run a domainkit service: what
runs, how to configure it, how to change its schema without downtime, what
to watch, and how to back it up. It orders what the reference pages say,
and links them for the detail. [runbook.md](runbook.md) is what to do when
an alarm fires; [security.md](security.md) is the hardening checklist;
[upgrading.md](upgrading.md) moves a project to a new minor version.

## What runs

A service is three kinds of process, from one image and one settings
module:

| Process | Command | How many |
| --- | --- | --- |
| Web | `uvicorn config.asgi:application ...` or Granian ([Running the web processes](#running-the-web-processes)) | One worker process per core, as many replicas as the load needs |
| Worker | `python manage.py runworker` | At least one per outbox database, more for throughput ([The worker](#the-worker)) |
| Migrations | `python manage.py migrate` | One job per rollout, before the new code serves ([Migrations in a rollout](#migrations-in-a-rollout)) |

Projects with `domainkit.contrib.messaging` installed need the worker: it
runs durable event handlers, background tasks, schedules, webhook
deliveries, and the audit trail's sealing. Without it, those wait in the
outbox; nothing is lost, but nothing happens.

## Configuration

The deployment sets `<PREFIX>SETTINGS_MODULE` (for `init bankAPI`,
`BANK_API_SETTINGS_MODULE=config.settings.production`); `.env` is for one
machine and is never deployed. The production settings `init` writes read
every address and secret from `<PREFIX>*` variables:

- `ALLOWED_HOSTS`, `DATABASE_URL`, `SECRET_KEY`, and `TRUSTED_PROXIES`
  (the networks of the load balancers, or empty when callers connect
  directly) are required; startup fails, naming the variable, without them.
- Any variable may instead be a file: `<NAME>_FILE=/run/secrets/<name>`
  reads the secret from the file a secret store mounts, so it is in no
  environment listing. Setting both is refused.
- `SECRET_KEY_FALLBACKS` holds the old key while what it signed expires,
  so a key is replaced without logging everyone out.

`ENVIRONMENT = "production"` (or `"staging"`) turns on the deployment
checks: startup refuses `DEBUG`, wildcard hosts, weak keys, a database
without verified TLS, and the rest of the list in
[reference.md](reference.md#deployment-checks), and `check` warns about
choices that are valid but weak. Run `python manage.py check` with the
production settings in CI, against the real variable names, so a refusal
shows up before a rollout.

## Running the web processes

Run uvicorn or Granian with one worker process per core, behind a reverse
proxy or load balancer that limits header sizes and slow clients. The
whole command, with each limit, is in
[reference.md](reference.md#server-limits); the two that matter most:

- **Shutdown.** The server's graceful timeout (`--timeout-graceful-shutdown`)
  plus `SHUTDOWN_TIMEOUT` (15 seconds) must fit in the orchestrator's grace
  period (30 seconds on Kubernetes), or a stop ends in `SIGKILL`
  ([runtime.md](runtime.md#shutting-down-within-a-budget)).
- **Proxies.** Run uvicorn with `--no-proxy-headers` and list the proxies
  in `TRUSTED_PROXIES`, so domainkit, not uvicorn, decides each caller's
  address: rate limits, audit records, and logs depend on it.

The probes:

| Path | Answers | Use it for |
| --- | --- | --- |
| `/health` | `200` while the process serves | Liveness: restart a process that stops answering |
| `/ready` | `200` when the databases, ledgers, caches marked `READINESS`, and domains answer; else `503` | Readiness: take a replica out of rotation |
| `/metrics` | Prometheus text (`domainkit[metrics]`) | Scraping |

None of the three is counted against the concurrency cap or the rate
limits, so a busy replica is not taken for a dead one.

## Sizing

Each process works on at most `MAX_CONCURRENT_REQUESTS` requests at once
(1000) and queues up to `MAX_QUEUED_REQUESTS` (100) for up to
`REQUEST_QUEUE_TIMEOUT` second; the rest get `503 overloaded` at once
([runtime.md](runtime.md#load-shedding)). A request that needs the
database waits for a pooled connection for up to `POOL_TIMEOUT` (10
seconds), then gets `503 database_busy`.

- Set the cap from what a process can finish. With a pool of `POOL_SIZE`
  plus `MAX_OVERFLOW` connections, requests past a few times that only
  wait for connections; a lower cap sheds them sooner and cheaper.
- PostgreSQL must accept every pool at once: web processes × replicas ×
  (`POOL_SIZE` + `MAX_OVERFLOW`), plus the workers' pools, plus
  `migrate`, plus your own sessions, below `max_connections`. Past a few
  hundred, put PgBouncer in front, in transaction pooling, and set
  `PGBOUNCER = True` ([runtime.md](runtime.md#database-timeouts-and-pgbouncer)).
- [performance.md](performance.md) gives what a request costs, and how many
  cores a load needs.

Each process opens up to `POOL_SIZE` + `MAX_OVERFLOW` connections to
each network database (5 + 10 by default), and admits up to
`MAX_CONCURRENT_REQUESTS` requests at once (1000). Past about four
requests per connection, requests wait for a connection where the
shedding queue does not see them, up to `POOL_TIMEOUT`, then fail with
503. `check` warns about both in staging and production: `deploy.W004`
for a network database without `POOL_SIZE`, and `deploy.W006` for a pool
that holds fewer than a quarter of `MAX_CONCURRENT_REQUESTS`. Size the pool
so that processes × (`POOL_SIZE` + `MAX_OVERFLOW`) stays below the
server's `max_connections`, with room for migrations and the worker; the
formula is in [reference.md](reference.md#sizing-pools). The defaults stay
as they are: with a pool of 15 and 20 ms per request, a burst of 1,000
requests was served within 3.3 seconds at a cap of 1,000, where a cap
sized to the pool (60) refused 840 of them.

## Migrations in a rollout

`migrate` applies every revision to every database of every target, then
creates partitions. It takes no lock of its own, so run it once per
rollout, as a job, not in each web process's start:

1. `python manage.py migrate --plan` lists what will run, and `--sql`
   prints the SQL, for review, without touching the schema.
2. The migration job runs `python manage.py migrate` with the new code,
   while the old code still serves.
3. The new code rolls out.

Between steps 2 and 3, the old code runs against the new schema. Keep each
release's schema change one the old code survives, and spread a breaking
change over two releases (expand, then contract):

| Change | Release N (expand) | Release N+1 (contract) |
| --- | --- | --- |
| Add a column | Nullable, or with a `server_default` (part 2 of the [tutorial](tutorial/2-rules.md) shows one) | Drop the default if it was only for the backfill |
| Rename a column | Add the new column; write both; backfill | Read the new one; drop the old |
| Drop a column | Stop reading and writing it | Drop it |
| Add a `NOT NULL` constraint | Write the value everywhere; backfill | Add the constraint |

A migration statement is exempt from `STATEMENT_TIMEOUT` and
`LOCK_TIMEOUT`, so one that waits for a lock waits for it. On a busy
table, take the lock briefly: build indexes with
`postgresql_concurrently=True` outside a transaction, and split backfills
into batches. `DATABASES[alias]["MIGRATION_URL"]` lets `migrate` connect
as the schema owner while the application keeps a role that cannot change
the schema. In staging and production, `migrate --downgrade` also needs
`--allow-data-loss`: roll forward with a new revision instead.

## The worker

`python manage.py runworker` claims the outbox's due messages and runs at
most `MESSAGING.CONCURRENCY` of them at once, retrying failures with a
growing delay until `MAX_ATTEMPTS`, then keeping them as dead letters.
Messages are at least once: a worker that dies mid-message leaves it to
another once its lease expires, so handlers must be idempotent.

- **More throughput.** Add worker processes; they share the outbox safely.
  Give a busy queue workers of its own with `--queue`.
- **Shutdown.** A stopping worker gives running messages
  `MESSAGING.SHUTDOWN_TIMEOUT` (20 seconds), then shuts down within
  `SHUTDOWN_TIMEOUT`: give worker pods a grace period of 45 seconds
  ([runtime.md](runtime.md#workers-and-the-outbox)).
- **Metrics.** `runworker --metrics-port 9100` serves the worker's
  Prometheus metrics.
- **Without a long-running worker.** `runworker --once` runs what is due
  and exits, for cron; messages then wait for the next run.
- **RabbitMQ.** With `MESSAGING = {"BROKER": ...}`, commits publish to the
  broker and workers consume from it; the outbox stays the source of
  truth, and relays publish what a commit could not
  ([contrib.md](contrib/messaging.md#workers-on-rabbitmq)).

`python manage.py messages` lists what waits (`--pending`), runs
(`--locked`), or failed (`--dead`), and `--retry` or `--cancel` acts on
one; with `domainkit.contrib.audit` installed, each action is recorded in
the audit trail with the operator's name.

## Sealing the audit trail

With `domainkit.contrib.audit`, the trail is tamper-evident only with four
things in place: a ledger (`AUDIT.LEDGER`, immudb with TLS and the server's
signing key), a namespace on it (`AUDIT.NAMESPACE`), something that seals
on a schedule, and the `protect()` triggers on both audit tables
([contrib.md](contrib/audit.md#in-staging-and-production)).

- The worker seals every `AUDIT.SEAL_EVERY` (5 minutes) and anchors the
  chain's head on the ledger at most every `AUDIT.ANCHOR_EVERY` (an hour).
  Without the messaging app, run `python manage.py auditseal` from cron and
  set `AUDIT.SEALED_EXTERNALLY = True`.
- Run `python manage.py auditverify` daily, and alert when it fails: it
  checks every row against its seal, every link, and every anchored head,
  and reports rows older than a day that were never sealed.
- `audit_unsealed_rows` is the backlog; the [runbook](runbook.md#the-seal-backlog-grows)
  says what to do when it grows.

With `AUDIT.LEDGER`, each ledger's last verified state is kept in
`AUDIT.DATABASE` (`domainkit_ledger_states`), shared by every process and
replica, and survives restarts. A ledger whose state goes back (restored
from an older copy) is refused with `LedgerRolledBack`, and one that
forked with `LedgerTampered`: every call fails and `/ready` answers 503
until someone looks ([runbook.md](runbook.md#the-ledgers-state-goes-back)).

`auditverify --export FILE` writes signed checkpoints of every trail
(Ed25519, `AUDIT.CHECKPOINT_KEY`) for an auditor, and `auditverify
--check-export FILE --public-key PEM` checks such a file offline, without
the databases. `auditprune --keep-days DAYS` removes rows sealed before
then: it verifies the chain up to the newest anchored seal before the cut,
stores a signed checkpoint, then deletes the oldest rows newest first, so a
run cut short still verifies and the next one finishes it. `auditverify`
starts from that checkpoint. Keep the checkpoint key out of the database,
and its public key where auditors can find it.

## Backups and restores

The databases hold everything a service knows; the ledger holds the
evidence that the audit trail was not changed. Back up both, separately,
and test restores.

- **PostgreSQL.** Continuous archiving (a base backup and the WAL, as
  pgBackRest, Barman, or a managed service keep them) restores to any
  point in time; `pg_dump` alone loses everything since the dump. Keep the
  outbox's database in the same backup as the domains' tables when they
  are one database, so a restore never holds a change without its
  messages, or messages without their change.
- **The ledger.** Back up immudb as its documentation says. Its state
  must never go back: a restored ledger that is older than what the
  service verified before is what a rollback attack looks like.
- **After a restore.** Restoring a database to a point before the newest
  anchored head leaves the ledger holding seals the database lacks:
  `auditverify` reports them as rewritten anchors, by number. That is the
  record of what the restore lost; keep its output with the incident, and
  do not try to make it go away. Messages that ran after the restore point
  run again (handlers are idempotent), and messages written after it are
  lost with their changes.
- **SQLite** is for development and tests: one file, on one host.

## Observability

- **Logs** are JSON lines on stdout with a `request_id`, and `trace_id`
  and `span_id` under a span. Query strings, bodies, and SQL values are
  never logged. `ACCESS_LOG_SAMPLE_RATE` samples successful requests;
  every answer from 400 up, and every slow one, is always written.
- **Errors**: every unexpected error (a 500, a dead-lettered message) goes
  to `ERROR_REPORTERS`, such as Sentry's SDK.
- **Traces**: run under `opentelemetry-instrument`; outbox messages carry
  the trace context of the commit that queued them
  ([reference.md](reference.md#observability)).

**Metrics to alert on** (`domainkit[metrics]`; every metric and label is
in [reference.md](reference.md#observability)). The thresholds are
starting points; tune them to the service.

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

`auditverify` failing, and `/ready` answering `503` on every replica, are
alarms too.

## Upgrades

Upgrade one minor version at a time, reading
[upgrading.md](upgrading.md): each **Breaking** change says how to keep
the old behaviour, and each deprecated name works, with a warning, until
the next minor. Run the test suite with deprecations as errors
(`pytest -W error::DeprecationWarning`) to find the deprecated names a
project still uses, then `makemigrations` (a contrib app's tables may have changed) and
`check` with the production settings, before the rollout.
