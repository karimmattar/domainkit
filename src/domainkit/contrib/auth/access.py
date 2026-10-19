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
"""

from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Final, cast

from fastapi import Depends, FastAPI, Request, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection

from domainkit.ddd import PermissionDenied
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import APIError

from .authentication import get_authenticators
from .backends import has_perms
from .models import AbstractBaseUser, AnyUser

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
FRAMEWORK_ROUTES: Final = "domainkit.http."
"""Modules whose routes (probes, metrics) guard themselves."""
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
    """Active superusers."""

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The user is an active superuser."""
        return user.is_active and user.is_superuser


class HasPermissions(BasePermission):
    """Users granted every permission named (``<domain>.<codename>``)."""

    def __init__(self, *perms: str) -> None:
        """Require ``perms``."""
        if not perms:
            raise ImproperlyConfigured("HasPermissions needs at least one permission.")
        self.perms = perms

    async def has_permission(self, request: HTTPConnection, user: AnyUser) -> bool:
        """The backends grant every permission."""
        return await has_perms(user, self.perms)


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
        """The backends grant every permission on ``obj``."""
        return await has_perms(user, self.perms, obj)


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
        return await has_perms(user, [f"{domain}.{action}_{model}"])

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


def open_routes(app: FastAPI) -> list[str]:
    """``METHOD /path`` of every route that declares no access rule.

    A route declares one with ``Require``, ``CurrentUser``,
    ``AuthenticatedUser``, or ``Public``, on itself or its router. The
    framework's own routes (``/health``, ``/ready``, ``/metrics``) guard
    themselves.
    """
    guards = (public, *USER_GUARDS)
    return [name for name, dependant in _routes(app) if not _uses(dependant, guards)]


def routes_need_users(app: FastAPI) -> bool:
    """Whether some route of ``app`` reads the user or checks rules."""
    return any(_uses(dependant, USER_GUARDS) for _, dependant in _routes(app))


def _routes(app: FastAPI) -> list[tuple[str, Dependant]]:
    """``METHOD /path`` and dependencies of each route, but the framework's own.

    Routers are walked as OpenAPI walks them, so the prefixes and
    dependencies of the routers a route is included through count.
    """
    found = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIWebSocketRoute):
            # Included WebSockets keep their full path and dependencies here.
            effective = getattr(context, "starlette_route", None)
            if isinstance(effective, APIWebSocketRoute):
                route = effective
            name, dependant = f"WEBSOCKET {route.path}", route.dependant
        elif isinstance(route, APIRoute):
            name = f"{' '.join(sorted(route.methods or ()))} {context.path}"
            dependant = context.dependant
        else:
            continue
        if not route.endpoint.__module__.startswith(FRAMEWORK_ROUTES):
            found.append((name, dependant))
    return found


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
