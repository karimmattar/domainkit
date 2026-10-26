# 0080. Access rules are core; auth is one provider

- Status: Accepted
- Date: 2026-10-26

## Context

The rule algebra Django REST framework keeps in its core (`BasePermission`,
`Require`, `Public`, `AllOf`, `AnyOf`, `Not`, `check_object_permissions`)
lived in `domainkit.contrib.auth.access`, bound to one user model and one
middleware. `domainkit.resources` imported it eagerly, and `schema_for`
imported auth's `PasswordHash`; deny by default was a check of
`contrib.auth`'s `check_application`. A project that authenticates its own
way (an OpenID Connect middleware, a gateway's header) had to install
`contrib.auth`, its tables, and content types to use `ResourceRouter`, and
without them nothing refused routes that declare no rule (DSGN-02 of the
0.9.0 design audit). At 0.9.2 the core imported a contrib app 12 times at
run time (5 at import, 7 inside functions: resources, the `broker`
command, operators' audit records, the test clients), and once for type
checkers only.

## Decision

- **Where rules live.** In the core, at `domainkit.http.access`: layer 4
  of the package's layering, beside the middleware and errors they use.
  It holds the algebra, the rules that read only what any principal has
  (`IsAuthenticated`, `IsAuthenticatedOrReadOnly`, `AllowAny`,
  `HasPermissions`, `HasObjectPermissions`, `ModelPermissions`, `HasScope`,
  `IsUnscoped`), `Require`, `Public`, `mark_public`, `current_user`,
  `CurrentUser`, `NotAuthenticated`, and `open_routes`. `contrib.auth`
  keeps what reads its own users: `IsAdminUser`, `IsSuperuser`,
  `AuthenticatedUser`, and `current_user` and `CurrentUser` typed as its
  users (the core's objects, so either counts as a rule). The moved names
  import from `contrib.auth` and `contrib.auth.access` until 0.11, warning
  with `DomainkitDeprecationWarning` (`domainkit.deprecation.moved`), and
  are the same objects, so `isinstance` and `except` hold either way.
- **What a principal is.** `Principal`, a protocol: `id`,
  `is_authenticated`, `is_active`, and `async ahas_perms(perms, obj=None)`,
  which answers permission keys (Django's name for the async check).
  `AnonymousPrincipal` is one for callers who proved nothing. `contrib.auth`'s
  users answer through its backends, as `backends.has_perms` did, so object
  permissions and `AUTH.BACKENDS` count as before. A rule's hooks take the
  principal typed as `Any`, so a rule written for auth's users may annotate
  it `AnyUser`: typed `Principal`, mypy found 11 overrides of the
  repository's own tests incompatible, and every project's rule would be.
- **How a provider plugs in.** An ASGI middleware puts a principal in
  `scope["user"]`, and may put credentials in `scope["auth"]`: when they
  carry `scopes` (permission keys), the permission rules allow only what
  both the principal and the scopes allow. A principal whose class has
  `challenge()` names the `WWW-Authenticate` of the 401 an anonymous caller
  gets: auth's `AnonymousUser` returns its first authentication class's,
  as before; others get `Bearer realm="api"`. A dependency of the
  provider's that reads the principal is marked `reads_principal`, so a
  route depending on it declares who may call it. `contrib.auth`'s
  `AuthenticationMiddleware` is one provider among others.
- **Deny by default is the core's.** `REQUIRE_ROUTE_RULES` (default true)
  is a core setting, and the check runs in `application_problems`, when
  the application is built and again when it starts serving, with
  `contrib.auth` or without it. `AUTH.REQUIRE_ROUTE_RULES` still turns it
  off until 0.11, with a warning.
- **Model keys are the core's.** Which classes are installed models, their
  keys (`billing.invoice`), and their `Meta` are `domainkit.db.models`,
  since permissions and the records of generic writes name them;
  `contrib.contenttypes` stores them as rows and keeps the names.
- **Secrets are a marker.** `schema_for` refuses a field whose type is a
  `domainkit.ddd.Sensitive`, as auth's `PasswordHash` is, instead of
  importing auth.
- **The framework's own import rule.** No core module imports a contrib
  app: not at import, not in a function, not by a name given to a loader.
  What the core needs of an app, it asks through a protocol the app's
  `DomainConfig` implements: `OperatorRecorder` (the audit trail records
  operators' actions), `QueueDeclarer` (messaging names the queues
  `broker declare` creates), `TokenIssuer` (auth issues the test clients'
  tokens). `test_framework_layers.py` fails on any new edge; its allowed
  list holds one, for type checkers only (`DomainConfig.RETENTION` names
  privacy's `Retention`, until hooks are registered by type, DSGN-03).

## Consequences

- `ResourceRouter`, the rules, field policies, and deny by default work in
  a project without `contrib.auth` or content types
  (`test_access_without_auth.py` is that project).
- Projects without `contrib.auth` now have deny by default: a route that
  declares no rule fails `check`, and staging and production refuse to
  start, until it declares one or `REQUIRE_ROUTE_RULES = False`.
- Code that read `current_user()` from the core gets a `Principal`; auth's
  names keep their types.
- A new kind of authentication is a middleware and a principal class; a
  new capability the core needs of an app is a protocol, never an import.
