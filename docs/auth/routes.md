# Protecting routes, and the admin API

Every route of a domainkit service says who may call it. You say it with
a [rule](../learning.md#words): a login, a permission, a step-up, or a
rule of your own. A route that says nothing is refused when the service
starts in staging and production. This page shows the rules, how to
combine them, how to protect a route without `domainkit.contrib.auth`,
and the admin API that staff use to manage users and groups.

On this page you learn how to:

- guard a route with `Require(...)`, `AuthenticatedUser`, or `Public`;
- combine rules, and write a rule of your own;
- check a rule on one object;
- find the routes that declare no rule;
- read who sent a request when another system authenticates it;
- manage users, groups, and permissions over the admin API, and act as
  a user for support.

## Protecting routes

Rules decide who may call a route. They belong to the core,
`domainkit.http.access`, and work with any authentication (see
[Another authentication](#another-authentication)). `domainkit.contrib.auth`
adds the rules about its own users: `IsAdminUser`, `IsSuperuser`,
`AuthenticatedUser`, and `StepUp`.

```python
from typing import Annotated

from fastapi import APIRouter, Depends

from domainkit.contrib.auth import AnyUser, AuthenticatedUser, CurrentUser
from domainkit.http.access import HasPermissions, ModelPermissions, Public, Require
from shop.domain import Product

router = APIRouter()


@router.post("/invoices", dependencies=[Depends(Require(HasPermissions("billing.add_invoice")))])
async def create_invoice() -> None: ...


@router.api_route("/products", methods=["GET", "POST"])
async def products(user: Annotated[AnyUser, Depends(Require(ModelPermissions(Product)))]) -> None: ...


@router.get("/me/invoices")
async def my_invoices(user: AuthenticatedUser) -> None: ...  # 401 for anonymous callers


@router.get("/greeting", dependencies=[Public])
async def greeting(user: CurrentUser) -> None: ...  # anyone; AnonymousUser if so
```

`Require(*rules)` makes the rules a FastAPI dependency that returns the
user, and documents the bearer token in OpenAPI. Without rules, it asks
for a user who logged in. A refused anonymous caller gets `401` with the
code `not_authenticated`, so it knows to log in. A refused user gets
`403 permission_denied`:

```json
{"type": "about:blank", "title": "Unauthorized", "status": 401, "detail": "Authentication credentials were not provided.", "instance": "/orders", "code": "not_authenticated", "request_id": "6250033d60192a5e78e56b3d08cd3b13"}
{"type": "about:blank", "title": "Forbidden", "status": 403, "detail": "You do not have permission to perform this action.", "instance": "/products", "code": "permission_denied", "request_id": "f6b904c5f5de5813662e0361eaa8b0e4"}
```

| Name | What it gives |
| --- | --- |
| `Require(*rules)` | A dependency: every rule must allow; returns the principal. |
| `AuthenticatedUser` | A parameter type: the user who logged in; 401 for anonymous callers. |
| `CurrentUser` | A parameter type: the user, or `AnonymousUser`. It is not a rule. |
| `Public` | A dependency that says "anyone, on purpose". |
| `current_user(request)`, `acurrent_user(request)` | The principal the middleware found, in code. |

## The rules

| Rule | Allows |
| --- | --- |
| `AllowAny` | Anyone. |
| `IsAuthenticated` | Users who logged in. |
| `IsAuthenticatedOrReadOnly` | Anyone for `GET`, `HEAD`, `OPTIONS` (`SAFE_METHODS`); users otherwise. A WebSocket handshake is a `GET`, so anyone may open one: guard sockets that take writes with `IsAuthenticated`. |
| `IsAdminUser` | Staff users (`is_staff`), with credentials that carry no scopes. |
| `IsSuperuser` | Active superusers, with credentials that carry no scopes. |
| `HasPermissions(*keys)` | Users granted every permission, by any backend. |
| `ModelPermissions(model)` | Django's model permissions by method (`MODEL_ACTIONS`): `GET` and `HEAD` need `view`, `POST` `add`, `PUT` and `PATCH` `change`, `DELETE` `delete`. `OPTIONS` needs a user, and a WebSocket handshake `view`. `model` is a mapped class or `"<domain>.<model>"`. |
| `HasObjectPermissions(*keys)` | Users granted every permission on the object, by a backend with object permissions. The request itself needs an active user who logged in; check each object with `check_object_permissions`. |
| `HasScope(*keys)` | Users whose credentials' scopes cover every key; credentials without scopes cover all. |
| `IsUnscoped` | Users whose credentials carry no scopes (a login, not a scoped token). |
| `AllowScoped` | Users who logged in, scoped tokens too, on purpose ([Scopes on every route](authentication.md#scopes-on-every-route)). |
| `StepUp(max_age=...)` | Users whose credentials authenticated again, with a second factor, within `max_age` (a day at most); refused with `401 step_up_required` ([Stepping up](step-up.md)). |

`ModelPermissions` works out a class's content type once. It works it
out again after `registry.populate` or `clear`, after a class is mapped,
or after a table is added to or removed from the project's metadata.

### Combining rules

Combine rule instances with `&`, `|`, and `~`, or with the classes
`AllOf`, `AnyOf`, and `Not`:

```python
from domainkit.contrib.auth import IsAdminUser
from domainkit.http.access import AllOf, AnyOf, HasPermissions, IsAuthenticated, Not, Require

Require(IsAdminUser() | HasPermissions("billing.view_invoice"))
Require(AnyOf(IsAdminUser(), HasPermissions("billing.view_invoice")))  # the same
Require(AllOf(IsAuthenticated(), Not(HasPermissions("billing.blocked"))))
```

`Require(A, B)` is `Require(A & B)`. A `Rule` is a rule instance or a
rule class; a class is made with no arguments.

### A rule of your own

A rule subclasses `BasePermission` and overrides `has_permission(request,
user)` or `has_object_permission(request, user, obj)`. Both are async.
It may set `message`, the `detail` of its `403`. `user` is the request's
`Principal`; annotate it as the users your rule is written for, such as
`AnyUser`. Where a route loads one object, check it with
`check_object_permissions`:

```python
from domainkit.http.access import BasePermission, check_object_permissions


class IsOwner(BasePermission):
    message = "Only the owner may change an invoice."

    async def has_object_permission(self, request, user, obj) -> bool:
        return obj.owner_id == user.id


@router.patch("/invoices/{invoice_id}", dependencies=[Depends(Require())])
async def change_invoice(invoice_id: InvoiceId, request: Request) -> None:
    async with BillingUnitOfWork() as uow:
        invoice = await uow.invoices.get(invoice_id)
        await check_object_permissions(request, invoice, IsOwner())
        ...
```

A rule may answer with its own error, to tell the client what to do. It
returns the error from `refusal(request, user)`, as `StepUp` does with
`401 step_up_required`. The rule's error answers in place of the `403`,
unrecorded, but only when no other rule of the request refuses plainly.
A plain refusal wins, and is recorded, whatever the order:
`StepUp(...) & HasPermissions(...)` answers a user without the
permission with the `403`.

A rule that reads `scopes_of(request)` and limits what scoped tokens
reach sets `bounds_scopes = True`
([Scopes on every route](authentication.md#scopes-on-every-route)).

### Refusals in the audit trail

A refused user (`403`) is recorded in the audit trail when
`domainkit.contrib.audit` is installed: `auth.access`, outcome `denied`,
with the actor, the route's method and template (`GET /products/{owner}`,
never its values), and the class of the rule that refused
(`scopes_unbounded` for a scoped token refused by `SCOPED_CREDENTIALS`).

- One actor's refusals on one route are recorded once a minute, so
  probing many object ids writes one row a minute per route.
- `401`s for anonymous callers are not recorded.
- Any installed domain whose config implements `DenialRecorder`
  (an async `record_denial(details)`) gets them, with `contrib.auth` or
  without it. A recorder that fails is logged; the refusal stands.

### Deny by default

Every route must say who may call it: a `Require(...)` dependency, an
`AuthenticatedUser` parameter, or `Public` for a route anyone may call.
Each can sit on the route, on a dependency it uses, or on a router it is
included through. A `CurrentUser` parameter alone says nothing: it lets
anyone in, an anonymous caller as `AnonymousUser`. So such a route is
listed until it adds `Public` (since 1.0, DSGN-17).

Routes that say nothing are listed by `manage.py check`, logged when the
application starts in development and tests, and refused in staging and
production, where the application does not start:

```text
The application has problems:
  security.E101: GET /reports declares no access rule: add Depends(Require(...)), or dependencies=[Public] if anyone may call it.
```

The application is checked when it is built, and again when it starts
serving (its lifespan, once the domains have started). So a router
included later, in `asgi.py` or by a domain's startup, is checked too.
Requests never pay for the check.

```python
from fastapi import APIRouter

from domainkit.http.access import Public


@router.get("/catalog", dependencies=[Public])  # anyone, on purpose
async def catalog() -> list[str]: ...


public_router = APIRouter(dependencies=[Public])  # every route in it
```

The framework's own routes (`/health`, `/ready`, `/metrics`, and the
`API_DOCS` pages) and the login and refresh routes are covered already.
Applications mounted with routes of their own (another `FastAPI`, a
Starlette `Mount` of routes) are checked route by route. What takes no
dependencies (a mounted `StaticFiles` or another ASGI application, and
Starlette's own `Route` and `WebSocketRoute`) cannot declare a rule. It
is listed (`MOUNT /static`) until you serve it from a FastAPI route with
`Require(...)`, or mark it public with `mark_public`:

```python
from starlette.staticfiles import StaticFiles

from domainkit.http.access import mark_public

router.mount("/static", mark_public(StaticFiles(directory="static")))
```

`open_routes(app)` returns the list, for a test of your own:

```python
from domainkit.http.access import open_routes

print(open_routes(app))
```

```text
['GET /reports', 'GET /greeting']
```

Here `/greeting` has only a `CurrentUser` parameter, so it is listed.

The check is the core's: it holds with this domain or without it.
`REQUIRE_ROUTE_RULES = False` turns it off. (`AUTH.REQUIRE_ROUTE_RULES`,
its name until 0.10, is refused since 0.11, with a message that names
the new setting.) Routes that read the user still need
`AuthenticationMiddleware`, and startup reports them while it is missing
(`auth.E101`).

### Another authentication

`ResourceRouter`, the rules, and deny by default need neither
`domainkit.contrib.auth` nor its tables. A project that authenticates
requests its own way (an OpenID Connect middleware, a gateway's header)
puts who sent each request in `scope["user"]`, from an ASGI middleware
of its own.

The rules read a `domainkit.http.access.Principal`: `id`,
`is_authenticated`, `is_active`, and `ahas_perms(perms, obj=None)`, which
answers permission keys (`billing.view_invoice`) as the project decides.
`AnonymousPrincipal` stands for a caller who proved nothing. A principal
whose class has `challenge()` names the `WWW-Authenticate` of the `401`
that an anonymous caller gets (`Bearer realm="api"` otherwise).
Credentials in `scope["auth"]` with `scopes` limit the permission rules
as a scoped token does
([ADR 0080](../adr/0080-access-rules-are-core-auth-is-one-provider.md)):

```python
from dataclasses import dataclass

from domainkit.http.access import AnonymousPrincipal


@dataclass(frozen=True)
class GatewayUser:
    id: str
    roles: frozenset[str]
    is_authenticated = True
    is_active = True

    async def ahas_perms(self, perms, obj=None) -> bool:
        return set(perms) <= self.roles


class GatewayAuthentication:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            headers = dict(scope["headers"])
            user = headers.get(b"x-user")
            roles = headers.get(b"x-roles", b"").decode().split()
            scope["user"] = (
                GatewayUser(user.decode(), frozenset(roles))
                if user
                else AnonymousPrincipal()
            )
        await self.app(scope, receive, send)
```

A dependency of your own that reads the principal counts as a rule for
deny by default once you mark it with `reads_principal`, as
`AuthenticatedUser` is. Mark only one that refuses anonymous callers
(it may raise `NotAuthenticated`, a 401). `CurrentUser` is marked too, so
scoped credentials are held to the route's rules where it reads them,
but it never counts as a rule.

## The admin API

`domainkit.contrib.auth.admin.router` is Django's admin for users,
groups, and permissions, over REST. Mount it where staff tools live:

```python
from domainkit.contrib.auth.admin import router as admin_router

router.include_router(admin_router, prefix="/admin/auth")
```

Every route needs a staff user (`is_staff`) or a superuser, with
credentials that carry no scopes, and the model permission of its
action: `view`, `add`, `change`, or `delete` on the user model
(`auth.change_user`, or `accounts.change_account` for a custom user),
`auth.*_group`, or `auth.view_permission`.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /users` | `?limit=50&cursor=&search=` | A page of users by username; `search` matches part of it, in any case. |
| `POST /users` | `{"username", "password", "is_active", "is_staff", "is_superuser", "fields"}` | `201` and the user. The password is validated; without one, it is unusable. |
| `GET /users/{id}` | | The user. |
| `PATCH /users/{id}` | `{"is_active", "is_staff", "is_superuser", "fields"}` | The user; what you leave out stays. |
| `DELETE /users/{id}` | | `204`; the user's tokens, second factor (secret and recovery codes), and object permissions go too. |
| `POST /users/{id}/password` | `{"password"}` | `204`; `null` makes it unusable. Every session of the user ends. |
| `POST /users/{id}/logout` | | `204`: every session of the user ends. |
| `POST /users/{id}/impersonate` | `{"reason"}` | `201` and a token that acts as the user for `AUTH.IMPERSONATION_TTL` ([Impersonation](#impersonation)). |
| `PUT /users/{id}/groups` | `{"groups": [names]}` | The user, in exactly those groups. |
| `PUT /users/{id}/permissions` | `{"permissions": [keys]}` | The user, granted exactly those permissions directly. |
| `GET /groups` | `?limit=50&cursor=` | A page of groups by name. |
| `POST /groups` | `{"name", "permissions"}` | `201` and the group. |
| `GET /groups/{id}` | | The group. |
| `PATCH /groups/{id}` | `{"name"}` | The renamed group. |
| `PUT /groups/{id}/permissions` | `{"permissions": [keys]}` | The group, granting exactly those permissions. |
| `DELETE /groups/{id}` | | `204`; its members lose its permissions. |
| `GET /permissions` | | Every permission: `key`, `name`, and `content_type`. |

A user is shown with `id`, `username`, `is_active`, `is_staff`,
`is_superuser`, `last_login`, `groups`, the `permissions` granted
directly, and `fields`: the fields the model lists in `EDITABLE_FIELDS`
and `READ_ONLY_FIELDS` (`email`, `first_name`, `last_name`,
`date_joined`, and `email_verified` for `AbstractUser`).

- `fields` in a request changes only those in `EDITABLE_FIELDS`
  (`editable_fields()`). Naming any other field answers
  `422 unknown_user_field`.
- Values are checked against the field's type and markers
  (`422 invalid_user_field`).
- `clean_field(name, value)`, which a custom user may override, tidies
  what is stored, when a user is made and when one is changed.

The listings and reads are resources ([resources.md](../resources.md)):
`limit` and `cursor` page them, and a user or a group carries an `ETag`,
so `If-None-Match` answers `304` while it is unchanged. Changes are
recorded as events (`UserUpdated`, `UserDeleted`, `UserStatusChanged`,
`UserJoinedGroup`, `GroupDeleted`, and the others), so with
`domainkit.contrib.audit` installed the audit trail names who changed
what.

### Staff hand out only what they have

- **Only superusers make, unmake, or change superusers.**
- **With `domainkit.contrib.approvals` guarding it**
  (`APPROVALS.SUPERUSER_APPROVALS`), no one makes a superuser here
  (`approval_required`): an approved `auth.grant_superuser` request does
  ([approvals.md](../contrib/approvals.md#making-superusers)).
- **Other staff cannot grant a permission they lack**, directly or
  through a group (`403`).
- **Nor can they change a user or a group that holds such a
  permission**: setting a user's password is as good as becoming them.
- **Grants on single objects count too** (AUTH-14). Staff change no user
  or group that holds a grant on an object, and add no one to a group
  that holds one, unless they hold the permission on the whole model or
  the same grant on that object (`403`, saying how many they lack, not on
  which objects). Every backend says what it grants on objects through
  `object_grants(holder)` ([Backends](users.md#backends)). While a
  backend of `AUTH.BACKENDS` other than `ModelBackend` does not, only
  superusers change users and groups here, and `check` warns
  (`auth.W007`). What every backend grants counts, and so does what a
  change would grant (a field a backend reads, a user made active again).
  Inactive users count with every grant they keep. While other backends
  are configured, only superusers change inactive users, because those
  backends grant them nothing until they are active.
- **Deactivating a user ends their sessions.** Their tokens are deleted,
  and do not work again if the user is made active again.
- **The last active superuser stays.** It cannot be demoted,
  deactivated, or deleted (`409 last_superuser`): make another one first.

### Impersonation

Support staff sometimes need to see what a user sees. Impersonation
gives them a token that acts as the user. It is off by default. Set
`AUTH.IMPERSONATION_TTL` (at most 8 hours) to turn it on:

```python
from datetime import timedelta

AUTH = {"IMPERSONATION_TTL": timedelta(minutes=30)}
```

```http
POST /admin/auth/users/6f1c.../impersonate
{"reason": "Ticket 42: the customer cannot see her invoices"}
```

```json
{"access_token": "dkt_...", "token_type": "Bearer", "expires_in": 1800}
```

Who may impersonate whom:

- **Only staff who may change the user.** They hold every permission the
  user holds; only superusers act as superusers. Never oneself, and never
  an inactive user (`403`). `reason` is required, 1 to 500 characters.
- **No chains.** A request made with an impersonation token cannot start
  another impersonation (`403` "An impersonation cannot impersonate."). A
  chain would name the wrong staff member in the trail, and start a new
  lifetime.
- **Checked again on every request.** Each request with the token checks
  that its staff member is still active, still staff or a superuser, and
  still holds the `change` permission on the user model. It also checks
  again that the staff member holds every permission the user holds, as
  when the token was issued. Deactivating or deleting the staff member,
  taking that permission away, or granting the user a permission the
  staff member does not have, ends the token at once with `401`, not when
  `IMPERSONATION_TTL` runs out.

What the token may do:

- **It is audited.** `auth.user_impersonated` names the staff member, the
  user, and the reason (kept as the user's personal data, erased with
  them). Every request the token makes is recorded under the staff
  member (the actor), so the trail shows who really acted.
- **It reaches no step-up.** `StepUp` refuses it: what a step-up guards
  is the user's own. For the same reason it cannot add a way into the
  account: a passkey, a bound device, or a linked identity
  (`401 reauthentication_required`).
- **It cannot make or decide approvals.** With
  `domainkit.contrib.approvals`, staff acting as a user can neither
  submit a request nor approve or reject one (`403` "Requests are made
  and decided in one's own name only."). Otherwise one person could make
  a request and decide it, under two names.
- **It is never refreshed.** Logging out with it ends it at once.

`impersonated(request)`, in `domainkit.contrib.auth.integration`, tells a
route of yours whether staff act as the user, and `request.auth.impersonator`
names the staff member.

## Options

| Setting | Default | What it does |
| --- | --- | --- |
| `REQUIRE_ROUTE_RULES` | `True` | Every route must declare a rule. |
| `SCOPED_CREDENTIALS` | `"refuse"` | What a scoped token gets on a route without a rule that checks scopes. |
| `AUTH.IMPERSONATION_TTL` | `None` | How long an impersonation token works; `None` turns impersonation off. |
| `AUTH.BACKENDS` | `ModelBackend` | What `HasPermissions` and the admin API ask. |

See [reference/settings.md](../reference/settings.md) and
[the auth settings](../auth.md#settings).

## Errors and refusals

| Status and code | When |
| --- | --- |
| `401 not_authenticated` | An anonymous caller on a route that needs a user. |
| `403 permission_denied` | A user whom a rule refuses; recorded in the audit trail. |
| `401 step_up_required` | `StepUp` refuses ([Stepping up](step-up.md)). |
| `403 permission_denied` "Impersonation is off (AUTH.IMPERSONATION_TTL)." | Impersonation without the setting. |
| `409 last_superuser` | The last active superuser would go. |
| `403 approval_required` | Making a superuser while approvals guard it. |
| `422 unknown_user_field`, `422 invalid_user_field` | A field the admin API may not change, or a value that does not fit. |
| `404 user_not_found`, `404 group_not_found` | No such id. |
| `security.E101`: "... declares no access rule ..." | A route without a rule; startup refuses it in staging and production. |

## Common mistakes

- **Using `CurrentUser` as a rule.** It lets anyone in. Add `Public` if
  that is what you mean, or use `AuthenticatedUser`.
- **Guarding a WebSocket that takes writes with
  `IsAuthenticatedOrReadOnly`.** A handshake is a `GET`. Use
  `IsAuthenticated`.
- **Mounting `StaticFiles` without `mark_public`.** Deny by default lists
  it, and production does not start.
- **Expecting a step-up rule's `401` to win over a permission's `403`.**
  A plain refusal always answers first.
- **Turning impersonation on for long.** Keep `IMPERSONATION_TTL` short;
  the token acts as the user until it ends.

## See also

- [Authenticating requests](authentication.md): who the rules see, and
  scopes.
- [Stepping up](step-up.md): the `StepUp` rule.
- [Users, groups, backends, and passwords](users.md): where permissions
  come from.
- [Resources](../resources.md): `ResourceRouter` takes a `rule=`.
- [Approvals](../contrib/approvals.md): maker-checker for sensitive
  commands.
