# Protecting routes, and the admin API

## Protecting routes

Rules decide who may call a route. They are the core's,
`domainkit.http.access`, and work with any authentication (see
[Another authentication](#another-authentication)); this app adds the ones
about its own users (`IsAdminUser`, `IsSuperuser`, `AuthenticatedUser`).
`Require(*rules)` makes them a FastAPI dependency that returns the user,
and documents the bearer token in OpenAPI; without rules it requires a
user. A refused anonymous caller gets `401 not_authenticated`, so it knows
to log in; a refused user gets `403`.

```python
from domainkit.contrib.auth import AuthenticatedUser, CurrentUser
from domainkit.http.access import HasPermissions, ModelPermissions, Require

@router.post("/invoices", dependencies=[Depends(Require(HasPermissions("billing.add_invoice")))])
async def create_invoice(...): ...

@router.api_route("/products", methods=["GET", "POST"])
async def products(user: Annotated[AnyUser, Depends(Require(ModelPermissions(Product)))]): ...

@router.get("/me/invoices")
async def my_invoices(user: AuthenticatedUser): ...      # 401 for anonymous callers

@router.get("/greeting")
async def greeting(user: CurrentUser): ...               # anyone; AnonymousUser if so
```

| Rule | Allows |
| --- | --- |
| `AllowAny` | Anyone. |
| `IsAuthenticated` | Users who logged in. |
| `IsAuthenticatedOrReadOnly` | Anyone for `GET`, `HEAD`, `OPTIONS`; users otherwise. A WebSocket handshake is a `GET`, so anyone may open one: guard sockets that take writes with `IsAuthenticated`. |
| `IsAdminUser` | Staff users (`is_staff`), with credentials that carry no scopes. |
| `IsSuperuser` | Active superusers, with credentials that carry no scopes. |
| `HasPermissions(*keys)` | Users granted every permission (by any backend). |
| `ModelPermissions(model)` | Django's model permissions by method: `GET`/`HEAD` need `view`, `POST` `add`, `PUT`/`PATCH` `change`, `DELETE` `delete`; `OPTIONS` needs a user, and a WebSocket handshake `view`. `model` is a mapped class or `"<domain>.<model>"`. A class's content type is worked out once, and again after `registry.populate` or `clear`, a class mapped, or a table added to or removed from the project's metadata. |
| `HasObjectPermissions(*keys)` | Users granted every permission on the object, by a backend with object permissions. The request itself needs an active user who logged in; check each object with `check_object_permissions`. |
| `HasScope(*keys)` | Users whose credentials' scopes cover every key; credentials without scopes cover all. |
| `IsUnscoped` | Users whose credentials carry no scopes (a login, not a scoped token). |
| `AllowScoped` | Users who logged in, scoped tokens too, on purpose ([Scopes on every route](authentication.md#scopes-on-every-route)). |
| `StepUp(max_age=...)` | Users whose credentials authenticated again, with a second factor, within `max_age` (a day at most); refused, `401 step_up_required` ([Stepping up](step-up.md)). |

A refused user (`403`) is recorded in the audit trail when
`domainkit.contrib.audit` is installed: `auth.access`, outcome `denied`,
with the actor, the route's method and template (`GET /products/{owner}`,
never its values), and the refusing rule's class (`scopes_unbounded` for
a scoped token refused by `SCOPED_CREDENTIALS`). One actor's refusals on
one route are recorded once a minute, so probing many object ids writes a
row a minute per route; 401s for anonymous callers are not recorded. Any
installed domain that implements `DenialRecorder`
(`record_denial(details)`) gets them, with `contrib.auth` or without it.
A recorder that fails is logged; the refusal stands.

Combine rule instances with `&`, `|`, and `~`:
`Require(IsAdminUser() | HasPermissions("billing.view_invoice"))`. A rule of
your own subclasses `BasePermission`, overrides `has_permission(request,
user)` or `has_object_permission(request, user, obj)` (both async), and may
set `message`. A rule whose refusal should tell the client what to do
returns its own error from `refusal(request, user)` (as `StepUp` does);
it answers in place of the `403`, unrecorded, and `&` passes on that of
its first refusing rule. `user` is the request's `Principal`; annotate it as the
users your rule is written for, such as `AnyUser`. Where a route loads one
object, check it:

```python
class IsOwner(BasePermission):
    message = "Only the owner may change an invoice."

    async def has_object_permission(self, request, user, obj):
        return obj.owner_id == user.id


invoice = await uow.invoices.get(invoice_id)
await check_object_permissions(request, invoice, IsOwner())
```

### Deny by default

Every route must say who may call it: a `Require(...)` dependency, a
`CurrentUser` or `AuthenticatedUser` parameter, or `Public` for a route
anyone may call. Each can sit on the route, on a dependency it uses, or on
a router it is included through. Routes that say nothing are listed by
`manage.py check`, logged when the application starts in development and
tests, and refused in staging and production, where the application does
not start. The application is checked when it is built, and again when it
starts serving (its lifespan, once the domains have started), so a router
included afterwards, in `asgi.py` or by a domain's startup, is checked
too; requests never pay for it:

```text
The application has problems:
  GET /reports declares no access rule: add Depends(Require(...)), or dependencies=[Public] if anyone may call it.
```

```python
from domainkit.http.access import Public


@router.get("/catalog", dependencies=[Public])  # anyone, on purpose
async def catalog(): ...


public_router = APIRouter(dependencies=[Public])  # every route in it
```

The framework's own routes (`/health`, `/ready`, `/metrics`, and the
`API_DOCS` pages) and the login and refresh endpoints are covered already.
Applications mounted with routes of their own (another `FastAPI`, a
Starlette `Mount` of routes) are checked route by route. What takes no
dependencies, a mounted `StaticFiles` or another ASGI application, and
Starlette's own `Route` and `WebSocketRoute`, cannot declare a rule, so it
is listed (`MOUNT /static`) until you serve it from a FastAPI route with
`Require(...)`, or mark it public with `mark_public`:

```python
from starlette.staticfiles import StaticFiles

from domainkit.http.access import mark_public

router.mount("/static", mark_public(StaticFiles(directory="static")))
```

The check is the core's: it holds with this app or without it.
`REQUIRE_ROUTE_RULES = False` turns it off (`AUTH.REQUIRE_ROUTE_RULES`,
its name until 0.10, is refused since 0.11, naming it); routes that
read the user still need `AuthenticationMiddleware`, and startup reports
them while it is missing. `open_routes(app)` returns the list, for a test
of your own.

### Another authentication

`ResourceRouter`, the rules, and deny by default need no
`domainkit.contrib.auth`, nor its tables: a project that authenticates
requests its own way (an OpenID Connect middleware, a gateway's header)
puts who sent each request in `scope["user"]`, from an ASGI middleware of
its own. The rules read a `domainkit.http.access.Principal`: `id`,
`is_authenticated`, `is_active`, and `ahas_perms(perms, obj=None)`, which
answers permission keys (`billing.view_invoice`) as the project decides;
`AnonymousPrincipal` stands for a caller who proved nothing. A principal
whose class has `challenge()` names the `WWW-Authenticate` of the `401`
an anonymous caller gets (`Bearer realm="api"` otherwise), and credentials
in `scope["auth"]` with `scopes` limit the permission rules as a scoped
token does ([ADR 0080](../adr/0080-access-rules-are-core-auth-is-one-provider.md)):

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
            scope["user"] = (
                GatewayUser(user.decode(), frozenset(...))
                if user
                else AnonymousPrincipal()
            )
        await self.app(scope, receive, send)
```

A dependency of the project's own that reads the principal counts as a
rule for deny by default once marked with `reads_principal`, as
`CurrentUser` and `AuthenticatedUser` are.

## The admin API

`domainkit.contrib.auth.admin.router` is Django's admin for users, groups,
and permissions, over REST. Mount it where staff tools live:

```python
from domainkit.contrib.auth.admin import router as admin_router

router.include_router(admin_router, prefix="/admin/auth")
```

Every endpoint needs a staff user (`is_staff`) or a superuser, and the
model permission of its action: `view`, `add`, `change`, or `delete` on
the user model (`auth.change_user`, or `accounts.change_account` for a
custom user), `auth.*_group`, or `auth.view_permission`.

| Endpoint | Body | Answer |
| --- | --- | --- |
| `GET /users` | `?limit=50&cursor=&search=` | A page of users by username; `search` matches part of it, in any case. |
| `POST /users` | `{"username", "password", "is_active", "is_staff", "is_superuser", "fields"}` | `201` and the user. The password is validated; without one, it is unusable. |
| `GET /users/{id}` | | The user. |
| `PATCH /users/{id}` | `{"is_active", "is_staff", "is_superuser", "fields"}` | The user; what is left out stays. |
| `DELETE /users/{id}` | | `204`; the user's tokens, second factor (secret and recovery codes), and object permissions go too. |
| `POST /users/{id}/password` | `{"password"}` | `204`; `null` makes it unusable. Every session of the user ends. |
| `POST /users/{id}/logout` | | `204`: every session of the user ends. |
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
`is_superuser`, `last_login`, `groups`, the `permissions` granted directly,
and `fields`: the ones the model lists in `EDITABLE_FIELDS` and
`READ_ONLY_FIELDS` (`email`, `first_name`, `last_name`, `date_joined`, and
`email_verified` for `AbstractUser`). `fields` in a request changes only those in
`EDITABLE_FIELDS` (`editable_fields()`); naming any other field answers
`422 unknown_user_field`. Values are checked against the
field's type annotation (`422 invalid_user_field`), and `clean_field(name,
value)`, which a custom user may override, tidies what is stored, when a
user is made as when one is changed.

Staff hand out only what they have:

- Only superusers make, unmake, or change superusers.
- Other staff cannot grant a permission they lack, directly or through a
  group (`403`).
- Nor can they change a user or a group that holds such a permission:
  setting a user's password is as good as becoming them. What every backend
  in `AUTH.BACKENDS` grants counts, and so does what a change would grant
  (a field a backend reads, a user made active again). Inactive users count
  with every grant they keep; while other backends are configured, only
  superusers change inactive users, since those backends grant them nothing
  until they are active.
- Deactivating a user ends their sessions: their tokens are deleted, and do
  not work again if they are reactivated.
- The last active superuser cannot be demoted, deactivated, or deleted
  (`409 last_superuser`): make another one first.

The listings and reads are resources ([resources.md](../resources.md)):
`limit` and `cursor` page them, and a user or group carries an `ETag`,
so `If-None-Match` answers `304` while it is unchanged.

Changes are recorded as events (`UserUpdated`, `UserDeleted`,
`UserStatusChanged`, `UserJoinedGroup`, `GroupDeleted`, and the others),
so with `domainkit.contrib.audit` installed the audit trail names who
changed what.

