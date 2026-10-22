# 0059. Read a principal in two queries, build each request's user, and deprecate its password hash

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
- the password hash should not be kept; but `request.user.password` is
  public API (0.8.0 loaded it), and `has_usable_password()` is Django's.

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
  user's columns, and every grant: the user's groups,
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
- **The password hash is deprecated on `request.user`, and removed in 0.10.**
  Removing it at once would break code without warning
  (`docs/versioning.md`). Until 0.10 the principal holds the hash and a
  `usable_password` flag. The user built for a request holds the flag, so
  `has_usable_password()` needs no hash and gives no warning. Its
  `password` is lent as SQLAlchemy loads a deferred column: the first read
  warns with `DomainkitDeprecationWarning`, naming `password_holds` and
  `UserRepository.password_hash`, then returns the stored hash. Reading the
  hash afresh instead would need I/O in a synchronous attribute. The hash
  is in process memory for `TTL` at most, as 0.8.0 held it for a request.
  It never shows in a `repr` (a user's `repr` leaves the password out), a
  log, a cache key, or a stamp. The routes that check the current password
  (`/password`, the second-factor changes) read the hash afresh
  (`password_holds`, `UserRepository.password_hash`). In 0.10 the
  statement stops reading the hash and the principal stops holding it;
  the flag stays.
- A user model that maps other relationships that load with it (not
  `raise`) cannot be held in a snapshot: it is read by the ORM, as before,
  on every request, and never kept. Its password is lent the same way.
- `last_used_at` is written in a transaction of its own, by one request
  per `LAST_USED_EVERY`, kept principal or not.

## Consequences

- A miss makes two `SELECT`s instead of four (a user in no group) or five
  (in a group).
- `request.user.password` works until 0.10, with a warning. Code that
  checks a password reads the hash in a unit of work. The changelog lists
  it under Deprecated.
- Reading the hash with the principal adds one column to a miss's
  statement. Lending it to a hit's user costs a few dictionary operations.
  The measured cost is in `docs/performance.md`.
- Building a user per request costs CPU in proportion to its groups and
  permissions (tens of microseconds for a few, more for hundreds), far
  less than the queries it replaces.
- A miss that finds `last_used_at` due opens a second transaction, once
  per token per five minutes.
