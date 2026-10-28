# 0104. Import what a process uses on first use, and gate the modules each rung loads

- Status: Accepted
- Date: 2026-10-28
- Amends: [0064](0064-gate-performance-on-counts-not-time.md)

## Context

PERF-15 of the 0.9.0 audit: startup imported everything. 0.10 loaded mail
on first use; at 0.11.1, measured best of five fresh processes on Python
3.13 with every extra installed:

| What is imported | Modules | CPU | Peak RSS |
| --- | --- | --- | --- |
| `import domainkit.http` | 679 | 580 ms | 69.2 MB |
| The `init` project's `config.asgi`, production settings (builds the application) | 824 | 907 ms | 85.6 MB |

`python -X importtime` and `sys.modules` say where they come from:

- `domainkit/http/__init__.py` imported `get_asgi_application`, so any
  module of the package (`domainkit.http.errors` in a domain's code, a
  management command) built the whole application's imports: FastAPI,
  SQLAlchemy and its ORM, every domainkit layer.
- A served project with messaging, and no broker or ledger, loaded the
  ledgers (the immudb adapter too), the brokers, and messaging's worker
  and dispatcher: through the lifespan's table of closers, readiness,
  messaging's closer of its publishers and its broker check, and the
  outbox's follow-up of every commit, which imported the dispatcher even
  with no broker to publish to.
- `difflib` (for a setting's "did you mean") and `csv` (for exports) were
  imported at startup for paths most processes never take.
- The rest of `config.asgi` is what serving the template's routes needs:
  FastAPI (351 modules on its own, `pydantic.v1` included, which it
  imports to check every route's response model), SQLAlchemy's async ORM
  and both dialects (the outbox's partial indexes name them), pydantic
  models built for each route, and `prometheus_client` where installed.
  `importlib.metadata`, which `domainkit.__version__` read, FastAPI and
  SQLAlchemy import anyway.

The options:

- Leave it: the process pays once, and a deployment rarely starts. But
  workers, commands, and tests pay it each time, and it only grows.
- Split the packages so the heavy parts live elsewhere: renames every
  import path a project uses, for 0.12, against the deprecation policy.
- Import on first use where the path does not need it, keeping every
  public name where it is (PEP 562's module `__getattr__`), and count the
  modules in CI so it cannot creep back.

## Decision

- **Names imported on first use.** `domainkit.http`'s names, messaging's
  worker names (`Worker`, `Attempt`, `current_attempt`, `Budget`), and
  `domainkit.__version__` are module attributes resolved when first read
  (`domainkit.utils.module_loading.lazy_names`), then set on the module,
  so later reads are plain attribute reads. Type checkers see them
  through imports under `TYPE_CHECKING`; `from domainkit.http import
  get_asgi_application` and every other public path still work, and
  return the same objects.
- **Looked up, not imported.** The lifespan closes the ledgers and the
  brokers only if their modules were loaded (`sys.modules`), as it
  already did for HTTP clients; nothing was opened otherwise. Messaging
  registers a closer that closes the dispatcher's publishers once it is
  loaded. Readiness imports the ledgers only where `LEDGERS` names one.
  The outbox imports the dispatcher only when `MESSAGING.BROKER` is set.
  `difflib` and `csv` are imported where they are used.
- **The ladder counts modules.** Each rung records how many modules its
  process holds once it has built the application and served the
  warm-up, past those the interpreter started with
  (`benchmarks.STARTUP_MODULES`); the counters are imported after the
  count, so their SQLAlchemy is not counted in a rung that does not use
  it. `budgets.toml` holds a `modules` budget per rung, with an absolute
  tolerance of 2, so a module the runner's Python adds does not fail the
  job, and a new import chain does.
- **One environment.** What a process loads depends on what is installed:
  with `prometheus_client`, metrics modules; with `opentelemetry`, FastAPI's
  telemetry imports it. The `bench` job installed three extras (`postgres
  sqlite uvloop`) and `make install` every one: 39 modules apart on each
  rung. The job now installs every extra, as `make install` and the other
  test jobs do, and the budgets are written from such a run.

What it gave, with the same measurement:

| What is imported | Modules | CPU | Peak RSS |
| --- | --- | --- | --- |
| `import domainkit.http` | 57 | 7 ms | 10.1 MB |
| `config.asgi`, production settings | 806 | 922 ms | 84.8 MB |

On the ladder, every extra installed: 18 modules fewer on every
domainkit rung without a database (bare 797 to 779, template 800 to 782),
and 16 on those with one (827 to 811; keyed write 828 to 812). Calls per
request do not move (within 3), but on the keyed write (ADR 0105).

## Consequences

- A process that imports part of domainkit, a worker or a command, loads
  that part. The template's web process saves little: what it imports,
  serving its routes needs. Its startup is FastAPI building each route
  (the admin routers' 230 ms, 0.10 found) and the libraries themselves.
- A new eager import of a heavy module fails the `bench` job, naming the
  rung and the count; a dependency upgrade that changes the count is
  re-budgeted in the same change (`--write-budgets`), as calls are.
- A name added to a lazily loaded package goes in its `lazy_names`
  mapping and its `TYPE_CHECKING` imports; `test_lazy_imports.py` checks
  that every name in `__all__` resolves, and that a served application
  with messaging and no broker or ledger loads none of them.
- Shutdown no longer names the ledger and broker handlers it closes; a
  test that replaces `ledgers.close_all` still reaches it, through the
  module that holds it.
