# Reading from a replica

A read replica takes reads off the primary. A unit of work made with
`readonly=True` reads from a replica of its database, when the database
names one, and refuses to commit; every write still goes to the primary
([ADR 0024](../adr/0024-replicas-and-derived-tables.md),
[databases.md](../databases.md#read-replicas)).

You need a project with `domainkit[postgres,sqlite,testing]` installed.
Every step here is run as written by `domainkit_tests/test_recipes.py`.

## A resource whose reads may go to a replica

```bash
domainkit init shopAPI shop
cd shop
python manage.py startdomain orders --aggregate Order --minimal
python manage.py makemigrations -m "create orders"
python manage.py migrate
```

The resource's unit of work is a dependency: give `GET` and `HEAD` a
read-only one, and the commands a writable one.

```python title="src/domains/orders/api/routes.py"
from fastapi import Request


async def get_unit_of_work(request: Request) -> SqlAlchemyOrdersUnitOfWork:
    """Reads on a replica, when the database names one; changes on the primary."""
    return SqlAlchemyOrdersUnitOfWork(readonly=request.method in {"GET", "HEAD"})
```

A replica may be behind the primary. A command answers with the aggregate
its own unit of work wrote, so a client that creates an order sees it at
once; a list read right after may not show it yet. A read that must see a
write, such as a check before a change, belongs in the writable unit of
work.

## The replica in production

The production database names its replica, an alias of its own with its
own pool, TLS, and readiness check:

```python title="src/config/settings/production.py"
DATABASES["default"]["REPLICAS"] = ["replica"]
DATABASES["replica"] = {
    "URL": env.str("DATABASE_REPLICA_URL"),
    "SSLMODE": "verify-full",
}
```

A replica is never migrated, nor named as a domain's database: replication
keeps it migrated. `check` reads the settings as a deployment sets them:

```bash
SHOP_API_SETTINGS_MODULE=config.settings.production SHOP_API_ALLOWED_HOSTS=api.example.com SHOP_API_DATABASE_URL=postgresql+asyncpg://app@primary/shop SHOP_API_DATABASE_REPLICA_URL=postgresql+asyncpg://app@replica/shop SHOP_API_REDIS_URL=rediss://:s3cret@cache:6380/0 SHOP_API_SECRET_KEY=a-production-secret-of-fifty-random-characters-or-more SHOP_API_TRUSTED_PROXIES=10.0.0.0/8 python manage.py check
```

The test settings name no replica: two SQLite databases do not
replicate, so the tests read the primary.

## The tests

```python title="tests/domains/orders/test_reads.py (new file)"
"""Reads take a read-only unit of work; it never writes."""

import pytest
from domainkit.ddd import ReadOnlyError
from domainkit.testing import TestClient

from domains.orders.domain.model import Order
from domains.orders.infrastructure.unit_of_work import SqlAlchemyOrdersUnitOfWork

URL = "/api/v1/orders/orders"


def test_listings_read_what_commands_wrote(admin_client: TestClient) -> None:
    """The order is created on the primary, and listed in a read-only unit."""
    created = admin_client.post(URL, json={"name": "First"})

    listed = admin_client.get(URL).json()["items"]

    assert created.status_code == 201
    assert [item["name"] for item in listed] == ["First"]


@pytest.mark.anyio
async def test_a_read_only_unit_of_work_refuses_to_commit(db: None) -> None:
    """Writes go to the primary, through a writable unit of work."""
    with pytest.raises(ReadOnlyError):
        async with SqlAlchemyOrdersUnitOfWork(readonly=True) as uow:
            await uow.orders.add(Order.create("Second"))
            await uow.commit()
```

```bash
ruff check --fix .
ruff format --check .
pytest
```

A unit of work that always reads, such as one behind reports, says so on
its class: `readonly = True`. `reads=Reads.SNAPSHOT` reads one snapshot,
for reads that must agree with each other ([queries.md](../queries.md#read-only-work)).
