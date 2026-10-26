# 0075. Declare ranges to the next major, and test the newest releases they allow

- Status: Accepted
- Date: 2026-10-25

## Context

A library's ranges say what projects may combine it with; its lock file
says what its own CI tests. Until 0.9.1 the two were confused. FastAPI was
held to one minor (`>=0.143.1,<0.144.0`, ADR 0065), as were uvicorn,
asyncpg, aiosqlite, prometheus-client and immudb-py; uvloop and Granian
were pinned exactly (`==0.23.0`, `==2.8.4`). Meanwhile Starlette,
SQLAlchemy and httpx2 could float inside wide ranges that no job tested.
The 0.9.0 audit (INFRA-07, INFRA-03, DOCS-01) found:

- FastAPI had made 19 minor releases past the cap: a project could not take
  a FastAPI fix without a domainkit release, and Renovate, whose "widen"
  rule never matched, moved the cap one minor at a time instead.
- The `==` pins made `pip install "domainkit[granian]" granian==2.8.5`
  unsatisfiable: a Granian or uvloop patch, a fix in a Rust HTTP parser
  for instance, waited for domainkit.
- What a fresh install resolved was never run: Starlette 1.9 broke the
  generated project's tests (DOCS-01, fixed in 0.9.1) while every CI job
  passed on the lock's 1.8.

Measured on 25 October 2026 (Python 3.13, wheel installed with every extra
and the dev group, no constraints, SQLite):

- With 0.9.1's ranges: FastAPI 0.143.5, Starlette 1.10.0, SQLAlchemy
  2.1.6, httpx2 2.16.0, Granian 2.8.4. 4,990 passed, 2 failed: httpx2
  2.16 builds the system trust context by the platform it runs on, which
  the test of macOS and Windows trust stores did not expect. The lock's
  2.13.1 passes. No job would have shown it.
- With the ranges below: FastAPI 0.162.0 and Granian 2.8.5 as well. The
  same 4,990 passed and the same 2 failed; the benchmark ladder passes
  `--strict` on these versions, every count within its budget.

Options considered:

- **Keep one-minor caps**, raised by Renovate after CI passes. Every range
  is tested, but projects wait for domainkit to take any upstream fix, and
  the ranges are not one policy (Starlette `<2.0` beside FastAPI `<0.144`).
- **Ranges to the next major, unchecked.** Projects choose, but nothing
  runs what they resolve: DOCS-01 again.
- **Ranges to the next major, and a job that tests their top.**

## Decision

- Each dependency's range runs from the oldest version CI tests to below
  its next major: `<1.0.0` for one still at 0.x (FastAPI, uvicorn,
  asyncpg, aiosqlite, prometheus-client, python-multipart, uvloop), `<2.0.0`
  for immudb-py, `<3.0.0` for Granian. No `==`.
- Two exceptions keep narrower tops: SQLAlchemy `<2.2`, because its second
  number marks API changes (2.1 deprecated `Result.tuples()` and changed
  `python_type`, ADR 0065); tzdata has no top, being data.
- The lock file pins the set every other job tests. A `newest` CI job
  installs the wheel with every extra and the dev group at the newest
  releases the ranges allow, and runs the suite, on every pull request and
  weekly. It gates no release: an upstream release must not hold back a
  fix here.
- When it fails, the next patch release fixes domainkit or lowers the
  range's top below the breaking release.
- Renovate widens a range when a new major appears (`rangeStrategy:
  widen`, matched by the poetry manager's `project.dependencies` and
  `project.optional-dependencies` dependency types), and CI runs with it
  before the range moves.
- Lower bounds stay where they are; raising one is a minor-release change
  (`docs/versioning.md`).

## Consequences

- Projects pick their FastAPI, Granian or uvloop, and take upstream
  security fixes without a domainkit release.
- A fresh install can meet a release domainkit has never run until the
  `newest` job does, at most a week later; projects that lock their
  dependencies (with Poetry, uv or pip-tools) upgrade when they choose.
- FastAPI's minor releases change how forms, defaults and `null` bodies
  validate (ADR 0065); a project that upgrades FastAPI meets those changes
  in its own API, as it would without domainkit.
- The `newest` job costs about ten job minutes per pull request and runs
  beside the others, so the wall time does not grow.
- This supersedes ADR 0065's one-minor cap on FastAPI; the rest of 0065
  stands.
