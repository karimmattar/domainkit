# Optional apps

Install an app by adding it to `INSTALLED_DOMAINS`, then run
`makemigrations` and `migrate`: each one brings its own tables. Each is
configured by one setting, a dictionary whose keys are all optional.

| App | Setting | Tables |
| --- | --- | --- |
| `domainkit.contrib.messaging` | `MESSAGING` | `domainkit_messages` |
| `domainkit.contrib.audit` | `AUDIT` | `domainkit_audit_log` |
| `domainkit.contrib.idempotency` | `IDEMPOTENCY` | `domainkit_idempotency_keys` |
| `domainkit.contrib.evidence` | `EVIDENCE` | none: records live on a ledger |
| `domainkit.contrib.contenttypes` | none | `domainkit_content_types` |
| `domainkit.contrib.auth` | `AUTH` | users, groups, permissions; see [auth.md](auth.md) |

With several databases ([databases.md](databases.md)), the outbox and the
audit trail commit with business data, so their tables are created in every
database that hosts a domain. The idempotency table stays in its own
`DATABASE`.

## Messaging: outbox, durable handlers, background tasks

Work that must happen after a change commits, even if the process dies right
after, is written to an outbox table **in the same transaction as the
change**. A worker process (`python manage.py runworker`) runs it.

```python
from domainkit.contrib.messaging import periodic, task


@task  # or @task(name=..., max_attempts=..., queue="emails")
async def send_welcome(user_id: UUID) -> None: ...


# In a use case: stored only if the change commits.
async with uow:
    ...
    uow.defer(send_welcome, user_id=user.id)  # delay=, dedupe_key=
    await uow.commit()

# In DomainConfig.ready(): run the handler in the worker, not in the request.
event_bus.subscribe(UserCreated, notify_crm, durable=True)


@periodic(every=timedelta(hours=1))  # once per interval across all workers
async def purge_expired() -> None: ...
```

Outside a unit of work, `await enqueue(send_welcome, user_id=...)` queues a
task in its own transaction. Arguments are stored as JSON and converted back
using the task's type hints (`UUID`, `datetime`, `Decimal`, models).

**Delivery is at least once.** A message is claimed with
`FOR UPDATE SKIP LOCKED` (so any number of workers can share the outbox) and
locked for `VISIBILITY_TIMEOUT`. If its worker dies, another claims it again.
Handlers and tasks must therefore be idempotent. Order between messages is
not guaranteed.

**Failures** are retried with exponential backoff and jitter, up to
`max_attempts`. Then the message is dead-lettered and sent to
`ERROR_REPORTERS`. A message naming an unknown task or handler is
dead-lettered at once. So is a message whose worker died (a crash, or a
kill) during its last attempt: it is not run again after `max_attempts`. `python manage.py messages --dead` lists dead
letters, and `--retry ID` (or `all`) queues them again after a fix.

**Database outages** do not stop a worker. When its outbox cannot be
reached (a restart, a network drop, a failover), it logs the error, waits
`POLL_INTERVAL`, doubling the wait after each failure in a row up to a
minute, and carries on; it logs again when the outbox answers. A message
that ran but whose outcome could not be written runs again once its lock
expires (`VISIBILITY_TIMEOUT`), as after a crash. A periodic task whose
queueing failed is queued on the next try.

A unit of work that defers tasks or publishes events with durable handlers,
without messaging installed, fails instead of losing the work.

**Several databases.** Each database that hosts domains (each shard, too)
has its own outbox, written in the same transaction as that database's
changes. `runworker` runs one worker per outbox, all in one process by
default; `runworker --database <target>` runs only a target's, so groups can
scale apart. Periodic tasks and `enqueue()` use the home outbox,
`MESSAGING.DATABASE`, which must be one alias. `messages --database <alias>`
inspects any outbox.

| `MESSAGING` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | The home outbox: periodic tasks and `enqueue()` write here, and its worker schedules periodic tasks. One alias, not a shard group. |
| `MAX_ATTEMPTS` | `10` | Runs before dead-lettering, unless the task sets its own. |
| `RETRY_BASE_SECONDS` | `2.0` | First retry delay; it doubles per attempt. |
| `RETRY_MAX_SECONDS` | `3600.0` | Cap on the retry delay. |
| `TASK_TIMEOUT` | `240.0` | Seconds a message may run; longer counts as a failure. |
| `VISIBILITY_TIMEOUT` | `300.0` | Seconds a claim lasts; must exceed `TASK_TIMEOUT`. |
| `POLL_INTERVAL` | `1.0` | Seconds an idle worker waits before looking again. |
| `BATCH_SIZE` | `50` | Most messages claimed at once. |
| `CONCURRENCY` | `10` | Messages one worker runs at the same time. |
| `RETENTION_SECONDS` | `604800.0` | Finished messages are deleted after this (7 days). |
| `CLEANUP_INTERVAL` | `3600.0` | Seconds between those deletions. |
| `BROKER` | `None` | An alias in `BROKERS`: workers take their work from that broker instead of polling the outbox (below). |
| `REPUBLISH_AFTER` | `3600.0` | With a broker: seconds a published message may wait for a worker before the relay publishes it again, as lost. |
| `PUBLISH_ON_COMMIT` | `True` | With a broker: the committing process publishes the commit's due messages right away, instead of leaving them to the relays' next poll. |

Run `runworker --metrics-port 9100` to expose the worker's metrics. Scale by
running more workers; `SIGTERM` lets running messages finish first.

### Queues

Every message belongs to a queue: `default`, or the one its task names
(`@task(queue="emails")`, `@periodic(..., queue="reports")`). Durable event
handlers use `default`. `runworker --queue emails` runs only that queue's
messages (repeat `--queue` for several), so slow or bulky work gets workers
of its own and cannot hold up the rest. Without `--queue`, a worker runs
every queue. Each queue needs at least one worker, or its messages wait.

### Workers on RabbitMQ

By default, workers poll the outbox table every `POLL_INTERVAL`, so a
message waits up to that long, and every worker queries the database even
when idle. With a broker, the process that commits a message hands it to
the broker right away, workers receive it at once, and most workers never
poll the database. The outbox stays the record of every message (see
[ADR 0014](adr/0014-outbox-as-record-broker-for-dispatch.md)):

```python
BROKERS = {
    "default": {
        "URL": env.str("BROKER_URL"),  # amqps://billing:pw@mq.internal:5671/billing
        "PREFIX": "billing",
    },
}
MESSAGING = {"BROKER": "default"}
```

Install the `rabbitmq` extra (`domainkit[rabbitmq]`, which brings
aio-pika). Then `runworker` works like this:

- **Publish on commit.** Right after a unit of work commits (and after
  `enqueue()`), its process locks the messages it wrote for
  `REPUBLISH_AFTER` and publishes a small pointer to each: its id, its
  database, and the lock. RabbitMQ confirms every publication. This adds a
  database update and a broker round trip to each request that writes
  messages; set `PUBLISH_ON_COMMIT` to `False` to leave publishing to the
  relays. Delayed tasks wait for the relay.
- **Relay.** Workers also claim due messages of their outbox, as a polling
  worker would, and publish them the same way: delayed tasks when they fall
  due, retries, and anything a commit could not publish (the broker was
  down, the process died). A publication that fails is unlocked and tried
  again on the next round.
- **Consume.** Workers consume their queues with a prefetch of
  `CONCURRENCY`. For each delivery, a worker takes the message over by id,
  which works only for the pointer of the current publication (or one whose
  lock has expired), runs it, records the outcome, and only then
  acknowledges the delivery. A duplicate, or a delivery of a message that
  already ran, is acknowledged and skipped.
- **Retries and dead letters** stay in the outbox. A failed message goes
  back with its backoff delay, and the relay publishes it again when it is
  due, so RabbitMQ needs no delayed-message plugin. `messages --dead` and
  `--retry` work as before.
- **Every delivery is answered**, so a failure never holds one of the
  consumer's `CONCURRENCY` slots. A delivery whose outbox cannot be reached
  is requeued after `POLL_INTERVAL`; any other is acknowledged once its
  outcome is recorded, or once it is clear it cannot run here. The relay
  waits out database outages as the polling worker does.
- **Losses** cost time, never messages. A worker that dies mid-message
  leaves it to come back after `VISIBILITY_TIMEOUT`; a message RabbitMQ
  lost is published again after `REPUBLISH_AFTER`.

On RabbitMQ, the worker uses one durable direct exchange, `<PREFIX>.work`,
and a quorum queue per queue, `<PREFIX>.<queue>`, bound by the queue's name.
Messages are persistent and `mandatory`. Give the workers a user of their
own, limited to the prefix on the virtual host (CI checks that these
permissions are enough):

```text
rabbitmqctl add_user billing-worker '<long random password>'
rabbitmqctl set_permissions -p billing billing-worker '^billing\.' '^billing\.' '^billing\.'
```

Relaying workers also schedule periodic tasks and clean up. A few are
enough: run the rest with `runworker --no-relay`, so they only consume and
never query the database until a message arrives.

On a laptop, with PostgreSQL 18 and RabbitMQ 4.3 in Docker and one worker
process (`CONCURRENCY` 50), a brokered message ran about 35 ms after its
commit, against about 870 ms when polling every second. `enqueue()` took
5 ms instead of 1.4 ms (RabbitMQ confirmed in 0.6 ms), and a full outbox
drained at 950 tasks per second instead of 470.

`python manage.py broker declare` creates the exchange and the queues ahead
of the workers; run it with an account that may configure the virtual host
if the workers' account may not. `broker check` tells whether each broker
answers. In staging and production, brokers must use `amqps://` with
certificate checks, and a user other than `guest`; `memory://`, an
in-process broker for tests and development, is refused there.

Keep `TASK_TIMEOUT` well under RabbitMQ's `consumer_timeout` (30 minutes by
default), which closes the channel of a consumer that holds a message
longer. Keep `REPUBLISH_AFTER` above the longest a message may wait in a
queue behind others: the relay takes an older message for a lost one and
publishes it again. Nothing runs twice, since the first copy is skipped,
but the queue grows. Add workers before a backlog gets that long. Quorum queues dead-letter or drop a message delivered more than
their delivery limit (20 by default); for the worker that is harmless, as
the relay publishes such a message again after `REPUBLISH_AFTER`.

**Upgrading to 0.5.** The outbox gained a `queue` column (default
`default`). Run `python manage.py makemigrations` and `migrate` in each
project that installs messaging.

## Audit: who did what, when, from where

Every committed domain event becomes a row in `domainkit_audit_log`, written
in the same transaction as the change. A row holds:

- `occurred_at`, `actor`, `request_id`, and `client_ip`, from the request
- `action`, named `<domain label>.<event in snake case>` (`catalog.product_created`)
- `outcome`, `target`, `event_id`, and the event's fields as `details`

Record what no event describes, such as refused logins or reads of sensitive
data, in its own transaction:

```python
from domainkit.contrib.audit import Outcome, record

await record("auth.login", Outcome.DENIED, target=username, reason="mfa")
```

`python manage.py auditlog --actor alice --action catalog.product_created` reads the
trail. Events are audited in the database where they commit, so with several
databases, `auditlog --database <alias>` reads another one.

The application only inserts. To make sure a compromised API cannot rewrite
history, make the table append-only in the database itself. Add a revision
that calls `domainkit.contrib.audit.protection.protect(op)` in `upgrade()`
and `unprotect(op)` in `downgrade()`. On PostgreSQL this installs triggers
that refuse `UPDATE`, `DELETE`, and `TRUNCATE` for every role. Only the table
owner (the migration role) can drop them, for example to apply a retention
policy:

```python
from alembic import op

from domainkit.contrib.audit.protection import protect, unprotect


def upgrade() -> None:
    protect(op)


def downgrade() -> None:
    unprotect(op)
```

| `AUDIT` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Where `record()` writes, and what `auditlog` reads by default. |
| `EVENTS` | `True` | Record every committed domain event. |
| `EXCLUDE` | `()` | Actions to skip, as shell patterns (`catalog.*`, `*.viewed`). |

## Idempotency: safe retries of unsafe requests

A client sends `Idempotency-Key: <random UUID>` with a `POST` or `PATCH`. The
first request runs and its response is stored. A retry with the same key and
the same request gets the stored response, with `Idempotent-Replayed: true`.

| Situation | Answer |
| --- | --- |
| Same key, same method, path, query and body | The stored response, replayed |
| Same key, different request | 422 `idempotency_key_reused` |
| Same key while the first is still running | 409 `idempotency_key_in_use`, `Retry-After: 1` |
| Key not 1 to 255 printable ASCII characters | 400 `invalid_idempotency_key` |
| First response was a 5xx, or larger than `MAX_RESPONSE_BYTES` | Not stored: the retry runs again |

Keys are scoped to the actor, which `AuthenticationMiddleware` sets (list
it first), so one user cannot replay another's response. Without an
actor, keys are scoped to the client address and a hash of the request's
`Authorization` and `Cookie` headers. Add
`IdempotencyMiddleware` last in `MIDDLEWARE`: it reads whole bodies, which
`RequestBodyLimitMiddleware` has already bounded. With messaging installed,
expired keys are purged hourly.

| `IDEMPOTENCY` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Alias holding the keys. |
| `TTL_SECONDS` | `86400.0` | How long a key is remembered (24 hours). |
| `METHODS` | `POST`, `PATCH` | Methods that honour the header; others ignore it. |
| `MAX_RESPONSE_BYTES` | `1048576` | Larger responses are passed through but not stored. |

## Evidence: records that provably never change

Evidence is what you may have to prove later, exactly as it was: a
verification result, a signed consent, a regulatory submission. It goes to a
ledger (an immudb database, [ADR 0011](adr/0011-immudb-ledger-for-evidence.md)),
not to PostgreSQL. The audit trail records who did what; evidence records
facts and the digests of the documents behind them.

```python
from domainkit.contrib.evidence import Evidence, evidence_store, sha256

evidence = Evidence(
    kind="kyc.document_verified",
    subject="customer:42",
    facts={"result": "match", "model": "v3"},
    artifacts={"passport": sha256(document_bytes)},
)
receipt = await evidence_store().record(evidence)  # keep it with your data
recorded = await evidence_store().verify(receipt)  # raises EvidenceTampered
```

- A record is canonical JSON (sorted keys, no spaces, UTF-8) with the id,
  kind, subject, facts, artifact digests, time, actor, and request id. The
  receipt holds its key, its SHA-256 digest, and the ledger transaction.
- Keys are `<NAMESPACE>/<subject digest>/<evidence id>`: no personal data
  in keys, and `for_subject("customer:42")` is a prefix scan.
- Recording is idempotent by evidence id: a retry returns the same receipt,
  and another record with that id is an `EvidenceConflict` (409).
- Reads return revision 1, the original. If anything wrote the key again,
  `RecordedEvidence.intact` is false and `verify()` raises
  `EvidenceTampered`. With immudb, every read and write is also checked
  against a Merkle proof and, in deployments, the server's signature.
- Receipts are what tie evidence to your data: anyone holding the ledger's
  write credentials can add records, though never change one unnoticed. Keep
  each receipt (or key) with the change it proves; `for_subject` is a
  convenience listing.
- Inside a use case, `key = defer_evidence(uow, evidence)` records it
  through the outbox once the change commits (messaging must be
  installed). Store the key with the change.

**Never put personal data in facts**: a ledger cannot forget. Store
references or digests, or encrypt personal fields with keys you can destroy.

`python manage.py evidence --subject customer:42` lists a subject's records;
`evidence --verify <key>` proves one.

| `EVIDENCE` key | Default | Meaning |
| --- | --- | --- |
| `LEDGER` | `"evidence"` | Alias in `LEDGERS` (`immudb://`; `memory://` in tests). |
| `NAMESPACE` | `"evidence"` | Key prefix, so projects or environments can share one ledger. |
| `MAX_BYTES` | `262144` | Largest record; keep documents elsewhere and record their digests. |
