# 0150. Take the tenant from the path, and let members hold permissions

- Status: Accepted
- Date: 2026-11-16

## Context

A project reported (D2) that tenancy fits "one tenant per request,
chosen before routing": no tenant from a path parameter, no `404` for
non-members, and no memberships that carry permissions. It kept
memberships of its own and wrote a `MembershipBackend` to make them
answer `has_perm`.

What the framework had:

- `TenantMiddleware` (`domainkit/tenancy.py`) works out the tenant before
  routing, from `RESOLVER`, the subdomain of `SUBDOMAIN_OF`, or `HEADER`
  (`TENANCY`, `conf/global_settings.py`). Before routing there are no
  path parameters: a `RESOLVER` could split the path itself, but it would
  repeat the router and could not tell a tenant route from any other.
- `MEMBERSHIP` is a function of the user and a tenant id that answers yes
  or no. A caller it refuses gets `403 not_a_member`.
- Permissions are global (`ModelBackend`: a user's and their groups'
  grants) or on one object (`contrib.objectpermissions`). Nothing grants
  a permission in one tenant only.

Constraints:

- The core never imports a contrib app (ADR 0080): the tenancy module is
  core, `has_perm` and its backends are `contrib.auth`'s.
- `TenantMiddleware`'s settings and answers are public. A project relies
  on `403 not_a_member` and on how the middleware resolves.
- FastAPI 0.143 resolves path parameters after routing, in the route's
  dependencies. A dependency with `yield` runs in the request's task,
  and by default (`scope="request"`) it exits after the response is
  sent, background tasks included (`fastapi.routing.request_response`).
  So a `with tenant(...)` inside one covers the endpoint, the response,
  and the tasks, as the middleware's covers the whole request. A plain
  function dependency runs on a thread, and what it sets is lost.

How others do it:

- django-tenants' `TenantSubfolderMiddleware` takes the tenant from the
  path segment after `TENANT_SUBFOLDER_PREFIX`, in a middleware, before
  URLs are resolved, and raises `Http404` for an unknown one [1, 2].
- Tenancy for Laravel's `InitializeTenancyByPath` is route middleware on
  a group prefixed `/{tenant}`: it reads the matched route's parameter,
  after routing, and removes it from the route's parameters [3, 4].
- Rails applications in Basecamp's style move the account id from the
  path into `SCRIPT_NAME` in a middleware, before routing [5].
- Spatie's laravel-permission keeps a team id on roles and their
  assignments; a middleware sets the current team, and every permission
  check answers for it. Its docs warn that the middleware must run before
  route-model binding, or callers get `404` where `403` is meant [6].
- Auth0 Organizations and WorkOS scope roles to an organization: a
  member holds one role (WorkOS, by default) or several, and the union of
  their permissions [7, 8].
- django-organizations ships an organization-user model with an
  `is_admin` flag, and view mixins that check membership; no roles, no
  permission backend [9].
- Django asks every backend's `has_perm(user_obj, perm, obj=None)`; any
  one may grant, and one that raises `PermissionDenied` ends the search
  [10].
- On `404` or `403`: RFC 9110 lets an origin server "hide" the existence
  of a forbidden resource by answering `404` [11]. OWASP's IDOR cheat
  sheet maps "not found" and "not yours" to one public answer, such as
  `404` [12]; its authorization cheat sheet asks for deny by default, and
  a check on every request [13]. GitHub's API answers `404`, not `403`,
  so as not to confirm that a private repository exists [14].

Options considered, for the tenant in the path:

- **A `RESOLVER` that splits the path.** It works today, but repeats the
  router, runs for every route, and answers `403`.
- **A prefix setting in `TenantMiddleware`**, as django-tenants does. It
  is still before routing, one prefix for the whole application, and
  the tenant segment either stays out of the routes' paths (so OpenAPI
  does not show it) or is cut from them.
- **A route class or router class of our own.** Every router would have
  to be that class; `select_routes` and a project's own `APIRouter`s
  would not have it.
- **A FastAPI dependency with `yield`**, put on a router: after routing,
  from the matched parameter, as Laravel does. FastAPI declares and
  documents the parameter, any router takes it, WebSocket routes too,
  and `select_routes` copies it.

For the answer to one who does not belong:

- **`403 not_a_member`**, as the middleware answers. It says the route is
  a tenant's, and with a resource path it is the answer that differs from
  "no such thing".
- **A new code, `404 tenant_not_found`.** Uniform across tenants, but not
  the answer of a route that does not exist, and one more code to list.
- **Starlette's own `404`**, the answer of a route that does not exist,
  word for word.
- For an anonymous caller: `404` too, as GitHub does, or `401`, as
  `Require` answers, which tells a client to log in.

For memberships that carry permissions:

- **A table the framework ships** (a contrib app: user, tenant, role;
  roles and their permissions). A stored format, with migrations, an
  admin API, invitations, and a place among the databases when tenants
  have schemas of their own. Every project already keeps memberships
  with fields of its own; django-organizations' users extend its models.
- **A role map in settings** (`TENANCY.ROLES`), the hook returning a role
  name. Roles fixed in code: roles a customer defines, as Auth0 and
  WorkOS allow, do not fit; and the hook can map a role in one line.
- **A hook that returns the member's permission keys, or None**: what
  the project's own backend did, without the backend. One query answers
  both "is a member" and "holds what"; the core keeps the hook, and
  `contrib.auth` grants the keys. A contrib app with a table can later
  implement the hook without changing it.

## Decision

- **`domainkit.tenancy.PathTenant(parameter)`** is a FastAPI dependency
  that serves a route as the tenant its path parameter names, once the
  route is matched:

  ```python
  router = APIRouter(
      prefix="/orgs/{org_id}",
      dependencies=[Depends(PathTenant("org_id"))],
  )
  ```

  It declares the path parameter (a signature of its own), so FastAPI
  passes it and OpenAPI documents it, and it returns the tenant id. The
  request is served as the tenant through the same context manager the
  middleware uses: rows, databases, caches, limits, logs, and tasks are
  the tenant's. A name that cannot be a path parameter is refused when
  the dependency is made.
- **Who belongs.** `PathTenant` needs `MEMBERSHIP` or
  `MEMBER_PERMISSIONS`, since a caller may type any id in a path; without
  either it raises `ImproperlyConfigured`.
- **The answers.** A caller who proved who they are and does not belong,
  a tenant that does not exist, an id that is not a tenant id, and a path
  naming a tenant other than the one `TenantMiddleware` already serves
  all get Starlette's `404`, the answer of a route that does not exist.
  An anonymous caller who does not belong gets `401 not_authenticated`,
  as `Require` answers, for every id alike: logging in may help, and the
  answer tells no tenant apart. A WebSocket handshake gets the same, as
  a denial response. While a WebSocket or an event stream stays open,
  membership is checked again with the route's rules, afresh.
- **`TenantMiddleware` keeps its answers**: `400` and `403 not_a_member`.
  Its tenant comes from the host or a header, not from a resource's path.
  A project that names some tenants before routing and others by path
  lists the path prefix in `EXEMPT_PATHS`.
- **`TENANCY.MEMBER_PERMISSIONS`**: a dotted path to a function of the
  user and a tenant id, async or not, that returns the permission keys
  the user holds as a member of that tenant, or None for one who is not
  a member. It replaces `MEMBERSHIP`; the two together are refused. A
  string or a bool returned is refused (`ImproperlyConfigured`), not read
  as keys.
- **Asked once per request.** `TenantMiddleware` and `PathTenant` ask it
  when they admit the caller, and keep the answer with the tenant until
  the request ends. `domainkit.tenancy.member_permissions(user)` returns
  it for the current tenant: the kept answer for the same principal
  object and tenant, else the hook's, asked afresh; none without a
  tenant or the setting.
- **`domainkit.contrib.auth.backends.MembershipBackend`** grants those
  keys: `get_all_permissions(user)` is `member_permissions(user)` for an
  active user, and `has_perm` follows. It grants on no object, as
  `ModelBackend`, and says so (`object_grants` is empty, so the admin API
  does not fall back to superusers only). It refuses to load without
  `MEMBER_PERMISSIONS`, which would make it grant nothing silently.
- **Ceilings: a member's keys never reach beyond the tenant.** Granted
  in one tenant, a key must not let its holder hand it out, or carry it,
  where it does not hold. Every ceiling reads one function,
  `backends.get_all_permissions(user)`: the admin API's `may_grant` and
  `may_hold` (what staff may grant, and touch on objects) and
  `may_change` (whom they may change), personal tokens' scopes (the route
  and `createtoken`), and the scopes of a sign-in approval's access token
  (`contrib.ciba`). It now leaves out every backend whose grants hold in
  one tenant only: `BaseBackend.tenant_scoped`, a class attribute, false
  by default and true on `MembershipBackend`. So, while a tenant is
  served, an owner who is staff grants none of the owner's keys through
  the admin API, and scopes no token with them, beyond what they hold
  everywhere; inside the tenant, `has_perm` and the rules still count
  them. `get_all_permissions(user, tenant_scoped=True)` adds them, for
  `/auth/me`, which lists what the request may do. A token's scopes only
  bound it (each request still needs the permission itself), so a
  role-scoped token could widen nothing; the scopes are refused anyway,
  which keeps every ceiling one rule, and costs a member nothing their
  session in the tenant cannot do.
- **Checks.** `check`, and the application as it starts, refuse a route
  whose `PathTenant` names a parameter its path does not have
  (`tenancy.E001`: every request would be refused with 422), and one
  whose own dependencies check callers before `PathTenant`
  (`tenancy.E002`). A `PathTenant` inside another dependency is checked
  against the path only.

## Consequences

- A project declares `/orgs/{org_id}/...` routes with one dependency, and
  its members' roles answer `HasPermissions`, `ModelPermissions`, and
  `has_perm`, without a resolver or a backend of its own.
- A stranger cannot tell an organization that exists from one that does
  not, through the routes that take it from the path.
- A route that puts a rule before `PathTenant`, or names a parameter its
  path lacks, does not start in staging and production.
- A member's keys count inside the tenant only. A member cannot scope a
  personal token with them; their session in the tenant still does what
  they allow.
- `may_change` does not see what a user holds through memberships: the
  hook answers one tenant at a time and cannot list a user's tenants. A
  password change hands over a user's memberships too, so a project whose
  staff should not change the members of tenants they do not run refuses
  it with `on_admin_change`; the docs say so.
- `contrib.objectpermissions` checks who may grant on an object with
  `has_perm`, not `get_all_permissions`, so it counts a member's keys in
  the tenant being served. On that tenant's own objects this stays in the
  tenant; on an object of no tenant it would not. It is left for later,
  below.
- The kept answer is tied to the principal object: a re-check of a live
  connection, with the principal loaded afresh, asks the hook again; a
  principal that is never reloaded keeps its answer for the request.
- Left for later: bounding `contrib.objectpermissions`' grants by what
  the actor holds everywhere; a `404` option for `TenantMiddleware`; a
  memberships table in a contrib app; and re-checking membership on
  connections that `TenantMiddleware` admitted.

## References

1. django-tenants, *Installation*, sub-folder support:
   <https://django-tenants.readthedocs.io/en/latest/install.html>
2. django-tenants, `TenantSubfolderMiddleware`:
   <https://github.com/django-tenants/django-tenants/blob/master/django_tenants/middleware/subfolder.py>
3. Tenancy for Laravel v3, *Tenant identification*, path identification:
   <https://tenancyforlaravel.com/docs/v3/tenant-identification>
4. Tenancy for Laravel, `PathTenantResolver`:
   <https://github.com/archtechx/tenancy/blob/3.x/src/Resolvers/PathTenantResolver.php>
5. *Account middleware in Ruby on Rails*:
   <https://dev.to/dpaluy/account-middleware-in-ruby-on-rails-1a9l>
6. Spatie laravel-permission v6, *Teams permissions*:
   <https://spatie.be/docs/laravel-permission/v6/basic-usage/teams-permissions>
7. Auth0, *Add roles to organization members*:
   <https://auth0.com/docs/manage-users/organizations/configure-organizations/add-member-roles>
8. WorkOS AuthKit, *Roles and permissions*:
   <https://workos.com/docs/authkit/roles-and-permissions>
9. django-organizations, *Customizing your organizations*:
   <https://django-organizations.readthedocs.io/en/latest/custom_usage.html>
10. Django, *Customizing authentication*, handling authorization in
    custom backends:
    <https://docs.djangoproject.com/en/5.2/topics/auth/customizing/>
11. RFC 9110, *HTTP Semantics*, section 15.5.4, 403 Forbidden:
    <https://www.rfc-editor.org/rfc/rfc9110#section-15.5.4>
12. OWASP, *Insecure Direct Object Reference Prevention Cheat Sheet*:
    <https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html>
13. OWASP, *Authorization Cheat Sheet*:
    <https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html>
14. GitHub REST API, *Troubleshooting*, 404 for private repositories:
    <https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api>
