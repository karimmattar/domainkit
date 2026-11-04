# domainkit

domainkit is a Python framework for REST services. It is built on
FastAPI, and it brings Django's conventions: settings, installed
domains, code generators, migrations, users and permissions, and a
pytest plugin. Around them, it gives you the tools of domain-driven
design: aggregates that hold the business rules, use cases that run in
one transaction, and events that other code reacts to after the commit.

## Who it is for

domainkit is for teams that build services where the business rules
matter, and where a mistake costs money or trust: banks and payments,
shops, bookings, and internal systems. It suits you if:

- you want the rules in one place, so that the API, a script, and a test
  all meet the same rules;
- you want safe defaults: every route says who may call it, money is
  never a float, and production refuses to start with unsafe settings;
- you know Django or FastAPI, and you want their habits in a service
  with a clear structure.

## A first look

An [aggregate](learning.md#aggregate) is a plain Python class. Its
methods hold the business rules:

```python
from typing import NewType
from uuid import UUID, uuid4

from domainkit.ddd import AggregateRoot, BusinessRuleViolation
from domainkit.money import Money

AccountId = NewType("AccountId", UUID)


class Account(AggregateRoot[AccountId]):
    name: str
    balance: Money = Money.zero("USD")

    def deposit(self, amount: Money) -> None:
        if amount.amount <= 0:
            raise BusinessRuleViolation("A deposit must be positive.")
        self.balance += amount


account = Account(id=AccountId(uuid4()), name="Savings")
account.deposit(Money.of("10.50", "USD"))
print(account.balance)
```

```text
10.50 USD
```

A deposit of `-1.00` raises
`BusinessRuleViolation: A deposit must be positive.` Over HTTP, the
client gets a `422` answer with the code `business_rule_violation`.

A [use case](learning.md#use-case) loads the aggregate, calls it, and
commits, in one [unit of work](learning.md#unit-of-work). One line makes
it a route:

```python
from uuid import UUID

from domainkit.ddd import Repository, UnitOfWork, use_case
from domainkit.resources import ModelActions, ResourceRouter


class Bank(UnitOfWork):
    accounts: Repository[Account, AccountId]


@use_case
async def deposit(uow: Bank, account_id: UUID, amount: Money) -> Account:
    """Deposit money into an account."""
    account = await uow.accounts.get(AccountId(account_id))
    account.deposit(amount)
    return account


accounts = ResourceRouter(
    "/accounts",
    model=Account,
    unit_of_work=get_unit_of_work,
    repository=lambda uow: uow.accounts,
    rule=ModelActions(Account),
)
accounts.use_case(deposit, path="/deposit")  # POST /accounts/{account_id}/deposit
```

`get_unit_of_work` returns the unit of work on the database; the
generator `startdomain` writes it for you. The route checks the
`accounts.change_account` permission, reads the body
`{"amount": {"amount": "10.50", "currency": "USD"}}`, runs the use case,
and answers the account. [The tutorial](tutorial.md) builds this service
step by step.

## Install

```bash
pip install "domainkit[postgres,redis,sqlite,testing]"
domainkit init bankAPI bank
```

You need Python 3.13 or 3.14. [Part 0 of the tutorial](tutorial/0-first-route.md)
explains each extra and what `init` makes.

## Where to go next

Choose the line that matches your goal.

### Learn domainkit

- [Getting started](tutorial.md): six short parts, from `pip install` to
  a tested service with rules, permissions, and a worker.
- [Learning domainkit](learning.md): the order in which to read the
  guides, [the glossary](learning.md#words), and the daily commands.
- [Coming from Django](from-django.md) and
  [coming from FastAPI](from-fastapi.md): what you know, and its name
  in domainkit.

### Build a service

The guides explain one part of domainkit each, with every option.

| Area | Guides |
| --- | --- |
| The domain and its API | [Resources](resources.md), [validation](validation.md), [the request flow](request-flow.md), [money](money.md) |
| Data | [Queries](queries.md), [databases](databases.md), [multi-tenancy](tenancy.md), [search and places](search.md) |
| Users and permissions | [Users and permissions](auth.md), and its pages on [routes](auth/routes.md), [logins](auth/logging-in.md), [step-up](auth/step-up.md), [identity providers](auth/identity.md), and [passkeys](auth/devices.md) |
| Optional apps | [Optional apps](contrib.md): messaging, webhooks, audit, idempotency, evidence, keys, flags, accounting, approvals, and limits |
| The edges of a service | [Caches](cache.md), [live connections](live.md), [files and uploads](uploads.md), [mail and notifications](mail.md), [languages](i18n.md), [privacy requests](privacy.md) |
| Quality and speed | [Testing](testing.md), [how it runs async work](runtime.md), [performance](performance.md) |

The [how-to recipes](recipes.md) each solve one task, from start to
finish.

### Run a service

- [Deployment checklist](deploying.md): from a project whose checks pass
  to one in production.
- [Running domainkit in production](operations.md) and the
  [runbook](runbook.md) for when an alarm fires.
- [Hardening a deployment](security.md), the
  [threat model](threat-model.md), and
  [a bank's first review](fintech.md).
- [Upgrading](upgrading.md): what each new version asks of a project.

### Look something up

- [Reference](reference.md): every setting, check, command, middleware,
  and problem code.
- [Public API](api.md): every public name, by module.
- [Versions and compatibility](versioning.md): what a release may
  change, and how a name is deprecated.
- [Decisions](adr/README.md): why domainkit is designed the way it is.

## About these pages

These pages describe domainkit 1.x. The pages of each release are under
its tag in the repository (`docs/` at `vX.Y.Z`). The pages on `main`
describe the next release. [Upgrading](upgrading.md) and the
[changelog](../CHANGELOG.md) say what changes between versions, and
[the style guide](style-guide.md) says how these pages are written.

The source, the issues, and the releases are on
[GitHub](https://github.com/karimmattar/domainkit).
