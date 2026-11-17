# 0177. Let a key reach every tenant, name a domain's own keys, and search every word at once

- Status: Accepted
- Date: 2026-11-17
- Amends: [0150](0150-take-the-tenant-from-the-path-and-let-members-hold-permissions.md), [0170](0170-let-a-resources-items-be-tenants-count-members-keys-alone-and-let-lists-name-what-they-refer-to.md)

## Context

A project that keeps its companies as tenants since 2.5 reported, against
2.6.0, what tenancy (D3) and lists (D2) still made it do on its own.

D3:

- **The admin API puts every tenant staff are not in beyond them.**
  `tenants_beyond` counts a tenant of the user's that the staff member
  does not belong to as beyond them, whatever they hold everywhere. A
  back-office officer granted every company permission everywhere changes
  no company's member: only superusers do. In a tenant they share, it
  compares what each holds *now* (`MEMBER_PERMISSIONS`). A suspended
  company's owner only looks, so staff who belong to it and hold its
  looking keys take over an owner who holds everything once the company
  is reactivated. The project lists what memberships grant whatever the
  company's state in a backend of its own (`object_grants`).
- **`MemberActions` names no key of a domain's own.** It asks
  `<domain>.<action>_<model>` for every action, as `ModelActions` does.
  An invitation posted to a company needs another domain's
  `organizations.add_membership`, which names no model of the companies'
  domain. The project subclasses `MemberActions`.
- **Nothing tells a person's limit from a tenant's.** A `Throttle` on a
  route served as a tenant counts per tenant unless it says
  `per_tenant=False`. Moving routes under `PathTenant` multiplies each
  person's limit by their tenants, silently.

D2:

- **A file gateway cannot name the route's file.** The infrastructure
  layer may import `domainkit.http.uploads` since 2.5, but not
  Starlette's `UploadFile`, the type of what its route hands it, nor
  `domainkit.http.errors`. The project keeps an extra `gateways` layer.
- **`search_through` asks per word.** Each word's ids stop at
  `max_reference_matches` before the words are intersected. Two common
  words, each naming thousands of users, find no row though rows match
  both, where one query for the users every word names would.
- **The admin user list answers whole users.** `GET /users?id=...` names
  a page's people, but with their groups, direct permissions, and every
  admin field, to a page that shows a name.

Constraints:

- 2.7 is a minor release: nothing a project relies on changes, and a new
  behaviour that widens access is opt-in.
- ADR 0150 decided that staff change "a user inside a unit (a tenant)
  only from inside it", and ADR 0150's consequences say so: a project may
  rely on it as a guarantee.
- The core never imports a contrib app (ADR 0080): `tenants_beyond` is
  core, the admin API is `contrib.auth`'s.
- The layer rules only add (ADR 0003): a project adds rules, never
  exceptions.

How others do it:

- Microsoft Entra ID scopes an administrator's role to the whole
  directory or to an administrative unit. A role assigned to the
  directory manages the users of ordinary units; only a *restricted
  management* unit keeps them out, "without having to remove
  tenant-level role assignments", and "only administrators with an
  explicit assignment at the scope of a restricted management
  administrative unit can change" its users [1].
- OWASP ASVS 5.0 asks that function-level access be restricted to
  consumers with explicit permissions (8.2.1), and that operations
  never affect tenants "with which they do not have permissions to
  interact" (8.4.1) [2].
- django-guardian counts a global permission as one held on every
  object, but only where the view says so (`accept_global_perms`) [3].
- Django's admin splits a search into words; each must be in at least
  one search field of the same row, a relation's included, by a join;
  a quoted phrase is searched whole [4].
- Google's AIP-157 offers partial responses through a view enumeration
  (`BASIC`, `FULL`) first, and field masks after; it warns that a
  partial default for standard methods hurts declarative clients [5].
  JSON:API names the fields per type (`fields[people]=name`) [6].
- GitHub counts a user's requests against one budget, whichever
  organisation they touch [7]. Django's checks warn of what is valid
  but likely a mistake, and let a project silence one by id [8].

Options for staff and tenants they are not in:

- **`keys <= held` for every tenant, by default** (the project's
  proposal). Any staff member who holds a member's keys everywhere would
  change them, and with them gain the member's admission to a tenant:
  a member holding no keys (`MEMBERSHIP`, or a role with none) would be
  anyone's who may change users. It turns ADR 0150's guarantee off in a
  minor release, and makes reaching a tenant follow from holding keys,
  not from a permission to reach it (ASVS 8.4.1).
- **A setting, true or false**, that makes every global key reach every
  tenant. Opt-in, but all-or-nothing: a helpdesk granted
  `auth.change_user` and a few keys reaches every tenant's members who
  hold no more.
- **A permission that reaches every tenant, named by the project**
  (`TENANCY.ALL_TENANTS_PERMISSION`): staff who hold it everywhere
  count a tenant they are not in as Entra counts a unit for an
  administrator assigned to the directory: beyond them only for keys
  they lack. Unset, as in 2.6. Granted per person or group, through the
  admin API's own ceilings, recorded as every grant is.

For what a role gives back, a backend's `object_grants` reads objects,
not tenants, and the admin API asks every backend; a hook beside
`MEMBER_PERMISSIONS` (`MEMBER_GRANTS`), answering keys whatever the
tenant's state, keeps it in tenancy's own vocabulary. The admin API
counts its keys with `MEMBER_PERMISSIONS`' answer, as the most a member
may hold there; requests are still served with `MEMBER_PERMISSIONS`
alone.

For a domain's own keys:

- **An action holding a dot names its key whole** (`action=
  "organizations.add_membership"`). A route's action is also what
  `outside` reads, OpenAPI's summaries, and approvals' rules; a dotted
  action would be a key in some places and a word in others, and on a
  resource whose rule is not `Actions` it would name a key that nothing
  asks.
- **`keys={action: key}` on the rule.** The resource's rule says, in one
  place, which key each of its own actions asks; routes keep plain
  actions. `ModelActions` takes it too, as it builds the same rule.

For throttles on tenants' routes:

- **Change the default to `per_tenant=False`.** It changes every count of
  every project, in a minor release.
- **A check that names each throttle on a route served as a tenant that
  says neither** (`tenancy.W001`): `per_tenant` defaults to None, which
  counts as before; `True` says the per-tenant count is meant. A route is
  served as a tenant when it takes one from its path (`PathTenant`), or,
  under `TenantMiddleware`, unless it serves none (a tenants' collection).
  It is a warning: `check` passes, `check --deploy` fails until the
  project says, or silences it.

For a gateway's file:

- **Allow `starlette.datastructures` and `domainkit.http.errors` in the
  infrastructure layer.** The layer rules only add, and 2.5 kept both out
  on purpose: an adapter raises domain errors, which the HTTP layer maps.
- **Name the type through the module the layer may import.** `Upload`,
  the alias `save_upload` and `receive_upload` take (a form's file,
  bytes, or a stream), was private. Public, a gateway types the route's
  file by it, and catches `UploadRefused` from the same module.

For searching every word:

- **Change `Finder` to take the words.** Breaks every finder written for
  2.5.
- **`EveryWord(finder)`**, a marker: the finder takes a tuple of words.
  It is asked with each word alone, so a search of a role and a user
  ("owner smith") still matches word by word, and once more with every
  distinct word, where one word alone named `max_reference_matches` rows
  through it (else the words' answers were whole, and their intersection
  is exact). A row whose field is among those ids matches the search.

For naming users:

- **`?fields=` on `GET /users`.** Its schema is fixed (`UserPage`): a
  field mask would answer what OpenAPI does not describe, and a client
  reading `groups` would get a 200 without them.
- **A view of its own: `GET /users/names`**, AIP-157's `BASIC` beside
  `/users`' `FULL`: the same filters, order, cursors, and rule, and each
  user's id, username, full name, and the identifier fields `/users`
  shows. `GET /users` stays as it is.

## Decision

- **`TENANCY.ALL_TENANTS_PERMISSION`**: a permission key, unset by
  default, needing `MEMBER_TENANTS`. `tenants_beyond(actor, user, held)`
  counts a tenant `actor` does not belong to as beyond them unless `held`
  (what they hold everywhere: no tenant-scoped backend) holds the key and
  every key the user holds there. A tenant no hook answers for, among
  those, stays beyond. Shared tenants are counted as before.
- **`TENANCY.MEMBER_GRANTS`**: a function of the user and a tenant id
  (async or not), needing `MEMBER_PERMISSIONS` and `MEMBER_TENANTS`, that
  answers the keys the user's roles grant there whatever the tenant's
  state, or None. `tenants_beyond` counts them with `MEMBER_PERMISSIONS`'
  answer in what the user holds; the staff member's own keys in a shared
  tenant stay what they hold now. A string or a bool answered is refused
  (`ImproperlyConfigured`).
- **`ModelActions(model, *rules, keys=None)` and `MemberActions(model,
  *rules, outside=None, keys=None)`**: `keys` maps an action to its key,
  asked whole (`HasModelPermission(action, model, key=...)`); other
  actions keep their model's key. A key that is not `<domain>.<codename>`
  is refused (`ValueError`).
- **`Throttle(per_tenant=None)`**: None counts per tenant, as `True` did;
  `per_tenant_said` says whether it was given. `check` warns, in every
  environment, of each throttle that leaves it unsaid on a route served
  as a tenant (`tenancy.W001`).
- **`domainkit.http.uploads.Upload`** is public. The layer rules do not
  change.
- **`domainkit.http.EveryWord(finder)`**: a `search_through` finder of
  the words and a limit. `FilterSet.through` keeps what it names for the
  words together under the distinct words, in lowercase, joined by
  spaces; a row matches every word, each as before, or its field is
  among those ids.
- **`GET /users/names`** on the admin API: `UserNamePage` of
  `UserNameOut` (`id`, `username`, `name`, `identifiers`), with
  `search`, `id`, and the page's `limit` and `cursor`, under
  `auth.view_user`.

## Consequences

- A back office reaches every company's members by a grant the project
  names, and only within what its staff hold everywhere. A project that
  sets nothing keeps 2.6's rule: staff change only the members of
  tenants they belong to.
- Granting `ALL_TENANTS_PERMISSION` is granting a takeover of every
  tenant's members who hold no more than the grantee: the admin API's
  ceilings bound who may grant it, as for any key.
- With `MEMBER_GRANTS`, a change asks one more hook per tenant of the
  user's that is counted; without it, nothing more is asked.
- A route of a domain's own action under `MemberActions` needs no
  subclass; routes keep plain action names.
- Projects with throttles on tenants' routes get `tenancy.W001` until
  each says `per_tenant=True` or `False`; `check --deploy` fails on it
  until then, or until `SILENCED_CHECKS` names it.
- `EveryWord` costs one more call per finder and search, only for
  searches of two words or more where a word reached the bound.
- `GET /users/names` is declared before the resource's `/users/{id}`, so
  `names` is never read as an id.
- Per request, `resource-list`, `resource-read`, `resource-command`,
  `read`, `me` and `throttled` make the same Python calls and load the
  same modules as 2.6.0 (2,717, 1,938, 2,681, 1,669, 686 and 505 calls;
  835 modules), on PostgreSQL.

## References

1. Microsoft Entra ID, *Restricted management administrative units*:
   <https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/admin-units-restricted-management>
2. OWASP ASVS 5.0, *V8 Authorization*, 8.2.1 and 8.4.1:
   <https://github.com/OWASP/ASVS/blob/master/5.0/en/0x17-V8-Authorization.md>
3. django-guardian, *Mixins*, `PermissionRequiredMixin.accept_global_perms`:
   <https://django-guardian.readthedocs.io/en/stable/api/mixins/>
4. Django, *The Django admin site*, `ModelAdmin.search_fields`:
   <https://docs.djangoproject.com/en/5.2/ref/contrib/admin/#django.contrib.admin.ModelAdmin.search_fields>
5. Google AIP-157, *Partial responses*:
   <https://google.aip.dev/157>
6. JSON:API 1.1, *Sparse fieldsets*:
   <https://jsonapi.org/format/#fetching-sparse-fieldsets>
7. GitHub REST API, *Rate limits for the REST API*:
   <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>
8. Django, *System check framework*:
   <https://docs.djangoproject.com/en/5.2/topics/checks/>
