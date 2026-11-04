# Messaging: outbox, durable handlers, background tasks

Some work must happen after a change commits, even if the process dies
right after: sending a welcome mail, calling a payment provider,
updating another system. The messaging app writes such work to an
**outbox** table, in the same transaction as the change. A **worker**
process (`python manage.py runworker`) then runs it, and runs it again
if it fails. Nothing is sent for a change that rolled back, and nothing
is lost for a change that committed.

On this page you learn how to:

- write a background task and queue it from a [use
  case](../learning.md#words);
- run an event handler in the worker (a durable handler);
- make work run once in the database (the inbox), or one at a time per
  key (ordering keys);
- set retries, timeouts, priorities, rates, and schedules;
- run workers, on the outbox alone or with a broker such as RabbitMQ;
- inspect, retry, and cancel messages, and test code that queues them.

## Install

```python
INSTALLED_DOMAINS = [
    "domainkit.contrib.messaging",
]
```

```bash
python manage.py makemigrations -m "messaging"
python manage.py migrate
python manage.py runworker   # next to the web process
```

The app brings three tables, in every database that hosts a domain:
`domainkit_messages` (the outbox), `domainkit_inbox`, and
`domainkit_ordering_keys`.

## A first example

A task is an async function marked `@task`. A use case queues it with
`uow.defer`, and it is stored only if the change commits:

```python
from uuid import UUID

from domainkit.contrib.messaging import task


@task(name="shop.send_welcome")
async def send_welcome(user_id: UUID) -> None: ...  # send the mail


async with uow:
    ...
    uow.defer(send_welcome, user_id=user.id)
    await uow.commit()
```

After the commit, the message waits in the outbox:

```bash
python manage.py messages status
```

```text
pending  1
held     0
done     0
dead     0
cancelled 0
lag default: 0.0s
```

`runworker` claims it and runs `send_welcome`. Once it succeeds, the
message is `done`.

## Background tasks

`@task` registers an async function as a task. Its options (a name, a
queue, retries) are in [Task options](#task-options). Queue a task in
two ways:

- **In a unit of work**: `uow.defer(task, **arguments)`. The message is
  written with the change, and only if it commits. `uow.defer` also takes
  `delay=` (a `timedelta` before it runs), `dedupe_key=` (a second
  message with the same key is not queued), and `ordering_key=`.
- **Outside one**: `await enqueue(task, **arguments)` queues it now, in
  its own transaction, in the home outbox (`MESSAGING.DATABASE`).

```python
from domainkit.contrib.messaging import enqueue

await enqueue(send_welcome, user_id=user.id)
```

Arguments are stored as JSON and converted back with the task's type
hints (`UUID`, `datetime`, `Decimal`, pydantic models). Pass ids, not
names or email addresses: arguments stay in the outbox while the message
may run.

A unit of work that defers tasks, or publishes events with durable
handlers, without messaging installed, fails instead of losing the work.

## Durable event handlers

A handler subscribed with `durable=True` runs in the worker, not in the
request. Each committed event becomes a message:

```python
from domainkit.ddd import event_bus

# In DomainConfig.ready():
event_bus.subscribe(UserCreated, notify_crm, durable=True)
```

A durable handler runs only for the events it is subscribed to (or their
subclasses). A message that pairs it with another event is
dead-lettered. Durable handlers use the `MESSAGING` settings for their
retries and timeouts.

## Delivery is at least once

A worker claims messages with `FOR UPDATE SKIP LOCKED`, so any number of
workers can share an outbox. It locks each one for `VISIBILITY_TIMEOUT`.
If its worker dies, another worker claims it again. So a task or a
handler may run twice: make it idempotent, or mark it `@inbox` so its
database work commits once ([the inbox](#once-in-the-database-the-inbox)).
Messages run in no set order, unless they share an ordering key
([ordering keys](#one-at-a-time-per-key-ordering-keys)).

### Failures, retries, and dead letters

A failure is retried with exponential backoff and jitter, up to
`max_attempts`. Then the message is dead-lettered and sent to
`ERROR_REPORTERS`. A message whose worker died (a crash, or a kill)
during its last attempt is dead-lettered too, as `AttemptsExhausted`: it
is not run again after `max_attempts`.

```text
task shop.flaky failed (attempt 1 of 2); retrying in 0.0s
Dead-lettered task shop.flaky after 2 attempts
```

A handler or task that raises `CancelledError` while the worker is not
stopping it (it awaited a task or future that was cancelled) has failed,
as `HandlerCancelled`. It is retried and counted as any failure.

A message keeps its last error, which is logged and reported too.
Validation errors (arguments or an event that no longer fit) are kept as
`InvalidData`, naming the fields and rules but not the values, which are
often personal data. `python manage.py messages list dead` lists dead
letters, and `messages retry ID` (or `all`) queues them again after a
fix ([Inspecting and cancelling](#inspecting-and-cancelling)).

### Unknown tasks and handlers, and deploys

A message names its task or durable handler. A worker that does not know
it (an older release, while a deploy rolls out) gives it back without
spending an attempt, after a delay that doubles. This is logged as a
warning and counted as the outcome `unknown`. The message keeps its
ordering key's place meanwhile. Once `UNKNOWN_TARGET_GRACE` (an hour)
has passed since a worker first found it unknown, it is dead-lettered,
and `messages retry` queues it again. So:

- **Deploy workers first** when a release adds tasks or durable
  handlers, and web processes first when it removes them, so no message
  waits for a worker that cannot run it. Within the grace, either order
  works.
- **Name your tasks.** A task's name is what its messages carry. Without
  `name=`, it is `module.function`: moving the function to another
  module, or renaming it, renames the task, and messages queued under the
  old name wait, then die. `@task(name="billing.push_invoice")` keeps the
  name whatever the code does. `check` warns about tasks without one in
  staging and production (`messaging.W001`). A durable handler's name is
  always `module.QualName`: keep a moved handler's old one subscribed for
  a release, or let its messages drain first.

The test plugin's `drain` dead-letters such a message at once and
reports it: in a test's process, every task is there.

### Database outages

A database outage does not stop a worker. When its outbox cannot be
reached (a restart, a network drop, a failover), it logs the error,
waits `POLL_INTERVAL`, doubling the wait after each failure in a row up
to a minute, and goes on. It logs again when the outbox answers. A
message that ran but whose outcome could not be written runs again once
its lock expires (`VISIBILITY_TIMEOUT`), as after a crash. A periodic
task whose queueing failed is queued on the next try.

## Once in the database: the inbox

At least once means that a handler may run twice for one message: its
worker died (or lost the database) after the handler committed but
before the message was recorded as done, RabbitMQ handed the message out
again, or the handler failed after its commit and was retried. Mark a
durable handler or a task `@inbox`, and its database work commits once
per message:

```python
from uuid import UUID

from domainkit.contrib.messaging import inbox, task
from domainkit.ddd import event_bus


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
  to. The row and the work commit or roll back together.
- A later run of the same message is skipped. If it is a retry or a
  redelivery, the worker looks the message up in the inbox of its
  outbox's database and does not call the handler at all. Otherwise (the
  handler commits on another database, or an operator retried a dead
  letter), its commit finds the row, raises `AlreadyHandled`, and rolls
  back. Either way the message is recorded as done, and logged as skipped
  (outcome `handled` in `messaging_messages_total`). Let `AlreadyHandled`
  propagate.
- Keep the work in one unit of work. Later commits of the same run write
  no row, so a run whose first commit landed is skipped whole when the
  message comes back: a second commit that had not happened yet never
  will.
- What the handler does outside the database, a mail or a call to
  another service, can still happen twice: before its commit, nothing
  tells the runs apart. Give such calls an idempotency key of their own
  (the message's id is `current_attempt().message_id`).
- In a chain, only the last step may be `@inbox` (`chain()` refuses
  others): a skipped run has no result to hand to the next step.
- Rows hold the handler's or task's name, the message id and the outbox
  holding it, and the time: nothing personal. A row lives as long as its
  message may run. Workers delete it, with finished messages, once the
  message is done or cancelled (or cleaned up), never while it is pending
  or dead, however long that lasts: a task held back by its `rate`, a
  queue no worker serves, a broker whose consumers are down, a dead
  letter an operator retries months later. A handler that commits on
  another database than its message's outbox has its rows there; that
  database's worker asks the outbox which messages finished. Rows of a
  database alias no longer configured stay.
- The inbox costs one insert in the handler's transaction, and one row
  per message until the message finishes. Handlers not marked keep the
  old rule: at least once, idempotent.

### A run that outlived its lease cannot commit

A handler can run past its claim's lease when its event loop is blocked
by synchronous code, its process is paused, or it ignores its
cancellation. Another worker then claims the message and runs it too.
Each unit of work that a running handler commits on the database of its
message's outbox first renews the lease, in the same transaction, if
this run still holds it. If another worker took the message over, the
commit raises `LeaseLost` and rolls back, and the run ends as
`superseded`, logged as a warning; the worker that holds the message
runs it. A run without a commit that finishes after losing its lease
changes nothing either. On another database there is no lease row to
check: mark such a handler `@inbox`. Let `LeaseLost` propagate.

## One at a time per key: ordering keys

Messages that share an ordering key run one at a time, in the order
their commits happened, across every worker and replica of the outbox.
Messages of other keys, and messages without one, run beside them. Give
a key when queueing, or declare how a task or durable handler derives
it:

```python
from decimal import Decimal
from uuid import UUID

from domainkit.contrib.messaging import enqueue, ordered, task

uow.defer(post_entry, account_id=account.id, ordering_key=f"account:{account.id}")
await enqueue(push_status, payment_id=payment.id, ordering_key=str(payment.id))


@task
@ordered("account_id")  # the argument's value is the key; before or after @task
async def post_entry(account_id: UUID, amount: Decimal) -> None: ...


@ordered(lambda event: f"payment:{event.payment_id}")  # or "payment_id"
async def send_status(event: PaymentEvent) -> None: ...


event_bus.subscribe(PaymentEvent, send_status, durable=True)
```

- **The key.** A string of 1 to 255 characters without a NUL character.
  Another value (a UUID, a number) is turned into one, and a declared key
  that comes out `None` leaves the message without one. A key given to
  `uow.defer` or `enqueue` that breaks these rules raises `ValueError`
  there; a derived one, at the commit. A task's key named by `@ordered`
  must be one of the task's arguments: `@task` refuses another with
  `ImproperlyConfigured`. `ordering_key=` wins over the declared key. A
  chain's steps carry its first step's key.
- **Choosing keys.** Keys are shared by every task and handler of an
  outbox, and by every tenant whose work it holds. A payment's status
  webhook and its ledger posting with one key run in one line, and so do
  two tenants' messages that happen to share one. Namespace keys by what
  they order and, with several tenants, by tenant
  (`f"account:{account.id}"`, `f"{tenant}:invoice:{invoice.id}"`). Put the
  target's name in the key to give it a line of its own. Never take a key
  raw from request data: a caller who chooses keys can join another's
  line and hold it back. Keep personal data out of keys (ids, not email
  addresses): `ordering_key` stays in the outbox with finished messages,
  and the key's row in `domainkit_ordering_keys`, and neither is erased
  or exported with a subject's data ([privacy.md](../privacy.md)).
- **Commit order.** Two transactions that write messages of one key take
  turns on the key: each writes its row in `domainkit_ordering_keys`
  first, and holds it until it commits. So their messages take places in
  the order of the commits, whatever the clocks say. A `REPEATABLE READ`
  or `SERIALIZABLE` unit of work whose snapshot is older than the key's
  last turn fails with a serialization failure instead of deciding on
  what it saw; `uow.run` runs it again.
- **Only the first waits to run.** The first unfinished message of a key
  is `pending`, and the ones after it are `held`: no worker claims them
  and no relay publishes them. When the first finishes, the worker that
  recorded it makes the next `pending` in the same transaction, and
  starts it (its own loop, `NOTIFY` on PostgreSQL, or, with a broker, by
  publishing it at once). The claim reads the same index as before, so a
  key with thousands of held messages costs it nothing (ADR 0118).
- **A failure holds the key.** A message that fails stays first of its
  key while it is retried, with its backoff: the key's later messages
  wait until it succeeds. So do a message over its `rate`, a delayed one
  (until its time), and the message of a worker that died, until its
  lease (`VISIBILITY_TIMEOUT`) ends. Then it runs again first, and the
  rest of the key after it, in order.
- **A dead letter lets the key go on.** Once a message is dead-lettered
  (after `max_attempts`, at once for `dont_retry_on`, or because its
  worker died during its last attempt), the key's next message runs, and
  the rest after it. The dead message is out of the line. `messages
  retry` puts it back in its place, before the key's messages still
  waiting: the pending one too, while no worker has started it and no
  relay has handed it to the broker (that one is held again behind it);
  else after the one under way. The messages that ran meanwhile stay
  run. A key that must stop at a failure (a ledger's, for example) gives
  its task a high `max_attempts` with a capped backoff: the message keeps
  retrying in place, holding the key, until it succeeds or a fix lets it.
  Watch for it in the logs and in `messages list held`.
- **Cancelling.** `messages cancel ID` cancels a held message too.
  Cancelling a key's first message lets the next one run.
- **What it costs.** A key runs one message at a time, with a round trip
  between each. Spread work over keys (per account, per payment), not one
  key for a whole kind of work. A keyed message costs its commit an
  upsert of its key's row, and its finish an upsert and an update.
  Messages without a key cost nothing more.
- **Where it holds.** Within one outbox: messages of one key written to
  two databases (shards) are two lines. Within one queue's workers too:
  a key whose messages go to a queue no worker serves waits for one.
- **A stalled key is released.** A key with held messages and none
  pending would wait for good: no worker claims held messages, and every
  new message of the key is held behind them. 1.0 never leaves a key so,
  but a 0.12 worker that finishes a keyed message that 1.0 wrote does
  (see [Upgrading](#upgrading)). Each worker's cleanup round makes the
  first held message of such a key pending, under the key's turn, and
  logs a warning naming the message. `messages release` does it at once.

`messages status` counts held messages, and `messages list held` lists
them by key, in order. Workers delete a key's row once none of its
messages waits or is dead and the key was not used for
`RETENTION_SECONDS`. That query reads the whole table of keys when few
are idle: 78 ms a batch at a million keys, once a `CLEANUP_INTERVAL`. An
index on `touched_at` would make it 12 ms, but every write and finish of
a keyed message updates that column, and would update the index too; the
table has none.

## Queues

Every message belongs to a queue: `default`, or the one its task names
(`@task(queue="emails")`, `@periodic(..., queue="reports")`). Durable
event handlers use `default`. `runworker --queue emails` runs only that
queue's messages (repeat `--queue` for several), so slow or bulky work
gets workers of its own and cannot hold up the rest. Without `--queue`, a
worker runs every queue. Each queue needs at least one worker, or its
messages wait.

## Task options

Each option of `MESSAGING` that concerns one task can be set on the task
itself, and wins over the setting:

```python
from uuid import UUID

import httpx2

from domainkit.contrib.messaging import Backoff, current_attempt, task


@task(
    name="billing.push_invoice",
    queue="billing",
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
    attempt = current_attempt()  # .message_id, .number, .max_attempts, .last
```

- **Backoff** waits `base` seconds before the first retry. Exponential
  backoff doubles that after each attempt, up to `cap` (an hour by
  default). With jitter (the default), each delay is drawn between half
  and all of it, so failures that happened together are retried apart.
  Delays are at most a year.
- **Retries.** `dont_retry_on` wins over `retry_on`; without `retry_on`,
  every error is retried. A run past its timeout raises `TimeoutError`.
- **Timeouts** must stay under `VISIBILITY_TIMEOUT`, or the worker
  refuses to start: a claim that ends while the task still runs would run
  it twice. A run's timeout is its deadline (`domainkit.deadlines`). On
  PostgreSQL its transactions lower `statement_timeout` to the time left,
  and its calls through `domainkit.http.client` are limited by it, as in
  a request.
- **Priorities** order the due messages of a queue: higher first, then
  the oldest. They do not reorder queues; give urgent work a queue of its
  own. On a broker, the relay publishes each round in priority order, but
  RabbitMQ delivers in queue order, so priorities count only while
  messages wait in the outbox.
- **Rates** are counted by `RATE_LIMIT_BACKEND`: the memory backend
  counts in each worker process, `RedisBackend` across all of them. A
  message over the rate goes back to the outbox, without spending an
  attempt, and comes due when the rate allows; held messages are spaced
  one interval apart.
- **Arguments** stay in the outbox row (`payload`, in clear) only while
  the message may still run. The worker blanks them (`{}`) in the
  statement that marks it done, and `cancel` when it cancels it. The row
  keeps the task's name, id, times, attempts, and error, for
  `RETENTION_SECONDS`. A dead message keeps its arguments, so `messages
  retry` can run it again, for as long as it stays dead, unless its task
  is `sensitive`. Then they are blanked with its error recorded, its
  headers say `payload_blanked`, and a retry refuses it. Mark a task
  sensitive when its arguments hold secrets or personal data; mail, text
  messages, and push notifications are sensitive. In a chain, every step
  after a sensitive one is treated as sensitive too (its headers say
  `sensitive`): it is handed that step's arguments, or what it returned.
  Better still, queue ids and build the secret in the task.

`current_attempt()` returns the `Attempt` of the message being run (its
`message_id`, `number`, `max_attempts`, and `last`), or `None` outside
the worker:

```text
welcome 00000000-0000-4000-8000-000000000007 1 10 False
```

## Schedules

`@periodic(every=timedelta(...))` runs an argument-free task once per
interval, at the start of each, across all workers:

```python
from datetime import timedelta

from domainkit.contrib.messaging import periodic


@periodic(every=timedelta(hours=1))  # once per interval across all workers
async def purge_expired() -> None: ...


@periodic(cron="0 3 * * 1-5", tz="Africa/Cairo")  # 03:00 on weekdays, Cairo time
async def nightly_report() -> None: ...
```

Workers queue each slot under a key of its own, and keep its run until
the slot ends, even past `RETENTION_SECONDS`. So a worker that starts
during the slot does not run it again. Queueing never holds up the work
that is due: a failure is logged and tried again after a pause that
doubles, up to a minute.

`@periodic(cron="...", tz="...")` runs the task at the wall-clock times
of a five-field cron expression, in an IANA time zone (UTC by default):

| Field | Values |
| --- | --- |
| minute | 0-59 |
| hour | 0-23 |
| day of month | 1-31 |
| month | 1-12 or `JAN`-`DEC` |
| day of week | 0-7 (0 and 7 are Sunday) or `SUN`-`SAT` |

Each field is `*`, a value, a range (`1-5`), a step (`*/15`, `0-30/10`,
or `5/15`, from 5 to the end), or a list of these (`1,15`). `@hourly`,
`@daily` (`@midnight`), `@weekly`, `@monthly`, and `@yearly`
(`@annually`) stand for the usual expressions. As in cron, when both day
fields are restricted, a day matches either: `0 0 1 * MON` runs on the
first of the month and on Mondays. An expression that never runs
(`0 0 30 2 *`) is refused at startup.

**Daylight saving time** never runs a time twice and never skips one:

- A time that a spring-forward gap skips (02:30 when clocks jump from
  02:00 to 03:00) runs once, at the end of the gap (03:00).
- A time that a fall-back repeats (01:30 when clocks go back from 02:00
  to 01:00) runs once, at its first occurrence. When the hour field
  starts with `*` (`*/15 * * * *`), times in the repeated hour run in
  both passes, so the schedule keeps its pace in elapsed time.

The worker of the home outbox queues each cron task's next run ahead of
time, under a key derived from its time, so all workers share one row per
run. A run whose time passes while every worker is down still runs once
one is back; earlier missed runs are not made up. A changed schedule
takes effect after the run already queued. Time zones come from the
system; Windows has none, so install `domainkit[tzdata]` there.

## Chains

`chain(a, b, c)` returns a `Chain`: it runs `a`, then `b` once `a`
succeeds, then `c`. Defer it like a task; the keyword arguments go to the
first step:

```python
from uuid import UUID

from domainkit.contrib.messaging import chain, task


@task(name="shop.charge")
async def charge(order_id: UUID) -> dict[str, str]:
    return {"order_id": str(order_id), "charge_id": "ch_1"}


@task(name="shop.send_receipt")
async def send_receipt(order_id: UUID, charge_id: str) -> None:
    print("receipt", order_id, charge_id)


uow.defer(chain(charge, send_receipt), order_id=order.id)
```

```text
receipt 00000000-0000-4000-8000-000000000007 ch_1
```

Each next step gets the previous step's result as its keyword arguments.
A step that returns `None` passes its own arguments on, and any other
value is passed as `result=`. Results must be JSON-friendly; one that is
not fails the step. The next step is written in the transaction that
marks the previous one done, so a crash between them loses or repeats
nothing. It runs on its own queue, with its own options, and under the
request id, actor, and trace of the chain's start. A step that is
dead-lettered ends the chain.

## Running workers

`python manage.py runworker` runs the outbox's messages until it is
stopped:

| Option | What it does |
| --- | --- |
| `--once` | Run the messages that are due now, then exit (cron, tests). |
| `--database D` | Run only the outboxes of a target (an alias or a shard group). |
| `--queue Q` | Run only this queue's messages; repeat for several. |
| `--no-relay` | With a broker: only consume, never poll the outbox. |
| `--metrics-port P`, `--metrics-address A` | Serve Prometheus metrics (needs `domainkit[metrics]`). |

Scale by running more workers. However many outboxes and queues a
`runworker` process serves, it runs at most `CONCURRENCY` messages at
once: its workers share one `Budget` of slots, given first come, first
served. In code, `Worker(database=..., queues=...)` runs one outbox:
`await Worker().run_once()` runs what is due and returns how many ran,
and `await Worker().run(stop)` runs until the `asyncio.Event` is set.

**Several databases.** Each database that hosts domains (each shard too)
has its own outbox, written in the same transaction as that database's
changes. `runworker` runs one worker per outbox, all in one process by
default; `runworker --database <target>` runs only a target's, so groups
can scale apart. Periodic tasks and `enqueue()` use the home outbox,
`MESSAGING.DATABASE`, which must be one alias. `messages status
--database <alias>` (and each action of `messages`) inspects any outbox.

On PostgreSQL, the database's clock decides what is due and when a lock
expires. So hosts whose clocks disagree neither run a message twice nor
leave it waiting. A commit wakes idle workers at once (`LISTEN`/`NOTIFY`,
see `NOTIFY`), so `POLL_INTERVAL` can be longer. Old messages are deleted
in small batches, by a task of each worker's own, so cleanup never holds
up claiming. [runtime.md](../runtime.md), "Workers and the outbox", has
the details.

**Idle workers.** An idle worker still polls every `POLL_INTERVAL`, even
while commits wake it: each poll is a claim that finds nothing, a round
trip a second per worker on the primary. `POLL_INTERVAL_IDLE` lets it
wait longer while it listens: each empty poll doubles the wait, up to
that value, and work or a commit starts again from `POLL_INTERVAL`. With
`30`, an idle worker made 7 claims in its first idle minute instead of
61, and then one every 30 seconds. Commits still start their messages at
once. What no commit announces (a retry coming due, a delayed task, the
lock of a crashed worker running out, a notification that never arrived)
waits up to `POLL_INTERVAL_IDLE` instead of `POLL_INTERVAL`, so set it no
higher than the delay such messages can take. Periodic and cron tasks are
among them: the worker queues a periodic slot when it next polls, and a
cron run is queued ahead, due later, so either can start up to
`POLL_INTERVAL_IDLE` after its time. Workers that cannot listen (SQLite,
PgBouncer, `NOTIFY = False`, a listening connection that dropped) poll
every `POLL_INTERVAL`.

**Watching PostgreSQL's notification queue.** PostgreSQL keeps sent
notifications in one queue per server (8 GB by default) until every
session that listens has read them. A listener that stops reading, such
as a session that runs `LISTEN` and then sits in a long transaction, or
a client whose process hangs, holds the queue's tail. The queue fills,
and once it is full every transaction that sends `NOTIFY` fails at
commit, domainkit's outbox writes included. Workers listen on a
connection of their own and throw it away when they stop. Watch the
queue:

```sql
SELECT pg_notification_queue_usage();  -- 0 (empty) to 1 (full)
```

Alert well before half full (0.5); from there on PostgreSQL also logs a
warning naming the process that holds the queue back. Ending that
process (`pg_terminate_backend(pid)`) lets the queue drain;
`MESSAGING.NOTIFY = False` stops domainkit's notifications, and workers
fall back on polling.

**Stopping.** `SIGTERM` (or Ctrl+C) stops claiming (and, with a broker,
consuming, within 5 seconds even if the broker does not answer), and
gives running messages `DRAIN_TIMEOUT` seconds to finish. Messages still
running then are cancelled and go back to the outbox, due at once; the
cut-short run does not count as an attempt. A second signal stops them
at once. Keep `DRAIN_TIMEOUT` a few seconds under the time your platform
waits before it kills the process (Kubernetes'
`terminationGracePeriodSeconds`, 30 by default), so they are given back
rather than left locked until `VISIBILITY_TIMEOUT`. Handlers see the
cancellation as `asyncio.CancelledError`; let it propagate.

## Workers on RabbitMQ

By default, workers poll the outbox table every `POLL_INTERVAL`, so a
message waits up to that long, and every worker queries the database
even when idle. With a broker, the process that commits a message hands
it to the broker at once, workers receive it at once, and most workers
never poll the database. The outbox stays the record of every message
([ADR 0014](../adr/0014-outbox-as-record-broker-for-dispatch.md)):

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
  publisher and returns: a slow or stopped broker never delays the
  commit. The publisher locks them for a short lease (the broker's
  `CONNECT_TIMEOUT` plus `PUBLISH_TIMEOUT`, and 5 seconds) and publishes a
  small pointer to each (its id, its database, and the lock),
  `BATCH_SIZE` at a time. RabbitMQ confirms every publication, and a
  confirmed message stays locked for `REPUBLISH_AFTER`; one that the
  broker did not confirm, whatever the reason, a stop too, is unlocked at
  once. It declares each queue once per process. If more than 10,000
  committed messages are waiting to be published, newer ones are left to
  the relays. After `BREAKER_FAILURES` broker failures in a row, commits
  leave their messages to the relays for `BREAKER_COOLDOWN`, then the
  publisher checks the broker and tries again; this is logged once when
  it starts and once when it ends. At shutdown the publisher gets a fifth
  of `SHUTDOWN_TIMEOUT`, the time that shutdown's drain phase always has,
  and gives back what it cut short within it; the outbox holds the rest.
  Set `PUBLISH_ON_COMMIT` to `False` to leave all publishing to the
  relays. Delayed tasks wait for the relay.
- **Relay.** Workers also claim due messages of their outbox, as a
  polling worker would, and publish them the same way: delayed tasks
  when they fall due, retries, and anything a commit could not publish
  (the broker was down, the process died). A publication that fails is
  unlocked and tried again on the next round.
- **Consume.** Workers consume each queue with a prefetch of
  `CONCURRENCY` (per queue: quorum queues take no prefetch shared by a
  channel), and run at most `CONCURRENCY` deliveries at once in all; the
  others wait for a slot. For each delivery, a worker takes the message
  over by id, which works only for the pointer of the current publication
  (or one whose lock has expired). It runs the message, records the
  outcome, and only then acknowledges the delivery. A duplicate, or a
  delivery of a message that already ran, is acknowledged and skipped.
- **Retries and dead letters** stay in the outbox. A failed message goes
  back with its backoff delay, and the relay publishes it again when it
  is due, so RabbitMQ needs no delayed-message plugin. `messages list
  dead` and `messages retry` work as before.
- **Every delivery is answered**, so a failure never holds one of the
  consumer's `CONCURRENCY` slots. A delivery whose outbox cannot be
  reached is requeued after `POLL_INTERVAL`; any other is acknowledged
  once its outcome is recorded, or once it is clear it cannot run here. An
  answer that fails (the delivery's channel closed when RabbitMQ
  restarted or the connection dropped) is logged, and RabbitMQ hands the
  message out again; it never stops the worker or its other messages. The
  relay waits out database outages as the polling worker does.
- **Losses** cost time, never messages. A worker that dies mid-message
  leaves it to come back after `VISIBILITY_TIMEOUT`; a message that
  RabbitMQ lost is published again after `REPUBLISH_AFTER`.
- **Ordering keys** need nothing of RabbitMQ. The outbox holds a key's
  later messages back, so only its first is ever published, and the
  consumer that finishes it publishes the key's next at once. Any
  consumer may take any of them; the order holds.

On RabbitMQ, the worker uses one durable direct exchange,
`<PREFIX>.work`, and a quorum queue per queue, `<PREFIX>.<queue>`, bound
by the queue's name. Messages are persistent and `mandatory`. Give the
workers a user of their own, limited to the prefix on the virtual host
(CI checks that these permissions are enough):

```text
rabbitmqctl add_user billing-worker '<long random password>'
rabbitmqctl set_permissions -p billing billing-worker '^billing\.' '^billing\.' '^billing\.'
```

Relaying workers also schedule periodic tasks and clean up. A few are
enough: run the rest with `runworker --no-relay`, so they only consume
and never query the database until a message arrives.

On a laptop, with PostgreSQL 18 and RabbitMQ 4.3 in Docker and one
worker process (`CONCURRENCY` 50), a brokered message ran about 35 ms
after its commit, against about 870 ms when polling every second.
`enqueue()` took 5 ms instead of 1.4 ms (RabbitMQ confirmed in 0.6 ms),
and a full outbox drained at 950 tasks per second instead of 470.

`python manage.py broker declare` creates the exchange and the queues
before the workers start; run it with an account that may configure the
virtual host if the workers' account may not. `broker check` tells
whether each broker answers. In staging and production, brokers must use
`amqps://` with certificate checks, and a user other than `guest`;
`memory://`, an in-process broker for tests and development, is refused
there.

Keep `TASK_TIMEOUT` well under RabbitMQ's `consumer_timeout` (30 minutes
by default), which closes the channel of a consumer that holds a message
longer. Keep `REPUBLISH_AFTER` above the longest a message may wait in a
queue behind others: the relay takes an older message for a lost one and
publishes it again. Nothing runs twice, because the first copy is
skipped, but the queue grows. Add workers before a backlog gets that
long. Quorum queues dead-letter or drop a message delivered more than
their delivery limit (20 by default); for the worker that is harmless,
because the relay publishes such a message again after
`REPUBLISH_AFTER`.

### Other brokers

The same worker takes its work from Redis Streams, NATS JetStream, Kafka,
or Amazon SQS: set `URL` to their scheme
([BROKERS](../reference/backends.md)). These are asked for messages. A
subscription runs one pump that asks for as many as its prefetch has
room for, hands each to a task of its own, and holds the slot until the
message is answered. A broker that cannot be reached is logged and asked
again after a pause that grows to 30 seconds: the outbox stays the
record, so an outage delays work and loses none.

| Broker | Queues | A message not answered |
| --- | --- | --- |
| Redis Streams (`redis://`, `rediss://`) | a stream `<PREFIX>:<queue>`, one consumer group; answered messages are deleted | claimed by another consumer after `REDELIVER_AFTER` (`XAUTOCLAIM`) |
| NATS JetStream (`nats://`, `nats+tls://`) | one stream `<PREFIX>` with work-queue retention, a durable pull consumer per queue; a message id published twice is stored once | handed out again after `REDELIVER_AFTER` (its `ack_wait`) |
| Kafka (`kafka://`, `kafka+ssl://`) | a topic `<PREFIX>.<queue>` of `PARTITIONS` partitions and `REPLICAS` copies, one consumer group; positions are committed as far as every earlier message is answered | read again by the worker that takes its partition |
| Amazon SQS (`sqs://<region>`) | a queue `<PREFIX>-<queue>`, made with `REDELIVER_AFTER` as its visibility timeout | visible again after `REDELIVER_AFTER` |

A requeued message comes back at once: added again (Redis, Kafka),
negatively acknowledged (NATS), or made visible (SQS). Keep
`REDELIVER_AFTER` above `TASK_TIMEOUT`, or a slow task runs twice (the
inbox skips the second run, but it costs a worker). Kafka's partitions
limit how many workers share a queue. Staging and production refuse these
without TLS (`rediss://`, `nats+tls://`, `kafka+ssl://`; SQS is HTTPS) or
without credentials.

## Inspecting and cancelling

The `messages` command inspects an outbox and acts on it. Every action
takes `--database` (default: `MESSAGING.DATABASE`):

| Action | What it does |
| --- | --- |
| `messages status` | Count messages by status, and show each queue's lag: how long its oldest due message has waited for a worker. |
| `messages list STATE [--queue Q] [--limit N]` | List messages, without their arguments: `pending` (in the order workers take them, `due` or `scheduled`), `held` (waiting for an earlier one of their key, by key and in order), `locked` (`running`, or `published` to the broker), or `dead`. `--limit` defaults to 20. |
| `messages retry ID` or `all` | Queue dead letters again, with fresh attempts. |
| `messages cancel ID` | Cancel a message that no worker is running. |
| `messages release` | Make the next message of each stalled ordering key pending. |

```bash
python manage.py messages list pending
python manage.py messages list dead
python manage.py messages retry all
```

```text
1bee7089-cb5f-47ea-b898-79c2f091c507  task shop.send_welcome  queue=default priority=0 x0/10  due 2026-11-04 18:39:00Z
8db6d3d4-43e8-4e3a-8f7f-ea92d5036a7e  task shop.flaky  x2  RuntimeError: the provider is down
Queued 1 message(s).
```

`retry` skips the dead letters of `sensitive` tasks, whose arguments were
blanked: `retry ID` refuses one, and `retry all` says how many it left.
`cancel` cancels a waiting or held message, or one on its way through the
broker (its delivery is then skipped). A running message is refused:

```text
CommandError: 00000000-0000-4000-8000-000000000001 was not cancelled: it is running, already finished, or not in this outbox.
```

A cancelled message never runs, nor does the rest of its chain. It is
kept as `cancelled`, without its arguments, and deleted after
`RETENTION_SECONDS`. `retry`, `cancel`, and `release` take `--operator`,
and are recorded in the audit trail with the operator who ran them
([operators](../reference/commands.md#operators)).

`domainkit.contrib.messaging.admin` has the same operations for your own
tools, unrecorded: `list_messages`, `cancel`, `queue_counts`,
`queue_lag`, `retry_dead`, `blanked_dead_letters`, and
`release_stalled`.

With Prometheus, `messaging_outbox_lag_seconds` (per queue) is the same
lag, next to `messaging_outbox_messages`; alert when it grows.

## Traces

A message carries the W3C trace context (`traceparent`, `tracestate`) of
the span that was active when it was written: the unit of work's commit,
or the request. With the `tracing` extra, the worker's `message.process`
span continues that trace, so a request and the work it queued show as
one trace. A context that does not follow the W3C format is dropped, and
the message starts a trace of its own. Without the extra, nothing is
written.

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
| `captured_events` | A `CapturedEvents`: the domain events published after commits. `captured_events.of_type(UserCreated)`, `.assert_published(UserCreated, username="ann")`, `.assert_not_published(...)`. |
| `outbox` | An `Outbox`: the messages committed during the test (`QueuedMessage`s), each with its `ordering_key`. `outbox.queued(send_welcome)`, `.assert_queued(send_welcome, user_id=user.id)`, `.assert_not_queued(...)`. `outbox.run_queued()`, or `await outbox.arun_queued()`, runs the due ones here as a worker would, chained steps too, until none is left; `delayed=True` runs delayed messages and retries too. A failed run raises `QueuedWorkFailed`. |
| `execute_deferred` | A `DeferredExecutor`: each commit's messages run right after it commits, before `commit()` returns. Failures are kept in `execute_deferred.failures` and fail the test at its end unless the test clears them. |

```python
async def test_signup_welcomes(db, outbox, captured_events):
    user = await sign_up("ann@example.com")

    outbox.assert_queued(send_welcome, user_id=user.id)
    captured_events.assert_published(UserCreated, user_id=user.id)
    await outbox.arun_queued()
```

`await drain()` runs every due message of every outbox in the test's
process, until none is left, and returns a `Drained` (`ran`, and
`failures`). With `delayed=True`, it makes delayed messages and retries
due first. It does not queue periodic tasks.

`with tasks_kept():` puts the registered tasks back as they were when
the block ends: tasks that the block registered (`@task` in a test) are
dropped. The plugin keeps each test's tasks to itself this way.

## Settings

Every key is optional. [Settings](../reference/settings.md) lists the
other settings.

| `MESSAGING` key | Default | Meaning |
| --- | --- | --- |
| `DATABASE` | `"default"` | The home outbox: periodic tasks and `enqueue()` write here, and its worker schedules periodic tasks. One alias, not a shard group. |
| `MAX_ATTEMPTS` | `10` | Runs before dead-lettering, unless the task sets its own. |
| `RETRY_BASE_SECONDS` | `2.0` | The first retry's delay; it doubles per attempt (at most 365 days). |
| `RETRY_MAX_SECONDS` | `3600.0` | The longest retry delay (at most 365 days). |
| `TASK_TIMEOUT` | `240.0` | Seconds a message may run; longer counts as a failure (at most a day). |
| `VISIBILITY_TIMEOUT` | `300.0` | Seconds a claim lasts; must be more than `TASK_TIMEOUT` (at most 2 days). |
| `POLL_INTERVAL` | `1.0` | Seconds an idle worker waits before looking again (at most an hour). |
| `POLL_INTERVAL_IDLE` | `None` | While commits wake the worker (`NOTIFY`, on PostgreSQL): the longest it waits between polls, from `POLL_INTERVAL` to an hour. Each poll that finds nothing doubles the wait up to this; work or a commit starts again from `POLL_INTERVAL`. A message that no commit announces (a retry, a delay, an expired lock) can wait this long. `None`: every wait is `POLL_INTERVAL`. |
| `BATCH_SIZE` | `50` | The most messages claimed at once (1 to 10,000). |
| `CONCURRENCY` | `10` | Messages one worker process runs at the same time, from all its outboxes and queues together (1 to 65,535, RabbitMQ's largest prefetch). |
| `DRAIN_TIMEOUT` | `20.0` | Seconds a stopping worker lets running messages finish; the rest are stopped and run again (at most an hour). |
| `UNKNOWN_TARGET_GRACE` | `3600.0` | Seconds a message naming a task or handler that no worker knows waits for one, given back with a doubling delay, before it is dead-lettered ([Unknown tasks and handlers](#unknown-tasks-and-handlers-and-deploys)); 0 dead-letters it at once (at most 365 days). |
| `RETENTION_SECONDS` | `604800.0` | Finished messages are deleted after this (7 days; at most 365 days); their arguments are blanked when they finish. Dead letters are kept until retried. A periodic task's run is kept until its slot ends, however long: it is what stops workers from queueing the slot again. |
| `CLEANUP_INTERVAL` | `3600.0` | Seconds between those deletions (at most a day). |
| `CLEANUP_BATCH_SIZE` | `1000` | The most finished messages one cleanup statement deletes (1 to 100,000). |
| `BROKER` | `None` | An alias in `BROKERS`: workers take their work from that broker instead of polling the outbox ([Workers on RabbitMQ](#workers-on-rabbitmq)). |
| `REPUBLISH_AFTER` | `3600.0` | With a broker: seconds a published message may wait for a worker before the relay publishes it again, as lost (at most a week). |
| `PUBLISH_ON_COMMIT` | `True` | With a broker: the committing process publishes the commit's due messages in the background at once, instead of leaving them to the relays' next poll. |
| `BREAKER_FAILURES` | `5` | With a broker: failed broker calls in a row after which commits stop publishing and leave their messages to the relays (at most 10,000). |
| `BREAKER_COOLDOWN` | `30.0` | Seconds commits leave their messages to the relays before the publisher tries the broker again (at most an hour). |
| `NOTIFY` | `True` | On PostgreSQL: commits wake idle workers at once (`LISTEN`/`NOTIFY`), unless a broker publishes each commit. |
| `METRICS_TTL` | `15.0` | Seconds the outbox gauges are reused between scrapes (at most an hour); 0 counts every scrape. |

## Checks

| Id or error | When | What to do |
| --- | --- | --- |
| `messaging.W001` | Staging or production: a task registered without `name=`. | Give every task a stable `name=`. |
| `ImproperlyConfigured` at startup | A task's `timeout` (or `TASK_TIMEOUT`) is not under `VISIBILITY_TIMEOUT`; a cron expression never runs; `@ordered` names an argument the task does not have. | Fix the task or the setting. |
| Refused in staging and production | A broker without TLS or credentials, `guest`, or `memory://`. | Use `amqps://` (or the TLS scheme of your broker) and a user of its own. |
| A commit that fails | A unit of work defers tasks or publishes events with durable handlers, and messaging is not installed. | Install messaging. |

## On PostgreSQL and SQLite

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Claims | `FOR UPDATE SKIP LOCKED`: any number of workers share an outbox. | SQLite runs one writer at a time, so claims take turns. Use it for development and tests. |
| What is due, lock expiry | The database's clock. | The host's clock. |
| Waking idle workers | `LISTEN`/`NOTIFY` (`NOTIFY`, `POLL_INTERVAL_IDLE`). | Polling every `POLL_INTERVAL`. |
| A run's deadline | Lowers `statement_timeout` to the time left. | Not applied to statements. |

## Upgrading

Each release below changed the tables. Run `makemigrations` and
`migrate` in each project that installs messaging, before deploying the
new code.

**0.5.** The outbox gained a `queue` column (default `default`).

**0.7.** The outbox gained `priority` and `started_at` columns, an index,
and a `cancelled` status.

**0.9.2.** The outbox gained an index for its cleanup,
`ix_domainkit_messages_finished`: `finished_at`, of done and cancelled
messages only. Without it, each cleanup read the whole outbox (460 ms
and 28,000 pages at 2 million rows, against 1.7 ms with it). The
revision builds the index in the migration's transaction, which blocks
writes to the outbox while it runs: about half a second per million rows
on a small server. For an outbox too large for that, edit the revision to
build it without blocking writes (PostgreSQL only):

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

**0.11.** Messaging has a new table, the inbox (`domainkit_inbox`), in
every database that holds an outbox. Until it is migrated, workers'
cleanup logs a failure each round, and `@inbox` handlers fail.

**1.0.** The outbox gained `ordering_key` and `sequence` columns, a
`held` status, and an index of keyed messages,
`ix_domainkit_messages_ordering`. Messaging has a new table of keys,
`domainkit_ordering_keys`, in every database that holds an outbox.
Workers claim through the new columns. Stop the 0.12 workers before any
1.0 process writes a message with an ordering key: a 0.12 worker finishes
such a message without making the next of its key pending, and the key
waits until a 1.0 worker's cleanup round, or `messages release`,
releases it ([upgrading.md](../upgrading.md#012-to-10)). Adding the
columns rewrites nothing; building the index reads the outbox once, and
adds no entry, because no existing message has a key. For an outbox too
large to block writes that long, build the index without blocking, as
for the cleanup index above (`postgresql_concurrently=True` in an
`autocommit_block()`).

## Errors and refusals

| Error | When |
| --- | --- |
| `AlreadyHandled` | An `@inbox` run's commit finds that an earlier run of the message committed. Let it propagate: the message is recorded as done. |
| `LeaseLost` | A run's commit finds that another worker took the message over. Let it propagate: the run ends as `superseded`. |
| `TimeoutError` | A run went past its timeout; it is retried. |
| `HandlerCancelled` | A handler raised `CancelledError` by itself; it is retried. |
| `AttemptsExhausted` | The worker died during the last attempt; the message is dead. |
| `InvalidData` | Arguments or an event no longer fit the task's or handler's types; the error names fields and rules, not values. |
| `ValueError` | An `ordering_key` that is empty, longer than 255 characters, or holds a NUL. |
| `QueuedWorkFailed` | In tests: queued work failed, or kept queueing more. |

## Common mistakes

- **A task without `name=`.** Moving the function renames its messages,
  which then die. Name every task.
- **Calling another service from a handler without an idempotency key.**
  At least once means it may run twice. Use `current_attempt().message_id`
  as the key, or mark the handler `@inbox` for its database work.
- **A timeout longer than `VISIBILITY_TIMEOUT`.** The worker refuses to
  start: another worker would claim the message while it runs.
- **One ordering key for a whole kind of work.** Everything then runs one
  at a time. Key by account, by payment, by order.
- **Personal data in task arguments or ordering keys.** Pass ids, and mark
  tasks whose arguments hold secrets `sensitive`.
- **A queue with no worker.** Its messages wait for ever. Run a worker
  for each queue, or one without `--queue`.

## See also

- [Runtime: workers and the outbox](../runtime.md#workers-and-the-outbox):
  how the worker runs, in depth.
- [Brokers](../reference/backends.md): `BROKERS` and each broker's
  settings.
- [The test plugin](../reference/test-plugin.md): the fixtures of
  `domainkit.testing`.
- [Exactly once in the database](../recipes/exactly-once.md): a how-to
  with the inbox.
- [Webhooks](webhooks.md) and [mail](../mail.md): apps that send through
  the outbox.
