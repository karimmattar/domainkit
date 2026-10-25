# Optional apps

Install an app by adding it to `INSTALLED_DOMAINS`, then run
`makemigrations` and `migrate`: each one brings its own tables. Each is
configured by one setting, a dictionary whose keys are all optional.

| App | Setting | Tables |
| --- | --- | --- |
| `domainkit.contrib.messaging` | `MESSAGING` | `domainkit_messages` |
| `domainkit.contrib.webhooks` | `WEBHOOKS` | `domainkit_webhook_subscriptions`, `_deliveries`, `_attempts` |
| `domainkit.contrib.audit` | `AUDIT` | `domainkit_audit_log` |
| `domainkit.contrib.idempotency` | `IDEMPOTENCY` | `domainkit_idempotency_keys` |
| `domainkit.contrib.evidence` | `EVIDENCE` | none: records live on a ledger |
| `domainkit.contrib.contenttypes` | none | `domainkit_content_types` |
| `domainkit.contrib.auth` | `AUTH` | users, groups, permissions, and the principal cache; see [auth.md](auth.md) |
| `domainkit.contrib.privacy` | none | none: exports, erases, and purges domains' rows; see [privacy.md](privacy.md) |
| `domainkit.contrib.flags` | `FLAGS` | `domainkit_flags`, `domainkit_dynamic_settings` |
| `domainkit.contrib.objectpermissions` | none | `domainkit_object_permissions`, in every database that hosts a domain; see [auth.md](auth.md#grants-on-single-objects) |

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


@task  # or @task(name=..., max_attempts=..., queue="emails"), see below
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


@periodic(cron="0 3 * * 1-5", tz="Africa/Cairo")  # 03:00 on weekdays, Cairo time
async def nightly_report() -> None: ...
```

A durable handler runs only for the events it is subscribed to (or their
subclasses); a message pairing it with another event is dead-lettered.

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
letters, and `--retry ID` (or `all`) queues them again after a fix. A
message's last error is kept with it, logged, and reported. Validation
errors (arguments or an event that no longer fit) are kept as `InvalidData`,
naming the fields and rules but not the values, which are often personal
data. Pass ids in task arguments rather than names or emails all the same.

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
| `TASK_TIMEOUT` | `240.0` | Seconds a message may run; longer counts as a failure (at most a day). |
| `VISIBILITY_TIMEOUT` | `300.0` | Seconds a claim lasts; must exceed `TASK_TIMEOUT` (at most two days). |
| `POLL_INTERVAL` | `1.0` | Seconds an idle worker waits before looking again (at most an hour). |
| `BATCH_SIZE` | `50` | Most messages claimed at once (1 to 10,000). |
| `CONCURRENCY` | `10` | Messages one worker process runs at the same time, from all its outboxes and queues together (1 to 65,535, RabbitMQ's largest prefetch). |
| `SHUTDOWN_TIMEOUT` | `20.0` | Seconds a stopping worker lets running messages finish; the rest are stopped and run again (at most an hour). |
| `RETENTION_SECONDS` | `604800.0` | Finished messages are deleted after this (7 days; at most a year); their arguments are blanked when they finish. Dead letters are kept until retried. |
| `CLEANUP_INTERVAL` | `3600.0` | Seconds between those deletions (at most a day). |
| `CLEANUP_BATCH_SIZE` | `1000` | Most finished messages one cleanup statement deletes (1 to 100,000). |
| `BROKER` | `None` | An alias in `BROKERS`: workers take their work from that broker instead of polling the outbox (below). |
| `REPUBLISH_AFTER` | `3600.0` | With a broker: seconds a published message may wait for a worker before the relay publishes it again, as lost (at most a week). |
| `PUBLISH_ON_COMMIT` | `True` | With a broker: the committing process publishes the commit's due messages in the background right away, instead of leaving them to the relays' next poll. |
| `BREAKER_FAILURES` | `5` | With a broker: failed broker calls in a row after which commits stop publishing and leave their messages to the relays. |
| `BREAKER_COOLDOWN` | `30.0` | Seconds commits leave their messages to the relays before the publisher tries the broker again. |
| `NOTIFY` | `True` | On PostgreSQL: commits wake idle workers at once (`LISTEN`/`NOTIFY`), unless a broker publishes each commit. |
| `METRICS_TTL` | `15.0` | Seconds the outbox gauges are reused between scrapes; 0 counts every scrape. |

Run `runworker --metrics-port 9100` to expose the worker's metrics. Scale by
running more workers. However many outboxes and queues a `runworker`
process serves, it runs at most `CONCURRENCY` messages at once.

On PostgreSQL, the database's clock decides what is due and when a lock
expires, so hosts whose clocks disagree neither run a message twice nor
leave it waiting, and a commit wakes idle workers at once
(`LISTEN`/`NOTIFY`, see `NOTIFY`), so `POLL_INTERVAL` can be longer. Old
messages are deleted in small batches, by a task of each worker's own, so
cleanup never holds up claiming. [runtime.md](runtime.md), "Workers and
the outbox", has the details.

**Watching PostgreSQL's notification queue.** PostgreSQL keeps sent
notifications in one queue per server (8 GB by default) until every
session that listens has read them. A listener that stops reading, such as
a session that runs `LISTEN` and then sits in a long transaction, or a
client whose process hangs, holds the queue's tail: the queue fills, and
once it is full every transaction that sends `NOTIFY` fails at commit,
domainkit's outbox writes included. Workers listen on a connection of
their own and throw it away when they stop. Watch the queue:

```sql
SELECT pg_notification_queue_usage();  -- 0 (empty) to 1 (full)
```

Alert well before half full (0.5); from there on PostgreSQL also logs a
warning naming the process that holds the queue back. Ending that process
(`pg_terminate_backend(pid)`) lets the queue drain; `MESSAGING.NOTIFY =
False` stops domainkit's notifications, and workers fall back on polling.

**Stopping.** `SIGTERM` (or Ctrl+C) stops claiming (and, with a broker,
consuming, within five seconds even if the broker does not answer) and
gives running messages `SHUTDOWN_TIMEOUT` seconds to finish. Messages still running then
are cancelled and go back to the outbox, due at once; the cut-short run
does not count as an attempt. A second signal stops them at once. Keep
`SHUTDOWN_TIMEOUT` a few seconds under the time your platform waits before
it kills the process (Kubernetes' `terminationGracePeriodSeconds`, 30 by
default), so they are given back rather than left locked until
`VISIBILITY_TIMEOUT`. Handlers see the cancellation as
`asyncio.CancelledError`; let it propagate.

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
  `enqueue()`), its process hands the messages it wrote to a background
  publisher and returns: a slow or stopped broker never delays the commit.
  The publisher locks them for a short lease (the broker's `CONNECT_TIMEOUT`
  plus `PUBLISH_TIMEOUT`, and five seconds) and publishes a small pointer to
  each (its id, its database, and the lock), `BATCH_SIZE` at a time.
  RabbitMQ confirms every publication, and a confirmed message stays
  locked for `REPUBLISH_AFTER`; one the broker did not confirm, whatever
  the reason, a stop included, is unlocked at once. It declares each queue
  once per process. If more than 10,000 committed messages are waiting to
  be published, newer ones are left to the relays. After `BREAKER_FAILURES`
  broker failures in a row, commits leave their messages to the relays for
  `BREAKER_COOLDOWN`, then the publisher checks the broker and tries again;
  this is logged once when it starts and once when it ends. At shutdown
  the publisher gets a fifth of `SHUTDOWN_TIMEOUT`, the time shutdown's
  drain phase always has, and gives back what it cut short within it; the
  outbox holds the rest.
  Set `PUBLISH_ON_COMMIT` to `False` to leave all publishing to the relays.
  Delayed tasks wait for the relay.
- **Relay.** Workers also claim due messages of their outbox, as a polling
  worker would, and publish them the same way: delayed tasks when they fall
  due, retries, and anything a commit could not publish (the broker was
  down, the process died). A publication that fails is unlocked and tried
  again on the next round.
- **Consume.** Workers consume each queue with a prefetch of `CONCURRENCY`
  (per queue: quorum queues take no prefetch shared by a channel), and run
  at most `CONCURRENCY` deliveries at once in all; the others wait for a
  slot. For each delivery, a worker takes the message over by id,
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
  outcome is recorded, or once it is clear it cannot run here. An answer
  that fails (the delivery's channel closed when RabbitMQ restarted or the
  connection dropped) is logged, and RabbitMQ hands the message out again;
  it never stops the worker or its other messages. The relay waits out
  database outages as the polling worker does.
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

**Upgrading to 0.7.** The outbox gained `priority` and `started_at`
columns, an index, and a `cancelled` status. Run `makemigrations` and
`migrate` before deploying the new code.

### Task options

Every option of `MESSAGING` that concerns one task can be set on the task
itself, and wins over the setting:

```python
from domainkit.contrib.messaging import Backoff, current_attempt, task


@task(
    timeout=30,  # seconds per run (MESSAGING.TASK_TIMEOUT)
    max_attempts=5,  # (MESSAGING.MAX_ATTEMPTS)
    backoff=Backoff(base=1, cap=60),  # or Backoff.fixed(30); jitter=False
    retry_on=(httpx2.TransportError,),  # only these are retried
    dont_retry_on=(InvoiceVoided,),  # these dead-letter at once
    priority=10,  # -100 to 100; higher runs first
    rate="20/minute",  # at most 20 runs a minute
    sensitive=False,  # True: blank its arguments once it is dead, too
)
async def push_invoice(invoice_id: UUID) -> None:
    attempt = current_attempt()  # .number, .max_attempts, .last
```

- **Backoff** waits `base` seconds before the first retry; exponential
  backoff doubles that after each attempt, up to `cap` (an hour by
  default). With jitter (the default), each delay is drawn between half
  and all of it, so failures that happened together are retried apart.
  Delays are at most a year.
- **Retries.** `dont_retry_on` wins over `retry_on`; without `retry_on`,
  every error is retried. A run past its timeout raises `TimeoutError`.
- **Timeouts** must stay under `VISIBILITY_TIMEOUT`, or the worker refuses
  to start: a claim that ends while the task still runs would run it twice.
  A run's timeout is its deadline (`domainkit.deadlines`): on PostgreSQL its
  transactions lower `statement_timeout` to the time left, and its calls
  through `domainkit.http.client` are bounded by it, as in a request.
- **Priorities** order the due messages of a queue: higher first, then the
  oldest. They do not reorder queues; give urgent work a queue of its own.
  On a broker, the relay publishes each round in priority order, but
  RabbitMQ delivers in queue order, so priorities count only while
  messages wait in the outbox.
- **Rates** are counted by `RATE_LIMIT_BACKEND`: the memory backend counts
  in each worker process, `RedisBackend` across all of them. A message
  over the rate goes back to the outbox, without spending an attempt, and
  comes due when the rate allows; held messages are spaced one interval
  apart.
- **Arguments** stay in the outbox row (`payload`, in clear) only while
  the message may still run. The worker blanks them (`{}`) in the
  statement that marks it done, and `cancel` when it cancels it; the row
  keeps the task's name, id, times, attempts, and error, for
  `RETENTION_SECONDS`. A dead message keeps its arguments, so
  `messages --retry` can run it again, for as long as it stays dead,
  unless its task is `sensitive`: then they are blanked with its error
  recorded, its headers say `payload_blanked`, and a retry refuses it.
  Mark a task sensitive when its arguments hold secrets or personal data;
  mail, text messages, and push notifications are. In a chain, every step
  after a sensitive one is treated as sensitive too (its headers say
  `sensitive`): it is handed that step's arguments, or what it returned.
  Better still, queue ids and build the secret in the task.

Durable event handlers use the `MESSAGING` settings.

### Schedules

`@periodic(every=timedelta(...))` runs a task once per interval, at the
start of each. `@periodic(cron="...", tz="...")` runs it at the wall-clock
times of a five-field cron expression in an IANA time zone (UTC by default):

| Field | Values |
| --- | --- |
| minute | 0-59 |
| hour | 0-23 |
| day of month | 1-31 |
| month | 1-12 or `JAN`-`DEC` |
| day of week | 0-7 (0 and 7 are Sunday) or `SUN`-`SAT` |

Each field is `*`, a value, a range (`1-5`), a step (`*/15`, `0-30/10`, or
`5/15`, from 5 to the end), or a list of these (`1,15`). `@hourly`,
`@daily` (`@midnight`), `@weekly`, `@monthly`, and `@yearly`
(`@annually`) stand for the usual expressions. As in cron, when both day
fields are restricted, a day matches either: `0 0 1 * MON` runs on the
first of the month and on Mondays. An expression that never runs (`0 0 30
2 *`) is refused at startup.

**Daylight saving time** never runs a time twice and never skips one:

- A time that a spring-forward gap skips (02:30 when clocks jump from
  02:00 to 03:00) runs once, at the end of the gap (03:00).
- A time that a fall-back repeats (01:30 when clocks go back from 02:00 to
  01:00) runs once, at its first occurrence. When the hour field starts
  with `*` (`*/15 * * * *`), times in the repeated hour run in both passes,
  so the schedule keeps its pace in elapsed time.

The worker of the home outbox queues each cron task's next run ahead of
time, under a key derived from its time, so all workers share one row per
run. A run whose time passes while every worker is down still runs once
one is back; earlier missed runs are not made up. A changed schedule takes
effect after the run already queued. Time zones come from the system;
Windows has none, so install `domainkit[tzdata]` there.

### Chains

`chain(a, b, c)` runs `a`, then `b` once `a` succeeds, then `c`. Defer it
like a task; the keyword arguments go to the first step:

```python
from domainkit.contrib.messaging import chain


@task
async def charge(order_id: UUID) -> dict[str, str]:
    ...
    return {"order_id": str(order_id), "charge_id": charge.id}


@task
async def send_receipt(order_id: UUID, charge_id: str) -> None: ...


uow.defer(chain(charge, send_receipt), order_id=order.id)
```

Each next step gets the previous step's result as its keyword arguments.
A step that returns `None` passes its own arguments on, and any other
value is passed as `result=`. Results must be JSON-friendly; one that is
not fails the step. The next step is written in the transaction that marks
the previous one done, so a crash between them loses or repeats nothing.
It runs on its own queue, with its own options, and under the request id,
actor, and trace of the chain's start. A step that is dead-lettered ends
the chain.

### Inspecting and cancelling

`python manage.py messages` counts messages by status and shows each
queue's lag: how long its oldest due message has waited for a worker. Its
options list messages without their arguments:

- `--pending`: messages waiting, in the order workers take them (`due` or
  `scheduled`);
- `--locked`: messages a worker is running (`running`) or a relay has
  handed to the broker (`published`);
- `--dead`: dead letters; `--retry ID` (or `all`) queues them again, except
  those of `sensitive` tasks, whose arguments were blanked: `--retry ID`
  refuses one, and `--retry all` says how many it left;
- `--queue NAME` and `--limit N` narrow a listing.

`messages --cancel ID` cancels a message no worker is running: a waiting
one, or one on its way through the broker (its delivery is then skipped).
A running message is refused. A cancelled message never runs, nor does the
rest of its chain; it is kept as `cancelled`, without its arguments, and
deleted after `RETENTION_SECONDS`. Retries and cancels are recorded in the
audit trail with the operator who ran them
([operators](reference.md#operators)). `domainkit.contrib.messaging.admin`
has the same operations for your own tools, unrecorded: `list_messages`,
`cancel`, `queue_counts`, `queue_lag`, `retry_dead`, and
`blanked_dead_letters`.

With Prometheus, `messaging_outbox_lag_seconds` (per queue) is the same
lag, next to `messaging_outbox_messages`; alert when it grows.

### Traces

A message carries the W3C trace context (`traceparent`, `tracestate`) of
the span that was active when it was written: the unit of work's commit,
or the request. With the `tracing` extra, the worker's `message.process`
span continues that trace, so a request and the work it queued show as one
trace. A context that does not follow the W3C format is dropped, and the
message starts a trace of its own. Without the extra, nothing is written.

### Testing

`domainkit.contrib.messaging.testing` is a pytest plugin, like
`domainkit.testing`; enable both:

```toml
[tool.pytest.ini_options]
addopts = "-p domainkit.testing -p domainkit.contrib.messaging.testing"
```

Its fixtures work in sync and async tests:

| Fixture | Gives |
| --- | --- |
| `captured_events` | The domain events published after commits: `captured_events.of_type(UserCreated)`, `.assert_published(UserCreated, username="ann")`, `.assert_not_published(...)`. |
| `outbox` | The messages committed during the test: `outbox.queued(send_welcome)`, `.assert_queued(send_welcome, user_id=user.id)`, `.assert_not_queued(...)`. `outbox.run_queued()`, or `await outbox.arun_queued()`, runs the due ones here as a worker would, chained steps included, until none is left; `delayed=True` runs delayed messages and retries too. A failed run raises `QueuedWorkFailed`. |
| `execute_deferred` | Each commit's messages run right after it commits, before `commit()` returns. Failures are kept in `execute_deferred.failures` and fail the test at its end unless the test clears them. |

```python
async def test_signup_welcomes(db, outbox, captured_events):
    user = await sign_up("ann@example.com")

    outbox.assert_queued(send_welcome, user_id=user.id)
    captured_events.assert_published(UserCreated, user_id=user.id)
    await outbox.arun_queued()
```

## Webhooks: signed events for other services

`domainkit.contrib.webhooks` sends domain events to URLs that staff
subscribe. Install it with messaging and the `webhooks` extra
(`domainkit[webhooks]`, which brings httpx2 and cryptography), set
`SECRET_KEY` (subscription secrets are encrypted under a key derived from
it; webhooks refuse to start without it), then expose the events:

```python
INSTALLED_DOMAINS = ["domainkit.contrib.messaging", "domainkit.contrib.webhooks", ...]

# In a DomainConfig.ready():
from domainkit.contrib.webhooks import expose

expose(
    InvoicePaid, "invoice.paid", payload=lambda event: {"invoice_id": event.invoice_id}
)
```

Without `payload`, every field of the event is sent: leave personal data
out. Subclasses of an exposed event are sent too. A receiver gets a `POST`
with a JSON body:

```json
{"type": "invoice.paid", "timestamp": "2026-10-19T12:00:00.123456Z", "data": {"invoice_id": "..."}}
```

**Delivery** goes through the outbox, so nothing is sent unless the change
commits. A committed exposed event becomes a durable handler call; the
worker then writes one delivery per active subscription that wants the
event type (or `*`), and a task for each, in one transaction. Deliveries
run on the `webhooks` queue (`QUEUE`), so slow receivers hold up no other
work. Any answer but 2xx fails the attempt, redirects included (they are
not followed). A failed attempt is retried with exponential backoff and
jitter, up to `MAX_ATTEMPTS`; then the delivery is marked failed. Receivers
must accept duplicates: delivery is at least once.

**Signatures** follow [Standard Webhooks](https://www.standardwebhooks.com):
each attempt carries `webhook-id` (the delivery's id, the same on retries
and replays), `webhook-timestamp`, and `webhook-signature`, `v1,` and the
base64 HMAC-SHA256 of `<id>.<timestamp>.<body>` keyed with the secret's
bytes. Secrets look like `whsec_` and 32 random bytes in base64; their
libraries verify them. In Python, `domainkit.contrib.webhooks.verify(secret,
headers, body)` checks one in constant time and refuses timestamps more
than five minutes away from `domainkit.clock` (frozen under
`freeze_time`). After a rotation, deliveries carry a second
signature with the old secret for `SECRET_OVERLAP_SECONDS`.

**What may be called.** Before every attempt, and when a subscription is
saved:

- the URL must use `https` (`http` only while `DEBUG` is on), and have no
  user name, password, or fragment; it is at most 2048 characters;
- `localhost`, `*.localhost`, and cloud metadata names are refused;
- every address the host resolves to must be public. Private, loopback,
  link-local, shared (CGNAT), reserved, multicast, and unspecified
  addresses are refused, in IPv4 and IPv6, and so are IPv4 addresses
  inside IPv6 ones (`::ffff:10.0.0.1`, NAT64). Cloud metadata addresses
  (`169.254.169.254`, `fd00:ec2::254`, `168.63.129.16`, and others) are
  always refused.
- The connection goes to the address that was checked, never to a second
  lookup, so a host that answers differently a moment later (DNS
  rebinding) gains nothing. The host name still serves for the `Host`
  header and for TLS: the certificate is checked against it.
- Proxies from the environment are not used. An attempt has `TIMEOUT`
  seconds in all, and at most `MAX_RESPONSE_BYTES` of the answer are read.
- Host names are looked up on the guard's own threads
  (`GUARD_LOOKUP_THREADS`, 2 at once per host). Each delivery's lookups
  are its subscription's (`webhook:<id>`), whoever caused the event, and
  one subscription may hold a quarter of the threads: a receiver whose
  DNS never answers leaves the rest to other subscriptions' deliveries of
  the same events. It holds its lookups until the system's resolver gives
  up; then its host fails at once for a minute, so retries of its
  deliveries do not take the threads again. A receiver whose DNS answers
  after 5 seconds or more has its answer kept for a minute, so its next
  attempts take no thread either; one whose DNS fails in under 5 seconds
  takes a thread on each attempt
  ([runtime.md](runtime.md#outgoing-http-calls)).
- Subscriptions have no owner yet, so the share is per subscription, not
  per tenant. One subscription holds at most 2 threads (its host's
  bound), but whoever may create many subscriptions can point them at
  hosts that never answer, or answer slowly, and hold many threads: all
  16 of the default with eight, for the first round of lookups (later
  rounds find their hosts backed off or their answers kept). The admin API lets only staff create subscriptions; a
  project that lets its own users create them should bound how many each
  may have, until 0.10 gives subscriptions an owner to share by.

`ALLOWED_PRIVATE_NETWORKS` lets receivers in named private networks
through (`["10.20.0.0/16"]`); staging and production refuse loopback and
link-local networks there. A refused URL fails the delivery at once.

**Staff** manage subscriptions over the admin API, with
`domainkit.contrib.auth` installed:

```python
from domainkit.contrib.webhooks.admin import router as webhooks_admin

router.include_router(webhooks_admin, prefix="/admin/webhooks")
```

| Method and path | Permission | Does |
| --- | --- | --- |
| `GET /subscriptions` | `webhooks.view_subscription` | List subscriptions (`limit`, `offset`). |
| `POST /subscriptions` | `webhooks.add_subscription` | Subscribe a URL to `event_types`; the answer holds the secret, shown this once (`no-store`). |
| `GET /subscriptions/{id}` | `view` | One subscription. |
| `PATCH /subscriptions/{id}` | `change` | Change `url`, `event_types`, `description`, or `active`. |
| `POST /subscriptions/{id}/rotate-secret` | `change` | A new secret, shown once (`no-store`). |
| `DELETE /subscriptions/{id}` | `delete` | Delete it; its pending deliveries are cancelled. |
| `GET /subscriptions/{id}/deliveries` | `view` | Its deliveries, newest first (`status`, `limit`, `offset`). |
| `GET /deliveries/{id}` | `view` | A delivery, its body, and every attempt (status code, latency, error). |
| `POST /deliveries/{id}/replay` | `change` | Send a finished delivery again. |

Each endpoint also needs a staff user or a superuser. Secrets never appear
in listings, logs, events, or reprs; changes are recorded as events
(`SubscriptionCreated` and others), which the audit trail keeps, with the
URL's host only, since URLs may hold tokens: a new URL is recorded in
`SubscriptionChanged` with its host and the host before (`host`,
`previous_host`), so the trail shows where events went. For the same reason, neither
a `Subscription`'s `repr` nor those of the admin API's bodies show its
URL. The two answers that hold a secret are marked `Cache-Control:
no-store`: no cache keeps them, and
`IdempotencyMiddleware` stores no copy, so a retry with the same
`Idempotency-Key` gets a 409 rather than the secret again.

**Secrets at rest.** The subscriptions table holds the current secret, and
the previous one during an overlap, encrypted with
`domainkit.encryption.SecretBox` (AES-256-GCM, purpose
`domainkit.webhooks.secret`) and bound to the subscription's id: the table
alone signs nothing, and a value copied to another row does not decrypt.
Deliveries decrypt the secrets they sign with, and a rotation the old
secret it keeps; nothing else does. To replace `SECRET_KEY`, put the old
key in `SECRET_KEY_FALLBACKS`: deliveries still verify, and each one
encrypts its subscription's secrets again under the new key (a rotation
does too). Drop the old key once every active subscription has had a
delivery or a rotation since. If no key decrypts a secret (`SECRET_KEY`
changed without the old one in `SECRET_KEY_FALLBACKS`), its deliveries
fail at once, unsent, with that reason in `last_error` and the error
log, and are reported: never sent unsigned. Put the old key back, or
rotate the secret (a rotation drops one no key decrypts, so there is no
overlap) and give receivers the new one; then `webhooks replay --failed`.

`python manage.py webhooks subscriptions` lists subscriptions (host only),
`webhooks deliveries [--status S] [--subscription ID]` the latest
deliveries, and `webhooks replay ID ...` (or `--failed`) sends finished
deliveries again under the same `webhook-id`. Finished deliveries and their
attempts are deleted after `RETENTION_SECONDS`.

| `WEBHOOKS` key | Default | Meaning |
| --- | --- | --- |
| `QUEUE` | `"webhooks"` | The messaging queue of deliveries. |
| `MAX_ATTEMPTS` | `12` | Attempts before a delivery is marked failed. |
| `RETRY_BASE_SECONDS` | `30.0` | First retry delay; it doubles per attempt, with jitter. |
| `RETRY_MAX_SECONDS` | `21600.0` | Cap on the retry delay (six hours). |
| `TIMEOUT` | `15.0` | Seconds an attempt may take in all (at most 120). Delivery tasks get 30 more for their database work, which must stay under `MESSAGING.VISIBILITY_TIMEOUT`. |
| `CONNECT_TIMEOUT` | `5.0` | Seconds to connect; at most `TIMEOUT`. |
| `MAX_RESPONSE_BYTES` | `65536` | Most bytes of an answer read. |
| `MAX_PAYLOAD_BYTES` | `262144` | Largest body; a larger event is reported and not sent. |
| `ALLOWED_PRIVATE_NETWORKS` | `()` | Private networks receivers may be in. |
| `CA_BUNDLE` | `None` | Certificates to trust instead of the system's. |
| `SECRET_OVERLAP_SECONDS` | `86400.0` | How long the old secret still signs after a rotation. |
| `RETENTION_SECONDS` | `2592000.0` | How long finished deliveries are kept (30 days). |
| `USER_AGENT` | `"domainkit-webhooks"` | The `User-Agent` of deliveries. |

## Audit: who did what, when, from where

Every committed domain event becomes a row in `domainkit_audit_log`, written
in the same transaction as the change. A row holds:

- `occurred_at`, `actor`, `request_id`, and `client_ip`, from the request.
  The actor is `anonymous` for callers who did not authenticate, and
  `system` outside requests, as in periodic tasks and other work no request
  queued; work a request queued keeps its actor.
- `action`, named `<domain label>.<event in snake case>` (`catalog.product_created`)
- `outcome`, `target`, `event_id`, and the event's fields as `details`

Record what no event describes, such as refused logins or reads of sensitive
data, in its own transaction:

```python
from domainkit.contrib.audit import Outcome, record

await record("auth.login", Outcome.DENIED, reason="mfa")
```

`python manage.py auditlog --actor alice --action catalog.product_created` reads the
trail. Events are audited in the database where they commit, so with several
databases, `auditlog --database <alias>` reads another one.

### Personal data

An append-only trail cannot delete what it holds, so personal data never
goes into it in clear. Each personal value is stored as a pseudonym,
`pseudonym:` and 32 hex digits: an HMAC-SHA256 of the field's name and the
value, keyed with a random key of the person it is about, its subject.
Subjects' keys live in `domainkit_audit_keys`, which is not append-only.

- **The client's address** is the actor's personal data. Without an actor,
  it belongs to that day's anonymous callers (subject `anonymous:2026-10-18`,
  in UTC).
- **Event fields** are personal when marked with `Personal`, naming the field
  that identifies the person. Auth marks the username of `UserCreated` and
  `UserDeleted`:

  ```python
  from typing import Annotated
  from uuid import UUID

  from domainkit.ddd import DomainEvent, Personal


  class MemberJoined(DomainEvent):
      member_id: UUID
      email: Annotated[str, Personal(of="member_id")]
      phone: Annotated[str, Personal(of="member_id")] | None = None
  ```

  The whole field becomes one pseudonym, also when it is optional. Mark
  the field itself: a marker inside it (`list[Annotated[str, Personal(...)]]`,
  or a value object's own marked fields) raises `TypeError`, since it would
  be ignored.

- **Explicit records** take personal details apart, under the actor (or the
  day's anonymous callers) unless a `subject` is named:
  `record("auth.login", Outcome.DENIED, personal={"username": username})`.
- **`actor` and `target`** are not pseudonymised. The actor is a user's id;
  give targets ids, not names or emails.
- **Lists that grow long or name people** can be kept as their size: a
  field marked `Counted()` (`users: Annotated[tuple[str, ...], Counted()]`)
  is audited as its number of items, while the event itself keeps them.
  Flags count the users of `FlagChanged` this way.

A value and its subject always give the same pseudonym, so the trail still
answers "did this happen to this person?":
`await pseudonym_of(str(user.id), "username", "alice")` returns the pseudonym
to look for, or None once the person is forgotten. Each database keeps its
own keys, so look in the one that recorded the rows (`database=`, by
default `AUDIT.DATABASE`). A row whose personal value is None stores None.
The same markers on aggregate fields let `domainkit.contrib.privacy`
export and erase what the domains' own tables hold about a person
([privacy.md](privacy.md)).

To honour an erasure request, delete the person's key, in every database
that holds an audit trail:

```shell
python manage.py auditforget 4b0c6f0e-...            # or: await forget(str(user.id))
python manage.py auditforget --anonymous-days 30     # anonymous callers' days before that
```

Each run is recorded as `operator.auditforget`, with the operator who ran
it and how many subjects and keys, not which subjects (see
[operators](reference.md#operators)). Their pseudonyms stay in the trail,
unchanged, but can no longer be linked to them by anyone: the key was
random and is gone. A later event about them
gets a new key, unlinkable to the old pseudonyms. Run `--anonymous-days` on
a schedule to keep anonymous callers' addresses only as long as you need
them. Rows written before this release keep their values in clear; the
append-only protection keeps them as they are.

A transaction reads the keys of all the subjects it writes about with one
query, and makes a new subject's key with an insert that returns it, so
each key is read once per transaction. No key is kept past its
transaction: `forget` reaches every process through the database, for
every transaction that starts after it, and drops at once the keys that
open transactions of its own process hold. A lookup that read a key
before `forget` began may still use it for the rows it is writing, as it
would had it run a moment earlier, but keeps nothing it read. A subject's
first key is made under a lock of that subject, which `forget` takes too
(a transaction advisory lock on PostgreSQL, one more statement for a new
subject only; SQLite runs one writer at a time): `forget` waits for a
writer making a key, then deletes that key as well.

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

### Tamper evidence

Triggers stop the application, but not the tables' owner, who can drop
them, rewrite rows, and put them back. So the trail is also sealed into a
hash chain, and the chain's head is anchored on a ledger the owner cannot
rewrite.

- **Sealing.** Each row not sealed yet gets the next number in
  `domainkit_audit_seals`, with its digest (the SHA-256 of the row's
  canonical JSON) and its link (the SHA-256 of the previous link and the
  digest). The worker seals every `AUDIT.SEAL_EVERY` when
  `domainkit.contrib.messaging` is installed; otherwise run
  `python manage.py auditseal` on a schedule. Each database's trail is its
  own chain. The newest sealed row is a high-water mark: a run looks for
  rows from `AUDIT.SEAL_LATE_WINDOW` before it (rows that committed late),
  so its cost follows what is new, not how long the trail is. The mark
  counts as now at most, by the database's clock on PostgreSQL (the
  host's on SQLite), which also dates the seals. A run seals
  `AUDIT.SEAL_BATCHES` batches at most and leaves the rest for the next;
  the `audit_unsealed_rows` gauge says how many it left, and a warning is
  logged past `AUDIT.SEAL_BACKLOG_WARNING`. Sealing also makes the keys of
  today's and tomorrow's anonymous callers ahead of time, so at midnight
  (UTC) audited requests do not queue behind one insert of the new day's
  key.
- **Sealing late rows.** A row that commits later than the window after
  the mark is out of a run's reach, and `auditverify` reports it as
  unsealed once it is a day old. Look into why (a transaction that ran
  that long, a host whose clock runs behind), then run
  `python manage.py auditseal --late`: it seals such rows into the same
  chain, each seal flagged `late`, and `auditverify` counts them
  afterwards ("rows sealed late"). They were open to change unnoticed for
  longer than the others. Finding them reads the whole trail, so the
  worker never does it on its own.
- **Anchoring.** With `AUDIT.LEDGER` naming a ledger in `LEDGERS` (immudb),
  sealing also writes each chain's head (its number and link) there, at
  most once per `AUDIT.ANCHOR_EVERY`, as a new revision of
  `domainkit/audit/<AUDIT.NAMESPACE>/<alias>`. immudb keeps every revision
  and verifies what it returns. Give each project and environment that
  shares a ledger a namespace of its own (`"shop-production"`): without
  one, anchors go under `domainkit/audit/<alias>`, the key of 0.9.0, which
  every project on the ledger shares, so one project's anchors hold
  another's back and read as rewritten anchors in its `auditverify`.
  Setting a namespace on a trail anchored before keeps it verified:
  `auditverify` also reads the heads anchored under the old key while
  `AUDIT.PREVIOUS_ANCHORS` is on. If other projects wrote that key too,
  their heads read as rewritten anchors there; set `PREVIOUS_ANCHORS` to
  `False` once your namespace holds an anchor of its own.
- **Verifying.** `python manage.py auditverify` checks every row against
  its seal, every link against the one before, and every anchored head
  against the chain. It prints what it found and fails on changed or
  missing rows, broken links, rewritten anchors, seals that no anchor
  covers a day past `AUDIT.ANCHOR_EVERY` (anchoring stopped), and rows
  older than a day that were never sealed.

Every row can be sealed, whatever its strings hold (a lone surrogate, which
JSON can escape, included). A host whose clock runs ahead cannot stop
sealing or anchoring: a row or an anchor dated in the future counts as
dated now, and on PostgreSQL "now" is the database's, so a sealer whose
own clock runs ahead cannot leave other hosts' rows behind.

A row can be changed unnoticed only until it is sealed and its head
anchored, so anchor as often as you need: the default is hourly. Erasing a
person (`auditforget`) deletes a key, not a row, so the chain stays intact.

**Upgrading.** The seals table has a new `late` column: run
`makemigrations` and `migrate` in each project that installs audit.

### In staging and production

The trail is tamper-evident only with four things in place, and each is
easy to leave out: a ledger (`AUDIT.LEDGER`), a namespace on it
(`AUDIT.NAMESPACE`), something that seals on a schedule (the worker, with
`domainkit.contrib.messaging` installed, or `auditseal` from cron, said
with `AUDIT.SEALED_EXTERNALLY = True`), and the `protect()` triggers on
both tables. In staging and production, `manage.py check` warns about each
setting that is missing, and the application, when it starts, asks each
audited PostgreSQL database whether `domainkit_audit_log` and
`domainkit_audit_seals` have their triggers as `protect()` makes them, and
logs a warning for each database without them, or that did not answer
within 10 seconds. A trigger that is disabled, or fires only in replica
sessions (`ENABLE REPLICA TRIGGER`), on other events, under a `WHEN`
condition, or for some columns only, or whose function is not the one
`protect()` writes (its body is compared, whitespace aside), counts as
missing. The probe cannot see a session that sets
`session_replication_role = replica`, which only a superuser may do, nor
stop a table owner from dropping the triggers between two starts:
`auditverify` checks what they protect.

These are warnings in 0.9.1. `AUDIT.REQUIRE_BASELINE = True` turns them
into a refusal to start, listing what is missing; 0.10 makes that the
default, so set the four now.

Protect the seals like the trail, in the revision that creates them:

```python
from domainkit.contrib.audit.tables import audit_seals


def upgrade() -> None:
    ...  # The revision's create_table.
    protect(op, audit_seals)


def downgrade() -> None:
    unprotect(op, audit_seals)
    ...
```

| `AUDIT` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Where `record()` writes, and what `auditlog` reads by default. |
| `EVENTS` | `True` | Record every committed domain event. |
| `EXCLUDE` | `()` | Actions to skip, as shell patterns (`catalog.*`, `*.viewed`). |
| `LEDGER` | `None` | `LEDGERS` alias to anchor each chain's head on; `None` seals without anchoring. |
| `NAMESPACE` | `None` | Part of the anchors' ledger key, `domainkit/audit/<NAMESPACE>/<alias>`: a lower-case letter, then up to 31 letters, digits, `_`, or `-`. `None` keeps the key of 0.9.0, which every project on the ledger shares; staging and production warn. |
| `PREVIOUS_ANCHORS` | `True` | With `NAMESPACE`, also verify the heads anchored under `domainkit/audit/<alias>` before it was set. Turn it off when other projects anchored there too. |
| `ANCHOR_EVERY` | 1 hour | Anchor a new head at most this often. |
| `SEAL_EVERY` | 5 minutes | How often the worker seals new rows. |
| `SEALED_EXTERNALLY` | `False` | `auditseal` runs from a schedule of your own (cron), not in the worker: staging and production then do not warn that nothing seals. |
| `REQUIRE_BASELINE` | `False` | Refuse to start in staging and production without a ledger, a namespace, a sealing schedule, and the triggers, instead of warning. 0.10 makes it the default. |
| `SEAL_LATE_WINDOW` | 15 minutes | How far before the newest sealed row a run looks for rows that committed late, up to a day. A row later than this stays unsealed until `auditseal --late`, and `auditverify` reports it once it is a day old: keep it above your longest transaction plus the clock skew between hosts. |
| `SEAL_BATCHES` | `100` | Batches of 1,000 rows one run seals at most (1 to 10,000); the rest wait for the next run. |
| `SEAL_BACKLOG_WARNING` | `100000` | Rows a run leaves unsealed past which it logs a warning. |

### History

A record's history is the rows about it, newest first, from every audited
database (Django admin's "History"). Rows name what they are about in two
ways:

- an explicit record has a `target`: `record("report.viewed", target="r-1")`;
- a committed event holds its aggregate's id in a field of `details`, such
  as `product_id`; give events the id of the aggregate they are about, as
  `startdomain` does.

```python
from domainkit.contrib.audit.history import history

entries = await history(target="r-1")
entries = await history(aggregate=("product_id", str(product.id)), limit=20)
older = await history(target="r-1", before=entries[-1].position)
```

Each entry lists its `changes`, where the details allow. An explicit record
can say what changed, with `before` and `after` mappings:
`record("report.renamed", target="r-1", before={"name": "Q1"}, after={"name": "Q2"})`.
They are kept as written, so never put personal data in them; pass it as
`personal=`. Otherwise each field is compared with the newest older entry of the
history that has it, on the same page or the one entry fetched past it, so
events read best when they carry the fields they change
(`ProductRenamed(product_id=..., name=...)`). A field nothing older has is
new (`before` is None).

Personal values stay the pseudonyms the trail holds, also for people who
were forgotten: reading a history reads no keys. An aggregate's id is
matched as text, so query by ids, never by personal fields (they are
pseudonyms).

From the command line, each change under its entry:

```shell
python manage.py auditlog --target r-1
python manage.py auditlog --aggregate product_id=6f1c7e6a-...
```

Staff read histories over HTTP, with `audit.view_history` (a permission
`migrate` creates for the trail's `AuditEntry` model) and `is_staff`, or as
superusers. Mount the router next to the admin API:

```python
from domainkit.contrib.audit.api import router as audit_router

router.include_router(audit_router, prefix="/admin/audit")
```

`GET /admin/audit/history?target=r-1` and
`GET /admin/audit/history?field=product_id&id=...` answer pages of up to
`limit` (50, at most 200) entries, with `next_cursor`. Entries leave out
the client's address. The permission opens every record's history, and
querying by a field reads the whole trail: grant it as you would read
access to the trail.

**Upgrading.** The trail gained an index on `target`, for histories. Run
`makemigrations` and `migrate` in each project that installs audit.

## Idempotency: safe retries of unsafe requests

A client sends `Idempotency-Key: <random UUID>` with a `POST` or `PATCH`. The
first request runs and its response is stored. A retry with the same key and
the same request gets the stored response, with `Idempotent-Replayed: true`.

| Situation | Answer |
| --- | --- |
| Same key, same method, path, query and body | The stored response, replayed |
| Same key, different request | 422 `idempotency_key_reused` |
| Same key while the first is still running, within its lease | 409 `idempotency_key_in_use`, `Retry-After: 1` |
| Key not 1 to 255 printable ASCII characters | 400 `invalid_idempotency_key` |
| First request failed (a 5xx or an exception) or was refused (a 4xx) before committing anything | Not stored: the retry runs again |
| First response marked `Cache-Control: no-store` (new tokens, from `/auth/login` and `/auth/refresh`) | Never stored: a retry runs again if nothing committed, else gets the 409 below |
| First response larger than `MAX_RESPONSE_BYTES`, nothing committed | Not stored: the retry runs again |
| First request died before committing, and its lease has ended | The retry takes the key over and runs |
| First request committed, then failed, died, or answered over `MAX_RESPONSE_BYTES` | 409 `idempotency_response_unavailable`: it never runs again |
| First request's commit on another database raised (its reply lost, refused, or cancelled while it ran) | 409 `idempotency_response_unavailable`: it may have committed, so it never runs again |
| First request ran past its lease and a retry took its key | The first gets 409 `idempotency_key_taken_over`; its changes are not saved |
| Client disconnected before sending the whole body | Nothing runs and no key is claimed: retry with the same key |
| Same key after `TTL_SECONDS`, even if the first request committed | A new request: it runs again (below) |

Within its TTL, a key's request runs twice only if nothing it did was
saved. When a SQL unit of work commits during a keyed request, the key is
marked committed in the same transaction, and a committed key is never released, even if the reply
to the commit was lost. A retry then gets the stored response or, when the
response could not be kept, a 409 telling the client to look up the result
instead. Only units of work are seen: call payment providers and other
services from a deferred task (`uow.defer`), which the outbox runs once, not
from the handler.

**After the TTL, a key is forgotten.** A key is remembered for
`TTL_SECONDS` (24 hours) from its first request. After that, a request
with the same key is a new request: it runs again, even if the first one
committed, and its response replaces the old one. A client that retries a
payment with a key older than the TTL (after a long outage, from a queue
it replays) is charged twice. Keys are replays of recent requests, not a
record of what was ever done: keep the TTL above the longest your clients
retry for (at most 30 days), and refuse duplicates your domain must never
accept, such as a payment reference, with a unique column of their own.

A request holds its key for `LEASE_SECONDS`. If the process dies before the
request commits anything, a retry after the lease takes the key over and
runs. Each holder has a random token, and the mark is written only under
it: a request that ran past its lease and lost its key to a retry cannot
commit, so the changes are made once. Set `LEASE_SECONDS` above the
longest a request may run:

- it must be above `REQUEST_TIMEOUT`, or startup fails naming both
  settings;
- a `POST` or `PATCH` route that gives itself a deadline at the lease or
  past it (`Depends(RequestTimeout(300))`), or none (`RequestTimeout(None)`),
  is reported by `check`, and staging and production do not start with it;
- with no deadline for any request (`REQUEST_TIMEOUT = None`, or no
  `DeadlineMiddleware` in `MIDDLEWARE`) nothing bounds a keyed request by
  the lease: `check` warns in staging and production, and the
  application still starts; 0.10 refuses to start. A route without a
  deadline is a refusal already because it is one route's exception,
  made in code; the whole application without one is a deployment
  choice that 0.9.1 leaves running;
- your server's or proxy's timeout, if it is longer, counts too.

**What the key covers.** The key holds the request until its response is
sent: the handler, its commits, and the response's body, streamed or not.
Before the last part of a response the key keeps is sent (a success, or
a refusal after a commit), the response, or its status, is stored, so a
client that has the whole response and retries at once gets the replay;
if storing fails, that last part is not sent, and the client retries.
Since this is before anything runs after the response, the work a route
does after it (Starlette's `BackgroundTasks`, which no deadline bounds)
runs once however long it takes: a retry meanwhile gets the stored
response, or a 409 `idempotency_response_unavailable`. A response the key does not keep
(a refusal or a server error before any commit) is settled once the
application returns, as before: a commit made after it still keeps the
key, and otherwise the key is released and a retry runs the request,
and its background work, again. A streamed response is held only by
the lease while it streams, since its deadline is lifted when it starts:
keep a keyed response's streaming within `LEASE_SECONDS`.

The key answers for the response, not for the work after it. A `202`
stored before its background work runs is what a retry gets while that
work runs, whether it then succeeds or not: the key does not tell the
client how the work ended, so give it a resource to look that up. If the
work raises, the key is settled again as for any request that raised:
released when nothing committed or pinned it, so a later retry runs the
request and the work again, or else kept with its status alone, a 409
`idempotency_response_unavailable`.

A body the application sends by path or by file descriptor
(`FileResponse` on a server with the ASGI `http.response.pathsend`
extension, or `http.response.zerocopysend`) never passes through the
middleware, so the key cannot keep it: it is treated as a response the
key does not keep, and a retry runs the request again, or gets the 409
after a commit.

The keys' database decides when a lease or a key runs out. On PostgreSQL,
the claim, the take-over and the purge compare and write
`statement_timestamp()`, as the outbox does, so hosts whose clocks
disagree never take a key from a request still running, nor replace or
purge a key that has not expired. SQLite's clock is the host's, and under
`freeze_time` the frozen clock decides on every database, so tests move
leases and expiry with it.

A request that raises, or is cancelled (its client gone, a deadline, an
anyio cancel scope that cancels every wait inside it), still releases its
key at once: the response is stored, or the key released, in a task of its
own that the request's cancellation cannot stop, within 5 seconds. If that
fails, the failure is logged and the lease applies.

Keep `IDEMPOTENCY.DATABASE` on the database your writes use where you can.
A unit of work on another database cannot carry the mark in its
transaction: before it commits, the key is pinned until it expires, and
right after, it is marked committed. A pinned key is never taken over by a
retry, nor released by its request, since its changes may have committed
whatever the commit reported. So if that commit raises (its reply lost, the
database refusing it, the request cancelled while it ran), retries get the
409 `idempotency_response_unavailable`, as after a commit on the keys'
database, even when nothing was in fact saved: look up the result. A
request that fails before its unit of work commits pinned nothing, and its
key is released as usual. A crash after the pin leaves the key in progress
until it expires.

**Upgrading.** The keys table has a new `pinned` column: run
`makemigrations` and `migrate` in each project that installs idempotency.

Keys are scoped to the actor, which `AuthenticationMiddleware` sets (list
it first), so one user cannot replay another's response. Without an
actor, keys are scoped to the client address and a hash of the request's
`CREDENTIAL_HEADERS` (`Authorization`, `Proxy-Authorization`, `Cookie`,
`X-API-Key`): list the header your project authenticates with if it is
another. Add `IdempotencyMiddleware` last in `MIDDLEWARE`: it reads whole
bodies, which `RequestBodyLimitMiddleware` has already bounded. With
messaging installed, expired keys are purged hourly; without it, each new
key deletes up to 100 expired ones once it has claimed its own, skipping
those another request is deleting (`SKIP LOCKED` on PostgreSQL), so
concurrent requests never queue behind one another's purge, nor deadlock
through PgBouncer.

**What a key costs.** A request claims its key with one statement, an
upsert that inserts the in-progress row or replaces an expired one, and
stores its response with one more. Each commits as it runs, without
`BEGIN` and `COMMIT`; through PgBouncer each is a transaction of its own,
which sets the timeouts. The commit mark is one `UPDATE` in the request's
own transaction. With authentication and one unit of work on PostgreSQL,
a first request makes 2 transactions and 4 pool checkouts (16 round
trips), and a replay 1 transaction and 2 checkouts (10 round trips). ADR
[0062](adr/0062-claim-idempotency-keys-in-one-statement.md) explains the
choices.

| `IDEMPOTENCY` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Alias holding the keys. |
| `TTL_SECONDS` | `86400.0` | How long a key is remembered (24 hours; at most 30 days, and at least `LEASE_SECONDS`, or startup fails). |
| `LEASE_SECONDS` | `60.0` | How long a request holds its key before a retry may take it over (at most a day, and above `REQUEST_TIMEOUT`, or startup fails). |
| `METHODS` | `POST`, `PATCH` | Methods that honour the header; others ignore it. |
| `MAX_RESPONSE_BYTES` | `1048576` | Larger responses are passed through but not stored. |
| `CREDENTIAL_HEADERS` | `authorization`, `proxy-authorization`, `cookie`, `x-api-key` | Headers that tell callers without an actor apart. |

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
- Amounts must be exact. A `Decimal` in the facts is written as a
  fixed-point string (`Decimal("10.00")` as `"10.00"`) and read back as that
  string. Floats are refused, with a `ValueError` naming where they are:
  most decimal amounts have no exact float, and the receipt would prove a
  rounded value. Records written before floats were refused still read,
  verify, and, if queued in the outbox, record as they were.
- The subject is an internal id, such as `customer:42` or a UUID: letters,
  digits, and `_ . : / -`. Email addresses, names, and phone numbers with a
  `+` are refused (`ValueError`, without echoing the value). A check cannot
  tell a bare phone number from a numeric id, so use ids with a type, such
  as `customer:42`.
- Keys are `<NAMESPACE>/<subject digest>/<evidence id>`. The digest is an
  HMAC-SHA-256 of the subject under `SUBJECT_KEY`, so keys show no subject
  and nobody without the key can find a subject's records by hashing
  guesses. `for_subject("customer:42")` is a prefix scan.
- `get` and `verify` read only keys of that form under `NAMESPACE` or one
  of `PREVIOUS_NAMESPACES`; any other key is `EvidenceNotFound`, so a
  caller who sends a key cannot read another project's or environment's
  records on a shared ledger, or anything else on it. Changing
  `NAMESPACE` hides the receipts written before until the old namespace
  is listed in `PREVIOUS_NAMESPACES` (in 0.9.0, `get` read any key).
- Recording is idempotent by evidence id: a retry returns the same receipt,
  and another record with that id is an `EvidenceConflict` (409). The ledger
  writes a key only if it is new, so writers racing to record the same
  evidence leave one revision and all get its receipt.
- Reads return revision 1, the original. If anything wrote the key again,
  `RecordedEvidence.intact` is false and `verify()` raises
  `EvidenceTampered`. So does a read of a record under a key that does
  not end with its own id, and a `for_subject` listing that finds a record
  about another subject under the prefix: real records copied elsewhere.
  With immudb, every read and write is also checked against a Merkle proof
  and, in deployments, the server's signature.
- Receipts are what tie evidence to your data: anyone holding the ledger's
  write credentials can add records, though never change one unnoticed. Keep
  each receipt (or key) with the change it proves; `for_subject` is a
  convenience listing.
- Inside a use case, `key = defer_evidence(uow, evidence)` records it
  through the outbox once the change commits (messaging must be
  installed). Store the key with the change.

**Never put personal data in the subject or facts**: a ledger cannot
forget, so an erasure request could not be met. Store references or
digests, or encrypt personal fields with keys you can destroy.

`python manage.py evidence --subject customer:42` lists a subject's records;
`evidence --verify <key>` proves one.

**Deployments need `SUBJECT_KEY`**: staging and production refuse to start
without it. Generate a value once, keep it in your secret store, and read
it from the environment:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```python
EVIDENCE = {"SUBJECT_KEY": env.str("EVIDENCE_SUBJECT_KEY")}
```

`env.str` reads `<PREFIX>EVIDENCE_SUBJECT_KEY`, or a mounted secret file
named by `<PREFIX>EVIDENCE_SUBJECT_KEY_FILE`. Back the key up with the
ledger's credentials, and never change it: `for_subject` finds records only
under the key they were written with, though the keys kept with their
receipts always work.

| `EVIDENCE` key | Default | Meaning |
| --- | --- | --- |
| `LEDGER` | `"evidence"` | Alias in `LEDGERS` (`immudb://`; `memory://` in tests). |
| `NAMESPACE` | `"evidence"` | Key prefix, so projects or environments can share one ledger. |
| `PREVIOUS_NAMESPACES` | `()` | Namespaces used before `NAMESPACE`, whose records `get` and `verify` still read; at most 8, each a namespace as `NAMESPACE` is. |
| `MAX_BYTES` | `262144` | Largest record; keep documents elsewhere and record their digests. |
| `SUBJECT_KEY` | `None` | Secret for the subject digests in keys, 32 characters or more. Required in staging and production; elsewhere a public development key is used. Keep it for as long as the evidence: a new key hides earlier records from `for_subject`, though the keys in their receipts still work. |

## Flags: feature flags and dynamic settings

A flag turns a feature on for everyone, for no one, or for some users, in
the database, without a deployment:

- `everyone`: `True` is on for all, `False` off for all; unset (`None`), the
  rules below decide;
- `users`: user ids it is on for;
- `groups`: group names it is on for;
- `percentage`: a share of the other users, 0 to 100. A user's place comes
  from a hash of the flag's name and the user's id, so they stay in or out
  while the percentage stays, and stay in as it grows.

Anonymous callers get a flag only when `everyone` is on. A flag nobody
created is off.

```python
from fastapi import Depends

from domainkit.contrib.auth import Public
from domainkit.contrib.flags import RequireFlag, flag_enabled

if await flag_enabled("new_checkout", user=user):
    ...


@router.get("/beta", dependencies=[Depends(RequireFlag("beta")), Public])
async def beta() -> ...: ...
```

`flag_enabled` takes the request's user (`domainkit.contrib.auth`), or any
object with an `id` and, optionally, `groups` with names. `RequireFlag`
answers 404 while the flag is off for the caller, so the route looks
absent. It is not an access rule: the route still needs `Require(...)` or
`Public`.

A dynamic setting is a value staff change at runtime, typed and validated
by pydantic. Declare it in code, with its default:

```python
from typing import Annotated

from pydantic import Field

from domainkit.contrib.flags import dynamic_setting

MAX_ITEMS = dynamic_setting(
    "checkout.max_items",
    Annotated[int, Field(ge=1, le=100)],
    default=10,
    description="Items per order.",
)

limit = await MAX_ITEMS.get()
```

`get()` returns the stored value, or the default when none is stored. A
stored value the type no longer accepts (the declaration changed) gives the
default, and a warning in the log.

The audit trail records each change of a setting with its value, for
good. Declare a setting that holds a secret, or personal data, with
`secret=True`: its changes are recorded as `{"key": ..., "value": null,
"secret": true}`, and the staff API and `flags settings` show no value
(`null`, `********`). The value is still stored as it is, in the table
and the flags' cache entry, and `flags setting KEY VALUE` leaves it in the
shell's history, so keep credentials such as provider API keys in settings
or a secret store rather than here. Changes of a flag record
its rules, with its users as a count, not their ids.

Change both from the command line, or through the staff API:

```shell
python manage.py flags                                    # every flag
python manage.py flags set beta --percentage 10 --groups staff
python manage.py flags set beta --everyone on            # on, off, or rules
python manage.py flags delete beta
python manage.py flags settings                           # every dynamic setting
python manage.py flags setting checkout.max_items 25      # the value, as JSON
python manage.py flags setting checkout.max_items --reset
```

```python
from domainkit.contrib.flags.admin import router as flags_router

router.include_router(flags_router, prefix="/admin/flags")
```

| Endpoint | Permission |
| --- | --- |
| `GET /flags` | `flags.view_flag` |
| `POST /flags` | `flags.add_flag`; a taken name is 409 |
| `PUT /flags/{name}` | `flags.change_flag` |
| `DELETE /flags/{name}` | `flags.delete_flag` |
| `GET /settings` | `flags.view_dynamicsetting`: values, defaults, descriptions, and JSON schemas |
| `PUT /settings/{key}` | `flags.change_dynamicsetting`; a value the type refuses is 422 |
| `DELETE /settings/{key}` | `flags.change_dynamicsetting`: back to the default |

Every endpoint also needs a staff user (`is_staff`) or a superuser. Each
change, from the API or the command, is a domain event (`FlagChanged`,
`FlagDeleted`, `SettingChanged`, `SettingReset`), so the audit trail records
who changed what, and `auditlog --aggregate name=beta` shows a flag's
history.

Every flag and stored setting is read in one query and cached as one entry
in `FLAGS.CACHE`. A change drops the entry once it commits, so this process,
and every process sharing a Redis cache, see it at once. Processes with
their own memory cache see it when their entry expires, within
`FLAGS.CACHE_TTL`.

| `FLAGS` key | Default | Meaning |
| --- | --- | --- |
| `CACHE` | `"default"` | The cache alias holding the flags and settings. |
| `CACHE_TTL` | `30.0` | Seconds a cached copy lives: how long other processes with a memory cache may see an old value. |
