# 0015. Users, groups, and permissions modelled on Django's

- Status: Accepted
- Date: 2026-10-14

## Context

The framework had no notion of a user ([SECURITY.md](../../SECURITY.md)):
rate limits, idempotency keys, and the audit trail were meant to be per
actor, but nothing set one, and generated routes were open. Every project
would build users, roles, and password storage again, each differently,
and some badly.

Django's `contrib.auth` and `contrib.contenttypes` are the model most
engineers who use this framework know: users, groups, permissions named
`<app>.<action>_<model>`, backends, password hashers and validators, and a
swappable user. What does not carry over is the request model: these
services are REST APIs, async, and domain-driven, with aggregates mapped
imperatively ([0004](0004-async-sqlalchemy-imperative-mapping.md)) and no
sessions or HTML pages.

## Decision

- Two apps, as in Django: `domainkit.contrib.contenttypes` (one row per
  mapped class of an installed domain, `<domain>.<model>`) and
  `domainkit.contrib.auth`, which needs it.
- Permissions are keyed `<domain>.<codename>`. `migrate` creates `add`,
  `change`, `delete`, and `view` for every content type, and what a class
  declares in a nested `Meta`, through a new hook,
  `DomainConfig.migrated(connection, database)` (Django's `post_migrate`).
  Existing rows are never changed or removed.
- `User`, `Group`, and `Permission` are domain classes mapped imperatively.
  Users load with their groups and permissions (`selectin`), so
  `user.has_perm` needs no query; backends (`AUTH.BACKENDS`) add object
  permissions and other sources, and `PermissionDenied` ends a search.
- The user model is swappable (`AUTH.USER_MODEL` with `AUTH.USER_TABLE`):
  subclass `AbstractUser` or `AbstractBaseUser`, build the table from the
  provided columns and tag it with its domain, and the link tables are
  named after it and routed with it. A second hook,
  `DomainConfig.holds_table(table)`, keeps the unused default tables out
  of migrations and content types, without dropping them.
- Passwords: a `PasswordHash` value object, so raw passwords cannot be
  stored by mistake; Django's hashers and formats (PBKDF2 by default,
  Argon2 through an extra, scrypt), re-hashing on login when settings
  change, hashing off the event loop, equal timing for unknown users, and
  Django's four validators with its list of common passwords.
- The test hasher is refused in staging and production.
- Authenticating requests (opaque tokens by default, JWT as an option), the
  middleware that sets the actor, FastAPI dependencies, and an admin API
  follow in later releases, on this model.

## Consequences

Projects get one, reviewed way to store users and decide permissions, and
Django users find it familiar; hashes moved from a Django database keep
working. Users, groups, permissions, and content types must share a
database (foreign keys join them), and a project must choose its user model
before its first users exist, as in Django. Permission checks through
backends are async (`await has_perm(...)`); the user's own check is not.
Content types and permissions grow with every mapped class, including the
framework's own.
