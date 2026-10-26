# Upgrading

How to move a project to a new minor version, and what each one asks of
it. A minor version may change a default or break a behaviour, each listed
here with the setting that keeps the old one, and it removes only names
the minor before deprecated ([versioning.md](versioning.md)). Patch
releases change nothing a project relies on: take them as they come.

## How to upgrade

Upgrade one minor at a time, from its newest patch release (0.9.2, then
0.10.x):

1. Read the section below for the version you move to.
2. Raise the version in the project's `pyproject.toml`, and install.
3. Run the tests with deprecations as errors,
   `pytest -W error::DeprecationWarning`, so each deprecated name the
   project still uses fails, naming its replacement. Replace them all:
   they are removed in the next minor.
4. `python manage.py makemigrations`: a contrib app's tables may have
   changed. Review the revision, then `migrate` it in a test database.
5. `python manage.py check` with the production settings and variables,
   for the deployment checks this version adds.
6. Deploy as usual: [operations.md](operations.md#migrations-in-a-rollout).

## 0.9 to 0.10

### Breaking changes

Each changes what a project gets without changing its code. Each says how
to keep the old behaviour, for as long as the project needs it.

- **Deny by default without `contrib.auth`** (DSGN-02). A project that
  authenticates its own way, without `domainkit.contrib.auth`, now has deny
  by default too: a route that declares no rule (`Require`, `CurrentUser`,
  a `reads_principal` dependency, or `Public`) is listed by `check`, and
  staging and production refuse to start. **Fix:** give each route its
  rule, or `dependencies=[Public]` for a route anyone may call. **To keep
  the old behaviour:** `REQUIRE_ROUTE_RULES = False`.

<!-- 0.10 placeholder for the coordinator: the **Breaking:** entries of the
other work packages (D2, H1, A1, P1, K1) go here, one bullet each, in this
form: what changed, the fix, and how to keep the old behaviour, ending in
the audit id. domainkit_tests/test_docs.py fails until every audit id of a
Breaking or Deprecated entry under [Unreleased] is on this page. -->

### Deprecated, removed in 0.11

Each still works in 0.10, with a `DomainkitDeprecationWarning` that names
its replacement.

| Deprecated | Use instead | Audit id |
| --- | --- | --- |
| `from domainkit.contrib.auth import IsAuthenticated` (and `AllowAny`, `BasePermission`, `HasObjectPermissions`, `HasPermissions`, `HasScope`, `IsAuthenticatedOrReadOnly`, `IsUnscoped`, `ModelPermissions`, `NotAuthenticated`, `Public`, `Require`, `check_object_permissions`, `mark_public`, `open_routes`) | `from domainkit.http.access import ...`, the same objects | DSGN-02 |
| `domainkit.contrib.auth.access` (all of it: the names above, and `AllOf`, `AnyOf`, `Not`, `Rule`, `SAFE_METHODS`, `MODEL_ACTIONS`, `scopes_of`) | `domainkit.http.access`; `IsAdminUser`, `IsSuperuser`, `AuthenticatedUser`, `CurrentUser`, and `current_user` stay in `domainkit.contrib.auth` | DSGN-02 |
| `AUTH.REQUIRE_ROUTE_RULES` | `REQUIRE_ROUTE_RULES` | DSGN-02 |

<!-- 0.10 placeholder for the coordinator: the other work packages'
### Deprecated entries go in this table. -->

The whole list, with the dates, is in
[versioning.md](versioning.md#deprecations-planned-for-011), and every
change in the [CHANGELOG](../CHANGELOG.md).
