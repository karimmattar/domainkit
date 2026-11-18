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
| `503 database_unavailable`, `/ready` answers 503 | [The database is unavailable](#the-database-is-unavailable) |
| A client asks whether a write that got `503 deadline_exceeded` happened | [A write ran past its deadline](#a-write-ran-past-its-deadline) |
| `409 idempotency_response_unavailable` | [A retry finds that the request ran](#a-retry-finds-that-the-request-ran) |
| `messaging_outbox_lag_seconds` grows | [The outbox backs up](#the-outbox-backs-up) |
| `messaging_outbox_messages{status="dead"}` above zero | [Messages are dead-lettered](#messages-are-dead-lettered) |
| `messaging_outbox_messages{status="held"}` does not fall | [A key's messages stall](#a-keys-messages-stall) |
| A subscriber misses events, or `webhooks deliveries --status failed` lists some | [Webhook deliveries fail](#webhook-deliveries-fail) |
| Login codes, reset links, or notices do not arrive | [Codes and mail do not arrive](#codes-and-mail-do-not-arrive) |
| New processes fail with `KeyProviderError` | [The key service is unreachable](#the-key-service-is-unreachable) |
| `accounting verify` exits 1, or `accounting.E001`/`E002` | [The ledger does not verify](#the-ledger-does-not-verify) |
| A restore is planned | [A backup is restored](#a-backup-is-restored) |
| `messaging_publisher_breaker_open` is 1, or `CircuitOpen` | [A circuit breaker opens](#a-circuit-breaker-opens) |
| `audit_unsealed_rows` grows | [The seal backlog grows](#the-seal-backlog-grows) |
| `audit verify` exits non-zero | [audit verify fails](#audit-verify-fails) |
| `LedgerRolledBack` or `LedgerTampered` | [The ledger's state goes back](#the-ledgers-state-goes-back) |
| `cache_errors_total` grows | [A cache fails](#a-cache-fails) |
| `db_replica_switches_total{to="primary"}` grows | [Reads leave a replica](#reads-leave-a-replica) |
| `503 throttle_unavailable` on logins | [Logins are refused while Redis is down](#logins-are-refused-while-redis-is-down) |
| `tasks_restarted_total` grows | [A background task restarts](#a-background-task-restarts) |
| A new release does not start | [A process refuses to start](#a-process-refuses-to-start) |
| `502` or refused connections while a release rolls out | [Requests fail during a rollout](#requests-fail-during-a-rollout) |
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

## The database is unavailable

**You see** these, while a database is down, restarting, or failing over:

- requests that use it answer `503 database_unavailable`, with
  `Retry-After: 5`. Requests that do not use it are served. A request
  whose connection was lost as it committed gets no `Retry-After`, and a
  detail that says its change may have been made;
- one warning per reason every 10 seconds, with a count of the requests
  since:
  `The database is unavailable (ConnectionRefusedError): GET /orders
  answered 503 database_unavailable`. The reason is the socket's error,
  or a SQLSTATE: class `08` (the connection failed or was lost), `57P01`
  (shutting down), `57P03` (starting up, or a failover), `53300` (too
  many connections). None of it is sent to the error reporters. It is
  told from asyncpg's errors and SQLAlchemy's; through psycopg, a
  connection that cannot be opened may still answer `500
  internal_error`;
- `/ready` answers 503, so the load balancer takes the replicas out.
  With `READINESS_DETAILS` it names `database:<alias>`;
- the worker logs `Could not reach the outbox of '<alias>' (N failures in
  a row); trying again in Ns`, and backs off.

**Check:**

1. The database's own state: is it up, and is it the primary? A failover
   takes from seconds to a couple of minutes.
2. The network and the address: can a replica reach the host and port in
   `DATABASES[alias]["URL"]` (or PgBouncer's)?
3. `53300`: the connections in use against `max_connections`. Processes x
   replicas x (`POOL_SIZE` + `MAX_OVERFLOW`) must stay below it
   ([operations.md](operations.md#sizing)).

**Do:**

- Wait out a restart or a failover: requests answer again by themselves.
  Nothing needs a restart: each pool opens new connections.
- Too many connections: lower the pools, or put PgBouncer in front.
- Tell client developers that `Retry-After` is safe for reads and for
  writes sent with an `Idempotency-Key`. A 503 without it, for a write
  whose connection was lost as it committed, means the write may have
  been applied: look it up before sending it again. Without a key, a
  retry may apply it twice ([idempotency](contrib/idempotency.md)).

## A write ran past its deadline

**You see** a `503 deadline_exceeded` answer to a `POST`, `PUT`,
`PATCH`, or `DELETE`, and the warning `POST /payments ran past its
deadline of 30s`. The client asks whether the change was made:

- with `Retry-After: 1`, the deadline passed before the `COMMIT`: the
  work was rolled back, and nothing was saved;
- without `Retry-After`, and with a detail that says the change may have
  been made, the deadline passed while the `COMMIT` was on the way: the
  database may have applied it ([commit](request-flow.md#8-commit)).

**Check:**

1. The answer's request id (its `X-Request-ID` header, and
   `request_id` in its body) in the logs: what the request did before
   the deadline.
2. The resource itself, or the audit trail's rows of that request id:
   did the change commit?
3. Whether the request carried an `Idempotency-Key`.

**Do:**

- Tell the client what the answer says: with `Retry-After`, nothing was
  saved; without it, the change may have been made.
- A client sends a write again only with the same `Idempotency-Key`. Its
  retry then gets the stored response, or
  `409 idempotency_response_unavailable` if the first request committed
  ([idempotency](contrib/idempotency.md#what-a-retry-gets)). Without a
  key, the client reads the result before it sends the write again.
- Find out why the request was slow, as for
  [requests that are shed](#requests-are-shed). A route that needs more
  time says so with `RequestTimeout`
  ([request deadlines](runtime.md#request-deadlines)).

## A retry finds that the request ran

**You see** `409 idempotency_response_unavailable` answers to requests
with an `Idempotency-Key`, and a client that reports it as an error.

**Check** what the first request with that key did: the audit trail's
rows of its request id, or the resource. The 409 means that the first
request committed, or may have, and that its response is not kept
([what a retry gets](contrib/idempotency.md#what-a-retry-gets)):

- it committed, then failed or died, or answered more than
  `MAX_RESPONSE_BYTES`;
- its commit on another database raised, its reply lost, refused, or
  cancelled while it ran, so it may have committed;
- it committed, and the key's TTL has passed.

**Do** nothing on the service: the 409 keeps the write from running
twice. Tell the client to look the result up instead of retrying, and
give it a resource where it can. Only when the lookup shows that the
first request did not do what was meant does the client send it again,
with a new key.

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
- `messages cancel ID` cancels one message that no worker is running. With
  the audit app, the audit trail records it with your name.

Nothing is lost while the outbox backs up. Messages stay until a worker
runs them, in the same database as the changes that wrote them.

## Messages are dead-lettered

**You see** `messaging_outbox_messages{status="dead"}` above zero, and
the error in `ERROR_REPORTERS` and in the worker's log.

**Check** `messages list dead`: each dead message, its handler, its
attempts, and its last error.

**Do** fix the cause, and deploy the fix. Then run `messages retry ID`,
or `messages retry all`. A dead message never runs again by itself.

The dead letters of `sensitive` tasks (mail, text messages, pushes)
keep no arguments, so `retry` leaves them: send them again from where
they came from, then drop them with `messages drop all --blanked`. Drop
any other one you will not retry with `messages drop ID`. Both are
recorded in the audit trail with your name. A dead message is kept
until it is retried or dropped, or for
`MESSAGING.DEAD_RETENTION_SECONDS` once that is set.

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

## Webhook deliveries fail

**You see** a subscriber report events it never got, deliveries in
`webhooks deliveries --status failed`, or
`messaging_outbox_lag_seconds{queue="webhooks"}` growing. Webhook
deliveries are tasks on the `webhooks` queue (`WEBHOOKS.QUEUE`): each
attempt that fails is tried again, until `MAX_ATTEMPTS`
([how a delivery runs](contrib/webhooks.md#how-a-delivery-runs)).

**Check:**

1. `webhooks deliveries --status failed --subscription ID`: each
   delivery's last status code and last error. A `4xx` or `5xx` is the
   receiver's answer; a timeout or a refused connection is the network
   or the receiver being down; "no key decrypts" is the subscription's
   secret ([secrets at rest](contrib/webhooks.md#secrets-at-rest)).
2. `webhooks deliveries --status pending`, and the lag of the `webhooks`
   queue: does a worker serve that queue (`runworker --queue webhooks`,
   or a worker without `--queue`)?
3. `messages list dead`: a delivery's task that died outside its own
   attempt (its worker killed, or the database lost, on the last attempt)
   is a dead letter, while its delivery may still say `pending`.

**Do:**

- The receiver was down or refused: once it answers, run
  `webhooks replay --failed --subscription ID`. It sends the failed
  deliveries again under the same `webhook-id`, which the receiver uses
  to drop a delivery it already has.
- No worker serves the queue: start one.
- A dead delivery task: fix the cause, then `messages retry ID`.

## Codes and mail do not arrive

**You see** users report that login codes, password reset links,
verification codes, or notices do not arrive. The worker logs
`MailDeliveryFailed` or `NotificationFailed`, and the messages are tried
again until `MAX_ATTEMPTS` (5). Then they are dead-lettered:
`messaging_outbox_messages{status="dead"}` grows with the tasks
`domainkit.mail.send`, `domainkit.notifications.send_sms`, or
`domainkit.notifications.send_push`. A provider called over HTTP also
shows in `http_client_requests_total` and `http_client_circuits_open`
([a circuit breaker opens](#a-circuit-breaker-opens)).

**Check:**

1. `messages list pending` and `messages list dead`: are these tasks
   waiting, retrying, or dead?
2. The worker's log: the provider's error, and whether it refused the
   message for good (`MailRefused`, `NotificationRefused`, dead-lettered
   at once with the provider's code) or failed for a while.
3. The provider's status page, and its credentials and quota.
4. `messaging_outbox_lag_seconds` for the mail and notification queues
   (`MAIL.QUEUE`, `NOTIFICATIONS.QUEUE`, `default` when unset): does a
   worker serve them?

**Do:**

- The provider is down: messages are retried with backoff while it
  recovers. A code a user waits for expires in minutes, so tell users to
  ask for a new one once it is back, rather than wait.
- These tasks are `sensitive`: their dead letters keep no arguments, so
  `messages retry` leaves them. Users ask again for a code; send a notice
  again from where it came from. Then drop the letters with
  `messages drop all --blanked`
  ([messages are dead-lettered](#messages-are-dead-lettered)).
- A refusal for good: fix the address, the number, or the template, as
  its code says ([mail.md](mail.md#errors-and-refusals)).

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
- **Each relay's** (with a broker): after as many failed publications,
  the relaying worker runs its outbox's due messages itself, until the
  broker answers again: `the relay of 'default' runs its due messages
  itself` in its log, then `the relay of 'default' publishes again`.
- **The HTTP client's** (`domainkit.http.client`): after
  `HTTP_CLIENT.BREAKER_FAILURES` failures in a row from one host, calls
  to that host fail at once with `CircuitOpen` for `BREAKER_COOLDOWN`
  seconds. The opening is logged once. `http_client_circuits_open`
  counts each client's open circuits, by the client's name, and
  `http_client_requests_total{outcome="circuit_open"}` the calls refused
  meanwhile.

**Check** the dependency itself: RabbitMQ's health and connections, or
the remote host's status. A breaker that opens and closes again and
again means the dependency is unstable.

**Do** nothing on the service while the dependency recovers. The
publisher's messages wait in the outbox, and the relaying workers run
them; give them `CONCURRENCY` enough for the whole load meanwhile, since
consume-only workers wait for the broker. The
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
Shared rate limits in Redis let requests through meanwhile, or count
them in each process (`rate_limit_fallbacks_total` counts both).
Password and code checks, and requests that send mail, are the
exception: staging and production refuse them
([Logins are refused](#logins-are-refused-while-redis-is-down)). Redis is
skipped for the cache's `FAILURE_COOLDOWN` (5 seconds) after a failure,
so a dead Redis costs no timeout per request.

**Check** Redis: its memory (a full Redis refuses writes), its replica
role after a failover (a read-only replica refuses writes), and the TLS
certificate's expiry.

**Do** restore Redis. Nothing in the cache needs repair, because entries
are computed again. Mark a cache `READINESS: True` only if the service
must not run without it.

## Reads leave a replica

**You see** `db_replica_switches_total{to="primary"}` growing, and
warnings from `domainkit.db.replicas`: "Read-only work on 'default'
reads the primary, not replica 'replica'", with the reason.

A replica with `MAX_LAG` is read only while it is known to be within it.
Past it, or out of reach, read-only work reads the primary, and comes
back once the replica catches up, with an info line
([databases.md](databases.md#lag-and-failover)). The service answers;
the primary carries the reads meanwhile.

**Check** the reason in the warning. "Seconds behind": the replica's
replay (a long query on it holding replay back, its disk, the network
from the primary), and `db_replica_lag_seconds`. "Could not be read":
the replica's server and the network to it. "Unknown": a standby that
has replayed no transaction since it started. Check the primary's load
and its pool (`db_pool_connections`) too, as it now serves the reads.

**Do** restore the replica, or raise its `MAX_LAG` if the reads can bear
more staleness. Nothing needs repair: reads go back on their own.

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

## Requests fail during a rollout

**You see** `502` answers from the load balancer, or refused
connections, while a release rolls out, and none once it is done.

When a process gets `SIGTERM`, the server stops taking new connections
at once, then finishes the requests in flight within
`SERVE["GRACEFUL_TIMEOUT"]`. The load balancer learns that the process
is gone a few seconds later. Meanwhile it still sends it requests, which
find nobody listening.

**Check** the times: when the process got `SIGTERM`, when it stopped
listening, and when the load balancer took it out of its targets.

**Do** keep serving until the load balancer has let the process go:

- On Kubernetes, give the container a `preStop` hook that waits, so
  `SIGTERM` comes after the endpoints are updated
  ([deploying.md](deploying.md#each-rollout)).
- Keep the grace period above the hook's wait, plus
  `SERVE["GRACEFUL_TIMEOUT"]`, plus `SHUTDOWN_TIMEOUT`: the wait counts
  against it.
- Behind another load balancer, delay the stop in the same way, by the
  time it takes to stop sending a target requests.

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
