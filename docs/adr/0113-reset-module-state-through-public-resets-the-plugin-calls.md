# 0113. Reset module state through public resets the plugin calls

- Status: Accepted
- Date: 2026-10-29

## Context

The framework keeps state in module-level singletons
([0090](0090-module-singletons-overridden-per-test.md)), so tests put it
back between them. The 0.9.0 audit's TEST-05 found the autouse fixtures
reaching into private names to do it. At 0.12 the plugin used public
calls only (`caches.reset()`, the outboxes' lists, `reset_sequence()`),
but put back less than a project's tests need: a test that subscribed a
handler, registered a task or a metric refresher, or made a throttled log
hold a line back left it for every later test (proven on 0.12.0 by a
project's tests). This repository's conftest undid those through
the log module's `_throttled_logs` and `ThrottledLog._seen`,
`messaging.registry._tasks`, `EventBus._handlers`, and
`metrics._refreshers`: a project that wanted the same had to copy it.

Options considered:

- **A public reset on each holder**, named for what it does, which the
  plugin calls: `reset_throttled()`, `EventBus.subscriptions_kept()`,
  messaging's `tasks_kept()`, `metrics.refreshers_kept()`. Optional
  modules (messaging, metrics, auth's principal cache) are found through
  `sys.modules`, as the outboxes already were, so the plugin imports no
  contrib app.
- **A registry of resettable state**: each module registers its reset
  when imported, and the plugin calls whatever is registered; projects
  register theirs. One API, and new state takes part by itself. But it
  is a new module that `domainkit.log`, `ddd.events`, and the others
  import, so every process loads it (the PERF-15 gates count the modules
  each rung loads), and one more piece of global state.

When to run them: an autouse fixture each costs every test (about 180 µs
for one with a session dependency); one `pytest_runtest_protocol` hook
calls them all for a few calls a test.

When to keep registrations: the domains register their handlers and
tasks in `ready()`, the first time a test serves the application. Putting
the registrations back after that test would drop them for the rest of
the run.

## Decision

- Each piece of state a test can leave behind has a public reset, in the
  module that keeps it, documented in `docs/api.md`:
  `domainkit.log.reset_throttled()` and `ThrottledLog.reset()` (forget,
  writing nothing), `EventBus.subscriptions_kept()`,
  `domainkit.contrib.messaging.tasks_kept()`, and
  `domainkit.http.metrics.refreshers_kept()` (context managers that put
  the registrations back when the block ends).
- The plugin calls them from its `pytest_runtest_protocol` hook: before
  each test, the throttled logs, the principal cache, memory ledgers
  (`LedgerHandler.reset()`), and storages (`storages.reset()`) forget;
  the registrations are kept from the start of the test if the domains
  are loaded then, or else from the start of its call (`pytest_runtest_call`),
  once its fixtures loaded them, and put back after its teardown. A test
  whose body loads the domains keeps what it registers: its registrations
  cannot be told from `ready()`'s (SEC-13b-2). Modules not imported are
  passed over.
- `docs/testing.md` lists what each test starts from, and the reset
  behind each line. The repository's conftest uses the same names.

## Consequences

- No fixture of the plugin or of this repository's conftest touches
  another module's private name.
- New module state that a test can leave behind needs its own public
  reset, and a line in the plugin's hook and in `docs/testing.md`. A
  project's own module state is the project's to reset; a registry
  remains the way to let projects join, and would supersede this record.
- The resets are public names: they follow the deprecation policy.
