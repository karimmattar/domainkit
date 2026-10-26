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

<!-- 0.10 placeholder (K1, DATA-05): "The ledger's state goes back": the
alarm or refusal when immudb answers with a state older than the one the
service verified, and what to do about it. -->

## A cache fails

**You see** `cache_errors_total{cache}` growing, and the cache's log lines.

The cache fails open: while it is down, reads miss and compute their value,
and writes are skipped, so the service answers, more slowly. Shared rate
limits in Redis let requests through meanwhile, except password and code
checks when `AUTH.THROTTLE_FAIL_CLOSED` is set. Redis is skipped for the
cache's `FAILURE_COOLDOWN` after a failure, so a dead Redis costs no
timeout per request.

**Check** Redis: memory (a full Redis refuses writes), its replica role
after a failover (a read-only replica refuses them), and the TLS
certificate's expiry.

**Do** restore Redis; nothing in the cache needs repair, since entries
are recomputed. Mark a cache `READINESS: True` only if the service must
not run without it.

## A background task restarts

**You see** `tasks_restarted_total{task}` (or `tasks_failed_total{task}`)
growing, and the task's exception logged once per crash, with its name.

**Check** the exception: the name says which task (the publisher, a
relay, a cleanup, or the project's own `supervise(..., name=...)`).

**Do** fix the cause. A supervised task restarts after a growing wait (up
to a minute for the framework's), so it recovers on its own once its
dependency does; a task that crashes as soon as it starts is a bug.
