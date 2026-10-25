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

## Amendment (2026-10-25, 0.9.2)

- The domain layer may also import `domainkit.clock`, as the code has
  allowed since the clock was added ([ADR 0037](0037-one-clock-frozen-per-context.md)):
  reading the time through it keeps a domain free of I/O and testable.
- The rules are the defaults of `domainkit.architecture.LayerRules`, and a
  project extends them with the `ARCHITECTURE` setting: more packages its
  domain layers may import (a shared kernel), more libraries or layers a
  layer must not use, layers of its own (`ports`), and more of a domain
  other domains may import. A setting adds and never removes, so every rule
  above still holds in every project; each entry is validated and bounded,
  and an unknown layer or a rule on the domain layer's allowlist is refused
  when the settings load. Before, a project could change the rules only by
  patching the module before `check` ran.
