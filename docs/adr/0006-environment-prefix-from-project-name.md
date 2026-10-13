# 0006. Environment variable prefix from the project name

- Status: Accepted
- Date: 2026-10-14

## Context

Several services run side by side in one environment, and generic variable
names (`DATABASE_URL`) collide or are read by the wrong process. A prefix
hard-coded in a project template is copied into every new service, and is
then wrong for all of them.

## Decision

Derive the prefix from `[project].name` in `pyproject.toml`: `billingAPI`
gives `BILLING_API_`. The framework's own variables use it too
(`BILLING_API_SETTINGS_MODULE`). `[tool.domainkit] env-prefix` overrides
it, and installed images without `pyproject.toml` fall back to the
distribution's name.

## Consequences

A new service gets its own namespace by renaming the project. Renaming a
project renames its variables, so deployments must be updated together with
the rename.
