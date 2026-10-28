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

A test can also make the user it needs, and sign in as them: here the
clerk of part 3, who may read accounts but not deposit. `create_user`
stores a user granted permissions, as the application would, and
`authenticated_as` returns a client that signs in as that user. A sync
test runs the async `create_user` in the client's event loop with
`client.run`; an async one awaits it:

```python title="tests/domains/accounts/test_deposit_account.py"
from domainkit.contrib.auth.testing import create_user


def test_a_clerk_reads_but_cannot_deposit(admin_client: TestClient) -> None:
    """``view_account`` alone: the account, but ``403`` for a deposit."""
    item = admin_client.post(URL, json=new_account_request()).headers["location"]
    clerk = admin_client.run(create_user, "accounts.view_account")
    as_clerk = admin_client.authenticated_as(clerk)

    assert as_clerk.get(item).status_code == 200
    answer = as_clerk.post(f"{item}/deposit", json={"amount": "1.00"})
    assert answer.status_code == 403
```

Run them all:

```bash
pytest
```

The plugin's other fixtures (a client for any user, frozen time, counted
queries, factories, the outbox's messages, settings changed for one test)
are in [testing.md](../testing.md).

## Check it as production starts it

`config.settings.production` reads its addresses and secrets from the
environment, and refuses to start without them, or with settings that are
unsafe there: `DEBUG`, wildcard hosts, a weak key, a database without
verified TLS. It counts requests, and wrong passwords and codes, in Redis,
once for every process: counted in each process, a guesser would get the
login rate once per process, which production refuses (`auth.E105`). Give
it what it needs, with the key in a file outside the project, as a secret
store would mount it, and run the same checks startup runs:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))" > ../bank-secret-key
BANK_API_SETTINGS_MODULE=config.settings.production BANK_API_ALLOWED_HOSTS=bank.example.com \
  BANK_API_SECRET_KEY_FILE=../bank-secret-key BANK_API_TRUSTED_PROXIES=10.0.0.0/8 \
  BANK_API_DATABASE_URL=postgresql+asyncpg://bank@db.example.com/bank \
  BANK_API_REDIS_URL=rediss://:cache-password@cache.example.com:6380/0 \
  python manage.py check
```

It passes, with two warnings worth reading, each with its id:

- `deploy.W004` and `deploy.W006`: the database pool is SQLAlchemy's
  default, 15 connections, while each process admits up to 1,000 requests
  at once. Size `POOL_SIZE` for the database server and the number of
  processes, and `MAX_CONCURRENT_REQUESTS` for what one process can serve
  ([operations.md](../operations.md#sizing)).

A warning a project has weighed and accepted can go in `SILENCED_CHECKS`;
an error cannot.

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
