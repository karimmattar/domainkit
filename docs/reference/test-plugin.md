# The test plugin

`domainkit.testing` is a pytest plugin, like pytest-django. Install it with
the `testing` extra, which brings pytest and httpx2, as a development
dependency, and enable it in the project's `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-p domainkit.testing"
domainkit_settings = "config.settings.test"
```

It selects the test settings, never reads `.env`, and refuses settings whose
`ENVIRONMENT` is not `"test"`. Its fixtures make the test databases
(`test_<name>` on PostgreSQL, dropped after the run unless `--keepdb`; see
`TEST_NAME`), put them back before each test (`db`), and serve the
application to sync and async tests (`client`, `client_for`, `admin_client`,
`async_client`, `async_client_for`, `async_admin_client`). It also brings
factories, a clock to freeze (`freeze_time`), and query-count assertions
(`assert_num_queries`, `assert_max_queries`). `--detect-blocking` fails
tests that hold their event loop. [docs/testing.md](../testing.md) covers them
all.

Fixtures for published events and queued work (`captured_events`,
`outbox`, `execute_deferred`) come with messaging's own plugin
([contrib.md](../contrib/messaging.md#testing)).
