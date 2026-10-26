# Part 5: tests, and getting ready for production

The rules of part 2 are only as safe as the tests that hold them. This
part tests them at the two levels a domain has, then checks the project
the way production will start it.

## Test the aggregate

The aggregate is plain Python, so its tests need no database and no
server: they make an account and call it. Add these to
`tests/domains/accounts/test_model.py`, which already has a `make()`
helper that returns a new account:

```python title="tests/domains/accounts/test_model.py"
from decimal import Decimal

from domains.accounts.domain.model import InvalidAmount


def test_deposits_add_to_the_balance() -> None:
    """Each deposit adds its amount, exactly: no float creeps in."""
    account = make()

    account.deposit(amount=Decimal("0.10"))
    account.deposit(amount=Decimal("0.20"))

    assert account.balance == Decimal("0.30")


@pytest.mark.parametrize("amount", [Decimal(0), Decimal("-0.01")])
def test_deposits_must_be_positive(amount: Decimal) -> None:
    """Nothing, or less, is refused; the balance stays, and nothing is recorded."""
    account = make()

    with pytest.raises(InvalidAmount):
        account.deposit(amount=amount)

    assert account.balance == 0
    assert account.pull_events() == []
```

## Test the API

The API tests go through the whole stack, routes to database, with the
test client and the fixtures of domainkit's pytest plugin: `admin_client`
is logged in as a superuser, and each test gets a fresh database. Add this
one to `tests/domains/accounts/test_deposit_account.py`:

```python title="tests/domains/accounts/test_deposit_account.py"
def test_an_archived_account_refuses_deposits(admin_client: TestClient) -> None:
    """``422 account_is_archived``, and the balance stays."""
    created = admin_client.post(URL, json=new_account_request())
    item = created.headers["location"]
    admin_client.post(f"{item}/archive")

    answer = admin_client.post(f"{item}/deposit", json={"amount": "1.00"})

    assert answer.status_code == 422
    assert answer.json()["code"] == "account_is_archived"
    assert admin_client.get(item).json()["balance"] == "0.00"
```

Run them all:

```bash
pytest
```

The plugin's other fixtures (a client for any user, frozen time, counted
queries, factories, the outbox's messages) are in
[testing.md](../testing.md).

## Check it as production starts it

`config.settings.production` reads its addresses and secrets from the
environment, and refuses to start without them, or with settings that are
unsafe there: `DEBUG`, wildcard hosts, a weak key, a database without
verified TLS. Give it what it needs, with the key in a file outside the
project, as a secret store would mount it, and run the same checks
startup runs:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))" > ../bank-secret-key
BANK_API_SETTINGS_MODULE=config.settings.production BANK_API_ALLOWED_HOSTS=bank.example.com \
  BANK_API_SECRET_KEY_FILE=../bank-secret-key BANK_API_TRUSTED_PROXIES=10.0.0.0/8 \
  BANK_API_DATABASE_URL=postgresql+asyncpg://bank@db.example.com/bank \
  python manage.py check
```

It passes, with one warning worth reading: the rate limits, login attempts
included, count in each process, so with several processes a password can
be guessed that many times faster. Production wants Redis for them, as the
warning says.

What a deployment adds is in [operations.md](../operations.md): the server
command and its limits, `migrate` before each rollout, `runworker` as a
second process, health checks, metrics, and backups.
[security.md](../security.md) is the checklist to go through before the
first real user.

## Where next

You have a domain with rules, a resource guarded by permissions, a durable
reaction, and the tests that hold them. From here:

- [resources.md](../resources.md): more aggregates, queries, filters, and
  the other generators (`startaggregate`, `addquery`, `addevent`).
- [queries.md](../queries.md): filters, pages, row locks, and savepoints;
  a transfer between two accounts needs the locks.
- [auth.md](../auth.md): tokens, sessions, two-factor logins, and route
  rules of your own.
- [contrib.md](../contrib.md): the audit trail, idempotent requests,
  webhooks, and feature flags.
- [The API reference](../api.md): every public name.
