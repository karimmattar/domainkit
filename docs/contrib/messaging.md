# Messaging: outbox, durable handlers, background tasks

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
Handlers and tasks must therefore be idempotent, or mark themselves
`@inbox` so their database work commits once
([below](#once-in-the-database-the-inbox)). Order between messages is
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
| `RETRY_BASE_SECONDS` | `2.0` | First retry delay; it doubles per attempt (at most 365 days). |
| `RETRY_MAX_SECONDS` | `3600.0` | Cap on the retry delay (at most 365 days). |
| `TASK_TIMEOUT` | `240.0` | Seconds a message may run; longer counts as a failure (at most a day). |
| `VISIBILITY_TIMEOUT` | `300.0` | Seconds a claim lasts; must exceed `TASK_TIMEOUT` (at most 2 days). |
| `POLL_INTERVAL` | `1.0` | Seconds an idle worker waits before looking again (at most an hour). |
| `POLL_INTERVAL_IDLE` | `None` | While commits wake the worker (`NOTIFY`, on PostgreSQL): the longest it waits between polls, from `POLL_INTERVAL` to an hour. Each poll that finds nothing doubles the wait up to this; work or a commit starts again from `POLL_INTERVAL`. A message no commit announces (a retry, a delay, an expired lock) can wait this long. `None`: every wait is `POLL_INTERVAL`. |
| `BATCH_SIZE` | `50` | Most messages claimed at once (1 to 10,000). |
| `CONCURRENCY` | `10` | Messages one worker process runs at the same time, from all its outboxes and queues together (1 to 65,535, RabbitMQ's largest prefetch). |
| `SHUTDOWN_TIMEOUT` | `20.0` | Seconds a stopping worker lets running messages finish; the rest are stopped and run again (at most an hour). |
| `RETENTION_SECONDS` | `604800.0` | Finished messages are deleted after this (7 days; at most 365 days); their arguments are blanked when they finish. Dead letters are kept until retried. |
| `CLEANUP_INTERVAL` | `3600.0` | Seconds between those deletions (at most a day). |
| `CLEANUP_BATCH_SIZE` | `1000` | Most finished messages one cleanup statement deletes (1 to 100,000). |
| `BROKER` | `None` | An alias in `BROKERS`: workers take their work from that broker instead of polling the outbox (below). |
| `REPUBLISH_AFTER` | `3600.0` | With a broker: seconds a published message may wait for a worker before the relay publishes it again, as lost (at most a week). |
| `PUBLISH_ON_COMMIT` | `True` | With a broker: the committing process publishes the commit's due messages in the background right away, instead of leaving them to the relays' next poll. |
| `BREAKER_FAILURES` | `5` | With a broker: failed broker calls in a row after which commits stop publishing and leave their messages to the relays (at most 10,000). |
| `BREAKER_COOLDOWN` | `30.0` | Seconds commits leave their messages to the relays before the publisher tries the broker again (at most an hour). |
| `NOTIFY` | `True` | On PostgreSQL: commits wake idle workers at once (`LISTEN`/`NOTIFY`), unless a broker publishes each commit. |
| `METRICS_TTL` | `15.0` | Seconds the outbox gauges are reused between scrapes (at most an hour); 0 counts every scrape. |

Run `runworker --metrics-port 9100` to expose the worker's metrics. Scale by
running more workers. However many outboxes and queues a `runworker`
process serves, it runs at most `CONCURRENCY` messages at once.

On PostgreSQL, the database's clock decides what is due and when a lock
expires, so hosts whose clocks disagree neither run a message twice nor
leave it waiting, and a commit wakes idle workers at once
(`LISTEN`/`NOTIFY`, see `NOTIFY`), so `POLL_INTERVAL` can be longer. Old
messages are deleted in small batches, by a task of each worker's own, so
cleanup never holds up claiming. [runtime.md](../runtime.md), "Workers and
the outbox", has the details.

**Idle workers.** An idle worker still polls every `POLL_INTERVAL`, even
while commits wake it: each poll is a claim that finds nothing, a round
trip a second per worker on the primary. `POLL_INTERVAL_IDLE` lets it
back off while it listens: each empty poll doubles the wait, up to that
value, and work or a commit starts again from `POLL_INTERVAL`. With `30`,
an idle worker made 7 claims in its first idle minute instead of 61, and
then one every 30 seconds. Commits still start their messages at once;
what no commit announces (a retry coming due, a delayed task, the lock
of a crashed worker running out, a notification that never arrived)
waits up to `POLL_INTERVAL_IDLE` instead of `POLL_INTERVAL`, so set it
no higher than the delay such messages can take. Periodic and cron
tasks are among them: the worker queues a periodic slot when it next
polls, and a cron run is queued ahead, due later, so either can start up
to `POLL_INTERVAL_IDLE` after its time. Workers that cannot
listen (SQLite, PgBouncer, `NOTIFY = False`, a listening connection that
dropped) poll every `POLL_INTERVAL`, as before.

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

## Once in the database: the inbox

At least once means a handler may run twice for one message: its worker
died (or lost the database) after the handler committed but before the
message was recorded as done, RabbitMQ handed the message out again, or
the handler failed after its commit and was retried. Mark a durable
handler or a task `@inbox`, and its database work commits once per
message:

```python
from domainkit.contrib.messaging import inbox, task


@inbox
async def credit_account(event: PaymentReceived) -> None:
    async with LedgerUnitOfWork() as uow:
        account = await uow.accounts.get(event.account_id)
        account.credit(event.amount)
        await uow.commit()  # also writes (handler, message id) to the inbox


event_bus.subscribe(PaymentReceived, credit_account, durable=True)


@task
@inbox  # before or after @task
async def close_invoice(invoice_id: UUID) -> None: ...
```

- The run's first commit writes `(target, message id)` to
  `domainkit_inbox`, in its own transaction, on the database it commits
  to: the row and the work commit or roll back together.
- A later run of the same message is skipped. If it is a retry or a
  redelivery, the worker looks the message up in the inbox of its outbox's
  database and does not call the handler at all; otherwise (the handler
  commits on another database, or an operator retried a dead letter) its
  commit finds the row, raises `AlreadyHandled`, and rolls back. Either
  way the message is recorded as done, and logged as skipped (outcome
  `handled` in `messaging_messages_total`). Let `AlreadyHandled`
  propagate.
- Keep the work in one unit of work. Later commits of the same run write
  no row, so a run whose first commit landed is skipped whole when the
  message comes back: a second commit that had not happened yet never
  will.
- What the handler does outside the database, a mail or a call to another
  service, can still happen twice: before its commit nothing tells the
  runs apart. Give such calls an idempotency key of their own (the
  message's id is `current_attempt().message_id`).
- In a chain, only the last step may be `@inbox` (`chain()` refuses
  others): a skipped run has no result to hand to the next step.
- Rows hold the handler's or task's name, the message id and the outbox
  holding it, and the time: nothing personal. A row lives as long as its
  message may run: workers delete it, with finished messages, once the
  message is done or cancelled (or cleaned up), never while it is pending
  or dead, however long that lasts: a task held back by its `rate`, a
  queue no worker serves, a broker whose consumers are down, a dead
  letter an operator retries months later. A handler that commits on
  another database than its message's outbox has its rows there; that
  database's worker asks the outbox which messages finished. Rows of a
  database alias no longer configured stay.
- The inbox costs one insert in the handler's transaction, and one row per
  message until the message finishes. Handlers not marked keep the old
  rule: at least once, idempotent.

**A run that outlived its lease cannot commit.** A handler can run past
its claim's lease when its event loop is blocked by synchronous code, its
process is paused, or it ignores its cancellation; another worker then
claims the message and runs it too. Each unit of work a running handler
commits on the database of its message's outbox first renews the lease,
in the same transaction, if this run still holds it. If another worker
took the message over, the commit raises `LeaseLost` and rolls back, and
the run ends as `superseded`, logged as a warning; the worker holding the
message runs it. A run without a commit that finishes after losing its
lease changes nothing either. On another database there is no lease row
to check: mark such a handler `@inbox`. Let `LeaseLost` propagate.

## Queues

Every message belongs to a queue: `default`, or the one its task names
(`@task(queue="emails")`, `@periodic(..., queue="reports")`). Durable event
handlers use `default`. `runworker --queue emails` runs only that queue's
messages (repeat `--queue` for several), so slow or bulky work gets workers
of its own and cannot hold up the rest. Without `--queue`, a worker runs
every queue. Each queue needs at least one worker, or its messages wait.

## Workers on RabbitMQ

By default, workers poll the outbox table every `POLL_INTERVAL`, so a
message waits up to that long, and every worker queries the database even
when idle. With a broker, the process that commits a message hands it to
the broker right away, workers receive it at once, and most workers never
poll the database. The outbox stays the record of every message (see
[ADR 0014](../adr/0014-outbox-as-record-broker-for-dispatch.md)):

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

**Upgrading to 0.9.2.** The outbox gained an index for its cleanup,
`ix_domainkit_messages_finished`: `finished_at`, of done and cancelled
messages only. Without it, each cleanup read the whole outbox (460 ms
and 28,000 pages at 2 million rows, against 1.7 ms with it). Run
`makemigrations` and `migrate` in each project that installs messaging.
The revision builds the index in the migration's transaction, which
blocks writes to the outbox while it runs: about half a second per
million rows on a small server. For an outbox too large for that, edit
the revision to build it without blocking writes (PostgreSQL only):

```python
def upgrade() -> None:
    """Build the outbox's cleanup index without blocking writes."""
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_domainkit_messages_finished",
            "domainkit_messages",
            ["finished_at"],
            unique=False,
            postgresql_where=sa.text("status IN ('done', 'cancelled')"),
            postgresql_concurrently=True,
        )
```

If a concurrent build fails, it leaves an invalid index behind: drop it
(`DROP INDEX CONCURRENTLY ix_domainkit_messages_finished`) and migrate
again.

**Upgrading to 0.11.** Messaging has a new table, the inbox
(`domainkit_inbox`), in every database that holds an outbox. Run
`makemigrations` and `migrate` in each project that installs messaging
before deploying: until then, workers' cleanup logs a failure each round,
and `@inbox` handlers fail.

## Task options

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

## Schedules

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

## Chains

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

## Inspecting and cancelling

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
([operators](../reference/commands.md#operators)). `domainkit.contrib.messaging.admin`
has the same operations for your own tools, unrecorded: `list_messages`,
`cancel`, `queue_counts`, `queue_lag`, `retry_dead`, and
`blanked_dead_letters`.

With Prometheus, `messaging_outbox_lag_seconds` (per queue) is the same
lag, next to `messaging_outbox_messages`; alert when it grows.

## Traces

A message carries the W3C trace context (`traceparent`, `tracestate`) of
the span that was active when it was written: the unit of work's commit,
or the request. With the `tracing` extra, the worker's `message.process`
span continues that trace, so a request and the work it queued show as one
trace. A context that does not follow the W3C format is dropped, and the
message starts a trace of its own. Without the extra, nothing is written.

## Testing

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

