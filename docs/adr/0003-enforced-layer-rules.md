# 0003. Layer rules enforced by `check`

- Status: Accepted
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
