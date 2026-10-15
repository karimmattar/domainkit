# 0017. Routes deny by default; staff manage users through an admin API

- Status: Accepted
- Date: 2026-10-15

## Context

[0016](0016-authenticating-requests.md) authenticated requests and gave
routes rules, but a route without a rule was still open to anyone. One
forgotten `Require` on a new route exposed it, and nothing said so: the
generated domains themselves had open routes. Django avoids the risk with
its admin and login-required views; Django REST framework with a default
permission class. FastAPI has neither, and a project-wide default rule
would also guard routes that are public on purpose.

Projects also had no way to manage users, groups, and grants over the API.
They had commands for one superuser and one token, and had to write their
own endpoints, each a chance to let staff grant themselves more than they
hold.

## Decision

- Every route must say who may call it: a `Require(...)` dependency, a
  `CurrentUser` or `AuthenticatedUser` parameter, or `Public`, a
  dependency that only marks the route as public on purpose. Each counts
  on the route, on a dependency it uses, or on a router it is included
  through. Routes are walked the way FastAPI builds OpenAPI, so prefixes
  and router dependencies apply.
- A new hook, `DomainConfig.check_application(app)`, reports problems
  with the built application. Auth reports routes without a rule, and
  routes that read the user while `AuthenticationMiddleware` is missing.
  `check` fails with them, staging and production refuse to start, and
  development and tests start and log them, so a project can move to the
  rule a route at a time. `AUTH.REQUIRE_ROUTE_RULES = False` turns the
  route rule off.
- The framework's own routes guard themselves (`/health`, `/ready`, and
  `/metrics` with its token) and are not checked.
- New projects install auth, its middleware, and its endpoints. Generated
  domains name the permission of each route (`view`, `add`, `change` on
  their model), so they start closed and are opened by granting
  permissions. `client_for` and `admin_client` let tests call them as
  users.
- The admin API (`auth.admin.router`) is Django's admin for users, groups,
  and permissions, as REST endpoints. Each endpoint needs a staff user or
  a superuser, and the model permission of its action.
- Staff hand out only what they hold. Only superusers make, unmake, or
  change superusers. Other staff cannot grant a permission they lack,
  directly or through a group. They cannot change a user or group that
  holds one either, because setting a password is as good as becoming the
  user. Inactive users count with the grants they keep. Django lets
  anyone with `change_user` do all of this; its documentation warns that
  this is the same as superuser status.
- Changes are domain events, so the audit trail records who did what.
  Fields of a custom user model are shown and changed by name. The model
  says which ones may change (`editable_fields()`, which leaves out
  `PROTECTED_FIELDS` and the username) and how to clean them
  (`clean_field`).

## Consequences

A route added without a rule fails `check` and cannot be deployed, so the
mistake is caught before it ships. Projects that built routes on 0016
must mark their public routes `Public`, or set
`REQUIRE_ROUTE_RULES = False`, before deploying this release. Routes
mounted outside FastAPI's routers (plain Starlette routes and mounts) are
not checked. Projects that want a staff user to administer everything
must make them a superuser, or grant them every permission they hand out.
