# Work that commits once per message

A worker runs each message at least once: a worker that dies after the
work committed, but before it recorded the message as done, leaves the
message to run again. Mark the task (or a durable event handler) `@inbox`
and its database work commits once per message, however often the
message runs
([ADR 0094](../adr/0094-an-inbox-of-handled-messages-written-with-the-handlers-commit.md),
[messaging](../contrib/messaging.md#once-in-the-database-the-inbox)).

You need a project with `domainkit[sqlite,testing]` installed. Every step
here is run as written by `domainkit_tests/test_recipes.py`.

## An account to credit

```bash
domainkit init ledgerAPI ledger
cd ledger
python manage.py startdomain accounts --aggregate Account --minimal
```

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

## The task, under the inbox

The task opens a unit of work on the database, so it lives in the
`infrastructure` layer:

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

The commit writes the message's id to the `domainkit_inbox` table in the
same transaction as the credit. When the message runs again, the worker
finds the row and does not call the task. Keep the work in one unit of
work: a run whose first commit landed is skipped whole. What the task
does outside the database, a mail or a call to another service, can
still happen twice; queue it with `uow.defer(...)` in the same commit,
as a task of its own.

The worker finds a task by its name once its module is imported:

```python title="src/domains/accounts/apps.py"
import domains.accounts.infrastructure.tasks  # noqa: F401 - registers the tasks.
```

## The table, and a test that runs a message twice

```bash
python manage.py makemigrations -m "create accounts"
python manage.py migrate
```

The test makes the first run fail after its commit, as a worker that died
there would, and runs the message until it is done:

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

```bash
ruff check --fix .
ruff format --check .
pytest
```

Without `@inbox` the second run would credit the account again: 50.00.
A task that is not marked keeps the old rule, at least once, and must be
idempotent on its own.
