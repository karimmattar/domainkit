# 0077. Declare what apps require, and reach other apps only through their public names

- Status: Accepted
- Date: 2026-10-25

## Context

Five contrib apps could not work without another (auth without content
types; mail, notifications, and webhooks without messaging; privacy without
the audit trail). Each checked for it in its own `ready()` with
`registry.is_installed("domainkit.contrib.X")` and a message of its own, so
a project learned of the dependency only when that app's `ready()` ran,
after the apps before it had wired themselves, and could not read the
dependencies anywhere. Apps also imported 33 internal modules of other apps,
and the core 8 (as measured in 0.9.1 for DSGN-04 of the 0.9.0 design audit),
so no app could change its internals, or be replaced by a project's own,
without breaking others.

Django leaves dependencies between apps undeclared and fails at first use.
Declaring them is cheap and gives one message for every app.

## Decision

- `DomainConfig.requires` lists the packages a domain cannot work without:
  package names, or `Requirement(name, reason)`. `DomainRegistry.populate`
  checks them after validating settings and before any `ready()`, in any
  order of `INSTALLED_DOMAINS`, and refuses with one error that names every
  missing package: `<app> needs '<package>' in INSTALLED_DOMAINS: <reason>.`
  Entries are validated (package names, not the domain itself, at most 32).
- Every contrib app declares `requires`, empty when it needs nothing. Only
  hard requirements are declared, exactly those enforced before, so every
  configuration that started before still starts. Optional companions
  (messaging for scheduled work) are not requirements.
- An app's public surface is its package and the modules with `__all__`
  (`docs/api.md`, [ADR 0076](0076-public-api-is-docs-api-md.md)).
  `test_contrib_boundaries.py` walks every import of the framework and fails
  on an import of another app's other modules, or of names outside an
  `__all__`. The imports that existed in 0.9.2 are listed there with their
  reasons; the list may only shrink, and the test fails on an entry no
  longer needed.

## Consequences

- A project reads an app's dependencies in one place, and a missing one is
  refused before anything is wired, with one message format.
- The privacy app's message changed wording to that format.
- New coupling between apps, or from the core into an app, fails CI. Once
  the modules the pages use had their `__all__`, 30 imports were left (24
  between apps, 6 from the core); they are 0.10's work: move e-mail syntax
  to `domainkit.utils`,
  give auth, content types, and messaging the public names the others use,
  and move the access rules out of `contrib.auth` (DSGN-02).
