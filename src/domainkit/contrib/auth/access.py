"""Who may call an endpoint (Django REST framework's permission classes).

A rule answers for the request (``has_permission``) and, where a route
loads one object, for that object (``has_object_permission``). Combine
rule instances with ``&``, ``|``, and ``~`` (``IsAdminUser() |
HasPermissions("billing.view_invoice")``). ``Require`` makes rules a FastAPI
dependency that returns the user::

    @router.post(
        "/invoices",
        dependencies=[Depends(Require(HasPermissions("billing.add_invoice")))],
    )
    async def create(...): ...

    @router.get("/invoices/{invoice_id}")
    async def read(
        invoice_id: UUID,
        request: Request,
        user: Annotated[AnyUser, Depends(Require(ModelPermissions(Invoice)))],
    ):
        invoice = ...
        await check_object_permissions(request, invoice, IsOwner())

A refused anonymous caller gets 401, so it knows to log in; a refused user
gets 403. ``AuthenticationMiddleware`` must be installed.

Credentials may be limited to scopes, permission keys (a token made with
``scopes``): the permission rules then allow only what both the user and
the scopes allow, ``IsSuperuser`` refuses them, and ``IsUnscoped`` keeps a
route to credentials without a limit.
"""

from collections.abc import Iterable, Iterator, Sequence
from typing import Annotated, Any, ClassVar, Final, cast

from fastapi import Depends, FastAPI, Request, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute, Host, Mount, Route, WebSocketRoute

from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import APIError

from .authentication import get_authenticators
from .backends import has_perms
from .models import AbstractBaseUser, AnyUser

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
FRAMEWORK_ROUTES: Final = ("domainkit.http.", "fastapi.")
"""Modules whose routes guard themselves (probes, metrics, ``API_DOCS``)."""
PUBLIC_MARK: Final = "domainkit_public"
"""The attribute ``mark_public`` sets."""
MODEL_ACTIONS: Final = {
    "GET": "view",
    "HEAD": "view",
    "POST": "add",
    "PUT": "change",
    "PATCH": "change",
    "DELETE": "delete",
}
"""The permission each method needs from ``ModelPermissions``."""


class _Bearer(HTTPBearer):
    """``HTTPBearer`` for WebSocket handshakes too: both carry the header."""

    async def __call__(
        self, request: HTTPConnection
    ) -> HTTPAuthorizationCredentials | None:
        """The token the connection names, if any."""
        return await super().__call__(cast(Request, request))


bearer = _Bearer(
    auto_error=False,
    scheme_name="Token",
    description="A token from logging in: `Authorization: Bearer <token>`.",
)
"""Documents the token in OpenAPI; ``AuthenticationMiddleware`` checks it."""


class NotAuthenticated(APIError):
    """The route needs a user, and the request named none (401)."""

    def __init__(self) -> None:
        """Use status 401, with the first authentication class's challenge."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication credentials were not provided.",
            code="not_authenticated",
            headers={"WWW-Authenticate": get_authenticators()[0].challenge()},
        )


class BasePermission:
    """Allows everything; subclasses override one hook or both."""

    message: ClassVar[str] = "You do not have permission to perform this action."
    """The detail of the 403 a refusal answers with."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """Whether ``user`` may make ``request`` at all."""
        return True

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Whether ``user`` may make ``request`` on ``obj``."""
        return True

    def __and__(self, other: "Rule") -> "BasePermission":
        """Both rules must allow."""
        return AllOf(self, _rule(other))

    def __rand__(self, other: "Rule") -> "BasePermission":
        """Both rules must allow (a class on the left)."""
        return AllOf(_rule(other), self)

    def __or__(self, other: "Rule") -> "BasePermission":
        """Either rule may allow."""
        return AnyOf(self, _rule(other))

    def __ror__(self, other: "Rule") -> "BasePermission":
        """Either rule may allow (a class on the left)."""
        return AnyOf(_rule(other), self)

    def __invert__(self) -> "BasePermission":
        """Allow what this rule refuses."""
        return Not(self)


type Rule = BasePermission | type[BasePermission]
"""A rule, or a rule class that takes no arguments."""


class AllOf(BasePermission):
    """Every rule must allow."""

    def __init__(self, *rules: Rule) -> None:
        """Combine ``rules``."""
        self.rules = tuple(_rule(rule) for rule in rules)

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """Every rule allows the request."""
        return await _first_refusal(self.rules, request, user) is None

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Every rule allows ``obj``."""
        return await _first_refusal(self.rules, request, user, obj) is None


class AnyOf(BasePermission):
    """One rule must allow, for the request and for the object alike."""

    def __init__(self, *rules: Rule) -> None:
        """Combine ``rules``."""
        self.rules = tuple(_rule(rule) for rule in rules)

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """Some rule allows the request."""
        for rule in self.rules:
            if await rule.has_permission(request, user):
                return True
        return False

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """Some rule allows both the request and ``obj``."""
        for rule in self.rules:
            if await rule.has_permission(
                request, user
            ) and await rule.has_object_permission(request, user, obj):
                return True
        return False


class Not(BasePermission):
    """Allows what ``rule`` refuses."""

    def __init__(self, rule: Rule) -> None:
        """Invert ``rule``."""
        self.rule = _rule(rule)

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The rule refuses the request."""
        return not await self.rule.has_permission(request, user)

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """The rule refuses ``obj``."""
        return not await self.rule.has_object_permission(request, user, obj)


class AllowAny(BasePermission):
    """Anyone, logged in or not."""


class IsAuthenticated(BasePermission):
    """Users who logged in."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The request named a user."""
        return user.is_authenticated


class IsAuthenticatedOrReadOnly(BasePermission):
    """Anyone may read (``GET``, ``HEAD``, ``OPTIONS``); users may write."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """A safe method, or a user."""
        return _method(request) in SAFE_METHODS or user.is_authenticated


class IsAdminUser(BasePermission):
    """Staff users (``is_staff``)."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The user is staff."""
        return user.is_authenticated and getattr(user, "is_staff", False) is True


class IsSuperuser(BasePermission):
    """Active superusers, with credentials that are not limited to scopes."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The user is an active superuser, and holds every permission here."""
        return user.is_active and user.is_superuser and scopes_of(request) is None


class IsUnscoped(BasePermission):
    """Users whose credentials are not limited to scopes (a login, not a key).

    For what scopes cannot bound, such as managing users or tokens.
    """

    message: ClassVar[str] = "Credentials limited to scopes cannot do this."

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """A user, with credentials that carry no scopes."""
        return user.is_authenticated and scopes_of(request) is None


class HasScope(BasePermission):
    """Users whose credentials allow every scope named; unscoped ones allow all."""

    def __init__(self, *scopes: str) -> None:
        """Require ``scopes`` (permission keys)."""
        if not scopes:
            raise ImproperlyConfigured("HasScope needs at least one scope.")
        self.scopes = scopes

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """A user whose credentials cover the scopes."""
        return user.is_authenticated and _within(request, self.scopes)


class HasPermissions(BasePermission):
    """Users granted every permission named (``<domain>.<codename>``)."""

    def __init__(self, *perms: str) -> None:
        """Require ``perms``."""
        if not perms:
            raise ImproperlyConfigured("HasPermissions needs at least one permission.")
        self.perms = perms

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The backends grant every permission, and the scopes allow them."""
        return _within(request, self.perms) and await has_perms(user, self.perms)


class HasObjectPermissions(HasPermissions):
    """Users granted every permission named on the object itself.

    Object permissions come from backends other than ``ModelBackend``
    (``AUTH.BACKENDS``). The request itself needs an active user who logged
    in; check each object with ``check_object_permissions``.
    """

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """An active user who logged in; the object decides the rest."""
        return user.is_authenticated and user.is_active

    async def has_object_permission(
        self, request: HTTPConnection, user: AnyUser, obj: object
    ) -> bool:
        """The backends grant every permission on ``obj``, within the scopes."""
        return _within(request, self.perms) and await has_perms(user, self.perms, obj)


class ModelPermissions(BasePermission):
    """Django's model permissions, by method, for one model.

    ``GET`` and ``HEAD`` need ``view``, ``POST`` ``add``, ``PUT`` and
    ``PATCH`` ``change``, and ``DELETE`` ``delete``; ``OPTIONS`` needs only
    a user. Other methods are refused. ``model`` is a mapped class or its
    content type (``"billing.invoice"``).
    """

    actions: ClassVar[dict[str, str]] = MODEL_ACTIONS

    def __init__(self, model: type | str) -> None:
        """Check permissions of ``model``."""
        self.model = model

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The user may take the method's action on the model."""
        if not user.is_authenticated:
            return False
        method = _method(request)
        if method == "OPTIONS":
            return True
        action = self.actions.get(method)
        if action is None:
            return False
        domain, model = self._content_type()
        perm = f"{domain}.{action}_{model}"
        return _within(request, [perm]) and await has_perms(user, [perm])

    def _content_type(self) -> tuple[str, str]:
        """``(domain, model)`` of the model."""
        if isinstance(self.model, str):
            domain, _, model = self.model.partition(".")
            return domain, model
        from domainkit.contrib.contenttypes import content_type_key

        return content_type_key(self.model)


class Require:
    """A FastAPI dependency: the rules must allow the request; returns the user.

    Without rules, it requires a user who logged in.
    """

    def __init__(self, *rules: Rule) -> None:
        """Check ``rules`` (default: ``IsAuthenticated``) on every request."""
        self.rules = tuple(_rule(rule) for rule in rules) or (IsAuthenticated(),)

    async def __call__(
        self,
        request: HTTPConnection,
        _: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> AnyUser:
        """The user, if every rule allows the request.

        Raises:
            NotAuthenticated: An anonymous caller was refused.
            PermissionDenied: A user was refused.
        """
        user = current_user(request)
        refusal = await _first_refusal(self.rules, request, user)
        if refusal is not None:
            raise _denied(user, refusal)
        return user


async def check_object_permissions(
    request: HTTPConnection, obj: object, *rules: Rule
) -> None:
    """Refuse the request unless every rule allows it on ``obj``.

    Raises:
        NotAuthenticated: An anonymous caller was refused.
        PermissionDenied: A user was refused.
    """
    user = current_user(request)
    refusal = await _first_refusal(
        tuple(_rule(rule) for rule in rules), request, user, obj
    )
    if refusal is not None:
        raise _denied(user, refusal)


def current_user(connection: HTTPConnection) -> AnyUser:
    """The user ``AuthenticationMiddleware`` found (``AnonymousUser`` if none)."""
    if "user" not in connection.scope:
        raise ImproperlyConfigured(
            "Add domainkit.contrib.auth.middleware.AuthenticationMiddleware to "
            "MIDDLEWARE to know who sends requests."
        )
    user: AnyUser = connection.scope["user"]
    return user


async def authenticated_user(
    request: HTTPConnection,
    _: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AbstractBaseUser:
    """The user who logged in.

    Raises:
        NotAuthenticated: The request named no user.
    """
    user = current_user(request)
    if not isinstance(user, AbstractBaseUser):
        raise NotAuthenticated
    return user


CurrentUser = Annotated[AnyUser, Depends(current_user)]
"""The request's user, or ``AnonymousUser``."""
AuthenticatedUser = Annotated[AbstractBaseUser, Depends(authenticated_user)]
"""The request's user; anonymous callers get 401."""


async def public() -> None:
    """Mark a route public on purpose: anyone may call it, without a token."""


Public = Depends(public)
"""``dependencies=[Public]``: the route needs no rule (``REQUIRE_ROUTE_RULES``)."""
USER_GUARDS: Final[tuple[object, ...]] = (current_user, authenticated_user)
"""Dependencies that read the user, besides ``Require``."""


def mark_public[T](served: T) -> T:
    """Mark a mounted application or a Starlette endpoint public on purpose.

    They take no dependencies, so ``Public`` cannot sit on them, and
    ``open_routes`` lists them otherwise::

        app.mount("/static", mark_public(StaticFiles(directory="static")))
    """
    setattr(served, PUBLIC_MARK, True)
    return served


def open_routes(app: FastAPI) -> list[str]:
    """``METHOD /path`` of every route that declares no access rule.

    A route declares one with ``Require``, ``CurrentUser``,
    ``AuthenticatedUser``, or ``Public``, on itself or its router. The
    framework's own routes (``/health``, ``/ready``, ``/metrics``, and the
    ``API_DOCS``) guard themselves. Mounted FastAPI and Starlette
    applications are walked route by route. What takes no dependencies (a
    mounted ``StaticFiles``, Starlette's own ``Route``) is listed unless
    marked with ``mark_public``.
    """
    return [name for name, _ in unguarded_routes(app)]


def unguarded_routes(app: FastAPI) -> list[tuple[str, bool]]:
    """``open_routes``, each with whether it could take a rule (a dependency)."""
    guards = (public, *USER_GUARDS)
    return [
        (name, dependant is not None)
        for name, dependant in _routes(app.routes)
        if dependant is None or not _uses(dependant, guards)
    ]


def routes_need_users(app: FastAPI) -> bool:
    """Whether some route of ``app`` reads the user or checks rules."""
    return any(
        dependant is not None and _uses(dependant, USER_GUARDS)
        for _, dependant in _routes(app.routes)
    )


def _routes(
    routes: Sequence[BaseRoute], prefix: str = ""
) -> Iterator[tuple[str, Dependant | None]]:
    """``METHOD /path`` and dependencies of each route, but the framework's own.

    Routers are walked as OpenAPI walks them, so the prefixes and
    dependencies of the routers a route is included through count, and so
    are the applications mounted with routes of their own. What takes no
    dependencies (an application without routes, Starlette's own routes)
    has None, unless it was marked public.
    """
    for context in iter_route_contexts(routes):
        route = context.original_route
        # Routes of included routers keep their full path (and a WebSocket
        # its dependencies) here; FastAPI's own context has them for others.
        effective = getattr(context, "starlette_route", None)
        if isinstance(effective, type(route)):
            route = effective
        found: tuple[str, Dependant | None]
        if isinstance(route, APIWebSocketRoute):
            found = (f"WEBSOCKET {prefix}{route.path}", route.dependant)
        elif isinstance(route, APIRoute):
            methods = " ".join(sorted(route.methods or ()))
            found = (f"{methods} {prefix}{context.path}", context.dependant)
        elif _marked_public(route):
            continue
        elif isinstance(route, Mount | Host) and route.routes:
            inner = prefix + route.path if isinstance(route, Mount) else prefix
            yield from _routes(route.routes, inner)
            continue
        else:
            path = getattr(route, "path", "")
            found = (f"{_kind(route)} {prefix}{path}".rstrip(), None)
        endpoint = getattr(route, "endpoint", None)
        if not getattr(endpoint, "__module__", "").startswith(FRAMEWORK_ROUTES):
            yield found


def _marked_public(route: BaseRoute) -> bool:
    """Whether what ``route`` serves was marked with ``mark_public``."""
    served = (getattr(route, name, None) for name in ("endpoint", "app", "_base_app"))
    return any(getattr(target, PUBLIC_MARK, False) is True for target in served)


def _kind(route: BaseRoute) -> str:
    """How ``open_routes`` names a route that takes no dependencies."""
    if isinstance(route, Route):
        return " ".join(sorted(route.methods)) if route.methods else "ANY"
    if isinstance(route, WebSocketRoute):
        return "WEBSOCKET"
    if isinstance(route, Host):
        return f"HOST {route.host}"
    return "MOUNT" if isinstance(route, Mount) else type(route).__name__


def _uses(dependant: Dependant, guards: tuple[object, ...]) -> bool:
    """Whether ``dependant`` depends, at any depth, on ``Require`` or ``guards``."""
    call = dependant.call
    if isinstance(call, Require) or any(call is guard for guard in guards):
        return True
    return any(_uses(dependency, guards) for dependency in dependant.dependencies)


def _rule(rule: Rule) -> BasePermission:
    """A rule instance (classes are instantiated)."""
    instance = rule() if isinstance(rule, type) else rule
    if not isinstance(instance, BasePermission):
        raise ImproperlyConfigured(
            f"{rule!r} is not a BasePermission; combine rule instances, as in "
            "IsAdminUser() | IsSuperuser()."
        )
    return instance


def scopes_of(connection: HTTPConnection) -> frozenset[str] | None:
    """The scopes the request's credentials are limited to; None: no limit."""
    credentials = connection.scope.get("auth")
    scopes: frozenset[str] | None = getattr(credentials, "scopes", None)
    return scopes


def _within(connection: HTTPConnection, perms: Iterable[str]) -> bool:
    """Whether the request's scopes, if any, cover every one of ``perms``."""
    scopes = scopes_of(connection)
    return scopes is None or scopes.issuperset(perms)


def _method(request: HTTPConnection) -> str:
    """The request method (``GET`` for a WebSocket)."""
    method: Any = request.scope.get("method", "GET")
    return str(method)


async def _first_refusal(
    rules: Sequence[BasePermission],
    request: HTTPConnection,
    user: AnyUser,
    *obj: object,
) -> BasePermission | None:
    """The first rule that refuses the request (or ``obj``), if any."""
    for rule in rules:
        allowed = (
            await rule.has_object_permission(request, user, obj[0])
            if obj
            else await rule.has_permission(request, user)
        )
        if not allowed:
            return rule
    return None


def _denied(user: AnyUser, rule: BasePermission) -> Exception:
    """401 for an anonymous caller, 403 with the rule's message for a user."""
    if not user.is_authenticated:
        return NotAuthenticated()
    return PermissionDenied(rule.message)
