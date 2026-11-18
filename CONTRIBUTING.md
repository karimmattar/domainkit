# Contributing to domainkit

Thank you for helping. This page says how to set up, which checks a change
must pass, and how a change is written up. Report a vulnerability as
[SECURITY.md](SECURITY.md) says, never in a public issue.

## Set up

You need Python 3.13 (3.14 works too), [Poetry](https://python-poetry.org/)
2.2, `make`, and, for the integration tests, Docker with Compose.

```bash
git clone https://github.com/karimmattar/domainkit && cd domainkit
pipx install pre-commit
make install        # every extra, the dev and docs groups, and the hooks
make                # lists the commands
```

## The checks

Each `make` command runs what CI runs, the same way:

| Command | What it runs | CI job |
| --- | --- | --- |
| `make lint` | `poetry check --lock`, `ruff check`, `ruff format --check`, `mypy` (strict), `deptry` | `lint` |
| `make test` | the suite on SQLite and in-memory fakes, in random order, with coverage | `test (3.13)`, `test (3.14)`, `test (3.15)` |
| `make integration` | the suite against PostgreSQL, PostGIS, immudb, Redis, Valkey, RabbitMQ, Kafka, and ElasticMQ (SQS) from `compose.yaml`, with `redis-server`, `nats-server`, and SoftHSM installed (the broker and key tests start their own); a skipped test fails | `integration` |
| `make cov` | both runs combined, which must cover 98%, and every line the change adds since `origin/stg` (`BASE=`) | `coverage` |
| `make bench` | Python calls and round trips per request against `benchmarks/budgets.toml`; then the rungs under load against `benchmarks/load.toml` | `bench` |
| `make docs` | the documentation site, with every link and public name checked | `docs-site` |
| `make audit` | pip-audit of the lock, and gitleaks over the history | `audit` |

`make check` runs lint, test, and docs: run it before you push. The
pre-commit hooks run the lint checks on the files a commit changes; they
report, and `make fix` lets ruff fix and format.

`make integration` starts the servers first (`make services`): the images
and ports CI's integration job uses, with throwaway certificates written
to `.ci/`. `make services-down` stops them and drops their data.

## Writing a change

- **Tests.** Every change comes with tests of what it does, its failure
  paths and its refusals included. The suite runs with every warning an
  error, in random order. Async tests use `@pytest.mark.anyio`. Never wait
  on the wall clock: use `domainkit.clock.freeze_time`, events, or fakes
  that block until released. New code is covered 100%, branches included,
  by the runs with and without servers together: on a pull request to
  `stg`, CI's `coverage` job fails on a line it adds or changes under
  `src/` that no job ran, or that left a branch untaken
  ([ADR 0191](docs/adr/0191-gate-the-lines-a-change-adds.md)); the
  release's pull request to `main` is not held to it (`make cov
  BASE=origin/main` checks a release by hand). An old line partly covered
  that you edit counts as added; a branch only a timing reaches, or a line
  only `test (3.14t)`, `newest` or `oldest` runs (they gather no
  coverage), takes a test or `# pragma: no cover` with its reason. A helper
  more than one test file needs goes in `domainkit_tests/support/`, by
  area, not in the test file that used it first (`test_support.py`
  checks the ones that are there); so does a fixture two files need.
- **Test files.** A test goes in the file of the module it tests,
  `domainkit_tests/test_<module>.py`, or `test_<module>_<topic>.py` once
  that file is long. A regression test names the finding or the report it
  answers in its docstring (`(A28-TEST-02)`), not in the file's name: the
  files named after findings (`test_sec16b_8_*`, `test_audit15_*`) are
  folded into their module's file when that file is next changed.
- **Mutation testing.** Run it yourself for the functions you changed:
  `make mutants MUTANTS="domainkit.http.middleware*"`. Kill the survivors
  that point at a missing test, and say in the pull request why each one
  left stays. CI runs mutation testing only on request: the jobs below run
  while the repository variable `MUTATION` is `on`, which it has not been
  since 1.5.0, and the weekly shards can be started by hand
  ([ADR 0106](docs/adr/0106-mutate-what-a-pull-request-changes.md)'s
  amendment of 2026-11-18). A new module goes in one of the weekly shards in
  `.github/workflows/ci.yml` (`test_ci.py` checks it). When it is on, a
  pull request's `mutation (changed)` check runs the mutants of the
  functions it changed and lists those that lived; for a release-size
  change, it hands what it did not reach to the shards
  `mutation (changed, N)`, and `mutation (changed, summary)` lists them. A
  test that reads domainkit's source text, or the frames of its calls,
  carries `@pytest.mark.reads_own_source`: mutmut rewrites both, and
  mutation runs leave such tests out. Under mutmut's stats run the suite
  empties domainkit's `functools` caches before each test, so a function
  behind a cache is mapped to every test that reaches it, not the first.
  What a score leaves out: decorated functions (routes, validators,
  properties, context managers, fixtures), which mutmut never mutates and
  the job lists apart; code only a server reaches, as the runs are on
  SQLite (a survivor there needs a run on PostgreSQL before it means a
  missing test); and a flaky test counts as a kill (ADR 0106's 0.15
  amendment).
- **The public API** is what `docs/api.md` lists ([ADR 0076](docs/adr/0076-public-api-is-docs-api-md.md)).
  A new public name goes in its module's `__all__` and on its package's
  page of `docs/api/`, with a docstring (Google style, the repository's
  plain voice); the site renders the docstring in the API reference.
- **Compatibility.** A patch release changes no behaviour a project relies
  on. A 1.x minor release adds, and may deprecate, but removes or changes
  nothing [versioning.md](docs/versioning.md) calls public: a deprecated
  name keeps working until 2.0, warning with `DomainkitDeprecationWarning`,
  naming the new place and the version that removes it.
- **Settings.** A new setting is bounded and validated, defaults to
  today's behaviour, and gets a row in its table with its default and
  bounds (`test_docs.py` checks the rows).
- **Docs.** Change the page that describes what you changed. A new command
  or setting is named on its page of `docs/reference/` (`docs/reference.md`
  lists them). The pages describe the release they ship with, as of its
  tag: what a 1.x minor adds says *New in 1.x* where it is described, and a
  fix to the pages of a released version goes to `stg` and its next patch
  release. Pages link to each other by relative paths; the README links to
  the files of `main` by absolute URLs, which CI's `build` points at the
  release's tag before it builds, so the PyPI page opens that release's
  pages (INFRA-22). Code names a page as `docs/<page>.md` (with its
  `#anchor`, or its heading quoted) and a decision as `ADR NNNN`. The
  wheel holds no `docs/`: `domainkit docs` links each one under the
  release's tag (ADR 0152), and `test_docs.py` checks that each one is
  there.
- **Layers.** `domainkit.architecture` keeps each layer to its own imports,
  and the core never imports `domainkit.contrib`
  ([ADR 0080](docs/adr/0080-access-rules-are-core-auth-is-one-provider.md)).
  The core's own packages sit in five layers, and import their own and
  those below: foundations, settings and the model, adapters, serving,
  and tools (`domainkit_tests/test_core_layers.py`,
  [ADR 0188](docs/adr/0188-write-down-the-cores-layering-and-hold-the-boundaries-it-draws.md)).
  A contrib app imports another app's public names, the core's public
  names, and no ring of apps (`test_contrib_boundaries.py`). Module state
  a test can leave behind comes with a public reset
  (`test_module_state.py`, ADR 0113). A function
  stays under ruff's complexity gates (`pyproject.toml`); the few that
  were over them in 2.9 say so with a `# noqa` on their `def` line.

## Writing it up

- **CHANGELOG.md.** One entry under `## [Unreleased]`, in the section that
  fits (`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`),
  saying what a project sees. A changed default or a breaking change starts
  with **Breaking:** and says how to keep the old behaviour, and goes in
  `docs/upgrading.md` too.
- **Decisions.** A decision that would be costly to reverse gets an ADR:
  copy `docs/adr/template.md` to the next number, add its row to
  `docs/adr/README.md`, and link it from the change. An ADR is amended,
  never rewritten: add a dated "Amendment" section and update its status.
- **Commits.** Small and logical, each with a subject in the imperative
  ("Add ...", "Refuse ...") and a body that says why.
- **Pull requests.** Branch from `stg` and open the pull request against
  `stg`: it gathers the changes of the next release. `main` holds what was
  last released, and changes only by a pull request from `stg` when a
  release is made ([Making a release](docs/versioning.md#making-a-release)).
  The template asks what changed, why, and how it was tested. Every check
  must pass; `stg` and `main` take squash merges only.
