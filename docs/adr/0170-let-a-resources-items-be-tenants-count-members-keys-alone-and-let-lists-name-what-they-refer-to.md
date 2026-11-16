# 0170. Let a resource's items be tenants, count members' keys alone, and let lists name what they refer to

- Status: Accepted
- Date: 2026-11-16
- Amends: [0150](0150-take-the-tenant-from-the-path-and-let-members-hold-permissions.md)

## Context

A project reported (D3) that, after 2.4 took the tenant from the path
(ADR 0150), it still kept memberships of its own, for three reasons:

- `PathTenant` does not fit a `ResourceRouter`. Its companies resource
  lists at `/companies`, whose path has no `{company_id}`. On the
  router, `PathTenant("company_id")` makes `check` refuse the list
  (`tenancy.E001`), and `command_route` takes no `dependencies=`, so it
  cannot be put on the item routes alone.
- `MembershipBackend` grants on no object, so a member's keys are read by
  `HasPermissions`, which also counts `ModelBackend`'s global grants. A
  back-office officer who is a company's member acts on its routes with
  their staff permissions.
- Limits and caches inside a `PathTenant` route count per tenant
  (`scoped_key`), with no way to keep one count: a member of ten
  companies sends ten times the day's invitations.

In the same report (D2), lists could not name what another domain holds:

- `present(aggregate)` is synchronous and called per item, so showing a
  member's name means a read per row. The admin API's user list takes
  `search` only, not ids, so a page cannot ask for its rows' people in
  one request. The project wrote `GET /backoffice/people?id=...` for it.
- A `FilterSet` searches its own fields, or a child entity's. A list of
  members cannot be searched by the member's user's name. The project
  turns the words into ids first, in a `filters` dependency of its own.

Constraints:

- 2.5 is a minor release: nothing a project relies on changes. New
  behaviour is opt-in.
- The core never imports a contrib app (ADR 0080). `domainkit.tenancy`
  and the rules are core; users and `has_perm` are `contrib.auth`'s.
- Aggregates refer to each other by id. A member's user may live in
  another database (auth's), so no SQL join reaches it.
- `ResourceRouter` declares every route through one function
  (`declaring.add_route`). Other route kinds (twins, uploads) are being
  added beside it, and must get the same treatment without work of their
  own.

How others do it:

- Tenancy for Laravel identifies the tenant by path with route
  middleware on the routes that hold `{tenant}`; central routes, without
  the parameter, are a separate group [1]. Django REST framework's
  viewsets choose permissions per action (`get_permissions`, by
  `self.action`), so a list and a detail route may differ [2].
- django-guardian's `PermissionRequiredMixin` checks object permissions
  only by default (`accept_global_perms = False`): a global grant does
  not count on an object unless the view says so [3]. Spatie's
  laravel-permission, with teams on, answers every check for the current
  team's assignments; a role is counted in a team only where it is
  assigned there [4].
- GitHub counts an authenticated user's requests against one budget,
  "all of these requests count towards your personal rate limit",
  whichever organisation they touch [5]. OWASP API4:2023 asks for limits
  on how often a client may call, and spending limits on what costs
  money, such as mail and SMS [6].
- RFC 9562 writes a UUID in lowercase and reads it in any case [7]: two
  spellings name one value.
- GraphQL's DataLoader collects the keys asked for in one tick and calls
  one batch function with all of them, which answers one value per key,
  in order [8]. JSON:API's `include` embeds related resources in one
  compound document [9]. The API composition pattern asks each service
  for its part and joins them in memory, and warns that large joins cost
  [10].
- Django's admin searches through a relation with `__`
  (`search_fields = ["user__email"]`), a join, and warns that fields
  without an index are slow on large tables [11]; Django REST framework's
  `SearchFilter` does the same [12]. PostgreSQL does not index a
  referencing column by itself [13].

Options considered, for a resource whose items are tenants:

- **`dependencies=` on `command_route`.** It reaches the commands, not the
  read route, which `ResourceRouter.__init__` declares, and each route
  would repeat it.
- **`PathTenant` that lets a path without its parameter through.** Then a
  route that forgot the parameter would serve no tenant, silently.
  `tenancy.E001` exists to catch that.
- **`ResourceRouter(tenant=PathTenant(...))`**, applied by `add_route` to
  every route whose path holds the parameter, before the route's rules.
  The collection's routes serve no tenant, item routes do, and every
  route kind gets it from the one function.

For members' rules:

- **A flag on `MembershipBackend`** that refuses global grants while a
  tenant is served. Backends answer `has_perm` for every rule in the
  request, the admin API's included: staff could not use the admin API
  while a tenant is served.
- **Grants on the tenant as an object**, as the project's backend did:
  `has_perm(user, perm, tenant)`. Rules would need the tenant object,
  which no route loads, and `HasObjectPermissions` would ask every
  backend that grants on objects.
- **Rules that read the member's keys alone**: `member_permissions(user)`
  for the tenant served, as django-guardian reads object permissions
  alone by default. Core, for any principal, and named so a reviewer
  sees which grants count.

For a resource's collection, which serves no tenant, the member rules
refuse. A companies list must still answer. Options:

- **A second rule on `ResourceRouter`** for the collection's routes. One
  more option, read by every route kind.
- **A rule that falls back while no tenant is served**: within
  `MemberActions`, `outside=` rules the routes that serve none. It is
  composed of rules that exist (`AnyOf`, `Not`, `AllOf`), so object
  checks and scope checks read it unchanged. A resource without
  `tenant=` would serve its items as no tenant too, so `outside=` there
  would rule them all: it is refused.

For limits across tenants:

- **`shared_keys()`**, which exists, wraps code. A `Throttle` is a
  dependency: there is no code to wrap.
- **`Throttle(per_tenant=False)`**: the count is kept with
  `shared_keys()`, once per caller and scope. A limit carries no data, so
  sharing its count across tenants leaks nothing.
- **The same option on `@cached`.** A cached answer is computed as the
  tenant being served: repositories read that tenant's rows. Shared, one
  tenant's answer would reach another. Code that knows an answer belongs
  to no tenant says so with `shared_keys()`, around the call, on purpose.

For lists that name their rows' people:

- **Ids on the admin API's user list** (`?id=`, repeated, as many as a
  page holds): the page asks once, with the user's own rules
  (`auth.view_user`).
- **An include parameter** (JSON:API). A client-driven join needs a
  description of relations, rules for each, and `400` for the rest. Too
  much for a minor release.
- **An async presenter called once per answer**, as DataLoader's batch
  function: `present_many(aggregates, request)` returns one schema per
  aggregate, in order. The project's code loads what another domain
  holds for the whole page, with whatever rules it needs, and nothing
  else changes: hidden fields, links, `ETag`s, exports.

For searching through a reference:

- **A join in the query layer** (`user__name`), as Django does. Aggregates
  refer to others by id, often in another database: no join reaches them.
- **A finder per referring field**: `search_through = {"user_id":
  users_named}`. For each word, the finder returns the ids of at most
  `max_reference_matches` rows it names, searched where they live and
  indexed there; the list keeps rows whose field is `in` those ids. It
  is the project's workaround, bounded, and FastAPI runs it as a
  dependency of the `FilterSet`, so a resource and a route of one's own
  get it alike.

## Decision

- **`ResourceRouter(tenant=PathTenant("company_id"))`** serves each route
  whose path holds the parameter as that tenant: `Depends(tenant)` comes
  first among the route's own dependencies, before its rules
  (`tenancy.E002` holds). The parameter may be the resource's id, its
  parent's, or one in its prefix; one no route holds is refused
  (`TypeError`). A nested resource takes its parent's tenant unless it
  has one. Routes of one's own declared on the resource, WebSockets
  included, are served alike: the router adds the dependency to every
  route whose path holds the parameter. OpenAPI documents `401` and
  `404` on those routes.
- **`PathTenant(parameter, *, id_type=str)`**: `int` or `UUID` reads the
  parameter as that type, and the tenant id is its text (`str(UUID(...))`,
  lowercase), so one tenant has one id however the path spells it. A
  value of the wrong type is `422`, as the route would answer, for
  members and strangers alike. A resource's tenant reads its id's type.
- **`domainkit.tenancy.HasMemberPermissions(*perms)`**: an active
  caller holding every key as a member of the tenant served
  (`member_permissions`), and within their credentials' scopes. Global
  grants and a superuser's status do not count. No tenant served:
  refused. Without `MEMBER_PERMISSIONS`: `ImproperlyConfigured`.
  `InTenant` allows a request served as a tenant.
- **`domainkit.resources.MemberActions(model, *rules, outside=None)`**:
  `ModelActions`' keys (`HasMemberModelPermission`), counted by
  `HasMemberPermissions`. `outside` (a rule, or `Actions`) rules the
  routes that serve no tenant; without it they are refused. A resource
  given `MemberActions(outside=...)` and no tenant is refused
  (`TypeError`).
- **`Throttle(..., per_tenant=False)`** counts the caller once across
  tenants. The default stays per tenant. `@cached` gets no such option;
  `shared_keys()` stays the way to share keys on purpose.
- **The admin API's `GET /users` takes `id`**, repeated, up to 200 (a
  page's most), with `search` or alone.
- **`ResourceRouter(present_many=...)`**, async, in `present`'s place
  (both is a `TypeError`), shows the aggregates of each answer in one
  call: a page, an export's chunk, a query's items, one read or write.
  It must answer one schema per aggregate, in order, or the request
  fails (`RuntimeError`); an empty page asks nothing.
- **`FilterSet.search_through`** maps a field to a finder of the word and
  a limit (async or not). Each distinct word asks each finder once, for
  at most `max_reference_matches` ids (100; more are left out). A word
  matches a row that holds it in a search field, or whose field is one
  of the ids. A search the set would refuse asks nothing. A resource's
  policy may not hide the field (as for every filtered field).

## Consequences

- A companies resource lists at `/companies` and serves
  `/companies/{company_id}/...` as the company, with no router of its
  own; its members act there with their role alone, and back-office
  staff use the back office.
- A member's role is all that counts on a `MemberActions` route: a
  superuser who is a member holds there what the membership gives. A
  project that wants staff to act in tenants writes the rule for it
  (`HasMemberPermissions(...) | IsSuperuser()`).
- Credentials limited to scopes reach a member route only with the key
  in their scopes. Tokens cannot carry member keys (ADR 0150), so a
  member route is for sessions.
- `MemberActions(outside=IsAuthenticated())` checks no scopes on the
  collection's routes: scoped credentials are refused there
  (`SCOPED_CREDENTIALS`).
- A tenant whose id was typed in two spellings before (a UUID in
  capitals, with `PathTenant` on a router) is one tenant on a resource,
  and on a router given `id_type`; routers left as `str` keep reading the
  text as it comes.
- `present_many` costs one call per answer: a batch of 100 items calls
  it 100 times, once per item's answer.
- A search word that names more rows than `max_reference_matches` in the
  other domain misses the rest. A finder kept to what the list can show
  (the current tenant's members, say) does not reach the bound.
- A referring field searched through is read with `in`: index it.
- Per request, a resource route makes about one more call for its
  answer's presentation: `resource-list` 2,716 to 2,717 Python calls,
  `resource-read` and `resource-command` within one call of 2.4.0 (they
  move by one between runs), `me` and `throttled` unchanged; modules
  unchanged.

## References

1. Tenancy for Laravel v3, *Tenant identification*, path identification:
   <https://tenancyforlaravel.com/docs/v3/tenant-identification>
2. Django REST framework, *Viewsets*, introspecting viewset actions:
   <https://www.django-rest-framework.org/api-guide/viewsets/#introspecting-viewset-actions>
3. django-guardian, *Mixins*, `PermissionRequiredMixin.accept_global_perms`:
   <https://django-guardian.readthedocs.io/en/stable/api/mixins/>
4. Spatie laravel-permission v6, *Teams permissions*:
   <https://spatie.be/docs/laravel-permission/v6/basic-usage/teams-permissions>
5. GitHub REST API, *Rate limits for the REST API*:
   <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>
6. OWASP API Security Top 10 2023, *API4: Unrestricted Resource
   Consumption*:
   <https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/>
7. RFC 9562, *Universally Unique IDentifiers*, section 4:
   <https://www.rfc-editor.org/rfc/rfc9562#section-4>
8. GraphQL, *DataLoader*:
   <https://github.com/graphql/dataloader>
9. JSON:API 1.1, *Fetching includes*:
   <https://jsonapi.org/format/#fetching-includes>
10. microservices.io, *API Composition*:
    <https://microservices.io/patterns/data/api-composition.html>
11. Django, *The Django admin site*, `ModelAdmin.search_fields`:
    <https://docs.djangoproject.com/en/5.2/ref/contrib/admin/#django.contrib.admin.ModelAdmin.search_fields>
12. Django REST framework, *Filtering*, `SearchFilter`:
    <https://www.django-rest-framework.org/api-guide/filtering/#searchfilter>
13. PostgreSQL 16, *Constraints*, foreign keys:
    <https://www.postgresql.org/docs/16/ddl-constraints.html#DDL-CONSTRAINTS-FK>
