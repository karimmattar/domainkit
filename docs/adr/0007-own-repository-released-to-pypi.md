# 0007. Its own repository, released to PyPI

- Status: Accepted
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
