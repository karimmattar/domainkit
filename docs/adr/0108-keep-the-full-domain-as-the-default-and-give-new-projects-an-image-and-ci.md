# 0108. Keep the full domain as `startdomain`'s default; give new projects an image and CI

- Status: Accepted
- Date: 2026-10-28

## Context

Two findings of the 0.9.0 audit are about the newcomer's path.

DSGN-11: too much before the first endpoint. Measured on 0.11.1, with
`init` and then `startdomain accounts --aggregate Account` as the tutorial
runs it: `init` writes 15 files, `startdomain` 21 more (15 in the package,
6 under `tests/`), and those 21 import 36 distinct names from domainkit,
29 of them in the package. The template shows every pattern at once: a
value object, a child entity, a reference, five events, a durable handler,
filters, `PATCH`, archive and delete.

INFRA-09: the new-project path ends at "check and tests pass". Nothing
says how the project becomes an image, or what its CI runs.

For DSGN-11 there were two ways:

1. **A smaller default.** Every project that runs `startdomain` gets the
   small domain. The tutorial's parts 2 to 5 build on the full one (the
   name value object, archive, the owner, the durable handler), and so do
   the generators' snapshots and the README's description: all would be
   rewritten, and a project's own scripts that run `startdomain` would get
   a different domain. A changed default, so **Breaking:**, with
   `--full` to keep the old.
2. **An option.** `startdomain --minimal` writes the small domain, in the
   same four layers, so `check` and the other generators treat it as any
   domain. The default stays, nothing a project runs changes, and the
   first page a newcomer reads (the README's quick start) uses the option.

Measured with `--minimal`: 18 files (13 in the package, 4 under the
domain's tests, and `tests/domains/__init__.py`) and 18 names (15 in the
package). The generators needed `domain/events.py` and
`application/handlers.py`, which the small domain leaves out; they now
start those modules when missing, as `addquery` already started
`application/queries.py`. With that, `addcommand`, `addevent`,
`addquery`, and `startaggregate` all grow a minimal domain, and its tests
and ruff pass after each.

For INFRA-09, the image and the workflow could be documented, or written
by `init`. Written, they are kept true by tests: the workflow's steps are
run on a new project by `test_init.py`, the image's command is checked
against the server limits in `docs/reference.md`, and CI's build job
builds and runs a new project's image with the wheel it built.

## Decision

- `startdomain` keeps its full template as the default; `--minimal`
  writes the small one. The README's quick start uses `--minimal`; the
  tutorial stays on the full domain.
- The generators start `domain/events.py` and `application/handlers.py`
  when a domain has none.
- `init` writes a `Dockerfile`, a `.dockerignore`, and
  `.github/workflows/ci.yml` into every new project:
  - the image installs the dependencies `pyproject.toml` names in a build
    stage (`--build-context wheels=<dir>` offers wheels of one's own
    first), copies the code as root, runs as uid 10001 who can write
    nothing in `/app`, sets the production settings module, and starts
    uvicorn with the limits of "Server limits", `--no-proxy-headers`,
    and `WEB_CONCURRENCY` for its workers;
  - the workflow runs ruff, `check`, `migrate` and `makemigrations
    --check` on a SQLite file, and the tests, and builds the image; its
    actions are pinned to the commit SHAs domainkit's own CI pins, and
    Renovate's GitHub Actions manager reads the template too, so one pull
    request updates both.
- `docs/deploying.md` is the checklist from a project that passes to one
  in production.

## Consequences

- Nothing changes for an existing project, or for a project that runs
  `startdomain` without the option. Projects made before 0.12 have no
  image or workflow; they can copy them from a new project.
- The image is a starting point, not a platform: no health check is
  built in, since a probe must send a `Host` of `ALLOWED_HOSTS` (the
  checklist says how), and nothing is pushed anywhere.
- The template's action pins and Python version move with domainkit's
  CI. Building the image needs the package index, so it is built in CI,
  not in the unit tests.
- Two domain templates are kept. The snapshots pin both
  (`snapshots/generators` and `snapshots/minimal_domain`), so a change to
  one is reviewed as such.
