# 0090. Keep framework state in module-level singletons, overridden per test

- Status: Accepted
- Date: 2026-10-26

This records a decision taken in 0.1.0 with
[0002](0002-django-conventions-for-ddd-services.md), which no record
described (DSGN-07 and DSGN-15 of the 0.9.0 design audit). It changes
nothing in the code.

## Context

Django keeps its state in module-level objects: `settings`, the app
registry, the database connections, the caches. domainkit followed it:
`domainkit.conf.settings`, `domainkit.apps.registry`, `ddd.event_bus`,
`db.routing.router`, `db.orm.metadata` and `mapper_registry`,
`db.connections.connections`, `cache.caches`, `broker.brokers`,
`ledger.ledgers`, and the closers of `domainkit.lifecycle`. At 0.9.0,
72 modules read `domainkit.conf` and 55 the registry.

Tests change that state in place: `override_settings` and `use_settings`
swap the settings object for the whole process, and `registry.clear()`
then `populate()` reload the domains.

What it costs: two applications with different settings cannot share a
process; libraries built on domainkit cannot be wired explicitly; and
state shared by event loops on several threads (free-threaded Python, a
loop per thread) must be guarded wherever it lives. Litestar, NestJS, and
Spring avoid this with an application object passed around.

Options considered:

- **An application object** (`Framework`: settings, registry, event bus,
  connections, caches, brokers, ledgers, lifecycle), built by `setup()`,
  reachable from the request, with the module-level names as proxies to
  the current one in a `ContextVar`. Two configurations per process, and
  `override_settings` as a context-local overlay. A branch of work across
  the whole framework, before 1.0.
- **Keep the singletons**, and make the state safe to share.

## Decision

- The framework keeps its state in module-level singletons, as Django does.
  Code reads them where it needs them.
- What event loops on several threads share is guarded by a lock, and a
  test in `domainkit_tests/test_free_threading.py` drives it from several
  threads: the registry's settings cache, each handler's per-loop state,
  the hook points of [0085](0085-register-hooks-by-type-for-the-domains-that-have-them.md).
- What belongs to one event loop (engines, cache clients, brokers, owned
  tasks) is kept per loop (ADR 0045).
- `override_settings` and `use_settings` swap the settings for the whole
  process. Tests that override settings run one at a time in a process;
  parallel tests use processes (xdist), not threads.
- Caches derived from settings remember the settings object they were
  derived from, and are worked out again when it changes
  (`registry.settings`).
- One process serves one configuration.

## Consequences

- The Django mental model holds, and so does every import path.
- Running two configurations in one process, or overriding settings per
  thread, is not supported. An application object, with the singletons as
  proxies, remains the way to it, and would supersede this record.
- Each new piece of shared state needs its lock and its free-threading
  test.
