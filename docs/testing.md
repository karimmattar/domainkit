# Testing

`domainkit.testing` is a pytest plugin, like pytest-django. It selects
the test settings, makes and migrates the test databases, puts them back
before each test, and serves the application to sync and async tests. It
also brings clients that log in as users, factories for aggregates, a
clock that tests can freeze, and assertions that count SQL statements.
This page is the reference for every fixture and helper, each with a
test that uses it.

On this page you learn how to:

- set up the plugin, and know what each test starts from;
- reach the test databases, and why a test must ask for them;
- call the application with `client`, `client_for`, `admin_client`, and
  their async twins, or serve it on a port with `live_server`;
- make users, groups, and aggregates with `create_user` and `Factory`;
- freeze time, change settings, and count queries;
- test second factors, approvals, key services, and outgoing calls;
- find tests that block the event loop.

## A first example

A project made by `domainkit init`, with a `catalog` domain made by
`startdomain catalog --aggregate Product`, can test its API like this:

```python
# tests/domains/catalog/test_products.py
from domainkit.testing import ClientFor, TestClient

URL = "/api/v1/catalog/products"


def test_anonymous_callers_must_log_in(client: TestClient) -> None:
    answer = client.get(URL)
    assert (answer.status_code, answer.json()["code"]) == (401, "not_authenticated")


def test_viewers_read_but_cannot_create(client_for: ClientFor) -> None:
    viewer = client_for("catalog.view_product")
    assert viewer.get(URL).status_code == 200
    assert viewer.post(URL, json={"name": "Lamp"}).status_code == 403


def test_admins_create(admin_client: TestClient) -> None:
    created = admin_client.post(URL, json={"name": "Lamp"})
    assert (created.status_code, created.json()["name"]) == (201, "Lamp")
```

```bash
pytest tests/domains/catalog/test_products.py
```

```text
...                                                                      [100%]
3 passed in 1.39s
```

Each test starts from the database as `migrate` left it, so the three
tests pass in any order. `client` sends no credentials; `client_for`
makes a new user with the permissions you name; `admin_client` makes a
superuser.

## Setting it up

Install the `testing` extra, which brings pytest and httpx2, as a
development dependency, and enable the plugin in `pyproject.toml`.
Projects made by `domainkit init` already do both:

```toml
[dependency-groups]
dev = ["domainkit[testing]"]

[tool.pytest.ini_options]
pythonpath = ["src"]
addopts = "-p domainkit.testing -p domainkit.contrib.messaging.testing"
domainkit_settings = "config.settings.test"
```

- `-p domainkit.testing` loads this plugin.
  `-p domainkit.contrib.messaging.testing` loads the fixtures for events
  and queued work ([Events and queued work](#events-and-queued-work)).
- `domainkit_settings` names the test settings module.
- The `.env` file is never read during tests.
- The test settings must say `ENVIRONMENT = "test"`. The database
  fixtures empty every database in `DATABASES`, so they refuse any other
  settings, and a missing `domainkit_settings` cannot empty the
  development database.

The test settings that `domainkit init` writes:

```python
# config/settings/test.py
from config.settings.base import *
from config.settings.base import AUTH

ENVIRONMENT = "test"
ALLOWED_HOSTS = ["testserver"]
SECRET_KEY = "domainkit-insecure-test-key"
DATABASES = {"default": {"URL": "sqlite+aiosqlite:///:memory:"}}
# One hash iteration: fast tests. Staging and production refuse it.
AUTH = {
    **AUTH,
    "PASSWORD_HASHERS": ["domainkit.contrib.auth.hashers.FastPBKDF2PasswordHasher"],
}
```

## Fixtures

| Fixture | Scope | Gives |
| --- | --- | --- |
| `database` | session | The test databases, made and migrated ([Test databases](#test-databases)). Gives the default database's URL. |
| `db` | test | Those databases, put back as `migrate` left them. Request it in tests that reach the database without a client: a test that did not request it is refused them ([Database access](#database-access)). |
| `database_blocker` | session | A `DatabaseBlocker`: `with database_blocker.unblock():` lets a fixture wider than one test reach the databases, as pytest-django's `django_db_blocker` does. |
| `client` | test | A `TestClient` for the project's application, with its lifespan, on the `db` databases. It sends no credentials ([Clients](#clients)). |
| `client_for` | test | `client_for("catalog.view_product", ...)`: a client of the same application that authenticates as a new user granted those permissions. Keyword arguments set the user's fields (`is_staff=True`, `username="carol"`); the username is unique otherwise. Needs `domainkit.contrib.auth`. |
| `admin_client` | test | A client that authenticates as a new superuser. |
| `async_client`, `async_client_for`, `async_admin_client` | test | The same, as `AsyncTestClient`s, for async tests; `await async_client_for(...)`. |
| `live_server` | test | A `LiveServer`: `live_server.url` is the application served by uvicorn on a port of the test process, for browsers and other processes, on the `db` databases ([A live server](#a-live-server)). |
| `freeze_time` | test | `clock = freeze_time("2026-01-01")`: the framework's clock stands still there until the test ends ([Time](#time)). |
| `assert_num_queries`, `assert_max_queries` | test | `with assert_num_queries(3):` ([Counting queries](#counting-queries)). |
| `settings` | test | `settings.DEBUG = True`: the setting changes, validated, until the test ends; `del settings.CACHES` puts its default back ([Settings](#settings)). |

The type of each fixture, to annotate your tests:

| Fixture | Type |
| --- | --- |
| `client`, `admin_client` | `TestClient` |
| `async_client`, `async_admin_client` | `AsyncTestClient` |
| `client_for` | `ClientFor` |
| `async_client_for` | `AsyncClientFor` |
| `live_server` | `LiveServer` |
| `freeze_time` | `FreezeTime` (it returns a `FrozenClock`) |
| `assert_num_queries`, `assert_max_queries` | they return a `CapturedQueries` |
| `settings` | `SettingsOverrides` |
| `database_blocker` | `DatabaseBlocker` |

All of them are in `domainkit.testing`.

### What each test starts from

The plugin puts the process's own state back between tests. It does each
piece through a public reset, which a project's fixtures may call too, so
a project's `conftest.py` never needs a private name:

| Before each test | Through |
| --- | --- |
| Memory caches (`memory://`) are empty. Redis caches are left alone: give the test settings their own `KEY_PREFIX`. | `caches.reset()` |
| The memory backends' outboxes are empty, as Django's runner empties `mail.outbox`. | `mail.outbox.clear()`; `sms_outbox` and `push_outbox` of `domainkit.contrib.notifications.backends` |
| Every [factory](#factories) numbers its objects from 0, so a test sees the same `Sequence` values in any order. | `Factory.reset_sequence()` |
| The throttled logs (`domainkit.log.ThrottledLog`) have forgotten what they held back, so a test's first line is written. | `domainkit.log.reset_throttled()` |
| The principal cache is empty: a user is read from the database again. | `domainkit.contrib.auth.principals.invalidate_all()` |
| Memory ledgers (`memory://` in `LEDGERS`) and memory storages are empty, as the databases' ids start again after `db`. Ledgers that hold connections (immudb's) stay open. | `ledgers.reset()`, `storages.reset()` |
| The databases are refused unless the test requested them ([Database access](#database-access)). | `database_blocker` |

| After each test | Through |
| --- | --- |
| The subscriptions, hooks (`on_ready(check, domain=...)` and every other hook point), tasks, metric refreshers, and currencies that the test added are dropped, and those it removed come back. When the test's fixtures load the domains (a client, `db`), what their `ready()` registered stays and what the test's body adds is dropped. A test whose body loads the domains itself keeps what it registers, because its registrations cannot be told from theirs. | `event_bus.subscriptions_kept()`, `domainkit.apps.hooks.hooks_kept()`, `domainkit.contrib.messaging.tasks_kept()`, `domainkit.http.metrics.refreshers_kept()`, `domainkit.money.currencies_kept()` |
| Its error reports are sent and its logs written ([Error reports and logs](#error-reports-and-logs)). | `flush_reports()`, `flush_logs()` |

What a module-scoped or session-scoped fixture sends to an outbox is gone
by the time a test runs: send it in the test, or in a function-scoped
fixture. The resets of an app that a project does not load are passed
over, never imported. Each reset costs a call or two, made from one hook
rather than a fixture per reset
([ADR 0113](adr/0113-reset-module-state-through-public-resets-the-plugin-calls.md)).

## Test databases

The plugin never uses the databases that the test settings name. For
each entry of `DATABASES`:

- **PostgreSQL**: it creates `test_<name>` on the same server
  (`test_billing` for `.../billing`), or the entry's `TEST_NAME`, and
  drops it when the run ends. The schema owner (`MIGRATION_URL` when set)
  creates it from the server's `postgres` database, so that role needs
  `CREATEDB`. Only names that start with `test_` are ever created or
  dropped; any other `TEST_NAME` is refused. A `test_` database left by
  an interrupted run is replaced.
- **SQLite in memory** (`sqlite+aiosqlite:///:memory:`, the constant
  `IN_MEMORY`): a file in the run's temporary directory, so every
  connection sees the same tables.
- **Anything else** is used as it is.

Each database is then migrated up, down to base, and up again, which
proves every revision reversible. You see it in the log of the first test:

```text
INFO [alembic.runtime.migration] Running upgrade  -> 0001_0339, Auth.
INFO [alembic.runtime.migration] Running upgrade 0001_0339 -> 0002_ef8c, Catalog.
INFO [alembic.runtime.migration] Running downgrade 0002_ef8c -> 0001_0339, Catalog.
INFO [alembic.runtime.migration] Running downgrade 0001_0339 -> , Auth.
INFO [alembic.runtime.migration] Running upgrade  -> 0001_0339, Auth.
INFO [alembic.runtime.migration] Running upgrade 0001_0339 -> 0002_ef8c, Catalog.
```

When an entry's `URL` connects as another role than its `MIGRATION_URL`
(the application's row-level role beside the schema owner, as
[deploying.md](deploying.md) asks), the owner then grants that role, on
the test database:

- `USAGE` on its schemas;
- `SELECT`, `INSERT`, `UPDATE`, `DELETE` on their tables;
- `USAGE`, `SELECT`, `UPDATE` on their sequences.

That is what a row-level role needs, and nothing more, so a test that
needs more fails as production would. The role must exist on the server;
a missing one stops the run, naming it. Without these grants, every test
of such a project failed with "permission denied" (TEST-13). For example:

```python
# config/settings/test.py
DATABASES = {
    "default": {
        "URL": env.str("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        "MIGRATION_URL": env.str("TEST_DATABASE_OWNER_URL", None),
    }
}
```

`domainkit init --profile fintech` writes these settings; domainkit's CI
runs that project's tests on PostgreSQL this way.

- **`pytest --keepdb`** keeps the PostgreSQL test databases for the next
  run. That run reuses them and only migrates up, which is faster. Each
  run puts the rows back as `migrate` left them when it ends. A role that
  cannot create databases can still run the tests: create `test_<name>`
  once by hand, and always pass `--keepdb`.
- **Under [pytest-xdist](https://pypi.org/project/pytest-xdist/)**, each
  worker gets its own database: `test_billing_gw0`, `test_billing_gw1`.
  The plugin reads the worker's id from `PYTEST_XDIST_WORKER`, so xdist is
  not a dependency.

So a test sees only the rows it writes, in any order, however it wrote
them: through a client, a unit of work, a command, or the worker. The
`db` fixture puts every database back before the test: it deletes every
row, and inserts the rows that it read once after migrating. On
PostgreSQL, tables with triggers of their own (an append-only audit
trail) have them off while this runs. The rows are read and restored as
the schema owner. Sequences keep counting, so do not assert on generated
ids.

`migrated_database(scratch, *, keepdb=False, worker=None)` is the context
manager behind the `database` fixture. It makes, migrates, and later drops
the test databases, and yields the default's URL. Call it yourself only
when you run tests without the plugin.

## Database access

A test reaches the test databases only when it asks for them, as
pytest-django's `db` mark makes it ask: by requesting `db`, or a fixture
that does (`client`, `async_client`, `live_server`, and the others). Any
other test is refused them, whatever path it takes (a unit of work,
`connections`, `autocommit`, a command, a factory's `create`), in every
thread, the application's own in a `TestClient` too. This test does not
request `db`:

```python
import asyncio

from tests.factories import ProductFactory


def test_forgets_to_ask() -> None:
    asyncio.run(ProductFactory.create())
```

It fails:

```text
E   domainkit.db.connections.DatabaseAccessRefused: This test reached the database without requesting it, so it would see whatever earlier tests left there. Request the db fixture (a client fixture requests it too); in a fixture wider than one test, use database_blocker.unblock(). To allow it as 0.12 did, mark the test @pytest.mark.allow_database, or set domainkit_allow_database = true in the pytest settings.
```

Request `db` to fix it. In an async test:

```python
import pytest


@pytest.mark.anyio
@pytest.mark.usefixtures("db")
async def test_asks_for_the_database() -> None:
    product = await ProductFactory.create(name="Lamp")
    assert product.name == "Lamp"
```

Until 1.0, a test that did not ask saw whatever the test before it left.
When it ran first, before any test had made the test databases, it
reached the databases that the test settings name.

### A fixture wider than one test: `database_blocker`

A module-scoped fixture that writes rows opens the databases while it
works:

```python
import pytest

from domainkit.management.base import run_async
from domainkit.testing import DatabaseBlocker, TestClient


@pytest.fixture(scope="module")
def catalogue(database: str, database_blocker: DatabaseBlocker) -> list[str]:
    with database_blocker.unblock():
        made = run_async(ProductFactory.create_batch(3))
    return [product.name for product in made]


def test_the_rows_are_put_back(catalogue: list[str], admin_client: TestClient) -> None:
    assert catalogue == ["Product 0", "Product 1", "Product 2"]
    assert admin_client.get("/api/v1/catalog/products").json()["items"] == []
```

The test passes: its `admin_client` requests `db`, which puts the rows
back as `migrate` left them. A test that should see the module's rows
unblocks too, without `db`.

`DatabaseBlocker` has `unblock()` and `block()`, both context managers,
and `blocked`, which says whether the databases are refused now. Blocks
nest: each puts back what was in force when it started.

### Allowing the databases as before: `allow_database`

A project that needs time to move keeps 0.12's behavior:

```python
import pytest

from domainkit.management.base import run_async


@pytest.mark.allow_database
def test_reaches_the_database_without_db() -> None:
    assert run_async(ProductFactory.create()).name == "Product 0"
```

- Mark a test, or a module with `pytestmark`, with
  `@pytest.mark.allow_database`. A marked test reaches the test databases
  as the last test left them. The mark makes them first, so it never
  reaches the ones that the settings name.
- Set `domainkit_allow_database = true` under `[tool.pytest.ini_options]`
  for every test. The ini option does not make the databases first, as
  0.12 did not.

### What the refusal covers

- Engines, sessions, and owner engines of `domainkit.db` raise
  `DatabaseAccessRefused` (`ConnectionHandler.refuse`). An engine that a
  test makes itself with SQLAlchemy is its own, and so are ledgers,
  caches, and brokers.
- Under pytest-xdist, each worker is a process of its own, with its own
  refusal. `--keepdb` reuses, migrates, and puts back its databases with
  them open, as without it.
- A function-scoped fixture of the project that reaches the databases
  requests `db` itself, or it runs before `db` opens them.
- It costs nothing a test can measure: a check where each engine or
  session is handed out, and a hook, not a fixture, for the mark. 3,000
  tests take as long as with 0.12
  ([ADR 0112](adr/0112-refuse-the-test-databases-to-a-test-that-did-not-request-them.md)).

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
`httpx2.AsyncClient` that runs the application in the test's own task
and event loop:

```python
import pytest

from domainkit.testing import AsyncClientFor, AsyncTestClient

URL = "/api/v1/catalog/products"


@pytest.mark.anyio
async def test_viewers_read(
    async_client: AsyncTestClient, async_client_for: AsyncClientFor
) -> None:
    viewer = await async_client_for("catalog.view_product")
    assert (await async_client.get(URL)).status_code == 401
    assert (await viewer.get(URL)).status_code == 200


@pytest.mark.anyio
async def test_admins_create(async_admin_client: AsyncTestClient) -> None:
    created = await async_admin_client.post(URL, json={"name": "Desk"})
    assert created.status_code == 201
```

- Both run the application's lifespan while the fixture lasts, and raise
  the application's errors in the test.
- Clients from `client_for` and `admin_client` share the `client`
  fixture's thread and event loop, and so its database connections. The
  async ones share the test's.
- Both clients start each request with the context variables of the
  code that sends it, so a frozen clock reaches the application.
- WebSockets need the sync client (`client.websocket_connect`; see
  [live.md](live.md#testing)).
- The sync client works with Starlette 1.8 to 1.10: whichever of AnyIO's
  portal methods a release starts the application's work with, the
  request still sees the sender's context.

Async tests run on AnyIO's pytest plugin, which comes with FastAPI, on its
default asyncio backend: mark them with `pytest.mark.anyio`. The sync
fixtures (`db`, `client`) work in them too.

### What a client can do

Each client has these methods besides HTTP's. Enter a client before you
use them (the fixtures do):

| Sync (`TestClient`) | Async (`AsyncTestClient`) | Returns |
| --- | --- | --- |
| `client.another(headers)` | `async_client.another(headers)` | A client of the same application that sends `headers`, on the same event loop and connections. |
| `client.authenticated(*permissions, **fields)` | `await async_client.authenticated(...)` | A client for a new user granted those permissions. |
| `client.authenticated_as(user)` | `await async_client.authenticated_as(user)` | A client that sends an access token for a stored user ([Users and groups](#users-and-groups)). |
| `client.browser_session_as(user)` | `await async_client.browser_session_as(user)` | A client signed in with a browser session. |
| `client.run(function, *args, **kwargs)` | (await it yourself) | What the async `function` returns, awaited in the client's event loop. |

```python
from domainkit.testing import TestClient


def test_answers_in_arabic(client: TestClient) -> None:
    arabic = client.another({"Accept-Language": "ar"})
    assert arabic.get("/api/v1/catalog/products").json()["detail"] == (
        "لم تُقدَّم بيانات اعتماد للمصادقة."
    )


def test_viewers_read(client: TestClient) -> None:
    viewer = client.authenticated("catalog.view_product")
    assert viewer.get("/api/v1/catalog/products").status_code == 200
```

Three functions give the credentials themselves, to send with your own
HTTP client:

| Function | Returns |
| --- | --- |
| `await user_token(*permissions, **fields)` | An access token for a new user granted those permissions. |
| `await token_for(user)` | An access token for a stored user, or its id. |
| `await browser_session_for(user)` | The headers of a browser signed in as a stored user: `{"Cookie": ..., "X-CSRF-Token": ...}`. |

```python
import pytest

from domainkit.testing import AsyncTestClient, user_token


@pytest.mark.anyio
async def test_a_token_from_outside(async_client: AsyncTestClient) -> None:
    token = await user_token("catalog.view_product")
    answer = await async_client.get(
        "/api/v1/catalog/products", headers={"Authorization": f"Bearer {token}"}
    )
    assert answer.status_code == 200
```

These tokens and sessions skip the password and the second factor, so
they are refused unless `ENVIRONMENT = "test"`.

### Clients for any application

Outside the fixtures, `TestClient(app)` and `AsyncTestClient(app)` work
the same way, for any ASGI application. Enter them with `with` and
`async with` to run the lifespan:

```python
from fastapi import FastAPI

from domainkit.testing import TestClient


def test_any_application() -> None:
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"answer": "pong"}

    with TestClient(app) as client:
        assert client.get("/ping").json() == {"answer": "pong"}
```

`AsyncTestClient(app, base_url=..., headers=..., client=..., lifespan=True)`
takes `lifespan=False` to skip the application's startup and shutdown.

## Users and groups

`domainkit.contrib.auth.testing` stores users and groups as the
application does, through `AuthUnitOfWork`, so their events are
published and the principal cache hears of them. The clients'
`authenticated_as` signs in as one, as DRF's `force_authenticate` does
([ADR 0107](adr/0107-make-test-users-through-the-domain-and-sign-in-through-a-protocol.md)):

```python
import pytest

from domainkit.contrib.auth.testing import create_group, create_user
from domainkit.testing import AsyncTestClient, TestClient

URL = "/api/v1/catalog/products"


@pytest.mark.anyio
async def test_clerks_read(async_client: AsyncTestClient) -> None:
    clerks = await create_group("catalog.view_product", name="clerks")
    ann = await create_user(groups=[clerks.name], email="ann@example.com")
    as_ann = await async_client.authenticated_as(ann)
    assert (await as_ann.get(URL)).status_code == 200


def test_owners_create(client: TestClient) -> None:
    owner = client.run(create_user, "catalog.add_product", password="s3cret-pass-word")
    as_owner = client.authenticated_as(owner)
    assert as_owner.post(URL, json={"name": "Chair"}).status_code == 201
```

- `await create_user(*permissions, password=None, groups=(),
  superuser=False, **fields)` stores a user granted the permissions (keys)
  and in the groups (names). Without a password, the user gets an
  unusable one. With one, it is hashed with `AUTH.PASSWORD_HASHERS`, so
  the test can log in through `/auth/login`. `superuser=True` makes a
  superuser, and staff where the model has staff. Keyword arguments set
  the user's fields; the username is unique unless given.
- `await create_group(*permissions, name=None)` stores a group granted
  the permissions.
- `authenticated_as(user)` gives a client that sends an access token for
  `user`, from the first `AUTH.AUTHENTICATION_CLASSES` class, as a login's
  would. `user` is a stored user, or its id: one these functions made, or
  one a [factory](#factories) stored.
- `client.run(function, *args, **kwargs)` awaits an async function in the
  sync client's event loop, on the application's own connections, with
  the caller's context (a frozen clock): async setup in a sync test.
- Object grants go through `domainkit.contrib.objectpermissions`'
  `grant_permission`, with a superuser as the actor.

`client_for(...)` still makes a new user for each client. Use these
functions for a user that the test needs to know: an object's owner, a
group's member, or the one who logs in.

### A browser session

`browser_session_as(user)` gives a client signed in with a browser
session, as Litestar's `set_session_data` does, without posting to
`/auth/session`. It sends the session cookie and the CSRF cookie, and
the CSRF token in `X-CSRF-Token`, as a page's script sends it, so unsafe
methods pass the CSRF checks:

```python
import pytest

from domainkit.contrib.auth.testing import create_user
from domainkit.testing import TestClient

AUTHENTICATION_CLASSES = [
    "domainkit.contrib.auth.authentication.TokenAuthentication",
    "domainkit.contrib.auth.cookies.CookieAuthentication",
]


@pytest.fixture
def cookies(settings) -> None:
    settings.AUTH = {**settings.AUTH, "AUTHENTICATION_CLASSES": AUTHENTICATION_CLASSES}


def test_a_page_creates(cookies: None, client: TestClient) -> None:
    owner = client.run(create_user, "catalog.add_product")
    page = client.browser_session_as(owner)
    assert page.post("/api/v1/catalog/products", json={"name": "Chair"}).status_code == 201
```

It needs `CookieAuthentication` in `AUTH.AUTHENTICATION_CLASSES`.
Without it, it raises `ImproperlyConfigured: A browser session needs
domainkit.contrib.auth.cookies.CookieAuthentication in
AUTH.AUTHENTICATION_CLASSES.` It is refused unless
`ENVIRONMENT = "test"`, as the tokens are: no route reaches it.

The `cookies` fixture comes before `client` on purpose: the application
reads its authentication classes when the client starts it
([Settings](#settings)).

## A live server

Browsers and other processes need a real socket. `live_server` serves
the project's application with uvicorn, in a thread of the test process,
on `127.0.0.1` and a port the system picks, on the `db` databases:

```python
import httpx2
from playwright.sync_api import Page

from domainkit.testing import LiveServer


def test_the_home_page(live_server: LiveServer, page: Page) -> None:
    page.goto(live_server.url)
    assert httpx2.get(f"{live_server.url}/health").json() == {"status": "ok"}
```

- It starts for the test, lifespan included, and stops after it. So each
  test gets an application built with its own settings (the `settings`
  fixture's too), and nothing it served reaches the next test. Starting
  takes about 2 ms; stopping about 0.14 seconds, because uvicorn checks
  every 0.1 seconds whether to stop.
- `127.0.0.1` is added to `ALLOWED_HOSTS` through the `settings` fixture.
  So it is undone with the test's other changes, in reverse order,
  whatever order the fixtures were requested in.
- Its thread is a daemon, so an interrupted run never waits for it. A
  request still open when it stops (a stream, a WebSocket) is cancelled
  after `stop_seconds` (5).
- Its thread starts with the test's context. Request `freeze_time` (and
  freeze the clock) before `live_server`, and the application's clock is
  the test's, moving with `clock.tick()`.
- Rows that the test stores (`run_async(create_user(...))`) are the
  server's to read, and the other way round: they share the databases.

`LiveServer(app, *, host="127.0.0.1", stop_seconds=5)` serves any ASGI
application the same way, in a `with` block; `server.url` and
`server.port` say where. The application's `ALLOWED_HOSTS` must hold
`127.0.0.1`:

```python
import httpx2

from domainkit.http import get_asgi_application
from domainkit.testing import LiveServer


def test_a_server_of_my_own(settings) -> None:
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "127.0.0.1"]
    with LiveServer(get_asgi_application()) as server:
        assert httpx2.get(f"{server.url}/health").json() == {"status": "ok"}
```

A startup that fails raises `RuntimeError`; uvicorn logs why.

## Factories

Factories make aggregates with defaults that each test overrides, as
factory_boy does:

```python
# tests/factories.py
from uuid import uuid4

from domainkit.testing import Factory, LazyFunction, Sequence

from domains.catalog.domain import Product, ProductId
from domains.catalog.models import SqlAlchemyCatalogUnitOfWork


class ProductFactory(Factory[Product]):
    class Meta:
        model = Product
        repository = "products"
        unit_of_work = SqlAlchemyCatalogUnitOfWork

    id = LazyFunction(lambda: ProductId(uuid4()))
    name = Sequence(lambda n: f"Product {n}")
```

```python
import pytest

from domainkit.ddd import in_memory

from domains.catalog.domain import CatalogUnitOfWork
from tests.factories import ProductFactory


def test_build() -> None:
    assert ProductFactory.build().name == "Product 0"
    assert ProductFactory.build(name="Lamp").name == "Lamp"  # takes number 1 too
    assert [p.name for p in ProductFactory.build_batch(2)] == ["Product 2", "Product 3"]


@pytest.mark.anyio
async def test_create_in_memory() -> None:
    uow = in_memory(CatalogUnitOfWork)
    product = await ProductFactory.create(uow, name="Lamp")
    assert await uow.products.get(product.id) is product
    assert uow.committed == 1
```

| Method | What it does |
| --- | --- |
| `build(**overrides)` | Makes one object in memory. |
| `build_batch(size, **overrides)` | Makes `size` objects in memory. |
| `await create(uow=None, **overrides)` | Stores one object as an application service would: it enters the unit of work, adds the object, and commits, so its events are published. Without `uow`, `Meta.unit_of_work()` makes one. |
| `await create_batch(size, uow=None, **overrides)` | Stores `size` objects, each committed on its own. |
| `stub(**overrides)`, `stub_batch(size, ...)` | The fields the model would receive, as a `SimpleNamespace`. No model is called. |
| `reset_sequence(value=0)` | Numbers the next object `value`. |

| `Meta` option | What it sets |
| --- | --- |
| `model` | The class, or any callable that returns the object (`Invoice.issue`, which records `InvoiceIssued`). |
| `repository` | The repository's name on the unit of work (`"products"`), for `create`. |
| `unit_of_work` | The unit of work class that `create` makes when it gets none. |
| `exclude` | Fields that other fields read but the model does not receive. |

- `create` works the same with an in-memory fake unit of work (pass it)
  and a SQL one, so the same factories serve use-case tests and API
  tests. With the SQL one, the test needs `db`.
- Keyword arguments replace defaults; `total__amount="5.00"` reaches the
  sub-factory; fields that the factory does not declare pass through.
- Each object takes its factory's next number, from 0, which all its
  `Sequence` fields share. The plugin resets every factory before each
  test.
- Plain defaults are copied for each object, so a list default is never
  shared.

### Declarations

| Declaration | Gives the field |
| --- | --- |
| `Sequence(lambda n: ...)` | A value from the object's number. |
| `LazyFunction(function)` | What `function()` returns, called for each object. |
| `LazyAttribute(lambda obj: ...)` | A value from the other fields, read in any order; fields that depend on each other in a circle are refused. |
| `SubFactory(factory, **overrides)` | An object made by another factory. `create` stores it through the same unit of work when its factory names a repository; a value object is only built. |
| `SelfAttribute("customer.id", default=...)` | Another field, or its attribute. Each dot past the first climbs to the factory whose `SubFactory` made this object (`"..currency"`). `default=` is taken when the path is missing. |
| `Maybe("paid_at", yes, no)` | `yes` when the field is true, else `no`. The decider may be a declaration, and either branch a `SubFactory`. |
| `Trait(**overrides)` | In `class Params`: defaults turned on by `paid=True`. |
| `@post_generation` | A method run on each object once it is made. |
| `RelatedFactory(factory, related_name)` | Another object made afterwards, with this one as its field `related_name`. |
| `MoneyFactory` | A `domainkit.money.Money` of 1, 2, 3, ... whole units, in USD or the currency given ([money.md](money.md)). |

This example uses them all, on plain dataclasses, so you can run it
without a database:

```python
from dataclasses import dataclass, field
from datetime import UTC, datetime

from domainkit.money import Money
from domainkit.testing import (
    Factory,
    LazyAttribute,
    Maybe,
    MoneyFactory,
    SelfAttribute,
    Sequence,
    SubFactory,
    Trait,
    post_generation,
)


@dataclass
class Customer:
    id: int
    name: str


@dataclass
class Invoice:
    number: str
    customer_id: int
    total: Money
    status: str = "open"
    paid_at: datetime | None = None
    receipt: str = "none"
    notes: list[str] = field(default_factory=list)


class CustomerFactory(Factory[Customer]):
    class Meta:
        model = Customer

    id = Sequence(lambda n: 100 + n)
    name = Sequence(lambda n: f"Customer {n}")


class InvoiceFactory(Factory[Invoice]):
    class Meta:
        model = Invoice
        exclude = ("customer",)

    class Params:
        currency = "EUR"
        paid = Trait(status="paid", paid_at=datetime(2026, 1, 1, tzinfo=UTC))

    number = Sequence(lambda n: f"INV-{n:05}")
    customer = SubFactory(CustomerFactory)
    customer_id = LazyAttribute(lambda invoice: invoice.customer.id)
    total = SubFactory(MoneyFactory, currency=SelfAttribute("..currency"))
    status = "open"
    paid_at = None
    receipt = Maybe("paid_at", "sent", "none")

    @post_generation
    def notes(invoice, create, extracted, **options):
        for text in extracted or ():
            invoice.notes.append(text)


print(InvoiceFactory.build())
print(InvoiceFactory.build(paid=True, notes=["thanks"]))
print(InvoiceFactory.build(total__amount="5.00", currency="USD"))
print(InvoiceFactory.stub())
```

```text
Invoice(number='INV-00000', customer_id=100, total=Money('1.00', 'EUR'), status='open', paid_at=None, receipt='none', notes=[])
Invoice(number='INV-00001', customer_id=101, total=Money('2.00', 'EUR'), status='paid', paid_at=datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc), receipt='sent', notes=['thanks'])
Invoice(number='INV-00002', customer_id=102, total=Money('5.00', 'USD'), status='open', paid_at=None, receipt='none', notes=[])
namespace(number='INV-00003', customer_id=103, total=namespace(amount=Decimal('4'), currency='EUR'), status='open', paid_at=None, receipt='none')
```

- **`class Params`** holds fields that the model does not receive, which
  other fields read (`currency`), and **`Trait`s**:
  `InvoiceFactory.build(paid=True)` turns their defaults on, in the order
  they are declared. Arguments given beside it still win
  (`paid=True, status="refunded"`), and `total__amount=` keys reach a
  sub-factory. A trait's `total__amount` and a given `total__currency`
  both hold, as in factory_boy; a given `total=` replaces the trait's
  keys for it.
- **`post_generation`** methods run on each object once it is made:
  `(obj, create, extracted, **options)`, with what was given under the
  method's name (`notes=["thanks"]`) and its `name__key=` arguments.
- **`RelatedFactory`** makes another object afterwards, with this one as
  its field `related_name`; a value given under its name makes none:

  ```python
  from domainkit.testing import RelatedFactory


  class OrderFactory(Factory[Order]):
      class Meta:
          model = Order
          repository = "orders"

      first_line = RelatedFactory(LineFactory, "order")
  ```

  With `create`, `post_generation` and `RelatedFactory` both run after
  the object is added and before the commit, through the same unit of
  work. So what they change (and the events it records) and the objects
  they add are committed with it, in one transaction.
- **`stub()`** gives no model call, sub-factories give stubs, and no
  `post_generation` or `RelatedFactory` runs.
- A declaration given as an argument is computed as a default would be:
  `SubFactory(MoneyFactory, currency=SelfAttribute("..currency"))`.
- `MoneyFactory` counts on its own, so every currency's places fit:
  `MoneyFactory.build(currency="KWD")` gave `10.000 KWD`.
- There is no `Faker`: it would make the plugin depend on Faker. Use
  `Sequence` and `LazyFunction`, or call Faker from a `LazyFunction` in a
  project that installs it.

## Time

`domainkit.clock` is the one clock the framework reads: `now()` (aware,
UTC), `today()`, and `monotonic()`. Freeze it, and these follow: token
expiry (opaque tokens and JWTs), idempotency leases, rate windows counted
in the process, memory cache lifetimes, and the timestamps of audit
entries, events, messages, and evidence
([ADR 0037](adr/0037-one-clock-frozen-per-context.md)):

```python
from datetime import timedelta

from domainkit.testing import ClientFor, FreezeTime


def test_tokens_expire(freeze_time: FreezeTime, client_for: ClientFor) -> None:
    clock = freeze_time("2026-01-01T09:00:00Z")
    viewer = client_for("catalog.view_product")
    assert viewer.get("/api/v1/catalog/products").status_code == 200
    clock.tick(timedelta(days=14))
    answer = viewer.get("/api/v1/catalog/products")
    assert (answer.status_code, answer.json()["detail"]) == (401, "Token expired.")
```

- The `freeze_time` fixture freezes the clock until the test ends:
  `freeze_time(when=None, *, tick=False)`. `freeze_time()` freezes it
  now; `tick=True` starts it at the moment and lets it run on.
- It returns a `FrozenClock`. `clock.tick(seconds or timedelta)` moves it
  forward (1 second by default); `clock.move_to(moment)` jumps either way.
  Both return the new time. `clock.now()` reads it.
- Moments are ISO strings, dates, or datetimes; those without a time zone
  are UTC.
- `with freeze_time("2026-01-01") as clock:` (from `domainkit.clock` or
  `domainkit.testing`) freezes it for a block, in any code.
  `clock.start()` and `clock.stop()` do the same without a block.
- The frozen clock belongs to the context that froze it, like the request
  context: tasks started from it see it; other tests and concurrent
  requests do not.
- Domain code may read `domainkit.clock` too (`from domainkit.clock import
  now`; the layer rules allow it), and then follows the same freeze.
- **The database's clock is the database's**: `now()` in SQL, a column's
  `server_default`, and shared rate limits counted on the Redis server are
  never frozen. Do not compare them with the frozen clock.
- `monotonic()` stands still with a frozen clock and moves forward with
  it, never back. Its frozen count starts at a whole second, so ticks of
  whole seconds add exactly, and a rate limit counted in this process lets
  exactly its limit through at one frozen moment. Intervals measured
  across the start or end of a freeze are meaningless.

## Settings

The `settings` fixture changes settings until the test ends, as
`override_settings` changes them for a block, and as pytest-django's
`settings` fixture does:

```python
from domainkit.testing import TestClient


def test_debug_pages(settings, client: TestClient) -> None:
    settings.DEBUG = True
    del settings.CACHES  # Its default, for this test.
    assert settings.DEBUG is True
```

- **Validated.** Each change is validated with the whole settings, as at
  startup. A value they refuse fails the assignment:

  ```text
  domainkit.exceptions.ImproperlyConfigured: Invalid settings override:
    settings: Value error, rate 'lots' must look like '100/minute' (second, minute, hour, day)
  ```

- **Read-only values.** Reading `settings.DEBUG` reads the current
  setting, read-only: mappings are `MappingProxyType`s and lists are
  tuples, all the way down. So `settings.CACHES["reports"] = {...}` raises
  `TypeError: 'mappingproxy' object does not support item assignment`,
  instead of changing the settings of every later test unvalidated
  (TEST-19). Assign the whole setting:
  `settings.CACHES = {**settings.CACHES, "reports": {...}}`.
- **Undone.** Changes are undone in reverse order when the test ends.
- **Before the application starts.** The application reads some settings
  once, when a client starts it: its middleware, its authentication
  classes, its routes. Change those in a fixture that the client fixture
  comes after, as the `cookies` fixture in
  [A browser session](#a-browser-session) does.

The fixture's type is `SettingsOverrides`.

## Counting queries

`assert_num_queries(n)` fails unless the block runs exactly `n` SQL
statements, and `assert_max_queries(n)` unless it runs at most `n`. Both
return a `CapturedQueries`, whose `statements` lists what ran, as SQL with
placeholders; bound values are never kept:

```python
from domainkit.testing import TestClient, assert_max_queries

URL = "/api/v1/catalog/products"


def test_a_second_listing_costs_one_query(admin_client: TestClient, assert_num_queries) -> None:
    admin_client.get(URL)  # reads the token and the user, once
    with assert_num_queries(1) as queries:
        admin_client.get(URL)
    assert "ORDER BY" in queries.statements[0]


def test_a_listing_runs_few_queries(admin_client: TestClient) -> None:
    with assert_max_queries(2):
        admin_client.get(URL)
```

The second test fails, and lists what ran (shortened here):

```text
E   AssertionError: Expected at most 2 queries, 4 ran:
E   1. SELECT domainkit_tokens.id, domainkit_tokens.user_id, ... FROM domainkit_tokens WHERE domainkit_tokens.id = ?
E   2. SELECT domainkit_users.id AS u0, ... FROM domainkit_users LEFT OUTER JOIN (...) AS granted ON 1 = 1 WHERE domainkit_users.id = ?
E   3. UPDATE domainkit_tokens SET last_used_at=? WHERE domainkit_tokens.id = ? AND (...)
E   4. SELECT catalog_products.id, catalog_products.version, catalog_products.name FROM catalog_products ORDER BY catalog_products.id ASC LIMIT ? OFFSET ?
```

- They count the statements of every engine of the process, in every
  thread and event loop, so a `TestClient`'s requests count.
  `using="reports"` counts one alias only.
- Each statement sent to the driver counts once, an `executemany` too.
  Connecting and committing do not, nor the timeouts that a PostgreSQL
  transaction sets for itself under a deadline or PgBouncer
  ([runtime.md](runtime.md#what-keeps-to-it)).
- An authenticated request costs a few statements of its own the first
  time (the token, the user, its groups and permissions); the principal
  cache keeps the user for later requests. So prefer `assert_max_queries`
  for whole requests and `assert_num_queries` for code you call
  directly.
- Without the fixtures, import them from `domainkit.testing`.
  `CapturedQueries(expected=None, *, at_most=False, using=None)` with no
  count only captures.

Importing `domainkit.testing` (the plugin does, as pytest starts) puts
one listener on SQLAlchemy's `Engine` class, and it stays. Blocks only
come and go from the list it hands statements to. SQLAlchemy reads its
listeners without a lock as each statement runs, so a listener added or
removed while another thread runs a statement fails that statement
("deque mutated during iteration"). Until 0.10, each block added and
removed its own, which now and then failed a request or a background task
running at that moment.

## Events and queued work

Fixtures for domain events and background work come with messaging's own
plugin, `-p domainkit.contrib.messaging.testing`, which projects made by
`domainkit init` enable too:

```python
from domainkit.testing import TestClient

from domains.catalog.domain import ProductRenamed

URL = "/api/v1/catalog/products"


def test_renaming_publishes(admin_client: TestClient, captured_events) -> None:
    item = admin_client.post(URL, json={"name": "Lamp"}).headers["location"]
    admin_client.post(f"{item}/rename", json={"name": "Desk lamp"})
    captured_events.assert_published(ProductRenamed, name="Desk lamp")
```

- `captured_events`: the domain events published after commits;
  `of_type`, `assert_published`, `assert_not_published`.
- `outbox`: what was queued; `assert_queued`, `assert_not_queued`, and
  `run_queued()` or `await arun_queued()` to run it here, as the worker
  would.
- `execute_deferred`: runs each commit's queued work right after the
  commit.

They follow the frozen clock: `outbox.run_queued(delayed=True)` makes
waiting work due at the clock's time. The full list is in
[messaging.md](contrib/messaging.md#testing). Mail, text messages, and
push notifications land in `mail.outbox`, `sms_outbox`, and `push_outbox`
([mail.md](mail.md#testing)).

## Moving money: second factors, approvals, the trail, key services

A project that moves money tests flows that a person completes with an
authenticator app. `domainkit.testing` does each step through the
application's own routes, with a sync client, as the person would. The
routes are where `domainkit init --profile fintech` mounts them
(`/auth`), or at `prefix=`:

```python
from domainkit.contrib.auth.testing import create_user
from domainkit.testing import TestClient, approve_request, confirm_sca, enrol_totp, step_up

URL = "/api/v1/payments"
PASSWORD = "correct-horse-battery-42"


def test_a_large_transfer_needs_an_approver(client: TestClient, freeze_time) -> None:
    freeze_time("2027-01-04T10:00:00Z")  # codes need a clock that moves on
    maker = client.run(create_user, "payments.add_transfer", password=PASSWORD)
    as_maker = client.authenticated_as(maker)
    app = enrol_totp(as_maker, PASSWORD)
    step_up(as_maker, app)
    body = {"card_id": card_id, "amount": {"amount": "5000.00", "currency": "USD"}}
    asked = as_maker.post(f"{URL}/transfers/authorize", json=body)
    authorization = confirm_sca(as_maker, asked.json()["challenge"], app)
    pending = as_maker.post(
        f"{URL}/transfers", json=body, headers={"X-Authorization": authorization}
    )
    assert pending.status_code == 202
    # An approver, with an app of their own, steps up and approves it:
    approve_request(as_approver, f"/api/approvals/requests/{pending.json()['approval_request']}")
```

The fintech profile's `tests/people.py` wraps these steps in a `Person`
class; `tests/payments/test_transfers.py` uses it.

| Helper | What it does |
| --- | --- |
| `totp_code(secret, at=None)` | The code an authenticator app shows: RFC 6238 as `domainkit.contrib.auth` checks it (HMAC-SHA1, 30 seconds, six digits). `secret` is raw bytes or base 32. `totp_code("JBSWY3DPEHPK3PXP", at="2026-01-01T00:00:00Z")` is `'260025'`. |
| `enrol_totp(client, password, *, prefix="/auth")` | Turns the app on for the client's user, with the user's password, and returns an `Authenticator`. |
| `Authenticator.code()` | A code that never repeats a 30-second step: the service refuses a step it has seen, so a second code in one step moves a frozen clock to the next step's start. It refuses without a frozen clock. |
| `step_up(client, app, *, prefix="/auth")` | Steps the user up with a code. |
| `confirm_sca(client, challenge, app, *, prefix="/auth")` | Confirms an SCA challenge with a code; returns the authorization. |
| `approve_request(client, url)` | Reads the approval request's fingerprint, then approves that version; returns the answer. |

A refused step raises `UnexpectedResponse`, an `AssertionError` that
names the step, the status, and the body. In a project without the MFA
routes, `enrol_totp` fails like this:

```text
E   domainkit.testing.authenticators.UnexpectedResponse: Starting TOTP enrolment: 404 {"type":"about:blank","title":"Not Found","status":404,"detail":"Not Found","instance":"/auth/mfa/totp","code":"not_found","request_id":"ed523ac7b0e92c7b0068e38812920702"}
```

**The audit trail.** `domainkit.contrib.audit.testing.audit_entries(action=,
actor=, target=)` reads the trail as `RecordedEntry` records, oldest
first:

```python
from domainkit.contrib.audit.testing import audit_entries
from domainkit.testing import TestClient


def test_the_transfer_is_audited(client: TestClient) -> None:
    ...  # make the transfer, as above
    (entry,) = client.run(audit_entries, action="payments.transfer_made")
    assert entry.outcome == "success"
```

**Key services.** `domainkit.testing.kms` has `FakeKms` (AWS KMS's JSON
API, which checks each request's SigV4 signature) and `FakeVault`
(Vault's transit engine). They are handlers for
`domainkit.http.client.stub`, so `KEYS["PROVIDER"] = "aws-kms"` or
`"vault"` can be tested without the service:

```python
from collections.abc import Iterator

import pytest

from domainkit.http.client import stub
from domainkit.testing.kms import FakeKms, FakeVault


@pytest.fixture
def kms() -> Iterator[FakeKms]:
    fake = FakeKms()  # accepts AWS's example access key, SECRETS
    fake.create("k-1", alias="alias/payments")
    with stub(fake):
        yield fake


@pytest.fixture
def vault() -> Iterator[FakeVault]:
    fake = FakeVault()  # checks X-Vault-Token against fake.token
    fake.create("payments")
    with stub(fake):
        yield fake
```

`kms.rotate("k-1")` gives a key a new version, and `kms.calls` lists the
operations asked, in order. The fintech profile's tests are written with
these helpers (TEST-21).

## Outgoing calls

Code that calls other services through `domainkit.http.client` is tested
without them. `stub(handler)` sends every call made in its block (and in
tasks it starts) to `handler`, a function of an `httpx2.Request` that
returns an `httpx2.Response` ([runtime.md](runtime.md#outgoing-http-calls)):

```python
import httpx2

from domainkit.http.client import stub
from domainkit.testing import TestClient


def rates(request: httpx2.Request) -> httpx2.Response:
    assert request.url.host == "rates.example.com"
    return httpx2.Response(200, json={"EUR": "0.92"})


def test_prices_in_euros(admin_client: TestClient) -> None:
    with stub(rates):
        answer = admin_client.get("/api/v1/catalog/prices?currency=EUR")
    assert answer.status_code == 200
```

Both clients carry the block's context into the application, so calls
made while serving a request are stubbed too.

## Blocking the event loop

`pytest --detect-blocking` fails a test that holds its event loop for 0.1
seconds or more in one step: a sync HTTP client, `time.sleep`, or a
CPU-heavy loop inside `async def`. `--blocking-seconds` raises the limit:

```python
import time

import pytest

from domainkit.testing import AsyncTestClient


@pytest.mark.anyio
async def test_blocks(async_client: AsyncTestClient) -> None:
    time.sleep(0.2)  # blocks the loop: use await anyio.sleep(0.2)
```

```text
The test held its event loop for 0.1s or more (--detect-blocking):
  Executing <Task pending name='Task-101' coro=<TestRunner._run_tests_and_fixtures() running at .../anyio/_backends/_asyncio.py:2395> ...> took 0.201 seconds
```

Every loop that the tests start runs in asyncio's debug mode, the test's
async loop and the clients' alike. Fixtures are not timed. The failure
quotes asyncio's report of the slow step. Debug mode also reports
resources left open, as `ResourceWarning`; with `filterwarnings = error`,
those fail tests too. See
[runtime.md](runtime.md#finding-blocking-code).

## Error reports and logs

Error reporters (`ERROR_REPORTERS`) and the logging thread work in the
background ([runtime.md](runtime.md#logging-and-error-reports)). What a
test reported is sent, and what it logged is written, before the next
test starts. To check what a reporter received inside the test, call
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

## In generated projects

`domainkit init` writes `tests/test_health.py` with a sync and an async
test. The tests that `startdomain` writes call the API with the client
fixtures (`client`, `admin_client`, `client_for`), in
`tests/domains/<domain>/test_api.py`, and the use cases with an in-memory
unit of work, in `tests/domains/<domain>/test_controllers.py`. A domain's tests can also
define factories next to its in-memory fakes
(`tests/<domain>/factories.py`), pass a fake unit of work to `create` in
use-case tests, and let `create` use the SQL unit of work in API tests.

## Options

| Option | Where | What it does |
| --- | --- | --- |
| `domainkit_settings` | `[tool.pytest.ini_options]` | The test settings module. |
| `domainkit_allow_database` | `[tool.pytest.ini_options]` | `true`: every test reaches the databases, as in 0.12. |
| `--keepdb` | command line | Keep the PostgreSQL test databases for the next run. |
| `--detect-blocking` | command line | Fail a test that holds its event loop. |
| `--blocking-seconds` | command line | The limit for `--detect-blocking` (0.1 seconds, and no less). |
| `@pytest.mark.allow_database` | a test or a module | That test reaches the databases without `db`. |
| `@pytest.mark.anyio` | a test or a module | Runs an async test. |

## Errors and refusals

| What you see | Why | What to do |
| --- | --- | --- |
| `DatabaseAccessRefused` | The test reached the database without requesting it. | Request `db` or a client fixture; in a wider fixture, use `database_blocker.unblock()`. |
| The run refuses the settings | `ENVIRONMENT` is not `"test"`. | Set it in the test settings. |
| A role is named and the run stops | A row-level role in `URL` does not exist on the server. | Create the role. |
| `AssertionError: Expected at most N queries, M ran:` | The block ran more statements. | Read the listed statements; add a join or a cache. |
| `ImproperlyConfigured: Invalid settings override` | The `settings` fixture got a value the settings refuse. | Fix the value. |
| `TypeError: 'mappingproxy' object does not support item assignment` | You changed a setting in place. | Assign the whole setting. |
| `UnexpectedResponse` | A step of `enrol_totp`, `step_up`, `confirm_sca`, or `approve_request` was refused. | Read the status and the body it names. |
| `ImproperlyConfigured` from `browser_session_as` | `CookieAuthentication` is not in `AUTH.AUTHENTICATION_CLASSES`. | Add it, in a fixture before `client`. |
| "The test held its event loop" | Blocking code in an async test, with `--detect-blocking`. | Await an async call, or move the work to a thread. |

## Common mistakes

- **Reaching the database without `db`.** The test is refused. Request
  `db`, or a client fixture, which requests it.
- **Changing a startup setting in the test body.** The client already
  built the application. Change it in a fixture that `client` comes
  after.
- **Asserting on generated ids.** Sequences keep counting between tests.
  Assert on the fields you set.
- **Comparing the database's `now()` with the frozen clock.** The
  database's clock is never frozen. Compare values that your code wrote.
- **Sending mail from a module-scoped fixture.** The outbox is emptied
  before each test. Send it in the test.

## Compared with pytest-django, DRF, Litestar, and factory_boy

What their testing tools give, and where domainkit stands as of 1.0.
Rows marked 0.12 and 1.0 were added for the 0.9.0 audit's TEST-08.

| They give | domainkit |
| --- | --- |
| pytest-django's `db`, `django_db_setup`, `--reuse-db` | `db`, `database`, `--keepdb`; a database per xdist worker |
| pytest-django's `django_db_blocker` and its `db` mark | the databases refused to a test that did not request `db`; `database_blocker`; `allow_database` and `domainkit_allow_database` to allow them as before (1.0) |
| pytest-django's `live_server`; Django's `LiveServerTestCase` | `live_server`, `LiveServer(app)` (1.0) |
| pytest-django's `client`, `admin_client`, `async_client` | `client`, `admin_client`, `async_client`, `async_admin_client` |
| pytest-django's `django_assert_num_queries`, `django_assert_max_num_queries` | `assert_num_queries`, `assert_max_queries` |
| pytest-django's `mailoutbox`; Django's emptied `mail.outbox` | `mail.outbox`, `sms_outbox`, and `push_outbox`, emptied before each test |
| pytest-django's `django_capture_on_commit_callbacks` | messaging's `outbox` (`assert_queued`, `run_queued`), `execute_deferred`, `captured_events` |
| pytest-django's `settings` | `settings` (0.12) |
| pytest-django's `admin_user`, `django_user_model`; factory_boy's model factories for users | `create_user`, `create_group` (0.12); `client_for(...)` for a new user per client |
| DRF's `force_authenticate`, `force_login` | `authenticated_as(user)`, `token_for(user)` (0.12) |
| Litestar's `set_session_data` | `browser_session_as(user)`, `browser_session_for(user)` (1.0) |
| DRF's `APIClient.credentials` | `client.another(headers)` |
| Litestar's `create_test_client`, `AsyncTestClient` | `TestClient(app)`, `AsyncTestClient(app)` for any ASGI application |
| freezegun, time-machine | `freeze_time`, which the application, tokens, and the outbox follow |
| pyotp in tests; moto for KMS | `totp_code`, `enrol_totp`, `step_up`, `confirm_sca`, `approve_request`; `domainkit.testing.kms` (1.0) |
| factory_boy's `Factory`, `Sequence`, `LazyFunction`, `LazyAttribute`, `SubFactory`, `build_batch`, `create_batch` | the same names |
| factory_boy's `Trait`, `post_generation`, `RelatedFactory`, `SelfAttribute`, `Maybe`, `stub()` | the same names, run through the unit of work before its commit (1.0) |

Not given, each with what to do instead:

- **A request factory** (Django's `RequestFactory`, DRF's
  `APIRequestFactory`): call dependencies with the objects tests build,
  or go through `client`.
- **factory_boy's `Faker`**: it would make the plugin depend on Faker.
  Use `Sequence` and `LazyFunction`, or call Faker from a `LazyFunction`
  in a project that installs it.

## See also

- [The test plugin reference](reference/test-plugin.md): the plugin in
  short.
- [Messaging](contrib/messaging.md#testing): `captured_events`, `outbox`,
  and `execute_deferred`.
- [Live connections](live.md#testing): WebSockets and event streams with a
  frozen clock.
- [Runtime](runtime.md#finding-blocking-code): finding code that blocks
  the event loop.
- [The tutorial's testing step](tutorial/5-testing-and-deploying.md): tests
  in a new project, step by step.
