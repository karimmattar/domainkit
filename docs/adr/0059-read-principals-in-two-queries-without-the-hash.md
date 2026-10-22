# 0059. Read a principal in two queries, without the password hash, and build each request's user

- Status: Accepted
- Date: 2026-10-21
- Amends: [0016](0016-authenticating-requests.md)

## Context

A principal was read with the ORM: the token, then the whole user row
(password hash included), then groups, the groups' permissions, and the
user's own permissions, each a `selectin` query (N6). Caching principals
(ADR 0058) adds three needs:

- a kept principal must give each request exactly the user a read would
  have built, and an instance of the user model, since rules, `/me`, and
  projects read its fields, groups, and permissions;
- ORM instances cannot be shared between requests: they are mutable, a
  handler may change them, and a session may adopt them;
- the password hash must never be kept.

ADR 0054 made relationships raise when they are not loaded; `has_perm` and
the access rules must behave as before.

Options considered:

- **Keep the ORM read, and copy its objects for each hit.** The hash would
  be kept, and copying ORM graphs is as costly as building them.
- **Keep a small principal (id, flags, permission keys) and make
  `request.user` another type.** `isinstance(user, AbstractBaseUser)`,
  `request.user.email`, and `/me` would break.
- **Load permissions only when a rule needs them** (as N6 suggested).
  `has_perm` is synchronous: an unloaded permission set would raise, or
  need I/O, on routes where it works today. With principals kept, a hit
  costs nothing either way; deferring would save one query on a miss.
- **Read a snapshot in two queries, and build a user from it per request.**

## Decision

- **Two queries.** The token by its digest (or a login's tokens, or a
  user by name for certificate subjects). Then, in one statement, the
  user's columns without the password, and every grant: the user's groups,
  each with its permissions, and the user's own permissions (a `UNION
  ALL`, joined to the user's row). The same statement checks that the
  token is still stored (and a certificate subject's name still the
  user's), so a revocation committed between the two queries is never kept.
- **The permission set loads with the principal**, for every route, so
  `has_perm` works wherever it did.
- **Each request gets its own user**, built from the snapshot as a closed
  session leaves a loaded one: the mapper's instance factory, the column
  values, `groups` and `user_permissions` as loaded collections of new
  `Group`, `Permission`, and `ContentType` objects, then detached with an
  identity. A miss builds it the same way, so a hit and a miss build the
  same user. Column values of mutable types are copied for each request.
- **The password hash is never read.** On `request.user` it is unloaded:
  reading it raises `DetachedInstanceError`, as unloaded attributes do
  (ADR 0054). The routes that check the current password
  (`/password`, the second-factor changes) read the hash afresh
  (`password_holds`, `UserRepository.password_hash`).
- A user model that maps other relationships that load with it (not
  `raise`) cannot be held in a snapshot: it is read by the ORM, as before
  but without the hash, on every request, and never kept.
- `last_used_at` is written in a transaction of its own, by one request
  per `LAST_USED_EVERY`, kept principal or not.

## Consequences

- A miss makes two `SELECT`s instead of four (a user in no group) or five
  (in a group).
- `request.user.password` no longer works: code that checks a password
  reads the hash in a unit of work. The changelog says so.
- Building a user per request costs CPU in proportion to its groups and
  permissions (tens of microseconds for a few, more for hundreds), far
  less than the queries it replaces.
- A miss that finds `last_used_at` due opens a second transaction, once
  per token per five minutes.
