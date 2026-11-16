# 0151. Object grants that expire, roles, and owners a caller administers

- Status: Accepted
- Date: 2026-11-16
- Amends: [0026](0026-object-permission-grants.md)

## Context

A project that builds on the framework reported two gaps (user report D2,
low). It does not use either app yet; it found them while planning.

1. `contrib.objectpermissions` ([ADR 0026](0026-object-permission-grants.md))
   stores one permission on one object for one user or group, for ever.
   It has no expiry and no roles, and it refuses a permission of another
   model on an object: "Ada may add members to this company" is
   `memberships.add_membership` on one `Company`, and `grant_permission`
   answers `InvalidGrant` (`services.py`, `_check_permission`).
2. The self-service webhooks (`contrib.webhooks.owned`) take the owner
   from the user alone: `owned_router(owner=...)` maps the user to one
   owner (`OwnerOf = Callable[[AbstractBaseUser], str]`). A company's
   webhooks, managed by whoever administers it, cannot be offered:
   nothing in the request may name the company, and nothing checks the
   caller's right to it. Events name their owner as text
   (`expose(..., owner=...)`), so the delivery side already takes any
   owner.

### What others do

- **django-guardian** [1] stores grants by content type and object id.
  `assign_perm` looks the permission up by the object's content type,
  and `BaseObjectPermission.save` refuses one of another model ("Cannot
  persist permission not designed for this class"). It has no expiry and
  no roles; groups stand in for roles.
- **django-rules** [2] computes object permissions from predicates in
  code, with no table: `is_book_author` decides from the object's
  fields. Roles and time are the project's own predicates.
- **Zanzibar** [3] and its open implementations store relations
  (`document:1#viewer@user:ada`) and compute permissions from them.
  **OpenFGA** [4] models roles as relations assigned to users and
  permissions as relations derived from roles (`define can_edit: editor
  or owner`), and expresses a grant in time as a condition on the tuple
  (`current_time < grant_time + grant_duration`) [5]. **SpiceDB** [6]
  has expiring relationships: "As soon as a relationship expires, it
  will no longer be used in permission checks", and garbage collection
  deletes it later (by default after 24 hours); writing it again
  (`TOUCH`) renews it.
- **Cloud IAM** grants for a time with a condition checked on each
  request (`request.time < timestamp(...)` in Google Cloud [7]).
  Temporal RBAC [8] and NIST SP 800-53 AC-2(2) [9] ask that temporary
  access end on its own at a set time, without someone remembering to
  remove it.
- **Webhooks owned by an organisation.** GitHub keeps repository,
  organisation, and user webhooks apart, each at its own path
  (`/repos/{owner}/{repo}/hooks`, `/orgs/{org}/hooks`), and "you must be
  an organization owner to create and manage webhooks in an
  organization" [10]. Stripe Connect has endpoints for the platform's
  own account and for connected accounts; each event names the account
  it is about (`account`) [11]. Both name the owner in the request, and
  check the caller's right to it on every call.

### Options

**Expiry.**

| Option | What it costs |
| --- | --- |
| A job that deletes grants when they expire | A grant counts until the job runs, and for ever if it stops. |
| Conditions on grants (OpenFGA's) | A small language to store, check, and compile to SQL, for one use. |
| **An `expires_at` column, read by every check** | One nullable column and one `OR` in each read; expired rows stay until deleted. |

**Roles.**

| Option | What it costs |
| --- | --- |
| A group per object and role (guardian's answer) | A group for each company and role: the admin's groups fill with them, and groups are global, not about one object. |
| Roles in tables, expanded at check time (Zanzibar's model) | Two tables; every read path joins them, `VisibleTo`'s `EXISTS` too. A role that grows hands each holder permissions no granter held. |
| One row per role, expanded from roles in code at check time | `permission` becomes nullable; every read path expands roles; the same ceiling problem. |
| **A role is a named set of permissions in code; granting it writes one grant per permission, named after the role** | One nullable `role` column. Reads are unchanged. A role's later change reaches its holders only when it is granted again. |

**A permission of another model.**

| Option | What it costs |
| --- | --- |
| Drop the check | A typo, or any permission, is granted on any object. |
| A flag on each call (`grant_permission(..., foreign=True)`) | Every caller decides; the check moves to call sites. |
| A custom permission on the target model (`companies.add_member`) | Possible today, but holders of `memberships.add_membership` on the model no longer count, and one right has two names. |
| **The project declares, per model, which permissions of other models apply (`grantable_on`), in `ready()`** | A hook point read at grant time and when `VisibleTo` is bound. Without a declaration nothing changes. |

**Owners a caller administers.**

| Option | What it costs |
| --- | --- |
| The owner from the body or the query, and a check function | Easy to check on create and forget on delete or deliveries. |
| A hook point every self-service router asks | One rule for every mount: user and company webhooks cannot sit side by side. |
| **A resolver per router: a FastAPI dependency inside the owner dependency every route already has** | The project writes one function; FastAPI fills its parameters (the path's company id, the user) and shows them in the schema. |

## Decision

### Grants that expire

- `ObjectPermission.expires_at` (nullable, indexed). A grant counts while
  `expires_at` is null or later than now (`domainkit.clock.now()`), the
  rule tokens follow. The backend's `get_all_permissions` and
  `object_grants`, `VisibleTo` in SQL and in memory, and `grants_on` each
  apply it when they read. No job is needed for an expired grant to stop
  counting.
- `grant_permission(..., expires_at=...)`. The expiry is aware and in the
  future, or `InvalidGrant`. Granting again sets the expiry asked for: the
  same one changes nothing; another (or none) renews the grant under its
  id and records `ObjectPermissionGranted` again, with `expires_at`. A
  grant past its expiry and not yet deleted comes back that way, as
  SpiceDB's `TOUCH` does.
- `clear_expired_grants()` and `manage.py cleargrants` delete expired
  rows in every database that holds grants, in one statement each. They
  record no event: the grant's event already said when it ends.

### Roles

- `ObjectRole(name, permissions)` is a value in the project's code: a
  name and a sorted set of permission keys. `grant_role` grants each
  permission on the object in one transaction, as one grant each, with
  `ObjectPermission.role` set to the role's name, and the same expiry.
  The ceiling is checked for every permission, so the granter holds all
  of them; it is all or nothing.
- `revoke_role` deletes the grants named after the role, whatever the
  role names now, and checks the ceiling for each permission they hold.
  Grants of the same permission alone, or with another role, stay: rows
  are kept per permission and role, so roles that share a permission do
  not take it from each other. `revoke_permission` still takes a
  permission back however it was granted, roles included, so a
  revocation always revokes.
- Checks read rows of permissions, as before. A role adds no cost to a
  check, and its definition need not be loaded to answer one.

### Permissions of another model

- `grantable_on(Company, "memberships.add_membership")`, called in a
  domain's `ready()`, registers on the hook point
  `objectpermissions.grantable`. `grant_permission` and `grant_role`
  accept a permission of another model when a hook names it for the
  object's content type; `VisibleTo.bind` accepts a key of another
  domain the same way. Without a registration the check is today's.
  `has_perm(user, permission, company)` needs nothing new: the backend
  answers with the keys of the grants on the object.

### Owners a caller administers

- `owned_router(resolver=...)` takes a FastAPI dependency that returns
  the owner the request acts for, or None when the caller may not manage
  it. It sits inside the owner dependency of every route (list, create,
  read, change, rotate, deliveries, delete), after the user is
  authenticated, so a resolver that forgets the user still leaves the
  routes closed. None answers 403 `permission_denied`. Within the owner
  the resolver names, another owner's subscription is 404, as before.
- `owned_router(owner=...)` and the module's `router` are unchanged: the
  user's id stays the default owner. Giving both `owner` and `resolver`
  is `ImproperlyConfigured`.
- The docs ask for prefixed owners (`company:42`, `tenant:42`), since a
  user's subscriptions are owned by the bare user id. It is not enforced:
  stored owners of earlier releases stay valid.

## Consequences

- The grants table gains two nullable columns and an index: projects
  with the app installed run `makemigrations` and `migrate` before the
  new code serves. Grants made before never expire and name no role.
- Expired rows stay until `cleargrants` runs. They cost a little in the
  indexes, never an answer.
- Time is the application's clock, not the database's: replicas whose
  clocks differ by a second disagree for that second about a grant at
  its edge.
- A role's change reaches its holders only when `grant_role` runs again,
  which adds what is new; what it no longer names stays until
  `revoke_role`. This keeps the ceiling exact: no holder gains a
  permission that no granter held when granting.
- An event still has one owner (`expose(owner=...)`). An event that
  concerns a user and their company goes to one of them; a project that
  needs both exposes two event types.
- Owners are text the project names. Two kinds of owners with bare ids
  can collide; the docs say to prefix them.

## References

1. django-guardian, `guardian/managers.py` (`assign_perm`) and
   `guardian/models/models.py` (`BaseObjectPermission.save`):
   <https://github.com/django-guardian/django-guardian>
2. django-rules: <https://github.com/dfunckt/django-rules>
3. Pang et al., "Zanzibar: Google's Consistent, Global Authorization
   System", USENIX ATC 2019:
   <https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/>
4. OpenFGA, roles and permissions:
   <https://openfga.dev/docs/modeling/roles-and-permissions>
5. OpenFGA, conditions (temporal access):
   <https://openfga.dev/docs/modeling/conditions>
6. SpiceDB, expiring relationships:
   <https://authzed.com/docs/spicedb/concepts/expiring-relationships>
7. Google Cloud IAM, temporary access:
   <https://docs.cloud.google.com/iam/docs/configuring-temporary-access>
8. Bertino, Bonatti, Ferrari, "TRBAC: A Temporal Role-Based Access
   Control Model", ACM TISSEC 4(3), 2001.
9. NIST SP 800-53 Rev. 5, AC-2(2), automated temporary and emergency
   account management: <https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final>
10. GitHub, types of webhooks:
    <https://docs.github.com/en/webhooks/types-of-webhooks>
11. Stripe, Connect webhooks: <https://docs.stripe.com/connect/webhooks>
