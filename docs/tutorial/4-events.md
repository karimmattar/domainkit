# Part 4: events and the outbox

A deposit records the event `AccountDeposited` (part 2). This part reacts
to it outside the request: the reaction is stored with the deposit, in the
same transaction, and a worker runs it afterwards. If the deposit rolls
back, the reaction is never stored; if the worker crashes, it runs again.

## Write a handler

Handlers live in `application/handlers.py`. Add one that records each
deposit in the log (a real one would update a statement, or tell another
service):

```python title="src/domains/accounts/application/handlers.py"
from domains.accounts.domain.events import AccountDeposited


async def log_deposit(event: AccountDeposited) -> None:
    """Log each deposit; the worker may run it twice, which is harmless."""
    logger.info("Deposited %s into account %s", event.amount, event.account_id)
```

Subscribe it in `apps.py`, where the domain connects its layers once
settings are loaded. Replace `ready` with this one:

```python title="src/domains/accounts/apps.py"
from domains.accounts.application.handlers import log_deposit
from domains.accounts.domain.events import AccountDeposited


class AccountsConfig(DomainConfig):
    ...

    def ready(self) -> None:
        """Connect the layers once settings are loaded."""
        start_mappers()
        # Durable: stored with the commit, run (and retried) by the worker.
        event_bus.subscribe(AccountCreated, log_account_created, durable=True)
        event_bus.subscribe(AccountDeposited, log_deposit, durable=True)
```

`durable=True` is what puts the reaction in the outbox. Without it, the
handler runs in the request, after the commit, and is lost if the process
stops first: right for refreshing a cache, wrong for anything that must
happen.

## Deposit, then run the worker

```bash
python manage.py runserver
```

From the second shell:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": "25.00"}'
```

Stop the server, and look at the outbox. The deposit's message waits
there, beside those of the accounts opened so far:

```bash
python manage.py messages --pending
```

Run the worker once: it runs every message that is due, then exits. In
production, `python manage.py runworker` runs beside the web processes
and never exits ([operations.md](../operations.md#the-worker)).

```bash
python manage.py runworker --once
python manage.py messages --pending
```

The log shows `Deposited 25.00 into account ...`, and the outbox is empty.
A handler that fails is retried with a growing delay, then kept as a dead
letter for `messages --dead` and `messages --retry`. Since a message may
run more than once (the worker can crash after the handler, before it
records the outcome), write handlers that do no harm the second time.

Queues, schedules, retries, and RabbitMQ are in
[contrib.md](../contrib.md#messaging-outbox-durable-handlers-background-tasks).
[Part 5](5-testing-and-deploying.md) tests the rules and gets the project
ready for production.
