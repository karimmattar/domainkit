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
- mount part of a framework router, and add a rule to one of its routes;
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


@router.post(
    "/invoices", dependencies=[Depends(Require(HasPermissions("billing.add_invoice")))]
)
async def create_invoice() -> None: ...


@router.api_route("/products", methods=["GET", "POST"])
async def products(
    user: Annotated[AnyUser, Depends(Require(ModelPermissions(Product)))],
) -> None: ...


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
| `HasPermissions(*keys, everywhere=False)` | Users granted every permission, by any backend. `everywhere=True` counts only what they hold where no tenant is served: a member's role does not count ([rows of no tenant](../tenancy.md#rows-of-no-tenant)). `HasObjectPermissions` takes it too. |
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
from domainkit.http.access import (
    AllOf,
    AnyOf,
    HasPermissions,
    IsAuthenticated,
    Not,
    Require,
)

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
A rule that reads the request's body, as `DeviceSigned` does, sets
`reads_body = True` (*new in 2.8*): an upload route whose body limit is
decided per request refuses it when it is declared, since the rule would
read the body before the limit is decided
([Bounds decided per request](../resources.md#bounds-decided-per-request)).
`A & B`, `A | B`, and `~A` read the body if a rule of theirs does.

### Rules asked outside a request

Some rules are asked where no request exists: approvals asks a guard's
`maker_rule` when the approved command runs in the worker, and a
policy's `approver_rule` in `approvers_of`
([approvals](../contrib/approvals.md#guarding-a-use-case)). They are asked
on an `OfflineContext` (*new in 2.9*): a call of its own, by the user, of
no client. It has the method and path it is given, no headers, query,
cookies, or session data, no credentials (`request.auth` is None, so
`scopes_of(request)` is None), and a `request.state` that no other call
sees. A rule reads it as it reads a request. A rule that needs what only
a request carries, such as a header or a step-up, refuses there.

When such a rule raises, approvals tells three kinds of error apart:

- A transient one: a timeout, a connection refused or lost, `ServiceBusy`,
  an outgoing call that failed (`UpstreamError`, a host's circuit open
  among them), or a database that is busy or out of reach
  (`domainkit.http.errors.transient`, *new in 2.9*). It goes up, and the
  run is tried again, as any run that meets one is; `approvers_of` raises
  it.
- A `DomainError` of a maker rule. The approved request fails with the
  error's own code, as in 2.8. An approver rule's leaves the user out.
- Any other error, a mistake in the rule. It refuses, and is logged as a
  warning by the rule's class and the error's, never the error's text:
  the request fails with `maker_not_allowed`, once, and `approvers_of`
  leaves the user out.

Code of your own that asks a rule outside a request makes one context
per call:

```python
from domainkit.http.access import OfflineContext

allowed = await rule.has_permission(OfflineContext(user, path="/payouts"), user)
```

`scopes=[...]` limits its credentials to those scopes, as a scoped token
limits a request's.

`RuleContext` is what both kinds of call have: `scope` (with its
`method`, `path`, `auth`, and `user`), `user`, and `state`. Annotate a
rule's `request` with it to say the rule reads no more; it then answers
offline as it does on a route. The built-in rules read no more, and keep
their `HTTPConnection` annotation:

```python
from domainkit.contrib.auth import AnyUser
from domainkit.http.access import BasePermission, RuleContext


class IsCashier(BasePermission):
    async def has_permission(self, request: RuleContext, user: AnyUser) -> bool:
        return user.is_active and "cashiers" in {g.name for g in user.groups}
```

### Refusals in the audit trail

A refused user (`403`) is recorded in the audit trail when
`domainkit.contrib.audit` is installed: `auth.access`, outcome `denied`,
with the actor, the route's method and template (`GET /products/{owner}`,
never its values), and the class of the rule that refused
(`scopes_unbounded` for a scoped token refused by `SCOPED_CREDENTIALS`).
A rule that answers with its own error (`StepUp`'s `401
step_up_required`, `DeviceSigned`'s `401 device_signature_refused`, or a
rule of yours with a coded `403`) is recorded the same way, with the
error's `code` in the details, since 2.4: a stolen session probing routes
behind a step-up or a device signature shows in the trail.

- One actor's refusals on one route are recorded once a minute, so
  probing many object ids writes one row a minute per route.
- `401`s for anonymous callers are not recorded; a known user's `401` from a rule's own refusal is.
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
The application has problems: security.E101: GET /reports declares no access rule: add Depends(Require(...)), or dependencies=[Public] if anyone may call it.
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

## Rules on the framework's routes

The framework's routers (`auth_router`, `devices_router`, and the
others) declare their own rules. To add a rule to one of their routes,
or to mount only some of them, mount a copy made by `select_routes`
(`domainkit.http.routers`, since 2.4). The router it copies is left as
it is.

```python
from datetime import timedelta

from fastapi import APIRouter, Depends

from domainkit.contrib.auth import StepUp
from domainkit.contrib.devices.api import router as devices_router
from domainkit.http.access import Require
from domainkit.http.routers import select_routes

recent = StepUp(max_age=timedelta(minutes=5))

router = APIRouter()
router.include_router(
    select_routes(
        devices_router,
        without=["POST /devices/login"],
        dependencies={"POST /devices/enroll/code": [Depends(Require(recent))]},
    ),
    prefix="/auth",
)
```

- `only=[...]` copies the routes it names. Without it, every route is
  copied.
- `without=[...]` leaves out the routes it names.
- `dependencies={name: [Depends(...)]}` adds dependencies to a route: a
  rule, as here, or a check of your own, which may read the request's
  body. They run after its routers' dependencies and the route's own
  `dependencies=[...]`, before its endpoint. OpenAPI shows them as it
  shows the route's other dependencies.
- **A rule a route takes as its endpoint's parameter runs after them.**
  FastAPI solves a route's `dependencies=[...]` first, then its
  endpoint's parameters (`user: AuthenticatedUser`), so a check you add
  answers before such a rule refuses. The admin API declares each
  route's staff rule among its dependencies, and takes the staff member
  from the same one, which FastAPI runs once (since 2.7): its rules
  answer first. Before, a check added to an admin route ran for anyone,
  signed in or not (user report A17). On another route, put the rule in
  front of your check: `[Depends(Require(rule)), Depends(check)]`.

A route's name is `METHOD /path`, as its router declares it: the path
has the prefixes of the routers it is included through, but not the
prefix you mount it under. A route with two methods is `GET POST /path`,
and a WebSocket route is `WEBSOCKET /path`. `route_names(router)` lists
them:

```python
from domainkit.http.routers import route_names

print(route_names(devices_router))
```

```text
['POST /passkeys/register/options', 'POST /passkeys/register', ..., 'POST /devices/enroll/code', 'POST /devices/enroll', ...]
```

A name that matches no route is refused with `ImproperlyConfigured`,
which names it and lists the router's routes. So a typo never mounts
every route, or leaves a rule out. Dependencies given to a route that
is left out are refused too.

- **The copies keep what the routes declare:** their rules, tags, and
  answers, and the prefixes and dependencies of the routers they are
  included through. They sit in a plain `APIRouter`, in the router's
  order, with its lifespan. Mount it where you mounted the router.
- **`account_router()` is a router too:**
  `select_routes(account_router(), ...)` adds a rule to a route of any
  app it mounts.
- **A router that serves requests in its own way is refused**, and so is
  one that includes such a router. A `matches` or `handle` of its own (an
  IP gate) or frontend routes would be lost in a copy: include that
  router whole.
- **Starlette's own routes and mounts** are copied as they are when the
  router declares them itself. They take no dependencies, so giving them
  some is refused.

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
`auth.*_group`, or `auth.view_permission`. The permission counts as the
staff member holds it everywhere: with multi-tenancy, a member's role in
the tenant served opens no route of the admin API, nor of the staff APIs
of flags, webhooks, the audit trail, and the admin site
([rows of no tenant](../tenancy.md#rows-of-no-tenant)).

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /users` | `?limit=50&cursor=&search=&id=` | A page of users, by the first field of `AUTH.IDENTIFIERS` (by username without it). `search` matches part of the username, or of the email address or mobile number where `AUTH.IDENTIFIERS` names them, in any case; a mobile number also in any form that normalizes to it. An encrypted field matches only its whole value, with a blind index. `id`, repeated up to 200 times (since 2.5), keeps the users it names, so a page of rows that refer to users names them in one request; unknown ids are left out. |
| `POST /users` | `{"username", "password", "is_active", "is_staff", "is_superuser", "fields"}` | `201` and the user. The password is validated; without one, it is unusable. A username, an address, or a number another user has is `409` before your checks run (since 2.8). |
| `GET /users/names` | `?limit=50&cursor=&search=&id=` | The page `GET /users` answers, with each user's `id`, `username`, `name`, and `identifiers`: no groups, permissions, or other fields, for a page that names its rows' people (since 2.7). `name` joins `first_name` and `last_name` where `/users` shows both among `fields` (the model's `admin_fields()`), else it is null; `AUTH.NAME_FIELDS` names other fields, the first entry that gives text winning, such as a `display_name` with the full name after it (since 2.8): each must be a text field `/users` shows, among `fields` or as the username (`auth.E012`), and one that is not shown gives no name. Values are stripped (since 2.8): one of white space alone gives nothing; `identifiers` are the fields of `AUTH.IDENTIFIERS` that `/users` shows. `search` matches the username and the identifiers, as on `/users`, never a name. The same order and cursors; `auth.view_user`. |
| `GET /users/{id}` | | The user. |
| `PATCH /users/{id}` | `{"is_active", "is_staff", "is_superuser", "fields"}` | The user; what you leave out stays. A new email or mobile number is set at once, unproved, and drops one the user held ([Changing an address or a number](users.md#changing-an-address-or-a-number)). One another user has is `409` (`email_taken` where the address identifies users, `mobile_number_taken`) before your checks and approvals' hold run (since 2.8). |
| `DELETE /users/{id}` | | `204`; the user's tokens, second factor (secret and recovery codes), and object permissions go too. |
| `POST /users/{id}/password` | `{"password"}` | `204`; `null` makes it unusable. Every session of the user ends. A password among the user's last ones (`AUTH.PASSWORD_HISTORY`) is refused (`422`); past `AUTH.LOGIN_RATE` of those from one staff member for one user, `429` for a while (since 2.7). |
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

- **Only superusers make, unmake, or change superusers.** Making a
  deactivated superuser active again is making one (since 2.7): where
  approvals hold that, it is refused here too.
- **With `domainkit.contrib.approvals` guarding it**
  (`APPROVALS.SUPERUSER_APPROVALS`), no one makes a superuser here
  (`approval_required`): an approved `auth.grant_superuser` request does
  ([approvals.md](../contrib/approvals.md#making-superusers)). An app
  of your own can hold superusers back the same way: register a check
  with `on_make_superuser` (`domainkit.contrib.auth.gates`) in its
  `ready()`, and raise to refuse. `on_create_staff` does the same for
  creating a user who is staff (since 2.5); both run in the creation's
  twin too.
- **Every change to a user passes the project's own checks**:
  `on_admin_change(check)` (`domainkit.contrib.auth.gates`) is called
  with the staff member, the user (None when creating one), and an
  `AdminChange`, once the admin API's own checks passed (the bounds below,
  the fields, a password's validators) and right before the change: its
  `kind` (`create`, `update`, `delete`, `password`, `logout`,
  `impersonate`, `groups`, `permissions`), the `fields` and flags it
  sets, the groups or permissions it `added` and `removed`, since
  2.4 the `values` it sets (`{"is_staff": True}`, the `reason` of an
  impersonation; never a password), and since 2.7 the `groups` it adds,
  each an `AddedGroup` (its id, name, permissions, and grants on objects
  as read), and its `last_check`: the admin API's own check that only
  the change makes (a password the user had before), which runs once,
  when the change is about to be made. Approvals' hold runs it under the
  approved request's lock, before spending the approval, so a change it
  refuses spends nothing; otherwise the admin API runs it after every
  check. Await it from a check of yours only once the change will surely
  be made: after every check that may still refuse it (an order above
  100 where approvals hold the change) and under whatever makes it sure.
  Awaited earlier, a refusal that follows it (`approval_pending`, say)
  answers a password the user had before differently from another.
  Raise
  `PermissionDenied` to refuse it
  ("nobody edits their own account here", "making staff needs another
  permission"). `admin.may_grant` and `admin.may_hold`, the ceilings
  below, are public for your own routes. With `AUTH.ADMIN_ACTION` on,
  `on_admin_screen(check)` (since 2.7) is called in the change's twin
  instead, with the user and the `AdminChange`, before an authorization
  is stored: it reads, and raises to refuse the change there (approvals
  refuses a change it would hold that its approver's screen could not
  show whole). The twin of a change to a stored group calls
  `on_admin_group_screen(check)` the same way, with the group and the
  `GroupChange`.
- **Every change to a group passes them too** (since 2.7):
  `on_admin_group_change(check)` is called with the staff member, the
  group as stored (None when creating one), and a `GroupChange`: its
  `kind` (`group_create`, `group_rename`, `group_permissions`,
  `group_delete`), the `name` it gives, and the permissions it `added`
  and `removed`. Before 2.7 the group routes called no check, so a check
  of a group's change could only be a dependency of its route (user
  report A19). A group's checks are a point of their own: a check written
  for a user's changes is never given a group.
- **Checks run in an order.** Hooks run by the `order` they registered
  with, lowest first (0 by default), then in `INSTALLED_DOMAINS` order
  (since 2.7). Approvals registers its hold at `order=100`, so your
  checks refuse a change before an approval is spent on it
  ([approvals](../contrib/approvals.md#changes-of-the-admin-api)).
- **Each change can ask for an authorization of its own** (since 2.5,
  `AUTH.ADMIN_ACTION`), confirmed on the staff member's phone, with a
  code, or with a passkey
  ([Each change, authorized as one act](#each-change-authorized-as-one-act)).
- **With `domainkit.contrib.approvals`, changes can wait for a second
  person** (since 2.4): `APPROVALS.ADMIN_APPROVALS` holds making staff,
  adding groups or permissions, setting a password, and impersonating
  for approval. The call answers `403 approval_pending` with the
  request's id, and, once other people approve it, the same call by the
  same staff member makes the change, once
  ([Changes of the admin API](../contrib/approvals.md#changes-of-the-admin-api)).
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
- **Memberships count too** (multi-tenancy). Staff change a user only in
  tenants they belong to, holding there, or everywhere, every key the
  user holds there (`403`, saying in how many tenants). While
  `TENANCY.MEMBER_PERMISSIONS` is set without `MEMBER_TENANTS`, only
  superusers change users here (`auth.W010`)
  ([tenancy](../tenancy.md#members-permissions)).
- **Deactivating a user ends their sessions.** Their tokens are deleted,
  and do not work again if the user is made active again.
- **The last active superuser stays.** It cannot be demoted,
  deactivated, or deleted (`409 last_superuser`): make another one first.

### Each change, authorized as one act

A step-up window lets every change through for a while, with any
arguments, and so does a copy of the staff member's session. With
`AUTH["ADMIN_ACTION"]` on (since 2.5, [ADR 0163](../adr/0163-authorize-admin-changes-and-requests-steps-as-acts.md)), each route that changes a
user or a group asks for an authorization of that change, the action
`auth.admin_change`, in place of the window
([Authorizing one act](actions.md)). Off, the default, nothing changes,
and the twins below answer `404`.

```python
AUTH = {"ADMIN_ACTION": True}
```

Each changing route has a twin that asks for the authorization: it takes
the same body, runs the route's own checks, and answers `201` with what
the staff member is shown. A route alone on its path has its twin at
`<path>/authorization`; where a `PATCH` and a `DELETE` share a path, each
twin names its method, `<path>/authorization/patch` and
`<path>/authorization/delete`, as a resource router's twins do.

| Route | Its twin |
| --- | --- |
| `POST /users` | `POST /users/authorization` |
| `PATCH /users/{id}` | `POST /users/{id}/authorization/patch` |
| `DELETE /users/{id}` | `POST /users/{id}/authorization/delete` |
| `POST /users/{id}/password` | `POST /users/{id}/password/authorization` |
| `POST /users/{id}/logout` | `POST /users/{id}/logout/authorization` |
| `POST /users/{id}/impersonate` | `POST /users/{id}/impersonate/authorization` |
| `PUT /users/{id}/groups` | `POST /users/{id}/groups/authorization` |
| `PUT /users/{id}/permissions` | `POST /users/{id}/permissions/authorization` |
| `POST /groups` | `POST /groups/authorization` |
| `PATCH /groups/{id}` | `POST /groups/{id}/authorization/patch` |
| `PUT /groups/{id}/permissions` | `POST /groups/{id}/permissions/authorization` |
| `DELETE /groups/{id}` | `POST /groups/{id}/authorization/delete` |

```http
PUT  /admin/auth/users/6f1c.../groups {"groups": ["auditors"]}                 401 authorization_required
POST /admin/auth/users/6f1c.../groups/authorization {"groups": ["auditors"]}   201 {"id": "9f0c...", "shown": {...}}
POST /auth/app-sign-in/authorizations/9f0c...                                  200 {"request": "kq3D1...", ...}
POST /auth/app-sign-in/authorizations/token {"request": "kq3D1..."}           200 {"authorization": "9f0c....mYd3..."}
PUT  /admin/auth/users/6f1c.../groups {"groups": ["auditors"]}
     Action-Authorization: 9f0c....mYd3...                                     200
```

- **The act is the change**: its kind (`AdminChange.kind`, or a group's:
  `group_create`, `group_rename`, `group_permissions`, `group_delete`),
  the user or group (`auth.user:<id>`, `auth.group:<id>`; `new` for one
  being created), the name it gives, the values it sets, what it adds
  and takes away, sorted, and whether it leaves a usable password. Never
  the password itself. Another change, or the same change to another
  user, is refused (`403 action_not_authorized`), and voids it. A user
  created active and one created inactive are two acts.
- **A group is bound by its id and what it grants** (since 2.8, [ADR
  0179](../adr/0179-bind-the-admin-acts-groups-by-id-and-keep-a-held-confirmation-to-its-request.md)).
  A change of a user's groups binds each group it adds as approvals'
  `auth.set_groups` holds it: its id, its name, the keys it grants, and
  its grants on single objects (how many, and their digest); and the id
  of each it takes away. Before, it bound their names: a group renamed
  while the change waited for approval, and another named so, was added
  with the confirmation kept for the first (user report A17). A group
  renamed, made again, or granted more since the staff member confirmed
  is another act now (`403 action_not_authorized`).
- **The screen says which user and what change**: "Change a user's
  groups", then "User: alice@example.com (…123456789abc)" (the user's
  first identifier and, since 2.7, the end of their id; a name too long
  is cut beside it, as the act binds the id) or the group's name and id,
  the flags that grant or take access ("Superuser", "Staff", "Active":
  yes or no), "Adds: auditors", "Takes away: clerks", the reason of an
  impersonation, then the other fields set, by name ("Email", "Display
  name"). Since 2.8 a group added shows as approvals shows it, in a few
  words: "Adds: auditors (…9abc) 3 perms + 2 object grants", and the
  keys `AUTH.AUTHORIZATIONS.NAMED_PERMISSIONS` names among the keys they
  grant show by name before them ("Grants:
  companies.administer_company"); in a list of keys, those come first.
  A grant on a single object is counted, whatever its key.
  A list
  goes over as many lines as it takes ("Adds", "Adds (2)"). Six lines at
  most: with `AUTH["AUTHORIZATIONS"]["WHOLE"]` on (since 2.7), a change
  whose values or lists do not fit whole is refused at the twin (`422
  too_much_to_show`), and staff make it in smaller steps; so is a change
  approvals would hold that its approver's screen, a line longer, could
  not show whole. A user's or a group's name never is. Off, what does not
  fit is cut and counted, the flags first ("auditors, clerks and 4 more",
  "2 more values"). The phone shows these words, read from the server's
  record ([Confirming one act](ciba.md#confirming-one-act), [What a
  screen shows](actions.md#what-a-screen-shows)). With
  `AUTH["AUTHORIZATIONS"]["TYPED_LINKS"]` set to `"refused"` (since 2.8),
  what staff typed that holds a link, an email address, or a number to
  call is refused at the twin (`422 link_not_shown`): an impersonation's
  reason, a group's new name (at the change too, without the act), and
  the fields set anew but those that identify the user ([Text a person
  typed](actions.md#text-a-person-typed)).
- **The twin checks what the change checks**: the staff member's bounds,
  the fields, a password's validators, impersonation's rules, a user or
  group that is not there, the last active superuser deleted or
  deactivated (`409 last_superuser`), and the `on_create_staff` checks (a
  user created staff while approvals hold making staff, `403
  approval_required`). Since 2.7 it runs the `on_admin_change` checks
  (a group's twin, the `on_admin_group_change` checks) with `asking`
  true: a change your checks refuse is refused before the staff member
  confirms it (user report A17), and approvals' hold submits and spends
  nothing there. A check that writes should write nothing while
  `asking`. The twin changes nothing. Since 2.8 it refuses a username,
  an address, or a number another user has (`409`), as the change does
  before its checks. What only the change finds is refused then, after
  the confirmation: a group name taken, one of those values taken in the
  moment between (where approvals hold the change, the approval is spent
  by then, and stays spent), and a password the user had before
  (`AUTH.PASSWORD_HISTORY`), which the twin does not compare, as it
  would answer that guess unconfirmed.
- **A reused password spends the authorization** (since 2.7). The act
  binds only that a password is set, so before 2.7 one confirmation
  tested password after password against the user's history, refused
  each time and spending nothing (user report A17). The history is
  compared only when the change is about to be made: after the
  `on_admin_change` checks, and, where approvals hold setting a
  password, under the approved request's lock, by the one call that
  claims it, before it is spent. While a check refuses the change or its
  request waits, nothing is compared, and every password gets the same
  answer; so does a call that loses the claim to one made at once. With
  `ADMIN_ACTION`, a call that sets a password with the history on spends
  its authorization first, before any check, so calls sent at once with
  one confirmation make one comparison; one that waits for approval
  spends it too, and the next try is confirmed again. A
  password among the user's last ones is then refused (`422
  invalid_password`), and leaves the approval unspent; a password that
  will do makes the change.
- **Where the history may be compared, the call spends its authorization
  first** (`AUTH.PASSWORD_HISTORY` on, a password given): before the
  checks, in a transaction of its own, whatever comes of the call, a
  change that waits for approval included. One confirmation is one
  comparison: calls sent at once with one confirmation are refused but
  one (`403 action_not_authorized`), before anything is compared, and
  only the call that holds the confirmation may spend an approval.
  Confirm each call.
- **Reuses are counted for the staff member and the user together**
  (since 2.7): `AUTH.LOGIN_RATE`, and `ACCOUNT_LOGIN_RATE` over the day,
  of reuses refused; past them, that staff member's password changes of
  that user answer `429 rate_limited` for a while. A password that will
  do gives its count back. The user's own counts of wrong passwords
  (their logins, their password checks) are apart: neither holds the
  other back. The count holds with `AUTH.ADMIN_ACTION` off too. With
  `AUTH.STAFF_REUSES_COUNT_AS_PASSWORD_CHECKS` on (since 2.8), a reuse
  counts against the user's own counts of wrong passwords too: staff
  acting together share one count of that user's history, past it the
  user's own password checks wait, and the user's own wrong checks
  hold back a staff reset. With `PASSWORD_CHECKS_COUNT_AS_LOGINS`, that
  is the account's count of wrong logins too.
- **Checked before the project's checks, spent with the change.** The
  authorization must bind the change before the `on_admin_change` checks
  run, so a change it does not bind is never held for approval. It is
  spent in the change's own transaction: a change refused after that (a
  username taken) spends nothing. A change held for approval (`403
  approval_pending`) spends nothing either: once the request is approved,
  the same call makes it with an authorization, the first one while it
  lives (`AUTH.AUTHORIZATIONS.TTL`) or a new one. With
  `AUTH.AUTHORIZATIONS.HELD_TTL` set (since 2.7), the first one is kept
  while the request waits ([Held for approval](actions.md#held-for-approval)),
  but for a password compared with the history, the exception above: it
  spends first. Kept for that request, it serves that request alone
  (since 2.8): a call whose change now makes another request (a user
  made active again who was put in a group since) is refused (`403
  action_not_authorized`) before it submits it; a new confirmation does.
- **Its own methods** (since 2.7): `AUTH.AUTHORIZATIONS.ACTIONS["auth.admin_change"]`
  names what confirming a change must prove (`{"METHODS": ["phone"]}`)
  and how long it lives, apart from the project's acts
  ([Each act its own methods and lifetime](actions.md#each-act-its-own-methods-and-lifetime)).
- **Staff acting as a user** cannot ask, and their changes are refused
  (`401 authorization_required`): the act is the staff member's own.

The staff rules still answer first: someone who is not staff gets `403`
from a twin as from its route. Take any `StepUp(max_age=...)` you added
to these routes with `select_routes` away when you turn the setting on:
it would ask for a window too. `StepUp(for_="auth.admin_change")` added
that way passes `check`; the routes ask for it already.

A twin is a route of its own to `select_routes`: leave it in or out with
its route. `without=["DELETE /users/{user_id}"]` leaves `POST
/users/{user_id}/authorization/delete` in, so name it too; `only=[...]`
names the twins of the routes it keeps. While the setting is off, the
twins answer `404`, and the admin API loads nothing of the action.

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
  still holds the `change` permission on the user model, as they hold it
  everywhere: a role in the tenant the request is served as counts for
  nothing there, since users belong to no tenant (since 2.9). It also checks
  again that the staff member holds every permission the user holds, as
  when the token was issued. Deactivating or deleting the staff member,
  taking that permission away, or granting the user a permission the
  staff member does not have, ends the token at once with `401`, not when
  `IMPERSONATION_TTL` runs out.

What the token may do:

- **It is audited.** `auth.user_impersonated` names the staff member, the
  user, and the reason (kept as the user's personal data, erased with
  them). Every request the token makes is recorded under the staff
  member (the actor), so the trail shows who really acted. The events it
  causes name both: `actor.id` is the staff member's id, and
  `actor.impersonating` the user's
  ([Who acted](../resources.md#the-domain-behind-a-resource)).
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
- **What it is answered is kept apart.** A page cached per user
  (`cache_page(vary_on_user=True)`) and a response kept for an
  `Idempotency-Key` are kept for the staff member acting as that user,
  apart from the staff member's own, the user's, and those of each other
  user the staff member acts as (since 2.9). A kept response is about
  the user too: erasing them drops it. Rate limits, throttles, and live
  connections count the staff member, whomever they act as.

`impersonated(request)`, in `domainkit.contrib.auth.integration`, tells a
route of yours whether staff act as the user, and `request.auth.impersonator`
names the staff member.

## Options

| Setting | Default | What it does |
| --- | --- | --- |
| `REQUIRE_ROUTE_RULES` | `True` | Every route must declare a rule. |
| `SCOPED_CREDENTIALS` | `"refuse"` | What a scoped token gets on a route without a rule that checks scopes. |
| `AUTH.IMPERSONATION_TTL` | `None` | How long an impersonation token works; `None` turns impersonation off. |
| `AUTH.ADMIN_ACTION` | `False` | Each change of the admin API asks for an authorization of its own (`auth.admin_change`), in place of a step-up window ([Each change, authorized as one act](#each-change-authorized-as-one-act)). |
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
| `403 approval_required` | Making a superuser while approvals guard it, or creating a user who is staff while approvals hold making staff. |
| `403 approval_pending` | A change `APPROVALS.ADMIN_APPROVALS` holds: it waits for approval (`approval_request_id`); send the same call again once it is approved. |
| `401 authorization_required`, `403 action_not_authorized` | With `AUTH.ADMIN_ACTION`: a change sent without an authorization of it, or with one that does not bind it ([Authorizing one act](actions.md#errors-and-refusals)). |
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
- **Naming a route with the prefix it is mounted under.** `select_routes`
  reads `POST /login`, not `POST /auth/login`, and refuses a name that
  matches no route.
- **Mounting a router and a copy of it.** Each route then appears
  twice, under one operation id, which `check` refuses (`openapi.E001`).
  Mount the copy in place of the router.

## See also

- [Authenticating requests](authentication.md): who the rules see, and
  scopes.
- [Stepping up](step-up.md): the `StepUp` rule.
- [Users, groups, backends, and passwords](users.md): where permissions
  come from.
- [Resources](../resources.md): `ResourceRouter` takes a `rule=`.
- [Approvals](../contrib/approvals.md): maker-checker for sensitive
  commands.
