# Testing

`domainkit.testing` is a pytest plugin, like pytest-django. It selects the
test settings, makes and migrates the test databases, puts them back before
each test, and serves the application to sync and async tests. It also
brings factories for aggregates, a clock tests can freeze, and query-count
assertions.

## Setting it up

Install the `testing` extra, which brings pytest and httpx2, as a
development dependency, and enable the plugin in `pyproject.toml`. Projects
made by `domainkit init` already do both:

```toml
[dependency-groups]
dev = ["domainkit[testing]"]

[tool.pytest.ini_options]
addopts = "-p domainkit.testing"
domainkit_settings = "config.settings.test"
```

The `.env` file is never read during tests. The test settings must say
`ENVIRONMENT = "test"`: the database fixtures empty every database in
`DATABASES`, so they refuse any other settings, and a missing
`domainkit_settings` cannot empty the development database.

## Fixtures

| Fixture | Scope | Gives |
| --- | --- | --- |
| `database` | session | The test databases, made and migrated (see below). Gives the default database's URL. |
| `db` | test | Those databases put back as `migrate` left them. Request it in tests that reach the database without a client. |
| `client` | test | A `TestClient` for the project's application, with its lifespan, on the `db` databases. It sends no credentials. |
| `client_for` | test | `client_for("billing.view_invoice", ...)`: a client of the same application that authenticates as a new user granted those permissions. Keyword arguments set the user's fields (`is_staff=True`, `username="carol"`); the username is unique otherwise. Needs `domainkit.contrib.auth`. |
| `admin_client` | test | A client that authenticates as a new superuser. |
| `async_client`, `async_client_for`, `async_admin_client` | test | The same, as `AsyncTestClient`s, for async tests; `await async_client_for(...)`. |
| `freeze_time` | test | `clock = freeze_time("2026-01-01")`: the framework's clock stands still there until the test ends ([Time](#time)). |
| `assert_num_queries`, `assert_max_queries` | test | `with assert_num_queries(3):` ([Counting queries](#counting-queries)). |
| `settings` | test | `settings.DEBUG = True`: the setting changes, validated, until the test ends; `del settings.CACHES` puts its default back ([Settings](#settings)). |

Memory caches are emptied before every test, so no test sees what another
cached. Redis caches are left alone: give the test settings their own
`KEY_PREFIX`. The memory backends' outboxes (`mail.outbox`, and
`sms_outbox` and `push_outbox` of `domainkit.contrib.notifications`) are
emptied before every test too, as Django's runner empties `mail.outbox`,
and every [factory](#factories) numbers its objects from 0 again, so a test
sees the same `Sequence` values in any order. What a module- or
session-scoped fixture sends is gone by the time a test runs: send it in
the test, or in a function-scoped fixture.

Error reporters (`ERROR_REPORTERS`) and the logging thread work in the
background ([runtime.md](runtime.md#logging-and-error-reports)): what a test
reported is sent, and what it logged is written, before the next test
starts. To check what a reporter received inside the test, call
`domainkit.reporting.flush_reports()` first:

```python
# tests/test_reports.py
from domainkit.conf import override_settings
from domainkit.reporting import flush_reports, report_error

seen = []


def remember(exc, context):
    seen.append(str(exc))


def test_reports_reach_the_reporter():
    with override_settings(ERROR_REPORTERS=["tests.test_reports.remember"]):
        report_error(ValueError("boom"))
    flush_reports()
    assert seen == ["boom"]
```

Fixtures for domain events and background work come with messaging's own
plugin, `-p domainkit.contrib.messaging.testing`, which projects made by
`domainkit init` enable too: `captured_events`, `outbox` (assert what was
queued, and `run_queued()` or `await arun_queued()` to run it here), and
`execute_deferred` ([contrib.md](contrib/messaging.md#testing)). They follow the
frozen clock: `outbox.run_queued(delayed=True)` makes waiting work due at
the clock's time.

## Test databases

The plugin never uses the databases the test settings name. For each entry
of `DATABASES`:

- **PostgreSQL**: it creates `test_<name>` on the same server (`test_billing`
  for `.../billing`), or the entry's `TEST_NAME`, and drops it when the run
  ends. The schema owner (`MIGRATION_URL` when set) creates it from the
  server's `postgres` database, so that role needs `CREATEDB`. Only names
  that start with `test_` are ever created or dropped; any other
  `TEST_NAME` is refused. A `test_` database left by an interrupted run is
  replaced.
- **SQLite in memory** (`sqlite+aiosqlite:///:memory:`): a file in the run's
  temporary directory, so every connection sees the same tables.
- **Anything else** is used as it is.

Each is then migrated up, down to base, and up again, which proves every
revision reversible.

`pytest --keepdb` keeps the PostgreSQL test databases for the next run. That
run reuses them and only migrates up, which is faster; each run puts the
rows back as `migrate` left them when it ends. A role that cannot create
databases can still run the tests: create `test_<name>` once by hand, and
always pass `--keepdb`.

Under [pytest-xdist](https://pypi.org/project/pytest-xdist/), each worker
gets its own database: `test_billing_gw0`, `test_billing_gw1`. The plugin
reads the worker's id from `PYTEST_XDIST_WORKER`, so xdist is not a
dependency.

So a test sees only the rows it writes, in any order, however it wrote
them: through a client, a unit of work, a command, or the worker. The `db`
fixture puts every database back before the test by deleting every row and
inserting the rows read once after migrating; on PostgreSQL, tables with
triggers of their own (an append-only audit trail) have them off while this
runs. The rows are read and restored as the schema owner. Sequences keep
counting, so do not assert on generated ids.

To run the same suite on PostgreSQL, point the test settings at it, for
example from a variable:

```python
# config/settings/test.py
DATABASES = {
    "default": {"URL": env.str("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")}
}
```

## Clients

`client` is a `domainkit.testing.TestClient`: Starlette's, which runs the
application in a thread of its own and is called synchronously, in sync
and async tests alike. `async_client` is an `AsyncTestClient`, an
`httpx2.AsyncClient` that runs the application in the test's own task and
event loop:

```python
import pytest

from domainkit.testing import AsyncClientFor, AsyncTestClient

pytestmark = pytest.mark.anyio


async def test_viewers_cannot_create(async_client_for: AsyncClientFor) -> None:
    viewer = await async_client_for("billing.view_invoice")
    assert (await viewer.get("/api/v1/billing/invoices")).status_code == 200
    assert (await viewer.post("/api/v1/billing/invoices", json={})).status_code == 403


async def test_admins_create(async_admin_client: AsyncTestClient) -> None:
    body = {"customer": "Acme", "total": "10.00"}
    created = await async_admin_client.post("/api/v1/billing/invoices", json=body)
    assert created.status_code == 201
```

Both run the application's lifespan while the fixture lasts, and raise the
application's errors in the test. Clients from `client_for` and
`admin_client` share the `client` fixture's thread and event loop, and so
its database connections; the async ones share the test's. Both clients
start each request with the context variables of the code that sends it,
so a frozen clock reaches the application. WebSockets need the sync client
(`client.websocket_connect`). The sync client works with Starlette 1.8 to
1.10: whichever of AnyIO's portal methods a release starts the
application's work with, the request still sees the sender's context.

Outside the fixtures, `TestClient(app)` and `AsyncTestClient(app)` work the
same way; enter them with `with` and `async with` to run the lifespan.
`client.authenticated("billing.view_invoice")` and
`await async_client.authenticated(...)` return clients for a new user, and
`await user_token("billing.view_invoice")` returns a token for one.

Async tests run on AnyIO's pytest plugin, which comes with FastAPI, on its
default asyncio backend: mark them with `pytest.mark.anyio`. The sync
fixtures (`db`, `client`) work in them too.

## Users and groups

`domainkit.contrib.auth.testing` stores users and groups as the
application does, through `AuthUnitOfWork`, so their events are published
and the principal cache hears of them. The clients' `authenticated_as`
signs in as one, as DRF's `force_authenticate` does
([ADR 0107](adr/0107-make-test-users-through-the-domain-and-sign-in-through-a-protocol.md)):

```python
import pytest

from domainkit.contrib.auth.testing import create_group, create_user
from domainkit.testing import AsyncTestClient, TestClient

pytestmark = pytest.mark.anyio


async def test_clerks_read(async_client: AsyncTestClient) -> None:
    clerks = await create_group("billing.view_invoice", name="clerks")
    ann = await create_user(groups=[clerks.name], email="ann@example.com")
    as_ann = await async_client.authenticated_as(ann)
    assert (await as_ann.get("/api/v1/billing/invoices")).status_code == 200


def test_owners_edit(client: TestClient) -> None:
    owner = client.run(create_user, "billing.change_invoice", password="s3cret-pass")
    as_owner = client.authenticated_as(owner)
    ...
```

- `await create_user(*permissions, password=None, groups=(), superuser=False,
  **fields)`: a user granted the permissions (keys) and in the groups
  (names). Without a password it gets an unusable one; with one, it is
  hashed with `AUTH.PASSWORD_HASHERS`, so the test can log in through
  `/auth/login`. `superuser=True` makes a superuser, and staff where the
  model has it. Keyword arguments set the user's fields; the username is
  unique unless given.
- `await create_group(*permissions, name=None)`: a group granted the
  permissions.
- `client.authenticated_as(user)` (sync) and `await
  async_client.authenticated_as(user)` (async): a client that sends an
  access token for `user`, from the first `AUTH.AUTHENTICATION_CLASSES`
  class, as a login's would. `user` is a stored user, or its id: one these
  functions made, or a [factory](#factories) stored. `await
  token_for(user)` returns the token itself. These tokens, as
  `client_for`'s, skip the password and the second factor, so they are
  refused unless `ENVIRONMENT = "test"`.
- `client.run(function, *args, **kwargs)` awaits an async function in the
  sync client's event loop, on the application's own connections, with the
  caller's context (a frozen clock): async setup in a sync test.
- Object grants go through `domainkit.contrib.objectpermissions`'
  `grant_permission`, with a superuser as the actor.

`client_for(...)` still makes a new user for each client; these are for a
user the test needs to know: as an object's owner, a group's member, or
the one who logs in.

## Factories

Factories make aggregates with defaults each test overrides, as
factory_boy does:

```python
from decimal import Decimal
from uuid import uuid4

from domainkit.testing import Factory, LazyAttribute, LazyFunction, Sequence, SubFactory

from billing.domain.model import Customer, Invoice, InvoiceId, Money
from billing.infrastructure.unit_of_work import SqlBillingUnitOfWork


class MoneyFactory(Factory[Money]):
    class Meta:
        model = Money

    amount = Decimal("10.00")
    currency = "EUR"


class CustomerFactory(Factory[Customer]):
    class Meta:
        model = Customer
        repository = "customers"

    id = LazyFunction(uuid4)
    name = Sequence(lambda n: f"Customer {n}")


class InvoiceFactory(Factory[Invoice]):
    class Meta:
        model = Invoice.issue  # any callable; this one records InvoiceIssued
        repository = "invoices"
        unit_of_work = SqlBillingUnitOfWork
        exclude = ("customer",)

    id = LazyFunction(lambda: InvoiceId(uuid4()))
    number = Sequence(lambda n: f"INV-{n:05}")
    total = SubFactory(MoneyFactory)
    customer = SubFactory(CustomerFactory)
    customer_id = LazyAttribute(lambda invoice: invoice.customer.id)
```

- `InvoiceFactory.build(number="INV-1")` makes one in memory;
  `build_batch(3)` makes three.
- `await InvoiceFactory.create(uow)` stores one as an application service
  would: it enters the unit of work, adds the invoice, and commits, so its
  events are published. Without `uow`, `Meta.unit_of_work()` makes one. The
  customer, whose factory names a repository, is added through the same
  unit of work and transaction; the money, a value object, is only built.
- It works the same with an in-memory fake unit of work (pass it) and a
  SQL one, so the same factories serve use-case tests and API tests.
- Keyword arguments replace defaults; `total__amount=Decimal(5)` reaches
  the sub-factory; fields the factory does not declare pass through.
- Each object takes its factory's next number, from 0, which all its
  `Sequence` fields share; `reset_sequence()` starts again, and the
  plugin does it for every factory before each test.
- Plain defaults are copied for each object, so a list default is never
  shared. `LazyAttribute` reads the other fields in any order; fields that
  depend on each other in a circle are refused.

## Time

`domainkit.clock` is the one clock the framework reads: `now()` (aware,
UTC), `today()`, and `monotonic()`. Freeze it, and token expiry (opaque
tokens and JWTs), idempotency leases, rate windows counted in the process,
memory cache lifetimes, and the timestamps of audit entries, events,
messages, and evidence all follow ([ADR 0037](adr/0037-one-clock-frozen-per-context.md)):

```python
from datetime import timedelta

from domainkit.testing import ClientFor, FreezeTime


def test_tokens_expire(freeze_time: FreezeTime, client_for: ClientFor) -> None:
    clock = freeze_time("2026-01-01T09:00:00Z")
    viewer = client_for("billing.view_invoice")
    clock.tick(timedelta(days=14))
    assert viewer.get("/api/v1/billing/invoices").json()["detail"] == "Token expired."
```

- The `freeze_time` fixture freezes the clock until the test ends.
  `freeze_time()` freezes it now; `tick=True` starts it at the moment and
  lets it run on. `clock.tick(seconds or timedelta)` moves it forward;
  `clock.move_to(moment)` jumps either way. Moments are ISO strings, dates,
  or datetimes; those without a time zone are UTC.
- `with freeze_time("2026-01-01") as clock:` (from `domainkit.clock` or
  `domainkit.testing`) freezes it for a block, in any code.
- The frozen clock belongs to the context that froze it, like the request
  context: tasks started from it see it; other tests and concurrent
  requests do not.
- Domain code may read `domainkit.clock` too (`from domainkit.clock import
  now`; the layer rules allow it), and then follows the same freeze.
- **The database's clock is the database's**: `now()` in SQL, a column's
  `server_default`, and shared rate limits counted on the Redis server are
  never frozen. Do not compare them with the frozen clock.
- `monotonic()` stands still with a frozen clock and moves forward with it,
  never back. Its frozen count starts at a whole second, so ticks of whole
  seconds add exactly, and a rate limit counted in this process lets
  exactly its limit through at one frozen moment. Intervals measured across
  the start or end of a freeze are meaningless.

## Settings

The `settings` fixture changes settings until the test ends, as
`override_settings` changes them for a block, and as pytest-django's
`settings` fixture does:

```python
def test_debug_pages(settings, client: TestClient) -> None:
    settings.DEBUG = True
    del settings.CACHES  # Its default, for this test.
    ...
```

Each change is validated with the whole settings, as at startup: a value
they refuse fails the assignment with `ImproperlyConfigured`. Reading
`settings.DEBUG` reads the current setting. Changes are undone in reverse
order when the test ends.

## Counting queries

`assert_num_queries(n)` fails unless the block runs exactly `n` SQL
statements, and `assert_max_queries(n)` unless it runs at most `n`. The
failure lists them, as SQL with placeholders; bound values are never kept:

```python
from domainkit.testing import TestClient, assert_max_queries


def test_listing_runs_few_queries(admin_client: TestClient) -> None:
    with assert_max_queries(6) as queries:
        admin_client.get("/api/v1/billing/invoices")
    assert "ORDER BY" in queries.statements[-1]
```

```text
AssertionError: Expected at most 6 queries, 7 ran:
1. SELECT ... FROM domainkit_tokens WHERE ...
2. ...
```

They count the statements of every engine of the process, in every thread
and event loop, so a `TestClient`'s requests count; `using="reports"`
counts one alias only. Each statement sent to the driver counts once, an
`executemany` too; connecting and committing do not, nor the timeouts a
PostgreSQL transaction sets for itself under a deadline or PgBouncer
([runtime.md](runtime.md#what-keeps-to-it)). An authenticated
request costs a few statements of its own (the token, the user, its groups
and permissions), so prefer `assert_max_queries` for whole requests and
`assert_num_queries` for code you call directly. Without the fixtures,
import them from `domainkit.testing`.

Importing `domainkit.testing` (the plugin does, as pytest starts) puts
one listener on SQLAlchemy's `Engine` class, and it stays; blocks only
come and go from the list it hands statements to. SQLAlchemy reads its listeners without a lock as each
statement runs, so a listener added or removed while another thread runs
a statement fails that statement ("deque mutated during iteration"). Until
0.10 each block added and removed its own, which failed a request or a
background task running at that moment now and then.

## Blocking the event loop

`pytest --detect-blocking` fails a test that holds its event loop for 0.1
seconds or more in one step (`--blocking-seconds` raises the bar): a sync
HTTP client, `time.sleep`, or a CPU-heavy loop inside `async def`. Every
loop the tests start runs in asyncio's debug mode, the test's async loop
and the clients' alike; fixtures are not timed. The failure quotes
asyncio's report of the slow step. See
[runtime.md](runtime.md#finding-blocking-code).
## Outgoing calls

Code that calls other services through `domainkit.http.client` is tested
without them: `stub(handler)` sends every call made in its block to
`handler`, a function of an `httpx2.Request` that returns an
`httpx2.Response` ([runtime.md](runtime.md#outgoing-http-calls)). Both
clients carry the block's context into the application, so calls made
while serving a request are stubbed too.

## In generated projects

`domainkit init` writes `tests/test_health.py` with a sync and an async
test. The tests `startdomain` writes use `client`, `client_for`, and
`admin_client`; each has an async twin. A domain's tests can also define
factories next to its in-memory fakes (`tests/<domain>/factories.py`), pass
a fake unit of work to `create` in use-case tests, and let `create` use the
SQL unit of work in API tests.

## Compared with pytest-django, DRF, Litestar, and factory_boy

What their testing tools give, and where domainkit stands as of 0.12. Rows
marked 0.12 were added for the 0.9.0 audit's TEST-08.

| They give | domainkit |
| --- | --- |
| pytest-django's `db`, `django_db_setup`, `--reuse-db` | `db`, `database`, `--keepdb`; a database per xdist worker |
| pytest-django's `client`, `admin_client`, `async_client` | `client`, `admin_client`, `async_client`, `async_admin_client` |
| pytest-django's `django_assert_num_queries`, `django_assert_max_num_queries` | `assert_num_queries`, `assert_max_queries` |
| pytest-django's `mailoutbox`; Django's emptied `mail.outbox` | `mail.outbox`, `sms_outbox`, and `push_outbox`, emptied before each test |
| pytest-django's `django_capture_on_commit_callbacks` | messaging's `outbox` (`assert_queued`, `run_queued`), `execute_deferred`, `captured_events` |
| pytest-django's `settings` | `settings` (0.12) |
| pytest-django's `admin_user`, `django_user_model`; factory_boy's model factories for users | `create_user`, `create_group` (0.12); `client_for(...)` for a new user per client |
| DRF's `force_authenticate`, `force_login` | `authenticated_as(user)`, `token_for(user)` (0.12) |
| DRF's `APIClient.credentials` | `client.another(headers)` |
| Litestar's `create_test_client`, `AsyncTestClient` | `TestClient(app)`, `AsyncTestClient(app)` for any ASGI application |
| freezegun, time-machine | `freeze_time`, which the application, tokens, and the outbox follow |
| factory_boy's `Factory`, `Sequence`, `LazyFunction`, `LazyAttribute`, `SubFactory`, `build_batch`, `create_batch` | the same names |

Not given yet, each with what to do meanwhile:

- **Refusing database access a test did not ask for** (pytest-django's
  `django_db_blocker` and its `db` mark): a test that reaches the
  database without `db` or a client sees whatever the last test left.
  Request `db`.
- **A live server on a port** (`live_server`): for browsers and other
  processes. Run `uvicorn` in a fixture of your own, on the test
  database's URL.
- **A request factory** (Django's `RequestFactory`, DRF's
  `APIRequestFactory`): call dependencies with the objects tests build,
  or go through `client`.
- **factory_boy's `Trait`, `post_generation`, `RelatedFactory`,
  `SelfAttribute`, `Faker`, `Maybe`, and `stub()`**: use `LazyAttribute`
  and `SubFactory`, or create the related objects in the test.
- **Session data helpers** (Litestar's `set_session_data`): log in
  through `/auth/session`, as a browser does.

