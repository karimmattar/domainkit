# Work that commits once per message

This recipe makes a background task commit its database work once per
message, even when the worker runs the message twice.

## The problem

A worker runs each message at least once. Think of a worker that dies
after the task's work committed, but before the worker recorded the
message as done. The message then runs again. For a task that credits an
account, the account is credited twice.

You could make every task check for itself whether it ran before. With
`@inbox`, you do not have to: you mark the task (or a durable event
handler), and its database work commits once per message, however often
the message runs
([ADR 0094](../adr/0094-an-inbox-of-handled-messages-written-with-the-handlers-commit.md),
[messaging](../contrib/messaging.md#once-in-the-database-the-inbox)).

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## The solution

### Step 1: make an account to credit

Make a project with an `accounts` domain:

```bash
domainkit init ledgerAPI ledger
cd ledger
python manage.py startdomain accounts --aggregate Account --minimal
```

Give the account a balance, and a method that credits it:

```python title="src/domains/accounts/domain/model.py"
from decimal import Decimal

from domainkit.ddd import Digits


class Account:
    ...
    balance: Annotated[Decimal, Digits(12, 2)] = Decimal("0.00")

    def credit(self, amount: Decimal) -> None:
        """Add ``amount`` to the balance."""
        self.balance += amount
```

### Step 2: write the task, under the inbox

The task opens a [unit of work](../learning.md#words) on the database, so
it lives in the `infrastructure` layer. `@task` registers it with the
worker; `@inbox` makes its commit happen once per message:

```python title="src/domains/accounts/infrastructure/tasks.py (new file)"
"""Background work of the accounts domain, run by the worker."""

from decimal import Decimal
from uuid import UUID

from domainkit.contrib.messaging import inbox, task

from domains.accounts.domain.model import AccountId
from domains.accounts.infrastructure.unit_of_work import SqlAlchemyAccountsUnitOfWork


@task
@inbox
async def credit_account(account_id: UUID, amount: Decimal) -> None:
    """Credit the account once per message, however often the message runs."""
    async with SqlAlchemyAccountsUnitOfWork() as uow:
        account = await uow.accounts.get(AccountId(account_id))
        account.credit(amount)
        await uow.commit()  # Also writes (task, message id) to the inbox.
```

### Step 3: register the task

The worker finds a task by its name, once the task's module is imported.
Import it where the domain starts:

```python title="src/domains/accounts/apps.py"
import domains.accounts.infrastructure.tasks  # noqa: F401 - registers the tasks.
```

### Step 4: make the table

```bash
python manage.py makemigrations -m "create accounts"
python manage.py migrate
```

### Step 5: test a message that runs twice

The test makes the first run fail after its commit, as a worker that died
there would. Then it runs the message until it is done, and checks that
the account was credited once:

```python title="tests/domains/accounts/test_credit.py (new file)"
"""A credit runs twice and lands once."""

from decimal import Decimal

import pytest
from domainkit.contrib.messaging import enqueue
from domainkit.contrib.messaging.testing import Outbox

from domains.accounts.domain.model import Account
from domains.accounts.infrastructure.tasks import credit_account
from domains.accounts.infrastructure.unit_of_work import SqlAlchemyAccountsUnitOfWork

pytestmark = pytest.mark.anyio


async def test_a_credit_run_again_after_its_commit_lands_once(
    db: None, outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run commits, then fails; the next one is skipped."""
    async with SqlAlchemyAccountsUnitOfWork() as uow:
        account = Account.create("Savings")
        await uow.accounts.add(account)
        await uow.commit()
    commit = SqlAlchemyAccountsUnitOfWork.commit
    crashed: list[bool] = []

    async def commit_then_crash(self: SqlAlchemyAccountsUnitOfWork) -> None:
        await commit(self)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("The worker died before it recorded the run.")

    monkeypatch.setattr(SqlAlchemyAccountsUnitOfWork, "commit", commit_then_crash)
    await enqueue(credit_account, account_id=account.id, amount=Decimal("25.00"))

    runs = await outbox.arun_queued(delayed=True, raise_errors=False)

    async with SqlAlchemyAccountsUnitOfWork(readonly=True) as uow:
        assert (await uow.accounts.get(account.id)).balance == Decimal("25.00")
    assert (runs, crashed) == (2, [True])
```

Run the checks and the tests:

```bash
ruff check --fix .
ruff format --check .
pytest
```

Without `@inbox`, the second run would credit the account again: 50.00.

## How it works

1. The worker runs the task for a message.
2. The task's commit also writes the message's id, with the task's name,
   to the `domainkit_inbox` table, in the same transaction as the credit.
   Both rows commit together, or neither does.
3. When the message runs again, the worker finds that row, and does not
   call the task.

Keep these rules in mind:

- **Keep the work in one unit of work.** A run whose first commit landed
  is skipped whole, so a second commit in the same task would never run.
- **Work outside the database can still happen twice.** A mail or a call
  to another service is not in the transaction. Queue it with
  `uow.defer(...)` in the same commit, as a task of its own.
- **A task that is not marked keeps the old rule:** at least once. It
  must be idempotent on its own.

## See also

- [Messaging](../contrib/messaging.md): tasks, durable handlers, and the
  worker.
- [A payment that runs again when it loses a race](payments.md): run a
  whole transaction again.
- [Commands](../reference/commands.md#messages-see-and-act-on-the-outbox):
  see and retry the outbox's messages.
