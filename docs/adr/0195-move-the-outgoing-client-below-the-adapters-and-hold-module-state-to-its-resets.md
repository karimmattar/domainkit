# 0195. Move the outgoing client below the adapters, and hold module state to its resets

- Status: Accepted
- Date: 2026-11-18
- Amends: [0003](0003-enforced-layer-rules.md), [0050](0050-outgoing-http-client.md), [0113](0113-reset-module-state-through-public-resets-the-plugin-calls.md), [0188](0188-write-down-the-cores-layering-and-hold-the-boundaries-it-draws.md)

## Context

ADR 0188 wrote the core's layering down and left 21 imports that point
up. Sixteen were adapters (keys, storage, cloud, search, documents, the
SQS broker) importing the outgoing HTTP client, which ADR 0050 put in the
serving package, `domainkit.http` (A28-ARCH-04). 0188 kept the client
where it was while other 2.9 work edited those adapters. That work now
lets this one change their import lines.

Moving `domainkit.http.client` moves `domainkit.http.guard` with it: the
client imports the guard at its top. The guard itself reached up twice,
inside functions:

- it registered its lookup threads with `domainkit.lifecycle` (serving)
  at its first lookup;
- it named an anonymous request's caller by the rate limit's
  `client_group` (`domainkit.http.ratelimit`), so one IPv6 network is one
  owner of lookups.

Three more follow-ups of 0188 were left:

- `domainkit.conf.global_settings` imported the import checker,
  `domainkit.architecture`, for its four layer names. Importing
  `domainkit.conf` loaded it.
- `ALLOWED_CORE` held 101 core names that apps import and no `__all__`
  publishes. The most used was `DEPLOYED_ENVIRONMENTS`, by 14 apps
  (A28-ARCH-06).
- Tests reached into module state: 34 lines popped tasks out of
  `messaging.registry._tasks`, and others cleared the push backends'
  tokens. Nothing failed when a module gained new state without a reset,
  which ADR 0113 asks of state a test can leave behind (A28-ARCH-14).

Constraints: 2.9 is a minor release, so every import a project makes
keeps working. The settings module and the rate limit's are edited by
other 2.9 work, so this change touches only their import lines. The
lists of 0188 only shrink.

How others do it:

- A module may define `__getattr__`, which Python calls for a name the
  module does not hold, `from` imports included (PEP 562) [1]. Pydantic 2
  answers the names it moved with it, and warns [2].
- `unittest.mock` patches a name where it is looked up, not where it is
  defined [3]: a name re-exported by another module is a second name for
  the same object, and patching it changes nothing the code reads.
- import-linter's layers contract checks that a higher layer is never
  imported by a lower one [4], as `test_core_layers.py` does.
- Celery's task registry has `unregister(name)` [5].

## Decision

### `domainkit.outbound`

The client and the guard move to a package of their own:
`domainkit.outbound.client` and `domainkit.outbound.guard`. It is a layer 2
package, with the adapters: its imports are of layers 0 and 1 (the clock,
deadlines, exceptions, tracing, utils; settings, the request context, the
domain's errors), so nothing in it points up, and it is the adapter the
other adapters call through. Its `__init__` imports neither module, so a
process that calls no service needs no `httpclient` extra.

- **The guard registers nothing.** `domainkit.lifecycle` closes the
  guard's resolver at shutdown, as one of its own kinds, `guard lookups`,
  found in `sys.modules` as it finds the HTTP clients. A second resolver
  no longer replaces the first one's closer. `register("guard lookups",
  ...)` now raises `ValueError`, as for the framework's other kinds.
- **A caller's address group** is `domainkit.context.client_group`, public,
  with the rate limit's rule: IPv4 as is, IPv4-mapped IPv6 as IPv4, IPv6
  by its `RATE_LIMIT_IPV6_PREFIX` network, each remembered for the latest
  4,096 hosts. The guard and auth's throttles use it.
  `domainkit.http.ratelimit` keeps its own copy until the other work on
  that module is done; a test gives both every host and four prefixes,
  and asserts one answer. The two cache constants move to
  `domainkit.context` too; `domainkit.http.middleware` imports them.
- **The guard's public names**, decided anew in the new package: its
  `__all__` lists what an app that takes URLs from its users needs, as
  webhooks does: `checked_url`, `checked_host`, `checked_address`,
  `lookup`, `lookups_for`, `tls_context`, `UnsafeURL`, `MAX_URL_LENGTH`,
  and the `Address` and `Network` types. 1.0 took the old module's names
  out of the API because no page used them; the runtime guide and an app
  use these. The resolver, its bounds, and the address tables stay
  internal.
- **The logger keeps its name**, `domainkit.http.client`: logging
  settings and alerts name it, and a new name would end them without a
  word.

### The 2.8 paths

`domainkit.http.client` and `domainkit.http.guard` stay, as modules that
import every name of the new ones: the public names by name, for type
checkers, and any other through a module `__getattr__`. Each is the same
object, so `is`, `isinstance`, and `except` hold either way, and there is
one set of clients, circuits, metrics, and lookup threads. They are not
deprecated in 2.9; a later minor release may deprecate them, with a
warning, before 3.0 removes them. `domainkit.http.client` keeps its
`__all__` and its section of the API pages; `domainkit.http.guard` stays
without one, internal as it was.

A name set on an old path (a test's `monkeypatch.setattr`) is not one the
code reads: patch `domainkit.outbound.client` or
`domainkit.outbound.guard`. What prints a class by its module prints the
new one.

Every import the framework makes is of the new package, its tests'
included, but for the tests of the old paths themselves. The plugin
resets `domainkit.outbound.client`'s circuits, and the lifespan closes its
pools, found by that name: a process that imports only the new path, as
the adapters do now, gets both.

### The layer rules

A project's infrastructure layer imports `domainkit.outbound` with no
exception: no rule names it. The application layer may not
(`FORBIDDEN_LIBRARIES["application"]` adds `domainkit.outbound`): it could
not call out through `domainkit.http.client`, and it cannot by the new
path either. The domain layer's allowlist leaves it out. The exception for
`domainkit.http.client` in `ALLOWED_MODULES` stays, and so does
`architecture.W001` for the names its `__all__` does not list: a project
that imports the old path changes nothing in 2.9.

### The settings and the checker

`ArchitectureConfig` imports the checker's layer names when it validates
`EXTRA_LAYERS`, not at the top of the settings module: importing
`domainkit.conf` loads no checker. Loading settings still does, since the
default `ARCHITECTURE` is validated too. Defining the four names in the
settings module would end that; it waits for the other work on that
module.

### Names the apps used

Published, each in its module's `__all__` and on its API page, and
imported from there by the apps: `domainkit.conf.DEPLOYED_ENVIRONMENTS`,
`domainkit.http.CURSOR_PATTERN` (beside `MAX_CURSOR_LENGTH`, public
already), `domainkit.context.client_group`, and the guard's ten names.
`ALLOWED_CORE` goes from 101 names to 90. The names left are internal to
the module that defines them, or in modules other 2.9 work edits.

### Module state and its resets

`domainkit_tests/test_module_state.py` reads every module of the core
and the apps. State is a container (a dict, list, set, deque, counter,
`WeakSet`, or weak dictionary) bound at module level that its module
changes in place, or a name a function rebinds through `global`. Each
piece is in `RESETS`, with the public name that resets or restores it, or
in `UNRESET`, the 54 pieces of 2.9 without one, grouped by why: kept for
a live object and gone with it, worked out once from what does not
change, the process's counts and locks, and registrations no test undoes
yet. `UNRESET` only shrinks.

Two resets are new, both public:

- `domainkit.contrib.messaging.unregister_task(key)` forgets one task; an
  unknown key is ignored. A test plays a worker of a release that does
  not know a task by registering, queueing, and forgetting it. The 34
  lines that popped the private table call it.
- `domainkit.contrib.notifications.push.reset_tokens()` forgets FCM's
  accounts, APNs' signed tokens, and Push Kit's apps. The plugin calls it
  before each test, as it calls `reset_reports()`.

Options weighed:

- **Make the old path the new module** (`sys.modules[__name__] =`). A
  patch at either path would reach the code. But type checkers and the
  API pages read the file, not what replaced it, and the old module's
  `__name__` would name the new one. Two modules, and the note on where
  to patch, are plainer.
- **Move only the client.** It imports the guard at its top, so the
  adapters would reach the serving package through it.
- **Layer 1 for the package.** Its imports allow it, but settings, logs,
  and error reports call no service; layer 2 is the narrower promise.
- **Keep the guard's lazy import of the rate limit**, listed among the
  edges that stay. The list only shrinks.
- **Move `client_group` out of the rate limit's module now.** Other 2.9
  work edits it; the copy and its test hold until that lands.
- **Warn on the old paths in 2.9.** Every project's adapters would warn
  in a minor release that asks nothing of them yet.
- **Rename the logger** after the module: see above.
- **Leave webhooks on the old guard path**, or list its eight names
  again under the new one. The first keeps the framework on a path meant
  to go, the second grows `ALLOWED_CORE`.
- **Find state at run time**, in each module's globals. A dict that is a
  table cannot be told from one that is state without reading the code.
- **`tasks_kept()` around each test that popped a task.** It puts back
  what a block registered, but a test that plays an older worker forgets
  a task in the middle, which a block cannot.

## Consequences

- No adapter imports the serving package; `test_core_layers.py` lists 5
  edges up, from 21. A worker that calls S3 loads no `domainkit.http`.
- Projects keep importing `domainkit.http.client` and
  `domainkit.http.guard`, and may move to `domainkit.outbound` when they
  choose. Their tests that patch a name of the client patch it at the new
  path.
- New module state fails a test until it has a public reset, or a review
  lets it join `UNRESET`.
- `client_group` exists twice until the rate limit imports it; a change
  to one fails the test that compares them.
- The published names follow the deprecation policy.

## References

1. PEP 562, *Module `__getattr__` and `__dir__`*:
   <https://peps.python.org/pep-0562/>
2. Pydantic, `pydantic/_migration.py`:
   <https://github.com/pydantic/pydantic/blob/main/pydantic/_migration.py>
3. Python, *unittest.mock: Where to patch*:
   <https://docs.python.org/3/library/unittest.mock.html#where-to-patch>
4. import-linter, *Layers*:
   <https://import-linter.readthedocs.io/en/stable/contract_types/layers/>
5. Celery, *celery.app.registry*:
   <https://docs.celeryq.dev/en/stable/reference/celery.app.registry.html>
