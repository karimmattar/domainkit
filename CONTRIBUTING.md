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
| `make test` | the suite on SQLite and in-memory fakes, in random order, with coverage | `test (3.13)`, `test (3.14)` |
| `make integration` | the suite against PostgreSQL, immudb, Redis, Valkey, and RabbitMQ from `compose.yaml`; a skipped test fails | `integration` |
| `make cov` | both runs combined, which must cover 100% | `coverage` |
| `make bench` | Python calls and round trips per request against `benchmarks/budgets.toml` | `bench` |
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
  by the runs with and without servers together.
- **Mutation testing.** For the functions you changed:
  `make mutants MUTANTS="domainkit.http.middleware*"`. Kill the survivors
  that point at a missing test, and say why each one left stays. A new
  module goes in one of the weekly shards in `.github/workflows/ci.yml`
  (`test_ci.py` checks it). A pull request's `mutation (changed)` check
  runs the mutants of the functions it changed and lists those that lived
  ([ADR 0106](docs/adr/0106-mutate-what-a-pull-request-changes.md)). A
  test that reads domainkit's source text, or the frames of its calls,
  carries `@pytest.mark.reads_own_source`: mutmut rewrites both, and
  mutation runs leave such tests out.
- **The public API** is what `docs/api.md` lists ([ADR 0076](docs/adr/0076-public-api-is-docs-api-md.md)).
  A new public name goes in its module's `__all__` and on its package's
  page of `docs/api/`, with a docstring (Google style, the repository's
  plain voice); the site renders the docstring in the API reference.
- **Compatibility.** A patch release changes no behaviour a project relies
  on. A minor release removes a public name only after a minor of
  deprecation: the old name keeps working, warning with
  `DomainkitDeprecationWarning`, naming the new place and the version that
  removes it ([versioning.md](docs/versioning.md)).
- **Settings.** A new setting is bounded and validated, defaults to
  today's behaviour, and gets a row in its table with its default and
  bounds (`test_docs.py` checks the rows).
- **Docs.** Change the page that describes what you changed. A new command
  or setting is named on its page of `docs/reference/` (`docs/reference.md`
  lists them).
- **Layers.** `domainkit.architecture` keeps each layer to its own imports,
  and the core never imports `domainkit.contrib`
  ([ADR 0080](docs/adr/0080-access-rules-are-core-auth-is-one-provider.md)).

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
- **Pull requests.** The template asks what changed, why, and how it was
  tested. Every check must pass; `main` takes squash merges only.
