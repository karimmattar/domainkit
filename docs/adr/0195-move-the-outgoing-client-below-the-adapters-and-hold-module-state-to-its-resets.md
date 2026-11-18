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
  `domainkit.architecture`, for its four layer names, so every process
  that loaded its settings loaded the checker.
- `ALLOWED_CORE` held 101 core names that apps import and no `__all__`
  publishes. The most used was `DEPLOYED_ENVIRONMENTS`, by 14 apps
  (A28-ARCH-06).
- Tests reached into module state: 34 lines popped tasks out of
  `messaging.registry._tasks`, and others cleared the push backends'
  tokens. Nothing failed when a module gained new state without a reset,
  which ADR 0113 asks of state a test can leave behind (A28-ARCH-14).

Constraints: 2.9 is a minor release, so every import a project makes
keeps working. The settings module and the rate limit's are edited by
other 2.9 work, so this change touches only the lines it moves there. The
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
- **A caller's address group** is `domainkit.context.client_group`,
  public, moved from the rate limit with its rule unchanged: IPv4 as is,
  IPv4-mapped IPv6 as IPv4, IPv6 by its `RATE_LIMIT_IPV6_PREFIX` network,
  each remembered for the latest 4,096 hosts. It is defined once: the
  rate limit, auth's throttles, and the guard import it, and the rate
  limit's module still holds it, the same function. Grouping
  callers decides who shares a budget, so two copies that drift apart
  would let one caller count as two. The two cache constants move to
  `domainkit.context` too; `domainkit.http.middleware` imports them.
- **The guard's public names**, decided anew in the new package: its
  `__all__` lists what an app that takes URLs from its users needs, as
  webhooks does: `checked_url`, `checked_host`, `checked_address`,
  `lookups_for`, `tls_context`, `UnsafeURL`, `MAX_URL_LENGTH`, and the
  `Address`, `Network`, and `Resolver` types (`checked_host` takes a
  `Resolver`). The bare lookup is not among them: its addresses are
  unchecked, which in a guard against forged requests is a trap, and
  `checked_host` resolves with it by default. This reverses 1.0's choice,
  which took the guard's names out of the API because no page used them.
  Now an app uses them: webhooks checks a subscription's URL when it is
  saved and resolves it as it is called, and a project's own outbound
  calls to URLs its users chose need the same checks without writing
  them again. The resolver, its bounds, and the address tables stay
  internal, and `domainkit.http.guard` stays internal as it was. Webhooks
  no longer passes the lookup as `resolve=`, which was the default.
- **The logger keeps its name**, `domainkit.http.client`: logging
  settings and alerts name it, and a new name would end them without a
  word.

### The 2.8 paths

`domainkit.http.client` and `domainkit.http.guard` stay, as modules that
read every name of the new ones through a module `__getattr__`, and
import the public names by name for type checkers only. Each is the same
object, so `is`, `isinstance`, and `except` hold either way, and there is
one set of clients, circuits, metrics, and lookup threads. They are not
deprecated in 2.9; a later minor release may deprecate them, with a
warning, before 3.0 removes them. `domainkit.http.client` keeps its
`__all__` and its section of the API pages; `domainkit.http.guard` stays
without one, internal as it was.

Each old module is a view of its new one (a `ModuleType` subclass set as
its class, in a private module of `domainkit.http`). It keeps no name of
the new module's, and a name set on it, by a 2.8 test's
`monkeypatch.setattr` or `mock.patch`, is set on the new module, where
the client, the guard, and the adapters read it: a 2.8 test that faked
the adapters by replacing `domainkit.http.client.HttpClient` still fakes
them. A delete through the view of a name the new module defined, while
something replaced it, puts back the value the name had when the old
path was first imported, rather than deleting it: `mock.patch` deletes
what it set on a name the view does not hold, and with `create=True`
puts nothing back, which would leave the adapters without `HttpClient`
for the rest of the process. For the names the module never rebinds
(its classes, functions, and constants), that value is the one the patch
replaced. A name that was not replaced, or that the module never had, is
deleted (`monkeypatch.delattr`; `create=True` on a new name). New code
patches the new path. What prints a class by its module prints the new
one.

Every import the framework makes is of the new package, its tests'
included, but for the tests of the old paths themselves. The plugin
resets `domainkit.outbound.client`'s circuits, and the lifespan closes its
pools, found by that name: a process that imports only the new path, as
the adapters do now, gets both.

### The layer rules

A project's infrastructure layer imports `domainkit.outbound` with no
exception: no rule names it. A project's own rule
(`ARCHITECTURE.FORBIDDEN_LIBRARIES`) that names `domainkit.http.client`
or `domainkit.http.guard`, or a name under one, names the same under
`domainkit.outbound` too; one that names `domainkit.http` names both new
modules, unless, in that layer, the exception for `domainkit.http.client`
lifted it from the client in 2.8, as it does in the infrastructure
layer. An entry the layer's rules hold already, such as the default
`domainkit.http` of the infrastructure layer repeated, adds nothing. The
rule keeps covering the code it covered. The application layer may not
(`FORBIDDEN_LIBRARIES["application"]` adds `domainkit.outbound`): it could
not call out through `domainkit.http.client`, and it cannot by the new
path either. The domain layer's allowlist leaves it out. The exception for
`domainkit.http.client` in `ALLOWED_MODULES` stays, and so does
`architecture.W001` for the names its `__all__` does not list: a project
that imports the old path changes nothing in 2.9.

### The settings and the checker

The four built-in layer names (`domain`, `application`,
`infrastructure`, `api`) are the settings' own: `LAYERS`, in the
settings module, which `ArchitectureConfig` checks `EXTRA_LAYERS` and the
other tables against. `domainkit.architecture` imports them from there,
the same object under the same name, so loading the settings, the
default `ARCHITECTURE` validated with them, loads no checker.

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
yet. `UNRESET` only shrinks. Module-level functions behind
`functools.cache` or `lru_cache` are listed apart (`CACHED`, 66 in 2.9,
by what they keep), and that list only shrinks too.

Reading the source sees only what the source shows. Not found: a class
attribute changed later, a `ContextVar` whose default is a container, a
container changed through another name (an alias, `globals()[...]`,
`operator.setitem`, `__setitem__`), one made by a call the scan does not
name (`dict.fromkeys`), and a cache made inside a function or a class.

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
  `__name__` would name the new one.
- **Re-export modules that keep their own copies**, with a note on where
  to patch. A 2.8 test that replaced `domainkit.http.client.HttpClient`
  to fake an adapter would build a real client and call the service, and
  a patch undone would leave a copy that hides the client's later value.
  2.9 must not break 2.8's tests, so the old modules forward sets and
  deletes instead.
- **Move only the client.** It imports the guard at its top, so the
  adapters would reach the serving package through it.
- **Layer 1 for the package.** Its imports allow it, but settings, logs,
  and error reports call no service; layer 2 is the narrower promise.
- **Keep the guard's lazy import of the rate limit**, listed among the
  edges that stay. The list only shrinks.
- **A copy of `client_group` beside the guard**, with a test that the
  copies agree, while other 2.9 work edits the rate limit's module. The
  test samples hosts; one rule for who shares a budget, defined once,
  cannot drift.
- **Import the checker's layer names lazily** in the settings. Importing
  `domainkit.conf` would load no checker, but validating the default
  `ARCHITECTURE` would, in every process.
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

- No adapter imports the serving package; `test_core_layers.py` lists 4
  edges up, from 21. A worker that calls S3 loads no `domainkit.http`,
  and no process loads the import checker unless it checks.
- Projects keep importing `domainkit.http.client` and
  `domainkit.http.guard`, and may move to `domainkit.outbound` when they
  choose. Their tests that patch a name of the client patch it at the new
  path.
- New module state fails a test until it has a public reset, or a review
  lets it join `UNRESET`.
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
