# Part 2: an aggregate with rules

In [part 1](1-first-resource.md), the accounts kept the one rule that
`startdomain` wrote: a name must not be blank. In this part you add a
balance, a deposit, and a way to close an account, and you write their
rules by hand. The rules live in the [aggregate](../learning.md#aggregate).
So every way into an account (the API, a script, a test) meets the same
rules.

## What you will build

- Two new [commands](../learning.md#command): `DepositAccount` and
  `ArchiveAccount`, each with its route.
- A `balance` in exact money, and an `archived` flag.
- Two rules: a deposit must be positive, and an archived account takes
  no more changes.
- A migration that adds the new columns to the table.

## Step 1: add two commands

The server from part 1 is stopped. Add a deposit, and a way to close an
account, with the `addcommand` generator:

```bash
python manage.py addcommand accounts Account DepositAccount --field amount:money
python manage.py addcommand accounts Account ArchiveAccount
```

Each command wrote four things:

- a method on the aggregate that records an
  [event](../learning.md#domain-event): `AccountDeposited` or
  `AccountArchived`;
- a `@use_case` in `controllers.py` that loads the account and calls the
  method;
- a route in `routes.py`: `POST /accounts/{id}/deposit` or
  `POST /accounts/{id}/archive`;
- a test in `tests/domains/accounts/`.

`--field amount:money` gives the deposit one field, `amount`, of type
`Money`. The new methods only record their events. You write the rules.

## Step 2: write the rules

Open `src/domains/accounts/domain.py` and make four changes:

1. Add `Digits` to the names imported from `domainkit.ddd`. You can add
   it to the list at the top, or on a line of its own.
2. Add the two refusals, `InvalidAmount` and `AccountIsArchived`, beside
   `InvalidAccountName`.
3. Add the `balance` and `archived` fields after `name`.
4. Replace the `deposit` and `archive` methods with the ones below, and
   add `_ensure_open`.

```python title="src/domains/accounts/domain.py"
from domainkit.ddd import Digits


class InvalidAmount(BusinessRuleViolation):
    """Amounts of money must be positive."""


class AccountIsArchived(BusinessRuleViolation):
    """An archived account takes no more changes."""


class Account(AggregateRoot[AccountId]):
    ...
    balance: Annotated[Money, Digits(12, 2)] = Money.zero("USD")
    archived: bool = False

    def deposit(self, *, amount: Money) -> None:
        """Add a positive ``amount`` to the balance of an open account."""
        self._ensure_open()
        if amount.amount <= 0:
            raise InvalidAmount("A deposit must be positive.")
        self.balance += amount
        self.record(AccountDeposited(account_id=self.id, amount=amount))

    def archive(self) -> None:
        """Close the account: it takes no more deposits."""
        self._ensure_open()
        self.archived = True
        self.record(AccountArchived(account_id=self.id))

    def _ensure_open(self) -> None:
        """Refuse a change to an archived account."""
        if self.archived:
            raise AccountIsArchived("The account is archived.")
```

The `...` stands for the parts of the class that stay as they are.

These rules hold, and no code outside the aggregate can skip them:

- `_ensure_open()` refuses every change to an archived account. The
  client gets `422` with the code `account_is_archived`.
- `InvalidAmount` is a `BusinessRuleViolation`. The HTTP layer answers
  each one with `422`, and a code made from the class name:
  `invalid_amount`.
- `Money` holds an exact amount and its currency
  ([money](../money.md)). In JSON it is
  `{"amount": "100.00", "currency": "USD"}`, never a float. An amount
  with more decimal places than its currency has is refused, never
  rounded. Amounts in two currencies never add up: a deposit in euros to
  a balance in dollars gets `422 currency_mismatch`.
- `Digits(12, 2)` limits the field to 12 digits, 2 of them after the
  decimal point. The table keeps the money in two columns: `balance`
  (the amount) and `balance_currency`. The API refuses an amount with
  more digits before the use case runs.

## Step 3: migrate

The table follows the aggregate's fields: `table_for` in `models.py`
reads them. So a new migration adds the new columns:

```bash
python manage.py makemigrations -m "account balance"
```

`makemigrations` finds three new columns: `balance`, `balance_currency`,
and `archived`. It writes them to a new file,
`migrations/0002_<id>_account_balance.py`.

The accounts table already has a row: the account you opened in part 1.
A column that may not be empty needs a value for the rows that exist.
So give the three columns defaults: zero dollars, and not archived. In
the new migration file, replace the `upgrade()` function with this one.
Keep its `downgrade()` as it is:

```python title="migrations/0002_*_account_balance.py"
def upgrade() -> None:
    """Add the balance and the archived flag: zero dollars, open, for the rows that exist."""
    with op.batch_alter_table("accounts_accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "balance",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "balance_currency",
                sa.String(length=8),
                nullable=False,
                server_default="USD",
            )
        )
        batch_op.add_column(
            sa.Column(
                "archived", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
```

Then apply the migration, and run the tests:

```bash
python manage.py migrate
pytest
```

`migrate` prints `Running upgrade ... Account balance.`, and pytest
prints `11 passed`. The generated test of the deposit deposits 1.50
dollars, which the rules allow. In [part 5](5-testing-and-deploying.md)
you add tests of the refusals.

## Step 4: try it

```bash
python manage.py runserver
```

Work in the second shell, where `$TOKEN` and `$ID` are still set. In a
new shell, set them again with the login and `ID=` lines of
[part 1](1-first-resource.md#step-4-call-it). Deposit 100 dollars:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "100.00", "currency": "USD"}}'
```

The answer is the account, with its new balance and version:
`{"id":"...","version":2,"name":"Savings","balance":{"amount":"100.00","currency":"USD"},"archived":false}`.

Now send three deposits that the rules refuse: a negative amount, an
amount with a third decimal place, and an amount in euros:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "-5.00", "currency": "USD"}}'   # 422
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "1.005", "currency": "USD"}}'   # 422
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "5.00", "currency": "EUR"}}'   # 422
```

Each answer is `422`, and its `code` names the cause:

| Deposit | Refused by | `code` | `detail` |
| --- | --- | --- | --- |
| `-5.00 USD` | the aggregate | `invalid_amount` | `A deposit must be positive.` |
| `1.005 USD` | the API, before the use case runs | `validation_error`, with `inexact_amount` in its `errors` | `The request is invalid.` |
| `5.00 EUR` | `Money`, because the balance is in dollars | `currency_mismatch` | `The amounts are in different currencies.` |

Last, open a second account, archive it, and see `_ensure_open()` refuse
a deposit to it:

```bash
CLOSED=$(curl -s http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Old savings"}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['id'])")
curl -X POST http://127.0.0.1:8000/api/v1/accounts/accounts/$CLOSED/archive -H "Authorization: Bearer $TOKEN"
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$CLOSED/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": {"amount": "10.00", "currency": "USD"}}'   # 422
```

The archive answers the account with `"archived":true`. The deposit
answers `422`, with the code `account_is_archived` and the detail
`The account is archived.`

Stop the server.

## What you learned

- `addcommand` adds a command to a domain: a method, a use case, a
  route, and a test.
- Business rules go in the aggregate's methods. A rule refuses with a
  `BusinessRuleViolation` subclass, and the client gets `422` and a code
  made from the class name.
- `Money` is exact: it is never a float, it is never rounded without
  asking, and two currencies never add up.
- Markers such as `Digits(12, 2)` limit a field in the object, in the
  request, and in the table.
- After you change fields, `makemigrations` writes the migration. You
  give new columns defaults when the table already has rows.

Next, [part 3](3-permissions.md) lets someone other than the superuser
in, with only the permissions they need.

To learn more about what an aggregate may hold (child entities,
references to other aggregates, value objects, and field rules), read
[resources](../resources.md) and [validation](../validation.md). For
migrations, read [databases](../databases.md).
