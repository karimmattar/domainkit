# Part 2: an aggregate with rules

[Part 1](1-first-resource.md) served accounts with the rules `startdomain`
wrote. This part adds a balance, a deposit, and the rules that guard
them, by hand. The rules live in the aggregate, so every way into the
account (the API, a script, a test) meets them.

## Add a command

The server from part 1 is stopped. Add the command with its generator:

```bash
python manage.py addcommand accounts Account DepositAccount --field amount:decimal
```

It wrote the `DepositAccount` command, a use case that loads the account
and calls `account.deposit(amount=...)`, the route `POST
/accounts/{id}/deposit`, the event `AccountDeposited`, and tests. The
method it put on the aggregate only records the event; the rules are
yours to write.

## Write the rules

Open `src/domains/accounts/domain/model.py`. Add the `Digits` import, a new
refusal beside `AccountIsArchived`, the `balance` field after the other
fields of `Account`, and replace its `deposit` method with this one:

```python title="src/domains/accounts/domain/model.py"
from domainkit.ddd import Digits


class InvalidAmount(BusinessRuleViolation):
    """Amounts of money must be positive."""


class Account(AggregateRoot[AccountId]):
    ...
    balance: Annotated[Decimal, Digits(12, 2)] = Decimal("0.00")

    def deposit(self, *, amount: Decimal) -> None:
        """Add a positive ``amount`` to the balance of an open account."""
        self._ensure_active()
        if amount <= 0:
            raise InvalidAmount("A deposit must be positive.")
        self.balance += amount
        self.record(AccountDeposited(account_id=self.id, amount=amount))
```

Three things hold here, and nothing outside the aggregate can skip them:

- `_ensure_active()` is the rule `startdomain` wrote: an archived account
  refuses every change.
- `InvalidAmount` is a `BusinessRuleViolation`. The HTTP layer answers each
  one `422`, with a code made from the class name: `invalid_amount`.
- `Digits(12, 2)` bounds the field: the table's column becomes
  `NUMERIC(12, 2)`, and the API refuses an amount with more digits before
  the use case runs. Money stays a `Decimal`, never a float.

## Migrate

The table follows the fields (`table_for` in `infrastructure/orm.py`
reads them), so a migration adds the column:

```bash
python manage.py makemigrations -m "account balance"
```

The accounts table has a row already, the one part 1 opened, and a column
that may not be empty needs a value for it. In the migration
`makemigrations` just wrote (`migrations/0002_..._account_balance.py`),
give the column a default for the rows that exist, then apply it:

```python title="migrations/0002_*_account_balance.py"
def upgrade() -> None:
    """Add the balance: zero for the accounts that exist."""
    with op.batch_alter_table("accounts_accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "balance",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
                server_default="0",
            )
        )
```

```bash
python manage.py migrate
pytest
```

The generated tests pass: the command's test deposits `1.50`, which the
rules allow. [Part 5](5-testing-and-deploying.md) adds tests of the
refusals.

## Try it

```bash
python manage.py runserver
```

From the second shell, where `$TOKEN` and `$ID` are still set, deposit
`100.00`. The answer is the account, with its new balance:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": "100.00"}'
```

A negative amount is refused by the aggregate, and a third decimal place
by the API, before the use case runs:

```bash
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": "-5.00"}'   # 422
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$ID/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": "1.005"}'   # 422
```

Archive a second account, and see the older rule hold for deposits too:

```bash
CLOSED=$(curl -s http://127.0.0.1:8000/api/v1/accounts/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Old savings"}' \
  | python -c "import json, sys; print(json.load(sys.stdin)['id'])")
curl -X POST http://127.0.0.1:8000/api/v1/accounts/accounts/$CLOSED/archive -H "Authorization: Bearer $TOKEN"
curl http://127.0.0.1:8000/api/v1/accounts/accounts/$CLOSED/deposit -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"amount": "10.00"}'   # 422
```

Stop the server. [Part 3](3-permissions.md) lets someone other than the
superuser in, with only the permissions they need.

More on what an aggregate may hold (child entities, references to other
aggregates, value objects) is in [resources.md](../resources.md), and on
migrations in [databases.md](../databases.md).
