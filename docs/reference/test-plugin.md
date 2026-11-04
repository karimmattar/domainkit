# The test plugin

`domainkit.testing` is a pytest plugin, like pytest-django. It selects
the test settings, makes the test databases and puts them back before
each test, and serves your application to the tests. It also brings
factories, a clock you can freeze, and checks of how many queries code
runs. This page lists what the plugin gives; [testing.md](../testing.md)
shows each part at work.

On this page you find:

- how to turn the plugin on;
- its fixtures, its command-line options, and its ini options;
- how a test gets the databases.

## Turning it on

Install domainkit with the `testing` extra, which brings pytest and
httpx2, as a development dependency. Then enable the plugin in the
project's `pyproject.toml`. The project that `init` writes has these
lines already:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
addopts = "-p domainkit.testing -p domainkit.contrib.messaging.testing"
domainkit_settings = "config.settings.test"
```

## A first example

```python
"""The health check answers; settings and the clock change for one test."""

from domainkit import clock
from domainkit.http import get_asgi_application
from domainkit.testing import FreezeTime, SettingsOverrides, TestClient


def test_health_answers(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_documentation_can_be_turned_on(settings: SettingsOverrides) -> None:
    settings.API_DOCS = True
    with TestClient(get_asgi_application()) as client:
        assert client.get("/openapi.json").status_code == 200


def test_the_clock_stands_still(freeze_time: FreezeTime) -> None:
    freeze_time("2026-01-01T09:00:00Z")
    assert clock.now().isoformat() == "2026-01-01T09:00:00+00:00"
```

```bash
pytest tests/test_plugin_demo.py
```

```text
...                                                                      [100%]
3 passed in 1.51s
```

## What the plugin does for every run

- It selects the settings that `domainkit_settings` names, and never
  reads `.env`.
- It refuses settings whose `ENVIRONMENT` is not `"test"`.
- It makes the test databases once per run, when a test first needs them
  (`test_<name>` on PostgreSQL;
  see `TEST_NAME`), and drops them after the run unless you pass
  `--keepdb`.
- Before each test, it empties the memory caches and the outboxes, and
  starts the factories' sequences again.

## Fixtures

| Fixture | What it gives |
| --- | --- |
| `db` | Puts the test databases back as `migrate` left them, and lets the test reach them. |
| `client` | A `TestClient` that serves the project's application on the test database (it requests `db`). |
| `client_for` | Makes clients that log in as new users with the permissions you name: `client_for("catalog.add_product")`. |
| `admin_client` | A client that logs in as a new superuser. |
| `async_client`, `async_client_for`, `async_admin_client` | The same, for `async` tests, in the test's event loop. |
| `live_server` | Serves the application on a port of the test process, for one test ([testing.md](../testing.md#database-access)). |
| `settings` | Changes settings until the test ends: `settings.DEBUG = True`. Changes are undone in reverse order. |
| `freeze_time` | Freezes the framework's clock until the test ends: `freeze_time("2026-01-01T09:00:00Z")` returns the clock, which `tick()` and `move_to()` move. |
| `assert_num_queries`, `assert_max_queries` | `with assert_num_queries(3):` checks that the block runs exactly that many statements, or at most that many. |
| `database` | The migrated test database's URL, for the whole session. |
| `database_blocker` | Lets a fixture of your own reach the databases: `with database_blocker.unblock():`. |

Fixtures for published events and queued work (`captured_events`,
`outbox`, `execute_deferred`) come with messaging's own plugin,
`domainkit.contrib.messaging.testing`
([messaging](../contrib/messaging.md#testing)).

`domainkit.testing` also exports factories (`Factory`, `SubFactory`,
`Sequence`, ...), clients (`TestClient`, `AsyncTestClient`), and helpers
for logins (`user_token`, `token_for`, `enrol_totp`, `totp_code`,
`step_up`, `confirm_sca`, `approve_request`, `browser_session_for`). The
[API page](../api.md) lists every name.

## Options

| Option | What it does |
| --- | --- |
| `--keepdb` | Keeps the PostgreSQL test databases for the next run, and only migrates them up. |
| `--detect-blocking` | Fails tests that hold their event loop (asyncio's debug mode, for every loop the tests start). |
| `--blocking-seconds SECONDS` | How long one step may hold the loop under `--detect-blocking`. |

| Ini option | What it does |
| --- | --- |
| `domainkit_settings` | The settings module of the tests, as `config.settings.test`. |
| `domainkit_allow_database` | `true` lets every test reach the databases without requesting `db`, as before 0.13 (default: `false`). |

## Database access

A test that did not request `db` is refused the databases
(`DatabaseAccessRefused`). Two ways allow them, as 0.12 did:

- `@pytest.mark.allow_database` on a test: it may reach the test
  databases without requesting `db`, but they are not put back first;
- the ini option `domainkit_allow_database = true`, for every test.

```python
import pytest


@pytest.mark.allow_database
def test_reads_what_the_session_fixture_wrote() -> None: ...
```

## See also

- [Testing](../testing.md): factories, clients, the clock, and query
  counts.
- [Settings](settings.md#debug-toolbar): find a request's queries, then
  keep them in check with `assert_max_queries`.
