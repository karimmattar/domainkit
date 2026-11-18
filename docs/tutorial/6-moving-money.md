# Part 6: money that moves

Until now, money only came into the bank's accounts. In this part it
leaves: a transfer sends money from an account to an account at another
bank. This is the first of three parts that add a bank's own controls to
the project of [part 5](5-testing-and-deploying.md). Work in the `bank`
directory, with the same environment turned on.

## The problem

A transfer looks like a deposit the other way round. But four things
go wrong with money that leaves, and a bank must stop each one:

- **A retry sends the money twice.** The client sends a transfer, and
  the network drops the answer. The client cannot tell whether the
  transfer ran, so it sends it again. Without care, the money leaves
  twice.
- **Two transfers at once overdraw the account.** Two transfers from one
  account arrive together. Each reads the balance, each finds enough
  money, and each takes it. The account pays out more than it holds.
- **A stolen token empties the account.** Whoever holds a clerk's token
  can send the whole balance away in a minute. A daily limit caps what
  one account can lose in a day.
- **A number on a row is not a record.** The balance is a column that
  any change can set. A bank keeps double-entry books: each movement is
  an entry whose debits equal its credits, entries never change, and the
  books show where every cent came from and went.

## What you will build

- `POST /api/v1/accounts/accounts/{id}/transfer`, which sends an amount
  to a payee, and which a retry with the same `Idempotency-Key` does not
  run twice.
- A transfer that locks its account's row, so two at once never both
  take the same money.
- A limit of 2,000 dollars a day out of each account.
- The bank's books, which book every deposit and transfer through the
  outbox, once each.
- Tests of a retry, of two transfers at once, and of the limit.

## Step 1: install the apps

Three apps of domainkit do the hard parts:

- `domainkit.contrib.idempotency` stores the answer of each request sent
  with an `Idempotency-Key`, and replays it to a retry
  ([idempotency](../contrib/idempotency.md)).
- `domainkit.contrib.accounting` keeps double-entry books
  ([accounting](../contrib/accounting.md)).
- `domainkit.contrib.limits` counts what moves against limits, in the
  transaction of the movement ([limits](../contrib/limits.md)).

Add them, the idempotency middleware, and their settings at the end of
`src/config/settings/base.py`:

```python title="src/config/settings/base.py"
INSTALLED_DOMAINS = [
    *INSTALLED_DOMAINS,
    "domainkit.contrib.idempotency",
    "domainkit.contrib.accounting",
    "domainkit.contrib.limits",
]
# Last: it replays the stored answer to a retry with the same key.
MIDDLEWARE = [
    *MIDDLEWARE,
    "domainkit.contrib.idempotency.middleware.IdempotencyMiddleware",
]
# The books take entries on any date: this tutorial opens no accounting period.
ACCOUNTING = {"REQUIRE_PERIOD": False}
# At most 2,000 dollars a day out of each account.
LIMITS = {
    "DEFAULTS": [
        {
            "code": "accounts.daily",
            "kind": "bank_account",
            "currency": "USD",
            "window": "day",
            "max_amount": "2000.00",
        },
    ],
}
```

A limit bounds a kind of subject. `bank_account` is a kind of the
project's own: the transfer names the account it counts against. The
amount is text, never a float.

The books' entries can be read over HTTP. Add these lines at the end of
`src/config/urls.py`:

```python title="src/config/urls.py"
from domainkit.contrib.accounting.api import entries as entries_router

router.include_router(entries_router, prefix="/api/accounting")
```

Make the apps' tables:

```bash
python manage.py makemigrations -m "books, limits, and idempotency keys"
python manage.py migrate
```

## Step 2: add a transfer, and its rule

Add the command with the generator, as in [part 2](2-rules.md):

```bash
python manage.py addcommand accounts Account TransferAccount --field payee:str:34 --field amount:money
```

`payee` is the account number at the other bank, such as an IBAN: text
of at most 34 characters. Now write the rule. In
`src/domains/accounts/domain.py`, give the event the transfer's id,
replace the `transfer` method, and add the refusal `InsufficientFunds`:

```python title="src/domains/accounts/domain.py"
class AccountTransferred(DomainEvent):
    """Money left an account, for a payee at another bank."""

    account_id: AccountId
    transfer_id: UUID
    payee: str
    amount: Money


class Account(AggregateRoot[AccountId]):
    ...

    def transfer(self, *, payee: str, amount: Money) -> UUID:
        """Send a positive ``amount`` to ``payee`` from an open account; its id."""
        self._ensure_open()
        if amount.amount <= 0:
            raise InvalidAmount("A transfer must be positive.")
        if amount > self.balance:
            raise InsufficientFunds("The account holds less than the transfer.")
        self.balance -= amount
        transfer_id = uuid4()
        self.record(
            AccountTransferred(
                account_id=self.id, transfer_id=transfer_id, payee=payee, amount=amount
            )
        )
        return transfer_id


class InsufficientFunds(BusinessRuleViolation):
    """An account sends no more than it holds."""
```

The client gets `422 insufficient_funds` for a transfer larger than the
balance, as it gets `invalid_amount` for one of zero.

## Step 3: lock the row, and count the limit

The use case that `addcommand` wrote loads the account with `get`. Two
transfers at once would each load the same balance. In
`src/domains/accounts/controllers.py`, replace `transfer_account` with
this one:

```python title="src/domains/accounts/controllers.py"
from domainkit.contrib.limits import Movement, consume


@use_case
async def transfer_account(
    uow: AccountsUnitOfWork, account_id: UUID, payee: str, amount: Money
) -> Account:
    """Send ``amount`` to ``payee``, within the account's daily limit."""
    account = await uow.accounts.get_for_update(AccountId(account_id))
    transfer_id = account.transfer(payee=payee, amount=amount)
    await consume(
        uow,
        Movement(amount, {"bank_account": str(account.id)}),
        key=f"accounts.transfer:{transfer_id}",
    )
    return account
```

- `get_for_update` locks the account's row until the transaction ends.
  On PostgreSQL, a second transfer waits for the first, then reads the
  balance the first left, and is refused with `422 insufficient_funds`.
  Every account also has a `version`, and a change made from an older
  one is refused: without the lock, that check alone would answer the
  second transfer `409 concurrent_update`, and its client would send it
  again. SQLite has no row locks, and runs one write at a time: there,
  the second transfer gets that `409`.
- `consume` counts the amount against the account's limits, in the same
  transaction. A transfer refused later rolls its count back with it.
  One past the limit is refused with `422 limit_exceeded`.

## Step 4: book every movement

The books follow the accounts. Each deposit and each transfer becomes
an entry, posted by a [durable handler](4-events.md), so the outbox
stores it with the change and the worker posts it after. Add these
lines at the end of `src/domains/accounts/controllers.py`:

```python title="src/domains/accounts/controllers.py"
from domainkit.contrib.accounting import AccountId as LedgerAccountId
from domainkit.contrib.accounting import (
    AccountCodeTaken,
    AccountingUnitOfWork,
    AccountType,
    Line,
    OpenAccount,
    PostEntry,
    open_account,
    post_entry,
)
from domainkit.contrib.messaging import ordered
from domainkit.ddd import F

from domains.accounts.domain import AccountTransferred

CHART = {
    "1000": ("Vault", AccountType.ASSET),
    "2000": ("Customer deposits", AccountType.LIABILITY),
}
"""The bank's books: the money it holds, and what it owes its customers."""


@handles(AccountDeposited, durable=True, name="accounts.book_deposit")
@ordered(lambda event: f"accounts.books:{event.account_id}")
async def book_deposit(event: AccountDeposited) -> None:
    """Book a deposit: cash into the vault, owed to the customers."""
    await book(event, debit="1000", credit="2000")


@handles(AccountTransferred, durable=True, name="accounts.book_transfer")
@ordered(lambda event: f"accounts.books:{event.account_id}")
async def book_transfer(event: AccountTransferred) -> None:
    """Book a transfer: no longer owed to the customers, paid out of the vault."""
    await book(event, debit="2000", credit="1000")


async def book(
    event: AccountDeposited | AccountTransferred, *, debit: str, credit: str
) -> None:
    """Post the event's amount from ``debit`` to ``credit``, once per event."""
    await post_entry(
        PostEntry(
            source="accounts",
            reference=str(event.event_id),
            booked_on=event.occurred_at.date(),
            lines=(
                Line.debit(await ledger_account(debit), event.amount),
                Line.credit(await ledger_account(credit), event.amount),
            ),
        ),
        AccountingUnitOfWork(),
    )


async def ledger_account(code: str) -> LedgerAccountId:
    """The id of the books' account ``code``, opened on its first use."""
    async with AccountingUnitOfWork() as books:
        found = await books.accounts.first(F("code") == code)
    if found is None:
        name, kind = CHART[code]
        try:
            found = await open_account(
                OpenAccount(code=code, name=name, type=kind, currency="USD"),
                AccountingUnitOfWork(),
            )
        except AccountCodeTaken:  # Another handler opened it first.
            return await ledger_account(code)
    return found.id
```

- An entry's debits equal its credits, or the books refuse it
  (`unbalanced_entry`). A deposit debits the vault, an asset, and
  credits the customers' deposits, a liability: the bank holds more
  cash, and owes its customers more. A transfer does the opposite.
- `source` and `reference` name the entry once. The worker may run a
  handler twice, and the second run returns the entry the first posted.
- `@ordered` gives the entries of one account an ordering key of their
  own, so the worker posts them one at a time, in the order the changes
  committed: a transfer is never booked before the deposits of its
  account that came before it. Other accounts' entries are posted beside
  them. One key for the whole bank would make every deposit and transfer
  wait for the one before it ([ordering keys](../contrib/messaging.md#one-at-a-time-per-key-ordering-keys)).
- Neither account of the books may go below zero. The books start
  empty: the 125 dollars deposited before this part are not in them,
  and new deposits are.

## Step 5: try it

```bash
python manage.py runserver
```

Work in the second shell, where `$TOKEN` and `$ID` are still set.
Deposit 3,000 dollars, then send 250 of them, with a key of your own.
Make a new random key, such as a UUID, for each new transfer:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "3000.00", "currency": "USD"}}'
curl -i http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/transfer -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 6f0e8a52-3c1d-4b7e-9a0f-2d5c8e1b7a40" -H "Content-Type: application/json" \
  -d '{"payee": "DE89370400440532013000", "amount": {"amount": "250.00", "currency": "USD"}}'
```

The transfer answers the account, with a balance of `2875.00` dollars.
Now send the same request again, as a client whose answer was lost
would:

```bash
curl -i http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/transfer -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 6f0e8a52-3c1d-4b7e-9a0f-2d5c8e1b7a40" -H "Content-Type: application/json" \
  -d '{"payee": "DE89370400440532013000", "amount": {"amount": "250.00", "currency": "USD"}}'
```

The answer is the first one, word for word, with the header
`idempotent-replayed: true`. The transfer did not run again: the balance
is still `2875.00`. The same key with another body is refused, and so
is a transfer past the daily limit, 250 and 1,900 dollars being more
than 2,000:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/transfer -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 6f0e8a52-3c1d-4b7e-9a0f-2d5c8e1b7a40" -H "Content-Type: application/json" \
  -d '{"payee": "DE89370400440532013000", "amount": {"amount": "260.00", "currency": "USD"}}'   # 422
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/transfer -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: 0b9d4c21-77e5-4f3a-8c6d-5e2a1f9b3c07" -H "Content-Type: application/json" \
  -d '{"payee": "DE89370400440532013000", "amount": {"amount": "1900.00", "currency": "USD"}}'   # 422
```

The first answer's code is `idempotency_key_reused`, and the second's is
`limit_exceeded`. A request sent without a key runs as before: a key
protects the requests that carry one. A route can refuse requests
without one, as the transfer route of `domainkit init --profile fintech`
does ([fintech](../fintech.md)).

Stop the server, and run the worker. It posts the entries of the deposit
and of the transfer:

```bash
python manage.py runworker --once
python manage.py accounting verify
```

`accounting verify` checks that every entry balances and that every
balance is the sum of its postings, and prints `The books balance.`
Serve the project again, and read the books:

```bash
python manage.py runserver
```

```bash
curl http://127.0.0.1:8000/api/accounting/entries -H "Authorization: Bearer $TOKEN"
```

Two entries: the deposit, 3,000.00 dollars debited to one account and
credited to the other, and the transfer, 250.00 dollars the other way.
Stop the server.

## Step 6: test it

`addcommand` wrote a test of the transfer on an in-memory unit of work.
The transfer now counts its limit in the database, so test it through
the API instead: replace the contents of
`tests/domains/accounts/test_transfer_account.py` with these.

```python title="tests/domains/accounts/test_transfer_account.py (whole file)"
"""Transfers: a retry runs once, two at once never overdraw, a day has a limit."""

import asyncio

import pytest
from domainkit.contrib.messaging.testing import Outbox
from domainkit.testing import AsyncTestClient, TestClient

pytestmark = pytest.mark.anyio
URL = "/api/v1/accounts/accounts"
PAYEE = "DE89370400440532013000"


def usd(amount: str) -> dict[str, str]:
    """``amount`` US dollars, as JSON."""
    return {"amount": amount, "currency": "USD"}


def test_a_retried_transfer_moves_the_money_once(
    admin_client: TestClient, outbox: Outbox
) -> None:
    """A retry with the same key gets the first answer; the books book it once."""
    item = admin_client.post(URL, json={"name": "Savings"}).headers["location"]
    admin_client.post(f"{item}/deposit", json={"amount": usd("500.00")})
    body = {"payee": PAYEE, "amount": usd("200.00")}
    key = {"Idempotency-Key": "4b1f9c7e-2a3d-4e5f-8a6b-7c8d9e0f1a2b"}

    first = admin_client.post(f"{item}/transfer", json=body, headers=key)
    again = admin_client.post(f"{item}/transfer", json=body, headers=key)
    outbox.run_queued()

    assert first.json()["balance"] == usd("300.00")
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    entries = admin_client.get("/api/accounting/entries").json()["items"]
    assert len(entries) == 2  # The deposit, and the transfer once.


async def test_two_transfers_at_once_never_overdraw(
    async_admin_client: AsyncTestClient,
) -> None:
    """Two transfers of 60 at once from 100: one is made, never both."""
    created = await async_admin_client.post(URL, json={"name": "Savings"})
    item = created.headers["location"]
    await async_admin_client.post(f"{item}/deposit", json={"amount": usd("100.00")})
    sixty = {"payee": PAYEE, "amount": usd("60.00")}

    answers = await asyncio.gather(
        async_admin_client.post(f"{item}/transfer", json=sixty),
        async_admin_client.post(f"{item}/transfer", json=sixty),
    )

    made, refused = sorted(answers, key=lambda answer: answer.status_code)
    assert made.status_code == 200
    assert refused.json()["code"] in {"concurrent_update", "insufficient_funds"}
    assert (await async_admin_client.get(item)).json()["balance"] == usd("40.00")


def test_an_account_sends_at_most_its_daily_limit(admin_client: TestClient) -> None:
    """2,000 dollars a day: the next cent is refused, and stays in the account."""
    item = admin_client.post(URL, json={"name": "Savings"}).headers["location"]
    admin_client.post(f"{item}/deposit", json={"amount": usd("5000.00")})
    thousand = {"payee": PAYEE, "amount": usd("1000.00")}

    sent = [admin_client.post(f"{item}/transfer", json=thousand) for _ in range(2)]
    refused = admin_client.post(
        f"{item}/transfer", json={"payee": PAYEE, "amount": usd("0.01")}
    )

    assert [answer.status_code for answer in sent] == [200, 200]
    assert (refused.status_code, refused.json()["code"]) == (422, "limit_exceeded")
    assert admin_client.get(item).json()["balance"] == usd("3000.00")
```

The first test also runs the outbox: the transfer's event books it,
once. The second sends two transfers at the same moment, through the
async client, and checks that only one was made: `pytestmark` runs the
file's async tests in an event loop. Run the checks and the tests:

```bash
ruff check --fix .
ruff format .
pytest
```

`ruff --fix` sorts the imports your edits added, and `ruff format` lays
out the lines they made long. pytest prints `20 passed`: the 17 tests of
part 5, and these 3.

## What you learned

- `IdempotencyMiddleware` replays the answer of a request sent again
  with the same `Idempotency-Key`. The same key with another request is
  refused.
- `get_for_update` locks a row for the transaction, and every aggregate's
  `version` refuses a change made from an older one. So two transfers at
  once never both take the same money.
- `consume` counts a movement against limits in the use case's own
  transaction, and refuses one past them.
- The books are double entry: each entry's debits equal its credits, and
  entries never change. Durable handlers post them once each, and an
  ordering key per account posts each account's in commit order.

Next, [part 7](7-four-eyes.md) makes a large transfer wait for a second
person.
