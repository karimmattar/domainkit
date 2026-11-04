# Runbook

This page says what to do when one of the alarms in
[operations.md](operations.md#metrics-to-alert-on) fires. Each entry has
three parts, in this order: what you see, what to check, and what to do.
The framework's own messages name the setting to change. The log lines
quoted here are what to search for.

Commands below are `python manage.py ...`, run with the production
settings. Commands that change data take `--operator NAME` (or read
`DOMAINKIT_OPERATOR`), so the audit trail names you
([Commands in production](operations.md#commands-in-production)).

## Find the entry

| You see | Entry |
| --- | --- |
| `503 overloaded`, `503 database_busy`, `503 deadline_exceeded` | [Requests are shed](#requests-are-shed) |
| `messaging_outbox_lag_seconds` grows | [The outbox backs up](#the-outbox-backs-up) |
| `messaging_outbox_messages{status="dead"}` above zero | [Messages are dead-lettered](#messages-are-dead-lettered) |
| `messaging_outbox_messages{status="held"}` does not fall | [A key's messages stall](#a-keys-messages-stall) |
| New processes fail with `KeyProviderError` | [The key service is unreachable](#the-key-service-is-unreachable) |
| `accounting verify` exits 1, or `accounting.E001`/`E002` | [The ledger does not verify](#the-ledger-does-not-verify) |
| A restore is planned | [A backup is restored](#a-backup-is-restored) |
| `messaging_publisher_breaker_open` is 1, or `CircuitOpen` | [A circuit breaker opens](#a-circuit-breaker-opens) |
| `audit_unsealed_rows` grows | [The seal backlog grows](#the-seal-backlog-grows) |
| `audit verify` exits non-zero | [audit verify fails](#audit-verify-fails) |
| `LedgerRolledBack` or `LedgerTampered` | [The ledger's state goes back](#the-ledgers-state-goes-back) |
| `cache_errors_total` grows | [A cache fails](#a-cache-fails) |
| `503 throttle_unavailable` on logins | [Logins are refused while Redis is down](#logins-are-refused-while-redis-is-down) |
| `tasks_restarted_total` grows | [A background task restarts](#a-background-task-restarts) |
| A new release does not start | [A process refuses to start](#a-process-refuses-to-start) |
| The migration job fails | [The migration job fails](#the-migration-job-fails) |

## Requests are shed

**You see** one of these:

- `503` answers with the code `overloaded`, a growing
  `http_requests_shed_total{reason}`, and the warning `Shed N request(s)
  past MAX_CONCURRENT_REQUESTS` at most every 10 seconds;
- `503 database_busy`: a request waited `POOL_TIMEOUT` for a connection;
- `503 deadline_exceeded`: a request ran past `REQUEST_TIMEOUT`.

**Check**, in this order:

1. `http_requests_admitted` against `MAX_CONCURRENT_REQUESTS`, and
   `http_requests_queued` against `MAX_QUEUED_REQUESTS`. Is each process
   full? Or is the queue timing out (`reason="queue_timeout"`) behind
   slow requests?
2. `db_pool_connections{state="checked_out"}` against the pool's size. If
   every connection is out, requests wait for the database, not for the
   CPU. `database_busy` answers say the same.
3. The database: slow statements (`pg_stat_activity`, sorted by
   `query_start`), locks (`pg_locks` with `granted = false`), and its CPU.
4. `http_request_duration_seconds` by `route`. One route that slowed down
   can hold every slot.

**Do:**

- More load than capacity: add replicas. Shedding is the system working
  as designed: it answers fast, instead of letting every request time
  out.
- One slow route or query: fix it, or add an index. PostgreSQL already
  cancels a statement past `STATEMENT_TIMEOUT`.
- Pools exhausted while the database is idle: the pool is too small for
  the cap. Lower `MAX_CONCURRENT_REQUESTS` towards a few times
  `POOL_SIZE` + `MAX_OVERFLOW`, or raise the pool within
  `max_connections` ([operations.md](operations.md#sizing)).
- Do not raise `MAX_CONCURRENT_REQUESTS` to stop the 503s. The requests
  then queue for connections instead, and time out after they have held
  memory.

## The outbox backs up

**You see** `messaging_outbox_lag_seconds{queue}` growing: the oldest due
message has waited that long for a worker.
`messaging_outbox_messages{status="pending"}` grows with it.

**Check:**

1. Are workers running, and ready? `messages list locked` lists what they
   hold now. Nothing at all means no worker claims.
2. Are messages failing and being retried? Look at
   `messaging_messages_total{outcome}` by `kind`, and at the worker's
   logs, by the message's handler name.
3. Are they slow? Compare `messaging_message_duration_seconds` by `kind`
   with `MESSAGING.TASK_TIMEOUT`.
4. With RabbitMQ: `messaging_publisher_breaker_open` and
   `messaging_left_to_relay_total`. See
   [the breaker](#a-circuit-breaker-opens).

**Do:**

- No worker claims: start `python manage.py runworker`, with the same
  settings as the web processes, and check its database URL.
- Too slow for the load: add worker processes, or give the busy queue its
  own workers (`runworker --queue NAME`). Raise `MESSAGING.CONCURRENCY`
  only while the database has connections to spare.
- One handler failing: fix it and deploy. Retries go on by themselves,
  and the messages that ran out of attempts are
  [dead letters](#messages-are-dead-lettered).
- `messages cancel ID` drops one message that no worker is running. With
  the audit app, the audit trail records it with your name.

Nothing is lost while the outbox backs up. Messages stay until a worker
runs them, in the same database as the changes that wrote them.

## Messages are dead-lettered

**You see** `messaging_outbox_messages{status="dead"}` above zero, and
the error in `ERROR_REPORTERS` and in the worker's log.

**Check** `messages list dead`: each dead message, its handler, its
attempts, and its last error.

**Do** fix the cause, and deploy the fix. Then run `messages retry ID`,
or `messages retry all`. A dead message is kept until it is retried or
cancelled. It never runs again by itself.

## A key's messages stall

**You see** `messaging_outbox_messages{status="held"}` that does not
fall, while the pending messages run. Messages wait behind an earlier one
of their ordering key
([messaging.md](contrib/messaging.md#one-at-a-time-per-key-ordering-keys)).

**Check:**

1. `messages list held` lists the waiting messages, by key.
2. Is the key's earlier message dead? Run `messages list dead`. A key
   waits behind its dead letter until the letter is retried or
   cancelled.
3. Is the key waiting for a task or handler that no worker knows? The
   worker logs `Waiting for a worker that knows it`, until
   `MESSAGING.UNKNOWN_TARGET_GRACE` passes.
4. Did a worker of a release before 1.0 run during a deploy? Such a
   worker finishes a keyed message without making the next one pending.
   When the cleanup round releases such a key, the worker logs `Made
   message ... pending: its ordering key had held messages and none
   pending`.

**Do:**

- A dead letter in front: fix the cause, then `messages retry ID`. The
  retry puts the message back in its place, before the key's next
  message. Or run `messages cancel ID` to let the rest go.
- An unknown target: deploy the workers that know it (workers before web
  processes, [operations.md](operations.md#migrations-in-a-rollout)).
- No message of the key pending at all: `messages release` makes the
  first held message of each such key pending now, audited with your
  name, instead of waiting for a cleanup round.

## The key service is unreachable

**You see** new processes fail to start, naming the provider and what
failed (`KeyProviderError`). Each process unwraps `KEYS.DATA_KEYS` at
startup, and does not start without them. Processes already running keep
serving: they hold the data keys in memory, and call the service no
more. `keys generate`, `keys rewrap`, and `keys reencrypt` fail too.

**Check:**

1. Can the host reach the service within `KEYS.TIMEOUT`? That is AWS
   KMS's regional endpoint (or `KEYS.AWS_KMS.ENDPOINT`), or
   `KEYS.VAULT.URL`.
2. Are the credentials valid? Look for an expired web identity token, a
   container credentials endpoint the pod cannot reach, or an expired
   Vault token.
3. Does the role still have `Decrypt` (Vault: `decrypt`) on the key, and
   is the key enabled? A disabled KMS key, or one scheduled for deletion,
   refuses every unwrap ([keys.md](contrib/keys.md#providers)).

**Do:**

- Keep the running processes. Do not restart or scale them down until
  the service answers, because their replacements cannot start.
- Restore access (the network, the credentials, the key's policy), then
  start new processes. Nothing needs re-encrypting.
- Never set `KEYS.ALLOW_LOCAL_KEK` to get past an outage. Values sealed
  under the KMS's data keys stay unreadable without the KMS, and new
  values would be sealed under a key derived from `SECRET_KEY`.

## The ledger does not verify

**You see** `accounting verify` fail (it exits with status 1), or
`accounting.E001` and `accounting.E002` refuse to start. The cause is
one of these: an entry that does not balance, a balance that is not the
sum of its postings, an account's `held` that is not the sum of its
active holds, or the append-only triggers missing or changed
([accounting.md](contrib/accounting.md#reports)).

**Check:**

1. What the command printed: each entry or account, and what differs.
2. `audit log --action accounting.entry_posted` for the entries named.
   The trail's `amounts` and `digest` say what each entry moved when it
   was posted, sealed and anchored with the trail.
3. `audit verify`: whether the trail itself was rewritten.
4. Who held the schema owner's role. The triggers keep rows from changing
   for every other role.

**Do:**

- Treat it as an incident: the books changed outside the ledger's code.
- Missing or changed triggers: `python manage.py migrate`, as the owner,
  makes them again.
- Once the cause is known, correct the books with reversals and new
  entries, never by editing rows. Keep the command's output with the
  incident.

## A backup is restored

**You see** nothing: a restore is planned, after data loss or for an
incident.

**Do,** as [operations.md](operations.md#backups-and-restores) says:

1. Before you restore, list the erasures made since the backup's time
   (`audit log --action privacy.erase`). Keep the list outside the
   databases being restored.
2. Restore, then run `privacy erase --subject <id>` for each one. The
   backup holds the erased people's keys and data again
   ([keys.md](contrib/keys.md#backups-taken-before-an-erasure)).
3. Run `audit verify`. Keep what it reports about anchors the restore
   lost with the incident ([audit verify fails](#audit-verify-fails)).

## A circuit breaker opens

Two breakers protect the service from a dependency that fails:

- **The publisher's** (with a broker): after `MESSAGING.BREAKER_FAILURES`
  failed broker calls in a row, commits stop publishing for
  `BREAKER_COOLDOWN` seconds, and leave their messages to the relays.
  `messaging_publisher_breaker_open` is 1, and the opening and the
  closing are each logged once.
- **The HTTP client's** (`domainkit.http.client`): after
  `HTTP_CLIENT.BREAKER_FAILURES` failures in a row from one host, calls
  to that host fail at once with `CircuitOpen` for `BREAKER_COOLDOWN`
  seconds. The opening is logged once.

**Check** the dependency itself: RabbitMQ's health and connections, or
the remote host's status. A breaker that opens and closes again and
again means the dependency is unstable.

**Do** nothing on the service while the dependency recovers. The
publisher's messages wait in the outbox, and the relays send them. The
client's callers get a fast failure instead of a timeout. Once the
dependency answers, one trial closes each breaker. If the publisher's
breaker stays open while RabbitMQ is healthy, check the broker's
credentials and TLS in `BROKERS`.

## The seal backlog grows

**You see** `audit_unsealed_rows` growing, or one of these warnings:

- `Audit sealing of '...' is behind: N rows wait to be sealed`: a run
  left more than `AUDIT.SEAL_BACKLOG_WARNING` rows unsealed;
- `Audit sealing of '...' has not advanced for N seconds`: the oldest row
  waiting is older than `AUDIT.SEAL_EVERY` and `AUDIT.SEAL_LATE_WINDOW`
  together.

**Check:**

1. Does anything seal? The worker seals when
   `domainkit.contrib.messaging` is installed. Otherwise, cron must run
   `audit seal`.
2. The log line `Another sealer is sealing ...`: one sealer per database
   at a time, under a PostgreSQL advisory lock.
3. If a run warns that the oldest waiting row is too old, something that
   does not seal holds the lock. Find the session in `pg_locks`
   (`locktype = 'advisory'`) and `pg_stat_activity`.

**Do:**

- Nothing seals: start the worker, or schedule `audit seal`.
- The volume outgrew the runs: raise `AUDIT.SEAL_BATCHES`, or seal more
  often (`AUDIT.SEAL_EVERY`).
- A stuck session holds the lock: end it (`pg_terminate_backend`).
- A row committed later than the window: once you know why (a long
  transaction, a clock behind), run `audit seal --late`.

Until a row is sealed and its head anchored, someone can change it
unnoticed. Treat a backlog that lasts hours as a gap in the evidence, and
note it.

## audit verify fails

**You see** `audit verify` exit non-zero. It names changed or missing
rows, broken links, rewritten anchors, unanchored seals, or old unsealed
rows.

**Do:**

- Changed, missing, broken, or rewritten: treat it as an incident.
  Someone with the tables' owner role changed the trail, or a restore
  went back past an anchor
  ([operations.md](operations.md#backups-and-restores)). Keep the output:
  it names each row and seal. Do not reseal or edit the trail to make
  the report pass.
- Unanchored seals: anchoring stopped. Check `AUDIT.LEDGER`, and that
  the ledger answers (`/ready` checks it). The next sealing run anchors.
- Old unsealed rows: see [the seal backlog](#the-seal-backlog-grows).

## The ledger's state goes back

**You see** `LedgerRolledBack` (the ledger is behind the state this
service verified) or `LedgerTampered` (the ledger forked: another digest
at the same transaction) in the logs. Every audited write and evidence
check fails, and `/ready` answers 503.

The ledger no longer holds what the service anchored. So domainkit
refuses to write to it or check against it, rather than trust it.

1. Treat it as an incident until shown otherwise: someone restored an
   older copy, or changed the ledger's storage.
2. Ask the ledger's operators whether a restore happened, and when.
3. Run `audit verify` against the databases. If the chain verifies and
   the restore was deliberate, delete the ledger's row from
   `domainkit_ledger_states`. The next call trusts the restored state,
   and records it again.
4. If no one restored it, keep the service refused. Keep the ledger's
   storage for investigation, and check the last export against the
   databases (`audit check-export FILE --public-key PEM`).

## A cache fails

**You see** `cache_errors_total{cache}` growing, and the cache's log
lines.

The cache fails open. While it is down, reads miss and compute their
value, and writes are skipped, so the service answers, more slowly.
Shared rate limits in Redis let requests through meanwhile. Password and
code checks, and requests that send mail, are the exception: staging and
production refuse them
([Logins are refused](#logins-are-refused-while-redis-is-down)). Redis is
skipped for the cache's `FAILURE_COOLDOWN` (5 seconds) after a failure,
so a dead Redis costs no timeout per request.

**Check** Redis: its memory (a full Redis refuses writes), its replica
role after a failover (a read-only replica refuses writes), and the TLS
certificate's expiry.

**Do** restore Redis. Nothing in the cache needs repair, because entries
are computed again. Mark a cache `READINESS: True` only if the service
must not run without it.

## Logins are refused while Redis is down

**You see** `503` answers with the code `throttle_unavailable` and a
`Retry-After`, from the routes that check passwords and codes, or send
mail: `/auth/login`, `/auth/session`, `/auth/mfa/login`,
`/auth/password`, and the password reset, verification, and signup
requests. Other requests are served. `cache_errors_total{cache}` grows
for `RATE_LIMIT_CACHE`, with the warning `Cache '...' is unavailable;
skipping it for N s`, or `Redis refused the rate limit; ... for N s` (out
of memory, or a read-only replica after a failover). With
`RATE_LIMIT_FALLBACK_REPLICAS` set, some answers are `429` instead, once
the local count of a caller fills.

Staging and production fail closed (`AUTH.THROTTLE_FAIL_CLOSED` unset,
since 0.11). An attempt that Redis cannot count could be one guess of
many, so it is refused rather than checked. Users cannot log in until
Redis counts again. Users already logged in are served.

**Check** Redis as for [a cache that fails](#a-cache-fails): that it is
up and reachable, its memory, its replica role, and its TLS
certificate's expiry.

**Do** restore Redis. Logins work again within the cache's
`FAILURE_COOLDOWN` (5 seconds by default), without a restart. To let
users log in during a long outage, set `AUTH.THROTTLE_FAIL_CLOSED =
False` and deploy. Know that passwords and codes can then be guessed
without a limit until you set it back. A gateway's own limit on the
login routes bounds that meanwhile.

## A background task restarts

**You see** `tasks_restarted_total{task}` (or `tasks_failed_total{task}`)
growing, and the task's exception logged once per crash, with its name.

**Check** the exception. The name says which task: the publisher, a
relay, a cleanup, or the project's own `supervise(..., name=...)`.

**Do** fix the cause. A supervised task restarts after a growing wait
(up to a minute for the framework's tasks), so it recovers by itself
once its dependency does. A task that crashes as soon as it starts is a
bug ([runtime.md](runtime.md#background-tasks-spawn-and-supervise)).

## A process refuses to start

**You see** new processes exit at once, with one of these:

- `ImproperlyConfigured: Set the <PREFIX>NAME environment variable.`: a
  required variable is missing.
- `CheckFailed`, with a list of ids such as `security.E007` or
  `security.E101`: a setting breaks the deployment baseline, or a route
  has no access rule.
- `KeyProviderError`: see
  [The key service is unreachable](#the-key-service-is-unreachable).

**Check** the first lines the process wrote. Each problem names its
setting or route, and `CheckFailed` lists every problem at once.

**Do:**

- Keep the old release serving while you fix the new one.
- Set the missing variable, or its `<NAME>_FILE`.
- Fix each listed check ([reference/checks.md](reference/checks.md)). Run
  `check --deploy` with the production settings in CI, so the next
  release finds these before it rolls out.

## The migration job fails

**You see** the migration job exit with status 1.

**Check** its output:

- `Database 'default' cannot be reached at ...`: the server is down, the
  host unknown, or the login refused. The line names the alias, the
  driver, the host, the port, and the database.
- `Multiple head revisions ... Run 'manage.py makemigrations --merge'
  first.`: two branches each added a migration.
- A statement that waits: migrations are exempt from `LOCK_TIMEOUT`, so
  a migration that needs a lock waits for it.

**Do:**

- The database cannot be reached: fix the access, then run the job
  again. The job is safe to run again.
- Several heads: merge them in a new release.
- A migration waits for a lock: find the session that holds it
  (`pg_locks`, `pg_stat_activity`). On a busy table, build indexes
  concurrently, and split backfills
  ([operations.md](operations.md#migrations-in-a-rollout)).
- Do not downgrade to undo a release: roll forward with a new revision
  ([Rolling back a migration](operations.md#rolling-back-a-migration)).

## See also

- [Operations](operations.md): what runs, and what to watch.
- [Runtime](runtime.md): shutdown, deadlines, and shedding.
- [Messaging](contrib/messaging.md#inspecting-and-cancelling): inspecting
  and cancelling messages.
- [Audit](contrib/audit.md#tamper-evidence): sealing and verifying the
  trail.
