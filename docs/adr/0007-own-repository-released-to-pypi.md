# 0007. Its own repository, released to PyPI

- Status: Accepted; amended 2026-10-26 (at the end)
- Date: 2026-10-14

## Context

domainkit started inside the first service built on it, then became a
package in that service's repository. Other services need it too, and
keeping it there makes it look like part of one service and couples its
releases to that service's history.

## Decision

- domainkit lives in its own GitHub repository, with its own CI, owners,
  security policy, and decision records.
- It is released to PyPI from version tags, using trusted publishing, and
  follows [docs/versioning.md](../versioning.md).
- Projects depend on a released version range (`domainkit>=0.1,<0.2`). A
  local path dependency is only for working on both at once.

## Consequences

Framework changes need a release before a service can use them, so
breaking changes are announced and deprecated first. The documentation and
examples must not depend on any particular service. The repository's own
suite is the proof that the framework works on its own; services add their
PostgreSQL and end-to-end tests on top.

## Amendment (2026-10-26, 0.10): a release reuses main's CI run

Until 0.10, `release.yml` ran the whole CI workflow again on the tagged
commit before publishing, about 40 job-minutes and 15 minutes of wall time
for a tree that the pull request and the push to `main` had each just
passed: the ruleset requires a pull request to be up to date with `main`,
so the squash merge's tree is the one CI checked (INFRA-06).

- The push to `main` still runs the whole workflow, and it is the run a
  release reuses: `release.yml` finds the CI run of the tagged commit's
  push to `main`, waits for it if it is still running, and stops unless it
  succeeded. The wheel and sdist it attests and publishes are that run's
  `dist` artifact, built and smoke-tested there, and the bill of materials
  its `sbom` artifact; CI keeps both for 90 days.
- Only pip-audit and gitleaks run again at release, since an advisory or a
  leaked secret can come to light after main's run.
- A push to `main` is never cancelled by the next one, and each gets a
  concurrency group of its own commit: a release must find its commit's
  run complete.
- A pull request that changes only docs (`docs/**`, and the Markdown files
  at the top of the repository) skips the suite and runs the tests that
  read the docs instead. The ruleset's required checks still all report:
  a job skipped by its condition reports success, and the test matrix
  runs with its steps skipped.

The release's provenance attestation names `release.yml` at the tagged
commit, as before; the files it covers were built by `ci.yml` at the same
commit, in the run the release's job summary links to.
