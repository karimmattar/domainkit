# Part 5: tests, and getting ready for production

The rules of [part 2](2-rules.md) are only as safe as the tests that
hold them. In this part you test the rules at the two levels a domain
has: the aggregate alone, and the whole API. Then you check the project
the way production starts it.

## What you will build

- Tests of the account's rules, in plain Python, with no database.
- Tests of the API, through the whole stack, as a superuser and as the
  clerk of [part 3](3-permissions.md).
- A run of the production checks, with production's settings.

## Step 1: test the aggregate

The aggregate is plain Python. So its tests need no database and no
server: they make an account and call its methods. Put these tests in a
new file, `tests/domains/accounts/test_domain.py`:

```python title="tests/domains/accounts/test_domain.py (new file)"
"""The account's rules, in plain Python: no database, no server."""

import pytest
from domainkit.money import CurrencyMismatch, Money

from domains.accounts.domain import Account, InvalidAmount


def make() -> Account:
    """A new, open account with nothing in it."""
    return Account.create("Savings")


def test_deposits_add_to_the_balance() -> None:
    """Each deposit adds its amount, exactly: no float creeps in."""
    account = make()

    account.deposit(amount=Money.of("0.10", "USD"))
    account.deposit(amount=Money.of("0.20", "USD"))

    assert account.balance == Money.of("0.30", "USD")


@pytest.mark.parametrize("amount", ["0", "-0.01"])
def test_deposits_must_be_positive(amount: str) -> None:
    """Nothing, or less, is refused; the balance stays, and nothing is recorded."""
    account = make()

    with pytest.raises(InvalidAmount):
        account.deposit(amount=Money.of(amount, "USD"))

    assert account.balance == Money.zero("USD")
    assert account.pull_events() == []


def test_a_deposit_in_another_currency_is_refused() -> None:
    """Euros never add to dollars."""
    account = make()

    with pytest.raises(CurrencyMismatch):
        account.deposit(amount=Money.of("5.00", "EUR"))

    assert account.balance == Money.zero("USD")
```

Each test follows the same three steps: make an account, act on it, and
check the result. `pull_events()` returns the events the account
recorded, and forgets them. After a refused deposit it returns `[]`: a
refused change records nothing.

With floats, 0.10 plus 0.20 is not 0.30. With `Money`, it is, and the
first test proves it.

## Step 2: test the API

The API tests go through the whole stack, from the routes to the
database. They use the test client and the [fixtures](../learning.md#fixture)
of domainkit's pytest plugin. `admin_client` is logged in as a
superuser, and each test starts from a fresh database. Add this test to
`tests/domains/accounts/test_api.py`, which `startdomain` wrote. That
file already defines `URL` and imports `TestClient`:

```python title="tests/domains/accounts/test_api.py"
DOLLAR = {"amount": "1.00", "currency": "USD"}


def test_an_archived_account_refuses_deposits(admin_client: TestClient) -> None:
    """``422 account_is_archived``, and the balance stays."""
    created = admin_client.post(URL, json={"name": "Savings"})
    item = created.headers["location"]
    admin_client.post(f"{item}/archive")

    answer = admin_client.post(f"{item}/deposit", json={"amount": DOLLAR})

    assert answer.status_code == 422
    assert answer.json()["code"] == "account_is_archived"
    assert admin_client.get(item).json()["balance"] == {
        "amount": "0.00",
        "currency": "USD",
    }
```

A test can also make the user it needs, and act as that user. Here it is
the clerk of part 3, who may read accounts but not deposit:

- `create_user` stores a user with the permissions you name, as the
  application would.
- `authenticated_as` returns a client that is logged in as that user.
- `create_user` is async. A sync test runs it in the client's event
  loop with `client.run`. An async test awaits it.

```python title="tests/domains/accounts/test_api.py"
from domainkit.contrib.auth.testing import create_user


def test_a_clerk_reads_but_cannot_deposit(admin_client: TestClient) -> None:
    """``view_account`` alone: the account, but ``403`` for a deposit."""
    item = admin_client.post(URL, json={"name": "Savings"}).headers["location"]
    clerk = admin_client.run(create_user, "accounts.view_account")
    as_clerk = admin_client.authenticated_as(clerk)

    assert as_clerk.get(item).status_code == 200
    answer = as_clerk.post(f"{item}/deposit", json={"amount": DOLLAR})
    assert answer.status_code == 403
```

Run all the tests:

```bash
pytest
```

pytest prints `17 passed`: the 11 tests of part 2, and 6 new ones. The
test with `parametrize` counts twice, once for each amount.

The plugin has more fixtures: a client for any user, a frozen clock,
counted queries, factories, the outbox's messages, and settings changed
for one test. [Testing](../testing.md) explains each one.

## Step 3: check the project as production starts it

The settings module `config.settings.production` reads its addresses
and secrets from the environment. It refuses to start without them. It
also refuses settings that are unsafe in production: `DEBUG`, wildcard
hosts, a weak key, or a database without verified TLS.

Production counts requests, wrong passwords, and wrong codes in Redis,
once for all processes. If each process counted for itself, a person
who guesses passwords would get the login limit once for each process.
So production refuses counts kept in each process (check `auth.E105`).

Give the production settings what they need, and run the same checks
that startup runs. The secret key goes in a file outside the project,
the way a secret store would mount it:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))" > ../bank-secret-key
BANK_API_SETTINGS_MODULE=config.settings.production BANK_API_ALLOWED_HOSTS=bank.example.com \
  BANK_API_SECRET_KEY_FILE=../bank-secret-key BANK_API_TRUSTED_PROXIES=10.0.0.0/8 \
  BANK_API_DATABASE_URL=postgresql+asyncpg://bank@db.example.com/bank \
  BANK_API_REDIS_URL=rediss://:cache-password@cache.example.com:6380/0 \
  python manage.py check
```

The check passes, and prints
`System check identified no issues and 2 warnings.` Each finding has an
id, and each one is worth reading:

- `security.I001` is a note: the application serves plain HTTP, so TLS
  must end at the proxy in front of it.
- `deploy.W004` and `deploy.W006` are warnings. The database pool is
  SQLAlchemy's default: 15 connections (5, plus 10 more when needed).
  But each process accepts up to 1,000 requests at once. Set
  `POOL_SIZE` for your database server and your number of processes.
  Set `MAX_CONCURRENT_REQUESTS` for what one process can serve
  ([sizing](../operations.md#sizing)).

When your team has read a warning and accepts it, you can add its id to
`SILENCED_CHECKS`. You cannot silence an error.

A deployment adds more, which [operations](../operations.md) explains:
the server command and its limits, `migrate` before each release,
`runworker` as a second process, health checks, metrics, and backups.
[Hardening a deployment](../security.md) is the checklist to go through
before the first real user.

## What you learned

- Aggregate tests are plain Python: fast, and with no database.
- API tests use the plugin's fixtures: `admin_client`, `client`, and a
  client for any user with `authenticated_as`.
- `create_user` makes a user with the permissions a test needs.
- `config.settings.production` reads secrets from the environment, and
  `check` with those settings shows what production would refuse.
- Each check has an id. You may silence a warning, but never an error.

## Where next

You have a domain with rules, a resource guarded by permissions, a
durable reaction, and the tests that hold them. From here:

- [Resources](../resources.md): more aggregates, queries, filters, and
  the other generators (`startaggregate`, `addquery`, `addevent`).
- [Queries](../queries.md): filters, pages, row locks, and savepoints. A
  transfer between two accounts needs the locks.
- [Users and permissions](../auth.md): tokens, sessions, two-factor
  logins, and access rules of your own.
- [Optional apps](../contrib.md): the audit trail, safe retries of
  requests, webhooks, and feature flags.
- [Deploying](../deploying.md): from a project whose checks pass to one
  that serves in production.
- [The public API](../api.md): every public name.
