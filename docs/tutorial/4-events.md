# Part 4: events and the outbox

A deposit records the event `AccountDeposited`, as you saw in
[part 2](2-rules.md). In this part you react to that event outside the
request. The reaction is stored with the deposit, in the same
transaction, and a [worker](../learning.md#worker) runs it afterwards.
If the deposit rolls back, the reaction is never stored. If the worker
stops in the middle, it runs the reaction again.

## What you will build

- A [handler](../learning.md#handler) that writes each deposit to the
  log.
- A deposit whose reaction waits in the [outbox](../learning.md#outbox).
- One run of the worker, which runs the reaction and empties the outbox.

## Step 1: write a handler

A handler is an async function that takes an event. You mark it with
`@handles`, in `controllers.py`. When the domain starts, it subscribes
every marked handler of that file. Add a handler that writes each
deposit to the log. A real one would update a statement, or tell
another service.

```python title="src/domains/accounts/controllers.py"
import logging

from domainkit.ddd import handles

from domains.accounts.domain import AccountDeposited

logger = logging.getLogger(__name__)


@handles(AccountDeposited, durable=True)
async def log_deposit(event: AccountDeposited) -> None:
    """Log each deposit; the worker may run it twice, which is harmless."""
    logger.info("Deposited %s into account %s", event.amount, event.account_id)
```

`durable=True` puts the reaction in the outbox: a table in the same
database, written in the same transaction as the deposit. Without
`durable=True`, the handler runs inside the request, after the commit.
It is then lost if the process stops first. That is right for work you
can lose, such as refreshing a cache. It is wrong for work that must
happen.

## Step 2: deposit, then run the worker

```bash
python manage.py runserver
```

From the second shell, deposit 25 dollars:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "25.00", "currency": "USD"}}'
```

The balance is now `125.00` dollars. Stop the server, and look at the
outbox. The deposit's message waits there:

```bash
python manage.py messages list pending
```

Each line is one message: its id, the handler it runs
(`event domains.accounts.controllers.log_deposit`), its queue, how many
times it has run out of how many it may (`x0/10`), and when it is due.

Run the worker once. With `--once`, it runs every message that is due,
and then it stops. In production, `python manage.py runworker` runs
beside the web processes and never stops
([the worker](../operations.md#the-worker)).

```bash
python manage.py runworker --once
python manage.py messages list pending
```

The log shows `Deposited 25.00 USD into account <the id>`. The second
`messages list pending` prints nothing: the outbox is empty.

A handler that fails runs again later. The first delay is 2 seconds, and
it doubles each time, up to 1 hour. After its last try (the 10th, by
default), the message becomes a
[dead letter](../learning.md#dead-letter). `messages list dead` shows
the dead letters, and `messages retry` queues them again.

A message may run more than once. For example, the worker can stop after
the handler ran but before it recorded the result. So write handlers
that do no harm when they run a second time.

## What you learned

- A [domain event](../learning.md#domain-event) is a fact that an
  aggregate records. Handlers react to it after the commit.
- `@handles(Event, durable=True)` stores the reaction in the outbox, in
  the same transaction as the change.
- The worker (`runworker`) runs the outbox's messages, and runs a failed
  one again.
- `messages list` and `messages retry` show and act on the outbox.
- A handler may run twice, so it must do no harm the second time.

Queues, schedules, retries, and other brokers such as RabbitMQ are in
[messaging](../contrib/messaging.md#messaging-outbox-durable-handlers-background-tasks).
Next, [part 5](5-testing-and-deploying.md) tests the rules, and gets the
project ready for production.
