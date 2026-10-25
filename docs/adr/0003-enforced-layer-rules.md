# 0003. Layer rules enforced by `check`

- Status: Accepted; amended by [0037](0037-one-clock-frozen-per-context.md), and 2026-10-25 (at the end)
- Date: 2026-10-14

## Context

Domain-driven layering (domain, application, infrastructure, api) erodes
quietly: one convenient import of SQLAlchemy into the model, or of another
domain's repository, and the boundaries are gone. Code review alone does not
catch it reliably.

## Decision

`domainkit.architecture` encodes the rules:

- Dependencies point inward: `api → infrastructure → application → domain`.
- The domain layer imports only the standard library and `domainkit.ddd`.
- Another domain is reachable only through its `application` layer or its
  `domain.events`.

`manage.py check` and the test suite parse every module's imports and fail on
a violation.

## Consequences

Violations fail CI, not review. Some code needs an extra port or event where
a direct import would have been shorter; that cost is intended. Framework
packages are exempt from the cross-domain rule.

## Amendment (2026-10-25): the clock

[0037](0037-one-clock-frozen-per-context.md) let the domain layer import
`domainkit.clock` as well, and `domainkit.architecture` has allowed it
since (`DOMAIN_ALLOWED_PREFIXES`): reading the time through it is as pure as
the standard library's, and tests can freeze it. The domain layer imports
the standard library, `domainkit.ddd`, and `domainkit.clock`. This record
did not say so until now.
