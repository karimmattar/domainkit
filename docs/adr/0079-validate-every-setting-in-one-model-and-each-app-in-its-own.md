# 0079. Validate every setting at startup: one model, and each app's own

- Status: Accepted
- Date: 2026-10-25

This records a decision taken in 0.1.0 and extended since, which no record
described (DSGN-15). It changes nothing in the code.

## Context

Django's settings are a module of names that nothing checks: a misspelled
name is ignored, a wrong type fails when first used, and a default lives
wherever the code reads it. For a service that moves money, a timeout
spelled wrong or a TLS mode left out should stop the deployment, not
surface during an incident. Settings also hold secrets (database URLs,
keys), which error messages and reprs must not show.

The framework's own settings, each optional app's (`AUTH`, `MESSAGING`,
...), and a project's need the same treatment, but an app's settings only
matter when the app is installed.

Options considered:

- **Django's way**, with checks added one by one. Every check is code to
  write, and nothing catches the name nobody thought of.
- **One model for everything**, apps included. The framework's model would
  have to know every app, installed or not.
- **One model for the framework's settings, a model per app, and a model
  the project may extend.**

## Decision

- A settings module stays a Django-style module of UPPERCASE names,
  selected by `<PREFIX>SETTINGS_MODULE` (ADR 0006), and read through
  `domainkit.conf.Env` for values from the environment.
- `domainkit.conf.Settings`, a frozen pydantic model, validates every name
  when the settings load. Its fields are the framework's settings, with
  their defaults, types, and bounds; finite numbers only. Validation errors
  name the setting and the rule, never the value.
- An installed app declares the model of its group with
  `DomainConfig.settings_class` (`AUTH` is `AuthSettings`).
  `registry.settings(AuthSettings)` validates the group when first read,
  and again when `override_settings` replaces it.
- A project declares its own names by subclassing `Settings` and naming the
  subclass with `SETTINGS_CLASS`.
- A name that neither the framework, an installed app, nor the project's
  class declares stops startup, with the closest known name.
- Settings are read-only at runtime; tests change them with
  `override_settings`.
- Secrets are `SecretStr`, and app groups show as `**********` in reprs and
  dumps.
- Staging and production add the deployment checks
  (`docs/reference.md`, "Deployment checks").

## Consequences

- A misspelled or misplaced setting fails at startup, with a suggestion.
- Defaults and bounds live in one place per group, which the reference
  pages are tested against (`domainkit_tests/test_docs.py`).
- A new setting is a field with a bound; in a patch release it defaults to
  the earlier behaviour (`docs/versioning.md`).
- The framework's settings are one flat namespace of some 80 names;
  DSGN-08 proposes grouping them, with the flat names kept as deprecated
  aliases, for 1.0.
- The settings object is a module-level proxy, as in Django. Two
  applications with different settings cannot share a process, and
  `override_settings` changes them for every thread; DSGN-07 plans a
  container for 1.0.
- Deployment checks stop at the first failure (DSGN-08); a check registry
  that reports them all is planned.
