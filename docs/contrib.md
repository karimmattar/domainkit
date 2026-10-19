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
| `TASK_TIMEOUT` | `240.0` | Seconds a message may run; longer counts as a failure. |
| `VISIBILITY_TIMEOUT` | `300.0` | Seconds a claim lasts; must exceed `TASK_TIMEOUT`. |
| `POLL_INTERVAL` | `1.0` | Seconds an idle worker waits before looking again. |
| `BATCH_SIZE` | `50` | Most messages claimed at once. |
| `CONCURRENCY` | `10` | Messages one worker runs at the same time. |
| `SHUTDOWN_TIMEOUT` | `20.0` | Seconds a stopping worker lets running messages finish; the rest are stopped and run again. |
| `RETENTION_SECONDS` | `604800.0` | Finished messages are deleted after this (7 days). |
| `CLEANUP_INTERVAL` | `3600.0` | Seconds between those deletions. |
| `BROKER` | `None` | An alias in `BROKERS`: workers take their work from that broker instead of polling the outbox (below). |
| `REPUBLISH_AFTER` | `3600.0` | With a broker: seconds a published message may wait for a worker before the relay publishes it again, as lost. |
| `PUBLISH_ON_COMMIT` | `True` | With a broker: the committing process publishes the commit's due messages in the background right away, instead of leaving them to the relays' next poll. |

Run `runworker --metrics-port 9100` to expose the worker's metrics. Scale by
running more workers.

**Stopping.** `SIGTERM` (or Ctrl+C) stops claiming and gives running
messages `SHUTDOWN_TIMEOUT` seconds to finish. Messages still running then
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
  The publisher locks them for `REPUBLISH_AFTER` and publishes a small
  pointer to each (its id, its database, and the lock), and RabbitMQ
  confirms every publication. It declares each queue once per process. If
  more than 10,000 committed messages are waiting to be published, newer
  ones are left to the relays, and at shutdown the publisher gets five
  seconds to finish; the outbox holds the rest. Set `PUBLISH_ON_COMMIT` to
  `False` to leave all publishing to the relays. Delayed tasks wait for the
  relay.
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
- `--dead`: dead letters; `--retry ID` (or `all`) queues them again;
- `--queue NAME` and `--limit N` narrow a listing.

`messages --cancel ID` cancels a message no worker is running: a waiting
one, or one on its way through the broker (its delivery is then skipped).
A running message is refused. A cancelled message never runs, nor does the
rest of its chain; it is kept as `cancelled` and deleted after
`RETENTION_SECONDS`. `domainkit.contrib.messaging.admin` has the same
operations for your own tools: `list_messages`, `cancel`, `queue_counts`,
`queue_lag`, `retry_dead`.

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
(`domainkit[webhooks]`, which brings httpx2), then expose the events:

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
than five minutes away. After a rotation, deliveries carry a second
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
URL's host only, since URLs may hold tokens. The two answers that hold a
secret are marked `Cache-Control: no-store`: no cache keeps them, and
`IdempotencyMiddleware` stores no copy, so a retry with the same
`Idempotency-Key` gets a 409 rather than the secret again. Secrets are
stored as they are in the subscriptions table, since signing needs them:
whoever reads that table can sign as you. They will move to encryption at
rest once domainkit has an encryption helper derived from `SECRET_KEY`.

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

A value and its subject always give the same pseudonym, so the trail still
answers "did this happen to this person?":
`await pseudonym_of(str(user.id), "username", "alice")` returns the pseudonym
to look for, or None once the person is forgotten. Each database keeps its
own keys, so look in the one that recorded the rows (`database=`, by
default `AUDIT.DATABASE`). A row whose personal value is None stores None.

To honour an erasure request, delete the person's key, in every database
that holds an audit trail:

```shell
python manage.py auditforget 4b0c6f0e-...            # or: await forget(str(user.id))
python manage.py auditforget --anonymous-days 30     # anonymous callers' days before that
```

Their pseudonyms stay in the trail, unchanged, but can no longer be linked
to them by anyone: the key was random and is gone. A later event about them
gets a new key, unlinkable to the old pseudonyms. Run `--anonymous-days` on
a schedule to keep anonymous callers' addresses only as long as you need
them. Rows written before this release keep their values in clear; the
append-only protection keeps them as they are.

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
  own chain.
- **Anchoring.** With `AUDIT.LEDGER` naming a ledger in `LEDGERS` (immudb),
  sealing also writes each chain's head (its number and link) there, at
  most once per `AUDIT.ANCHOR_EVERY`, as a new revision of
  `domainkit/audit/<alias>`. immudb keeps every revision and verifies what
  it returns.
- **Verifying.** `python manage.py auditverify` checks every row against
  its seal, every link against the one before, and every anchored head
  against the chain. It prints what it found and fails on changed or
  missing rows, broken links, rewritten anchors, seals that no anchor
  covers a day past `AUDIT.ANCHOR_EVERY` (anchoring stopped), and rows
  older than a day that were never sealed.

Every row can be sealed, whatever its strings hold (a lone surrogate, which
JSON can escape, included). A host whose clock runs ahead cannot stop
sealing or anchoring: a row or an anchor dated in the future counts as
dated now.

A row can be changed unnoticed only until it is sealed and its head
anchored, so anchor as often as you need: the default is hourly. Erasing a
person (`auditforget`) deletes a key, not a row, so the chain stays intact.

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
| `ANCHOR_EVERY` | 1 hour | Anchor a new head at most this often. |
| `SEAL_EVERY` | 5 minutes | How often the worker seals new rows. |

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
| First request ran past its lease and a retry took its key | The first gets 409 `idempotency_key_taken_over`; its changes are not saved |
| Client disconnected before sending the whole body | Nothing runs and no key is claimed: retry with the same key |

A request runs twice only if nothing it did was saved. When a SQL unit of
work commits during a keyed request, the key is marked committed in the
same transaction, and a committed key is never released, even if the reply
to the commit was lost. A retry then gets the stored response or, when the
response could not be kept, a 409 telling the client to look up the result
instead. Only units of work are seen: call payment providers and other
services from a deferred task (`uow.defer`), which the outbox runs once, not
from the handler.

A request holds its key for `LEASE_SECONDS`. If the process dies before the
request commits anything, a retry after the lease takes the key over and
runs. Each holder has a random token, and the mark is written only under
it: a request that ran past its lease and lost its key to a retry cannot
commit, so the changes are made once. Set `LEASE_SECONDS` above the
longest a request may run, such as your server's or proxy's timeout.

Keep `IDEMPOTENCY.DATABASE` on the database your writes use where you can.
A unit of work on another database cannot carry the mark in its
transaction: before it commits, the key is pinned until it expires, so no
retry takes it over, and right after, it is marked committed. A crash
after the pin leaves the key in progress until it expires. If the reply to
that commit is lost, the key is released as though nothing was saved.

Keys are scoped to the actor, which `AuthenticationMiddleware` sets (list
it first), so one user cannot replay another's response. Without an
actor, keys are scoped to the client address and a hash of the request's
`CREDENTIAL_HEADERS` (`Authorization`, `Proxy-Authorization`, `Cookie`,
`X-API-Key`): list the header your project authenticates with if it is
another. Add `IdempotencyMiddleware` last in `MIDDLEWARE`: it reads whole
bodies, which `RequestBodyLimitMiddleware` has already bounded. With
messaging installed, expired keys are purged hourly; without it, each new
key deletes up to 100 expired ones.

| `IDEMPOTENCY` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | Alias holding the keys. |
| `TTL_SECONDS` | `86400.0` | How long a key is remembered (24 hours). |
| `LEASE_SECONDS` | `60.0` | How long a request holds its key before a retry may take it over. |
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
| `MAX_BYTES` | `262144` | Largest record; keep documents elsewhere and record their digests. |
| `SUBJECT_KEY` | `None` | Secret for the subject digests in keys, 32 characters or more. Required in staging and production; elsewhere a public development key is used. Keep it for as long as the evidence: a new key hides earlier records from `for_subject`, though the keys in their receipts still work. |
