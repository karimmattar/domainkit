# Runbook

What to do when one of the alarms in [operations.md](operations.md#observability)
fires. Each entry says what you see, what to check, and what to do, in
that order. The framework's own messages name the setting to change; the
log lines quoted here are what to search for.

## Requests are shed

**You see** `503` answers with the code `overloaded`, a growing
`http_requests_shed_total{reason}`, and the warning "Shed N request(s)
past MAX_CONCURRENT_REQUESTS" at most every 10 seconds; or `503 database_busy`, or `503
deadline_exceeded`.

**Check**, in this order:

1. `http_requests_admitted` against `MAX_CONCURRENT_REQUESTS`, and
   `http_requests_queued` against `MAX_QUEUED_REQUESTS`: is each process
   full, or is the queue timing out (`reason="queue_timeout"`) behind slow
   requests?
2. `db_pool_connections{state="checked_out"}` against the pool's size: if
   every connection is out, requests wait for the database, not for CPU.
   `database_busy` answers say the same.
3. The database: slow statements (`pg_stat_activity`, sorted by
   `query_start`), locks (`pg_locks` with `granted = false`), and its CPU.
4. `http_request_duration_seconds` by `route`: one route that slowed down
   can hold every slot.

**Do:**

- More load than capacity: add replicas. Shedding is the system working
  as designed: it answers fast instead of letting every request time out.
- One slow route or query: fix or index it; a statement past
  `STATEMENT_TIMEOUT` is cancelled by PostgreSQL already.
- Pools exhausted while the database is idle: the pool is too small for
  the cap. Lower `MAX_CONCURRENT_REQUESTS` towards a few times
  `POOL_SIZE` + `MAX_OVERFLOW`, or raise the pool within
  `max_connections` ([operations.md](operations.md#sizing)).
- Do not raise `MAX_CONCURRENT_REQUESTS` to stop the 503s: the requests
  then queue for connections instead, and time out after holding memory.

## The outbox backs up

**You see** `messaging_outbox_lag_seconds{queue}` growing: the oldest due
message has waited that long for a worker. `messaging_outbox_messages{status="pending"}`
grows with it.

**Check:**

1. Are workers running, and ready? `python manage.py messages --locked`
   lists what they hold now; none at all means no worker claims.
2. Are messages failing and being retried? `messaging_messages_total{outcome}`
   by `kind`, and the logs of the worker, by the message's handler name.
3. Are they slow? `messaging_message_duration_seconds` by `kind`, against
   `MESSAGING.TASK_TIMEOUT`.
4. With RabbitMQ: `messaging_publisher_breaker_open` and
   `messaging_left_to_relay_total`; see [the breaker](#a-circuit-breaker-opens).

**Do:**

- No worker claims: start `python manage.py runworker`, with the same
  settings as the web processes, and check its database URL.
- Too slow for the load: add worker processes, or give the busy queue its
  own workers (`runworker --queue`); raise `MESSAGING.CONCURRENCY` only
  while the database has connections to spare.
- One handler failing: fix it and deploy; retries continue on their own,
  and those that ran out are [dead letters](#messages-are-dead-lettered).
- `python manage.py messages --cancel ID` drops one message no worker is
  running; with the audit app, the audit trail records it with your name
  (`--operator`).

Nothing is lost while the outbox backs up: messages stay until a worker
runs them, in the same database as the changes that wrote them.

## Messages are dead-lettered

**You see** `messaging_outbox_messages{status="dead"}` above zero, and the
error in `ERROR_REPORTERS` and the worker's log.

**Check** `python manage.py messages --dead`: each dead message, its
handler, attempts, and last error.

**Do** fix the cause, deploy the fix, then
`python manage.py messages --retry ID` (or `--retry all`). A dead message
is kept until retried or cancelled; it never runs again on its own.

## A key's messages stall

**You see** `messaging_outbox_messages{status="held"}` that does not fall,
while the pending messages are run: messages wait behind an earlier one of
their ordering key ([messaging.md](contrib/messaging.md#one-at-a-time-per-key-ordering-keys)).

**Check:**

1. `python manage.py messages --held` lists the waiting messages, by key.
2. Is the key's earlier message dead? `messages --dead`: a key waits
   behind its dead letter until it is retried or cancelled.
3. Is it waiting for a task or handler no worker knows? The worker logs
   "Waiting for a worker that knows it", until
   `MESSAGING.UNKNOWN_TARGET_GRACE` passes.
4. Did a worker of a release before 1.0 run during a deploy? It finishes
   a keyed message without making the next one pending; the worker logs
   "Made message ... pending: its ordering key had held messages and none
   pending" when its cleanup round releases such a key.

**Do:**

- A dead letter in front: fix the cause, then `messages --retry ID`, which
  puts it back in its place, before the key's next message; or
  `messages --cancel ID` to let the rest go.
- An unknown target: deploy the workers that know it (workers before web
  processes, [operations.md](operations.md#migrations-in-a-rollout)).
- No message of the key pending at all: `python manage.py messages
  --release` makes the first held message of each such key pending now,
  audited with your name, instead of waiting for a cleanup round.

## The key service is unreachable

**You see** new processes fail to start, naming the provider and what
failed (`KeyProviderError`): each process unwraps `KEYS.DATA_KEYS` at
startup, and does not start without them. Processes already running keep
serving: they hold the data keys in memory and call the service no more.
`manage.py keys generate`, `rewrap`, and `reencrypt` fail too.

**Check:**

1. Can the host reach the service? AWS KMS's regional endpoint (or
   `KEYS.AWS_KMS.ENDPOINT`), or `KEYS.VAULT.URL`, within `KEYS.TIMEOUT`.
2. Are the credentials valid? An expired web identity token, a container
   credentials endpoint the pod cannot reach, an expired Vault token.
3. Does the role still have `Decrypt` (Vault: `decrypt`) on the key, and
   the key its state? A disabled or scheduled-for-deletion KMS key refuses
   every unwrap ([keys.md](contrib/keys.md#providers)).

**Do:**

- Keep the running processes: do not restart or scale them down until the
  service answers, as their replacements cannot start.
- Restore access (network, credentials, the key's policy), then start new
  processes; nothing needs re-encrypting.
- Never set `KEYS.ALLOW_LOCAL_KEK` to get past an outage: values sealed
  under the KMS's data keys stay unreadable without it, and new ones would
  be sealed under a key derived from `SECRET_KEY`.

## The ledger does not verify

**You see** `python manage.py accounting verify` fail (it exits 1), or
`accounting.E001` and `accounting.E002` refuse to start: an entry that
does not balance, a balance that is not the sum of its postings, an
account's `held` that is not the sum of its active holds, or the
append-only triggers missing or changed
([accounting.md](contrib/accounting.md#reports)).

**Check:**

1. What the command printed: each entry or account, and what differs.
2. `auditlog --action accounting.entry_posted` for the entries named: the
   trail's `amounts` and `digest` say what each entry moved when it was
   posted, sealed and anchored with the trail.
3. `auditverify`: whether the trail itself was rewritten.
4. Who held the schema owner's role: the triggers keep rows from changing
   for every other role.

**Do:**

- Treat it as an incident: the books changed outside the ledger's code.
- Missing or changed triggers: `python manage.py migrate` makes them
  afresh, as the owner.
- Correct the books with reversals and new entries, never by editing rows,
  once the cause is known, and keep the command's output with the
  incident.

## A backup is restored

**You see** nothing: a restore is planned, after data loss or for an
incident.

**Do,** as [operations.md](operations.md#backups-and-restores) says:

1. Before restoring, list the erasures made since the backup's time
   (`manage.py auditlog --action privacy.erase`), and keep the list
   outside the databases being restored.
2. Restore, then run `manage.py privacy erase --subject <id>` for each:
   the backup holds the erased people's keys and data again
   ([keys.md](contrib/keys.md#backups-taken-before-an-erasure)).
3. Run `auditverify`, and keep what it reports about anchors the restore
   lost with the incident ([auditverify fails](#auditverify-fails)).

## A circuit breaker opens

Two breakers protect the service from a dependency that fails:

- **The publisher's** (with a broker): after `MESSAGING.BREAKER_FAILURES`
  failed broker calls in a row, commits stop publishing for
  `BREAKER_COOLDOWN` seconds and leave their messages to the relays.
  `messaging_publisher_breaker_open` is 1, and the opening and closing are
  each logged once.
- **The HTTP client's** (`domainkit.http.client`): after
  `HTTP_CLIENT.BREAKER_FAILURES` failures in a row from one host, calls to
  it fail at once with `CircuitOpen` for `BREAKER_COOLDOWN` seconds; the
  opening is logged once.

**Check** the dependency itself: RabbitMQ's health and connections, or the
remote host's status. A breaker that opens and closes again and again
means the dependency is flapping.

**Do:** nothing on the service while the dependency recovers: the
publisher's messages wait in the outbox and the relays send them, and
the client's callers get a fast failure instead of a timeout. Once the
dependency answers, one trial closes each breaker. If the publisher's
breaker stays open while RabbitMQ is healthy, check the broker's
credentials and TLS in `BROKERS`.

## The seal backlog grows

**You see** `audit_unsealed_rows` growing, or the warning that a run left
more than `AUDIT.SEAL_BACKLOG_WARNING` rows unsealed, or the warning that
the oldest row waiting is older than `AUDIT.SEAL_EVERY` and
`AUDIT.SEAL_LATE_WINDOW` together.

**Check:**

1. Does anything seal? The worker seals when `domainkit.contrib.messaging`
   is installed; otherwise cron must run `python manage.py auditseal`.
2. The log line saying another sealer is at work: one sealer per database
   at a time, under a PostgreSQL advisory lock.
3. If a run warns that the oldest waiting row is too old, the lock is held
   by something that does not seal: find the session in `pg_locks`
   (`locktype = 'advisory'`) and `pg_stat_activity`.

**Do:**

- Nothing seals: start the worker, or schedule `auditseal`.
- The volume outgrew the runs: raise `AUDIT.SEAL_BATCHES`, or seal more
  often (`AUDIT.SEAL_EVERY`).
- A stuck session holds the lock: end it (`pg_terminate_backend`).
- A row that committed later than the window: once you know why (a long
  transaction, a clock behind), `python manage.py auditseal --late`.

Until a row is sealed and its head anchored, it can be changed unnoticed:
treat a backlog that lasts hours as a gap in the evidence, and note it.

## auditverify fails

**You see** `python manage.py auditverify` exit non-zero, naming changed or
missing rows, broken links, rewritten anchors, unanchored seals, or old
unsealed rows.

**Do:**

- Treat changed, missing, broken, or rewritten as an incident: someone
  with the tables' owner role changed the trail, or a restore went back
  past an anchor ([operations.md](operations.md#backups-and-restores)).
  Keep the output; it names each row and seal. Do not reseal or edit the
  trail to make the report pass.
- Unanchored seals: anchoring stopped. Check `AUDIT.LEDGER` and that the
  ledger answers (`/ready` checks it); the next sealing run anchors.
- Old unsealed rows: see [the seal backlog](#the-seal-backlog-grows).

## The ledger's state goes back

**You see** `LedgerRolledBack` (the ledger is behind the state this
service verified) or `LedgerTampered` (it forked: another digest at the
same transaction) in the logs, every audited write and evidence check
failing, and `/ready` answering 503.

The ledger no longer holds what the service anchored, so domainkit
refuses to write to it or check against it rather than trust it.

1. Treat it as an incident until shown otherwise: someone restored an
   older copy, or changed the ledger's storage.
2. Ask the ledger's operators whether a restore happened, and when.
3. Run `auditverify` against the databases. If the chain verifies and the
   restore was deliberate, delete the ledger's row from
   `domainkit_ledger_states`; the next call trusts the restored state and
   records it again.
4. If no one restored it, keep the service refused, keep the ledger's
   storage for investigation, and check the last export
   (`auditverify --check-export`) against the databases.

## A cache fails

**You see** `cache_errors_total{cache}` growing, and the cache's log lines.

The cache fails open: while it is down, reads miss and compute their value,
and writes are skipped, so the service answers, more slowly. Shared rate
limits in Redis let requests through meanwhile, except password and code
checks, and requests that send mail, which staging and production refuse
([Logins are refused](#logins-are-refused-while-redis-is-down)). Redis is
skipped for the cache's `FAILURE_COOLDOWN` after a failure, so a dead
Redis costs no timeout per request.

**Check** Redis: memory (a full Redis refuses writes), its replica role
after a failover (a read-only replica refuses them), and the TLS
certificate's expiry.

**Do** restore Redis; nothing in the cache needs repair, since entries
are recomputed. Mark a cache `READINESS: True` only if the service must
not run without it.

## Logins are refused while Redis is down

**You see** `503` answers with the code `throttle_unavailable` and a
`Retry-After` from the routes that check passwords and codes, or send
mail: `/auth/login`, `/auth/session`, `/auth/mfa/login`,
`/auth/password`, and password reset, verification, and signup requests.
Other requests are served. `cache_errors_total{cache}` grows for
`RATE_LIMIT_CACHE`, with the warning "Cache '...' is unavailable;
skipping it for N s", or "Redis refused the rate limit; ... for N s"
(out of memory, a read-only replica after a failover). With
`RATE_LIMIT_FALLBACK_REPLICAS` set, some answers are `429` instead, once
the local count of a caller fills.

Staging and production fail closed (`AUTH.THROTTLE_FAIL_CLOSED` unset,
since 0.11): an attempt Redis cannot count could be one guess of many, so
it is refused rather than checked. Users cannot log in until Redis counts
again; those already logged in are served.

**Check** Redis as for [a cache that fails](#a-cache-fails): that it is
up and reachable, its memory, its replica role, and its TLS certificate's
expiry.

**Do** restore Redis. Logins work again within the cache's
`FAILURE_COOLDOWN` (5 seconds by default), without a restart. To let users
log in during a long outage, set `AUTH.THROTTLE_FAIL_CLOSED = False` and
deploy, knowing that passwords and codes can then be guessed without a
limit until it is set back; a gateway's own limit on the login routes
bounds that meanwhile.

## A background task restarts

**You see** `tasks_restarted_total{task}` (or `tasks_failed_total{task}`)
growing, and the task's exception logged once per crash, with its name.

**Check** the exception: the name says which task (the publisher, a
relay, a cleanup, or the project's own `supervise(..., name=...)`).

**Do** fix the cause. A supervised task restarts after a growing wait (up
to a minute for the framework's), so it recovers on its own once its
dependency does; a task that crashes as soon as it starts is a bug.
