# 0190. Keep runnable examples as projects that domainkit's tests run

- Status: Accepted; amended 2026-11-18 (at the end)
- Date: 2026-11-18

## Context

The 2.8 audit found no runnable example of the problems the framework
exists for (A28-DX-06). The tutorial stops at the core. The flagship
features (the ledger, approvals, action authorizations, the outbox,
webhooks) are learnt from reference pages of 500 to 1,500 lines, whose
first examples are fragments that fail when run alone. The one runnable
project, `domainkit init --profile fintech`, books nothing in the ledger
and is presented as a checklist.

The audit proposed an `examples/` directory: one project per problem,
made by `domainkit init`, each with a walkthrough and tests, run by CI
on SQLite and PostgreSQL. Three come first: payments in double-entry
books with idempotency keys, maker-checker approvals with each act
confirmed, and an outbox with signed webhooks. Two more, a multi-tenant
service and an upload pipeline, wait for the tenancy fixes of 2.9.

Left out of the audit's list, to keep each example to one problem:
closing a period and reconciling a CSV statement (payments-ledger),
confirming on a bound device or by the phone's sign-in approval rather
than with an authenticator's code (maker-checker), and ordering keys and
a worker on RabbitMQ (outbox-webhooks). The reference pages cover each;
an example may take one up later. The two deferred examples,
`saas-tenancy` and `document-intake`, are named in the index as planned.

Examples rot unless something runs them. Other projects show both ends:
FastAPI keeps its documentation's code in `docs_src/` and imports each
file from `tests/test_tutorial/` [1]; SQLAlchemy ships an `examples/`
tree that it calls runnable, and its suite runs a part of it [2].
domainkit already runs a generated project whole: `test_init_fintech.py`
plays the steps of the project's own CI workflow (ruff, mypy, `check`,
migrations, its tests) in a subprocess, and runs its tests again on
PostgreSQL.

The questions, and the options weighed for each:

- **Layout.**
  - A whole project per example, as `init` writes it, edited (chosen).
    A reader opens it, runs it, and copies it.
  - Only the files an example changes, laid over a project that the test
    makes with `init`. Less to keep, but nothing a reader can run as it
    is.
  - Single modules, as FastAPI's `docs_src` holds. The problems span
    settings, a domain's layers, routes, and tests, and the point is a
    project shaped as `init` shapes them.
- **Migrations.**
  - Committed. A framework change that adds a column (2.9 adds several)
    would need a migration in each example in the same pull request,
    and parallel work would conflict there.
  - Made when the example runs (chosen), as a project makes them after
    an upgrade ([upgrading.md](../upgrading.md#how-to-upgrade), step 4).
- **How CI runs them.**
  - A job of its own that runs `pytest examples/<name>`, as the audit
    proposed: a new required check, its runner and its setup, and only
    the tests, without the examples' lint, types, or migrations.
  - A test module of the suite that plays each example's own CI workflow
    (chosen), as `test_init_fintech.py` does: every job that runs the
    suite runs it, the PostgreSQL job on PostgreSQL. No workflow
    changes.
  - Walking each README as the recipes are walked (`test_recipes.py`).
    The walkthroughs explain code that is there, rather than have a
    reader type it in; walking them would put whole files in fenced
    blocks.
- **Lint and types.**
  - Out of scope. The examples are code to copy: they should pass.
  - Each with its own configuration (chosen). Ruff takes, for each file,
    the nearest `pyproject.toml` that configures it [3], so the root's
    `ruff check .` checks each example as its project would. mypy cannot
    check them in the root's run: each holds a package named `config`
    and one named `domains`. Each runs `mypy --strict` in its own
    workflow, which the test plays.

## Decision

- **`examples/<name>/` is a project `domainkit init` made**, with a
  domain from `manage.py startdomain`, then edited. Every file `init`
  wrote stays; the example adds what solves its problem, and mypy in
  strict mode as `init --profile fintech` adds it (its dev group, its
  `[tool.mypy]`, and `mypy` in its workflow's lint step). Its test
  settings read `<PREFIX>TEST_DATABASE_URL`, as the fintech profile's
  do. `examples/README.md` lists each example, and the planned ones.
- **Each README opens with the problem** (`## The problem`), then walks
  through the code that solves it, and ends with what its tests prove.
  Every Python block of a walkthrough is code the example holds;
  `test_examples.py` checks it, indentation aside.
- **An example holds no migrations**, nor the `.env` that `init` writes.
  Its README's first steps export the settings module and make the
  migrations, for the domainkit installed.
- **`domainkit_tests/test_examples.py` runs each example.** It copies
  the example, makes its migrations, and plays the steps of its
  workflow's test job but install and audit, with the framework of the
  checkout and none of the suite's settings. With
  `DOMAINKIT_TEST_POSTGRES_URL` set, it runs the example's tests again
  on a database of their own there. It checks that each example's
  domainkit range holds this version, that the index lists each one,
  and that the build ships `src/domainkit` alone.
- **The examples stay out of the distributions.** poetry-core builds the
  wheel and the sdist from `packages` and the files it names (the
  README, the licence, `pyproject.toml`) [4]; there is no `include`.
  deptry leaves `examples/` out (each declares what it needs), and
  mutmut's copy holds it, since the suite reads it.
- **They follow the framework.** An example's range is `>=2.8.0,<3`:
  `init` writes the version installed where it runs, and the range was
  raised by hand to 2.8.0, the release the examples were made on. A
  change to the framework that breaks one fails CI, and is fixed in the
  same pull request.

## Consequences

- A team that evaluates domainkit for payments runs a whole project, and
  reads the code that a test runs on every change.
- A change to a contrib app that an example uses must keep the example
  working, or change it. That is the point; it costs those pull requests
  some work.
- The suite runs three more projects: each plays ruff, mypy, migrations,
  and a dozen or so tests in subprocesses: 17 to 31 seconds of a
  worker's time per example, measured on a shared 4-core machine, and 6
  to 17 more on PostgreSQL.
  Every job that runs the suite pays it. Running the examples' ruff,
  mypy, and `check` in the lint job alone was weighed and left: that
  job's commands are the Makefile's `lint` target and the pre-commit
  hooks' too (`test_ci.py` holds the three equal), `check` needs each
  project's settings, and the fintech profile's test pays the same in
  every job. A pull request that changes only `docs/` runs
  `test_docs.py`, which checks the examples' links.
- Renovate's `config:recommended` ignores `examples/` [5], and the
  workflows inside an example never run on GitHub. The action pins of
  an example's `ci.yml` are those `init` wrote; the test plays its
  steps, not its pins. Refresh them with the templates' when they move.
- A new example needs its line in `PASSED` (its settings' prefix and
  how many tests pass) and in the index.

## Amendment (2026-11-18): an example's floor is the release it needs

2.9.0 moves the outgoing client to `domainkit.outbound` (ADR 0195), and
`outbox-webhooks` imports it from there, so 2.8 cannot run it: its range
is `>=2.9.0,<3`. `payments-ledger` and `maker-checker` keep
`>=2.8.0,<3`, as their workflows' steps pass on 2.8.2 too. An example's
floor is the oldest release it runs on: the release that gives it a name
it imports raises it by hand, and `test_examples.py` still checks that
each range holds this version.

[1]: https://github.com/fastapi/fastapi/tree/master/tests/test_tutorial
[2]: https://docs.sqlalchemy.org/en/20/orm/examples.html
[3]: https://docs.astral.sh/ruff/configuration/#config-file-discovery
[4]: https://python-poetry.org/docs/pyproject/#packages
[5]: https://docs.renovatebot.com/presets-default/#ignoremodulesandtests
