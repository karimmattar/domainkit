"""Who may call an endpoint: access rules, for any authentication (ADR 0080).

Django REST framework's permission classes. A rule answers for the request
(``has_permission``) and, where a route loads one object, for that object
(``has_object_permission``). Combine rule instances with ``&``, ``|``, and
``~`` (``IsAuthenticated() & ~HasScope("billing.view_invoice")``).
``Require`` makes rules a FastAPI dependency that returns the principal::

    @router.post(
        "/invoices",
        dependencies=[Depends(Require(HasPermissions("billing.add_invoice")))],
    )
    async def create(...): ...

    @router.get("/invoices/{invoice_id}")
    async def read(
        invoice_id: UUID,
        request: Request,
        user: Annotated[Principal, Depends(Require(ModelPermissions(Invoice)))],
    ):
        invoice = ...
        await check_object_permissions(request, invoice, IsOwner())

A refused anonymous caller gets 401, so it knows to log in; a refused one
who is known gets 403.

Who sent a request is a ``Principal`` that an authentication middleware puts
in ``scope["user"]``: ``domainkit.contrib.auth``'s ``AuthenticationMiddleware``,
or a project's own (an OpenID Connect middleware, a gateway's header). The
rules read only what the protocol states; a principal answers permission
keys itself (``ahas_perms``). A middleware may put the credentials in
``scope["auth"]``: when they carry ``scopes`` (permission keys), the
permission rules allow only what both the principal and the scopes allow,
and ``IsUnscoped`` keeps a route to credentials without a limit.

Credentials limited to scopes are bounded on every route, not only on
routes that check a permission (``SCOPED_CREDENTIALS``): a route reached
with them must declare a rule that checks scopes (``HasScope``, a permission
rule, ``IsUnscoped``, or ``AllowScoped`` for what a scope cannot bound,
such as logging out). In 0.10 a route that declares none still lets them
through, with a ``DomainkitDeprecationWarning`` and a security log line;
``SCOPED_CREDENTIALS = "refuse"`` refuses them with 403 now, as 0.11 will.

A refusal of a principal who proved who they are (403) goes to the audit
trail when an installed domain keeps one (``DenialRecorder``:
``domainkit.contrib.audit``): the route, the rule, and the actor, at most
once per actor and route each ``DENIAL_AUDIT_INTERVAL``.

Every route must declare who may call it (``REQUIRE_ROUTE_RULES``): with
``Require``, ``CurrentUser``, a dependency marked ``reads_principal``, or
``Public``. ``open_routes`` lists those that do not.
"""

import logging
import threading
import warnings
from collections import OrderedDict
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Final, Protocol, cast, runtime_checkable

from fastapi import Depends, FastAPI, Request, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute, Host, Mount, Route, WebSocketRoute

from domainkit import clock
from domainkit.apps import registry
from domainkit.conf import settings
from domainkit.db.models import model_key
from domainkit.ddd import PermissionDenied
from domainkit.deprecation import DomainkitDeprecationWarning
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.errors import APIError
from domainkit.http.live import record_check
from domainkit.log import SECURITY_LOGGER, ThrottledLog

__all__ = [
    "MODEL_ACTIONS",
    "SAFE_METHODS",
    "AllOf",
    "AllowAny",
    "AllowScoped",
    "AnonymousPrincipal",
    "AnyOf",
    "BasePermission",
    "CurrentUser",
    "DenialRecorder",
    "HasObjectPermissions",
    "HasPermissions",
    "HasScope",
    "IsAuthenticated",
    "IsAuthenticatedOrReadOnly",
    "IsUnscoped",
    "ModelPermissions",
    "Not",
    "NotAuthenticated",
    "Principal",
    "Public",
    "Require",
    "Rule",
    "acurrent_user",
    "check_object_permissions",
    "check_scopes_bounded",
    "current_user",
    "mark_public",
    "open_routes",
    "reads_principal",
    "scopes_of",
]

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that only read: ``IsAuthenticatedOrReadOnly`` lets anyone use them."""
FRAMEWORK_ROUTES: Final = ("domainkit.http.", "fastapi.")
"""Modules whose routes guard themselves (probes, metrics, ``API_DOCS``)."""
PUBLIC_MARK: Final = "domainkit_public"
"""The attribute ``mark_public`` sets."""
READS_PRINCIPAL: Final = "domainkit_reads_principal"
"""The attribute ``reads_principal`` sets."""
DEFAULT_CHALLENGE: Final = 'Bearer realm="api"'
"""The ``WWW-Authenticate`` of a 401 when the principal names none."""
MODEL_ACTIONS: Final = {
    "GET": "view",
    "HEAD": "view",
    "POST": "add",
    "PUT": "change",
    "PATCH": "change",
    "DELETE": "delete",
}
"""The permission each method needs from ``ModelPermissions``."""
SCOPES_UNBOUNDED: Final = (
    "Credentials limited to scopes cannot call this route: it declares no "
    "rule that checks scopes."
)
"""The detail of the 403 ``SCOPED_CREDENTIALS = "refuse"`` answers with."""
DENIAL_AUDIT_INTERVAL: Final = 60.0
"""Seconds during which further refusals of one actor on one route go unrecorded."""
MAX_DENIAL_KEYS: Final = 10_000
"""Actors and routes whose last recorded refusal is remembered."""

security_logger = logging.getLogger(SECURITY_LOGGER)
logger = logging.getLogger(__name__)


class Principal(Protocol):
    """Who sent a request, as the access rules read them (``scope["user"]``).

    ``domainkit.contrib.auth``'s users and ``AnonymousUser`` are principals;
    so is whatever another authentication middleware puts in the scope, if
    it has these. Rules of a project's own may read more of its own
    principals. A principal whose class has ``challenge()`` names the
    ``WWW-Authenticate`` of the 401 an anonymous one gets.
    """

    @property
    def id(self) -> object:
        """Who it is (None for an anonymous caller)."""
        ...

    @property
    def is_authenticated(self) -> bool:
        """Whether the request proved who sent it."""
        ...

    @property
    def is_active(self) -> bool:
        """Whether it may act at all: an inactive principal holds no permission."""
        ...

    async def ahas_perms(self, perms: Collection[str], obj: object = None) -> bool:
        """Whether it holds every permission key of ``perms`` (on ``obj``)."""
        ...


@dataclass(frozen=True, slots=True)
class AnonymousPrincipal:
    """A caller who proved nothing: no id, no permissions, for any middleware.

    Every instance equals every other. An authentication middleware of a
    project's own may put one in ``scope["user"]`` for requests without
    credentials.
    """

    id: ClassVar[None] = None
    is_authenticated: ClassVar[bool] = False
    is_active: ClassVar[bool] = False

    async def ahas_perms(self, perms: Collection[str], obj: object = None) -> bool:
        """Never."""
        return False


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
"""Documents the token in OpenAPI; the authentication middleware checks it."""


class NotAuthenticated(APIError):
    """The route needs a principal who proved who they are, and none did (401)."""

    def __init__(self, challenge: str = DEFAULT_CHALLENGE) -> None:
        """Use status 401, with ``challenge`` as ``WWW-Authenticate``."""
        super().__init__(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication credentials were not provided.",
            code="not_authenticated",
            headers={"WWW-Authenticate": challenge},
        )


class BasePermission:
    """Allows everything; subclasses override one hook or both.

    ``user`` is a ``Principal``; a rule may annotate it as the principals it
    is written for, such as ``domainkit.contrib.auth``'s ``AnyUser``.
    """

    message: ClassVar[str] = "You do not have permission to perform this action."
    """The detail of the 403 a refusal answers with."""
    bounds_scopes: ClassVar[bool] = False
    """Whether the rule, when it allows a request, has checked its scopes.

    A route reached with credentials limited to scopes must declare a rule
    that does (``SCOPED_CREDENTIALS``). A rule of a project's own that reads
    ``scopes_of`` sets it.
    """

    def checks_scopes(self) -> bool:
        """Whether allowing a request means its scopes were checked."""
        return self.bounds_scopes

    async def has_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - the rule's principal type (a ``Principal``).
    ) -> bool:
        """Whether ``user`` may make ``request`` at all."""
        return True

    async def has_object_permission(
        self,
        request: HTTPConnection,
        user: Any,  # noqa: ANN401 - the rule's principal type (a ``Principal``).
        obj: object,
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

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """Every rule allows the request."""
        return await _first_refusal(self.rules, request, user) is None

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """Every rule allows ``obj``."""
        return await _first_refusal(self.rules, request, user, obj) is None

    def checks_scopes(self) -> bool:
        """Some rule checks them: all must allow."""
        return any(rule.checks_scopes() for rule in self.rules)


class AnyOf(BasePermission):
    """One rule must allow, for the request and for the object alike."""

    def __init__(self, *rules: Rule) -> None:
        """Combine ``rules``."""
        self.rules = tuple(_rule(rule) for rule in rules)

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """Some rule allows the request."""
        for rule in self.rules:
            if await rule.has_permission(request, user):
                return True
        return False

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """Some rule allows both the request and ``obj``."""
        for rule in self.rules:
            if await rule.has_permission(
                request, user
            ) and await rule.has_object_permission(request, user, obj):
                return True
        return False

    def checks_scopes(self) -> bool:
        """Every rule checks them: any one may allow."""
        return all(rule.checks_scopes() for rule in self.rules)


class Not(BasePermission):
    """Allows what ``rule`` refuses."""

    def __init__(self, rule: Rule) -> None:
        """Invert ``rule``."""
        self.rule = _rule(rule)

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The rule refuses the request."""
        return not await self.rule.has_permission(request, user)

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """The rule refuses ``obj``."""
        return not await self.rule.has_object_permission(request, user, obj)


class AllowAny(BasePermission):
    """Anyone, logged in or not."""


class AllowScoped(BasePermission):
    """Principals who proved who they are, credentials limited to scopes too.

    For a route whose work no scope names and none needs to: logging out,
    reading who one is. It says so on purpose, as ``Public`` does for
    anonymous callers (``SCOPED_CREDENTIALS``).
    """

    bounds_scopes: ClassVar[bool] = True

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The request named a principal."""
        return user.is_authenticated


class IsAuthenticated(BasePermission):
    """Principals who proved who they are (users who logged in)."""

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The request named a principal."""
        return user.is_authenticated


class IsAuthenticatedOrReadOnly(BasePermission):
    """Anyone may read (``GET``, ``HEAD``, ``OPTIONS``); users may write."""

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """A safe method, or a principal who proved who they are."""
        return _method(request) in SAFE_METHODS or user.is_authenticated


class IsUnscoped(BasePermission):
    """Users whose credentials are not limited to scopes (a login, not a key).

    For what scopes cannot bound, such as managing users or tokens.
    """

    message: ClassVar[str] = "Credentials limited to scopes cannot do this."
    bounds_scopes: ClassVar[bool] = True

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """A user, with credentials that carry no scopes."""
        return user.is_authenticated and scopes_of(request) is None


class HasScope(BasePermission):
    """Users whose credentials allow every scope named; unscoped ones allow all."""

    bounds_scopes: ClassVar[bool] = True

    def __init__(self, *scopes: str) -> None:
        """Require ``scopes`` (permission keys)."""
        if not scopes:
            raise ImproperlyConfigured("HasScope needs at least one scope.")
        self.scopes = scopes

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """A user whose credentials cover the scopes."""
        return user.is_authenticated and _within(request, self.scopes)


class HasPermissions(BasePermission):
    """Principals granted every permission named (``<domain>.<codename>``)."""

    bounds_scopes: ClassVar[bool] = True

    def __init__(self, *perms: str) -> None:
        """Require ``perms``."""
        if not perms:
            raise ImproperlyConfigured("HasPermissions needs at least one permission.")
        self.perms = perms

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The principal holds every permission, and the scopes allow them."""
        return _within(request, self.perms) and await user.ahas_perms(self.perms)


class HasObjectPermissions(HasPermissions):
    """Principals granted every permission named on the object itself.

    With ``domainkit.contrib.auth``, object permissions come from backends
    other than ``ModelBackend`` (``AUTH.BACKENDS``). The request itself
    needs an active principal who proved who they are; check each object
    with ``check_object_permissions``.
    """

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """An active principal who proved who they are; the object decides the rest."""
        return user.is_authenticated and user.is_active

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """The principal holds every permission on ``obj``, within the scopes."""
        return _within(request, self.perms) and await user.ahas_perms(self.perms, obj)


class ModelPermissions(BasePermission):
    """Django's model permissions, by method, for one model.

    ``GET`` and ``HEAD`` need ``view``, ``POST`` ``add``, ``PUT`` and
    ``PATCH`` ``change``, and ``DELETE`` ``delete``; ``OPTIONS`` needs only
    a principal who proved who they are. Other methods are refused.
    ``model`` is a mapped class or its key (``"billing.invoice"``).
    """

    actions: ClassVar[dict[str, str]] = MODEL_ACTIONS
    bounds_scopes: ClassVar[bool] = True

    def __init__(self, model: type | str) -> None:
        """Check permissions of ``model``."""
        self.model = model

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The principal may take the method's action on the model."""
        if not user.is_authenticated:
            return False
        method = _method(request)
        if method == "OPTIONS":
            return True
        action = self.actions.get(method)
        if action is None:
            return False
        domain, model = self._model_key()
        perm = f"{domain}.{action}_{model}"
        return _within(request, [perm]) and await user.ahas_perms([perm])

    def _model_key(self) -> tuple[str, str]:
        """``(domain, model)`` of the model."""
        if isinstance(self.model, str):
            domain, _, model = self.model.partition(".")
            return domain, model
        return model_key(self.model)


class Require:
    """A FastAPI dependency: the rules must allow the request; returns the principal.

    Without rules, it requires a principal who proved who they are. On a
    WebSocket or an event stream, the rules are checked again while it
    stays open (``LIVE_REAUTH_INTERVAL``), with the principal loaded afresh.
    """

    def __init__(self, *rules: Rule) -> None:
        """Check ``rules`` (default: ``IsAuthenticated``) on every request."""
        self.rules = tuple(_rule(rule) for rule in rules) or (IsAuthenticated(),)

    async def __call__(
        self,
        request: HTTPConnection,
        _: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        """The principal, if every rule allows the request.

        Raises:
            NotAuthenticated: An anonymous caller was refused.
            PermissionDenied: A principal who proved who they are was refused.
        """
        user = current_user(request)
        refusal = await _first_refusal(self.rules, request, user)
        if refusal is not None:
            raise await _denied(request, user, refusal)
        if _scoped(request.scope):
            await check_scopes_bounded(request)
        record_check(request, self.allows)
        return user

    async def allows(self, connection: HTTPConnection) -> bool:
        """Whether every rule still allows ``connection``'s current principal."""
        user = current_user(connection)
        return await _first_refusal(
            self.rules, connection, user
        ) is None and not _scopes_refused(connection)


async def check_object_permissions(
    request: HTTPConnection, obj: object, *rules: Rule
) -> None:
    """Refuse the request unless every rule allows it on ``obj``.

    Raises:
        NotAuthenticated: An anonymous caller was refused.
        PermissionDenied: A principal who proved who they are was refused.
    """
    user = current_user(request)
    refusal = await _first_refusal(
        tuple(_rule(rule) for rule in rules), request, user, obj
    )
    if refusal is not None:
        raise await _denied(request, user, refusal)


async def check_scopes_bounded(connection: HTTPConnection) -> None:
    """Hold credentials limited to scopes to routes with a rule that checks them.

    ``Require`` calls it once its rules allow; so does a provider's
    dependency that reads the principal (``reads_principal``), such as
    ``AuthenticatedUser``. Credentials without scopes, and routes that
    declare a rule that checks scopes (``bounds_scopes``: ``HasScope``, the
    permission rules, ``IsUnscoped``, ``AllowScoped``, on the route or its
    routers), pass. Others, with ``SCOPED_CREDENTIALS = "warn"`` (the
    default until 0.11), pass with a ``DomainkitDeprecationWarning`` and a
    security log line, once per route a while.

    Raises:
        PermissionDenied: With ``SCOPED_CREDENTIALS = "refuse"``, the route
            declares no rule that checks scopes.
    """
    if scopes_of(connection) is None or _route_checks_scopes(connection):
        return
    route = _route_name(connection)
    if settings.SCOPED_CREDENTIALS == "refuse":
        security_logger.info(
            "Refused credentials limited to scopes on %s, which checks none", route
        )
        user = current_user(connection)
        raise await _denied(connection, user, None)
    scoped_log.warning(
        "scoped_credentials",
        route,
        "Credentials limited to scopes called %s, which declares no rule that "
        'checks scopes: SCOPED_CREDENTIALS = "refuse" refuses them, as 0.11 '
        "will by default",
        route,
    )
    warnings.warn(
        f"Credentials limited to scopes called {route}, which declares no rule "
        "that checks scopes: they are refused there from domainkit 0.11 (removed "
        "in 0.11). Add HasScope(...), a permission rule, or AllowScoped() to "
        'the route, or set SCOPED_CREDENTIALS = "refuse" to refuse them now.',
        DomainkitDeprecationWarning,
        stacklevel=2,
    )


scoped_log: Final = ThrottledLog(security_logger)
"""Calls of routes that check no scope with scoped credentials: a line per route."""


def _scoped(scope: Mapping[str, Any]) -> bool:
    """Whether the request's credentials carry scopes, read without a call.

    ``Require`` asks it on every request; credentials without ``scopes``
    (a provider's own) carry none.
    """
    if "auth" not in scope or scope["auth"] is None:
        return False
    try:
        return scope["auth"].scopes is not None
    except AttributeError:
        return False


def _scopes_refused(connection: HTTPConnection) -> bool:
    """Whether ``check_scopes_bounded`` would refuse ``connection`` now."""
    return (
        settings.SCOPED_CREDENTIALS == "refuse"
        and scopes_of(connection) is not None
        and not _route_checks_scopes(connection)
    )


def _effective_route(connection: HTTPConnection) -> object:
    """The route FastAPI serves the request with, its routers' prefixes included.

    A flattened route (``domainkit.http.routing``) is ``scope["route"]``; a
    route of a router FastAPI keeps nested is served through its effective
    context, which holds the routers' dependencies and prefixes.
    """
    fastapi_scope = connection.scope.get("fastapi")
    effective = (
        fastapi_scope.get("effective_route_context")
        if isinstance(fastapi_scope, dict)
        else None
    )
    return effective if effective is not None else connection.scope.get("route")


def _route_checks_scopes(connection: HTTPConnection) -> bool:
    """Whether the request's route declares a rule that checks scopes.

    Through ``Require`` at any depth of its dependencies, on the route or
    its routers. A request with no route known declares none.
    """
    dependant = getattr(_effective_route(connection), "dependant", None)
    return isinstance(dependant, Dependant) and _checks_scopes(dependant)


def _checks_scopes(dependant: Dependant) -> bool:
    """Whether some ``Require`` that ``dependant`` uses checks scopes."""
    call = dependant.call
    if isinstance(call, Require) and AllOf(*call.rules).checks_scopes():
        return True
    return any(_checks_scopes(each) for each in dependant.dependencies)


def _route_name(connection: HTTPConnection) -> str:
    """``METHOD /template`` of the request's route: never its values."""
    path = getattr(_effective_route(connection), "path", None)
    return f"{_method(connection)} {path if isinstance(path, str) else 'unmatched'}"


@runtime_checkable
class DenialRecorder(Protocol):
    """An installed domain that keeps the record of refused requests.

    ``domainkit.contrib.audit`` is one: it writes ``auth.access`` with the
    outcome ``denied`` and the actor. The details are the route's method
    and template, and the refusing rule's class: never the request's values
    or credentials.
    """

    async def record_denial(self, details: Mapping[str, object]) -> None:
        """Record that a principal who proved who they are was refused."""
        ...


class _Sampler:
    """Remembers when each key was last let through, to let it once per interval.

    Bounded: past ``keys`` entries the oldest is forgotten. Event loops on
    several threads (free-threaded Python) share one, so its state changes
    only under its lock.
    """

    def __init__(self, interval: float, keys: int) -> None:
        """Let each key through once per ``interval`` seconds; remember ``keys``."""
        self.interval = interval
        self.keys = keys
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def first(self, key: tuple[str, str]) -> bool:
        """Whether ``key`` is let through now (and so not again for a while)."""
        now = clock.monotonic()
        with self._lock:
            since = self._seen.get(key)
            if since is not None and now - since < self.interval:
                return False
            self._seen[key] = now
            self._seen.move_to_end(key)
            if len(self._seen) > self.keys:
                self._seen.popitem(last=False)
            return True

    def clear(self) -> None:
        """Forget every key."""
        with self._lock:
            self._seen.clear()


denials: Final = _Sampler(DENIAL_AUDIT_INTERVAL, MAX_DENIAL_KEYS)
"""Refusals recorded, per actor and route."""


async def record_denial(
    connection: HTTPConnection, user: Principal, rule: BasePermission | None
) -> None:
    """Send a 403 to the audit trail, once per actor and route a while.

    Every installed ``DenialRecorder`` records it. A recorder that fails is
    logged, never raised: the refusal stands either way.
    """
    recorders = [config for config in registry if isinstance(config, DenialRecorder)]
    route = _route_name(connection)
    if not recorders or not denials.first((str(user.id), route)):
        return
    details = {
        "route": route,
        "rule": "scopes_unbounded" if rule is None else type(rule).__name__,
    }
    for recorder in recorders:
        try:
            await recorder.record_denial(details)
        except Exception as exc:  # noqa: BLE001 - logged; the refusal stands.
            logger.error(  # Not the error itself: it could quote the details.
                "Could not record a refused request to %s (%s)",
                route,
                type(exc).__name__,
            )


def reads_principal[F: Callable[..., object]](dependency: F) -> F:
    """Mark ``dependency`` as one that reads the principal, as ``CurrentUser`` does.

    A route that depends on it declares who may call it, as one with
    ``Require`` does, so ``open_routes`` leaves it out; a provider marks the
    dependencies it offers (``AuthenticatedUser``)::

        @reads_principal
        async def tenant_user(request: HTTPConnection) -> TenantUser: ...
    """
    setattr(dependency, READS_PRINCIPAL, True)
    return dependency


@reads_principal
def current_user(connection: HTTPConnection) -> Principal:
    """The principal the authentication middleware found (anonymous if none).

    Call it from code that has the request. As a dependency, use
    ``CurrentUser`` (``acurrent_user``): FastAPI awaits that on the event
    loop, and would call this one on its thread pool.

    Raises:
        ImproperlyConfigured: No middleware put a principal in ``scope["user"]``.
    """
    if "user" not in connection.scope:
        raise ImproperlyConfigured(
            "Add an authentication middleware to MIDDLEWARE to know who sends "
            "requests: domainkit.contrib.auth.middleware.AuthenticationMiddleware, "
            'or one of your own that sets scope["user"].'
        )
    user: Principal = connection.scope["user"]
    return user


@reads_principal
async def acurrent_user(connection: HTTPConnection) -> Principal:
    """``current_user`` as a dependency FastAPI awaits, with no thread to hop to."""
    return current_user(connection)


CurrentUser = Annotated[Principal, Depends(acurrent_user)]
"""The request's principal, or an anonymous one."""


async def public() -> None:
    """Mark a route public on purpose: anyone may call it, without a token."""


Public = Depends(public)
"""``dependencies=[Public]``: the route needs no rule (``REQUIRE_ROUTE_RULES``)."""


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

    A route declares one with ``Require``, ``CurrentUser``, a dependency
    marked ``reads_principal`` (``AuthenticatedUser``), or ``Public``, on
    itself or its router. The framework's own routes (``/health``,
    ``/ready``, ``/metrics``, and the ``API_DOCS``) guard themselves.
    Mounted FastAPI and Starlette applications are walked route by route.
    What takes no dependencies (a mounted ``StaticFiles``, Starlette's own
    ``Route``) is listed unless marked with ``mark_public``.
    """
    return [name for name, _ in unguarded_routes(app)]


def unguarded_routes(app: FastAPI) -> list[tuple[str, bool]]:
    """``open_routes``, each with whether it could take a rule (a dependency)."""
    return [
        (name, dependant is not None)
        for name, dependant in served_routes(app.routes)
        if dependant is None or not _uses(dependant, or_public=True)
    ]


def routes_need_users(app: FastAPI) -> bool:
    """Whether some route of ``app`` reads the principal or checks rules."""
    return any(
        dependant is not None and _uses(dependant, or_public=False)
        for _, dependant in served_routes(app.routes)
    )


def route_rule_problems(app: FastAPI) -> list[str]:
    """What ``REQUIRE_ROUTE_RULES`` refuses: each route that declares no rule."""
    return [
        f"{route} declares no access rule: add Depends(Require(...)), or "
        "dependencies=[Public] if anyone may call it."
        if takes_rules
        else f"{route} takes no access rule: serve it from a FastAPI route "
        "with Require(...), or wrap it in mark_public(...) if anyone may "
        "call it."
        for route, takes_rules in unguarded_routes(app)
    ]


def served_routes(
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
        # The context answers for the route as served: an included router's
        # carries its full path, dependencies, and (a Host's) prefixed routes.
        found: tuple[str, Dependant | None]
        if isinstance(route, APIWebSocketRoute):
            found = (f"WEBSOCKET {prefix}{context.path}", context.dependant)
        elif isinstance(route, APIRoute):
            methods = " ".join(sorted(route.methods or ()))
            found = (f"{methods} {prefix}{context.path}", context.dependant)
        elif _marked_public(route):
            continue
        elif isinstance(route, Mount | Host) and context.routes:
            inner = prefix + str(context.path) if isinstance(route, Mount) else prefix
            yield from served_routes(context.routes, inner)
            continue
        else:
            path = context.path or ""
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


def _uses(dependant: Dependant, *, or_public: bool) -> bool:
    """Whether ``dependant`` depends, at any depth, on ``Require`` or a reader.

    A reader is a dependency marked ``reads_principal``; with ``or_public``,
    ``Public`` counts too.
    """
    call = dependant.call
    if (
        isinstance(call, Require)
        or getattr(call, READS_PRINCIPAL, False) is True
        or (or_public and call is public)
    ):
        return True
    return any(_uses(each, or_public=or_public) for each in dependant.dependencies)


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
    user: Principal,
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


async def _denied(
    connection: HTTPConnection, user: Principal, rule: BasePermission | None
) -> Exception:
    """401 for an anonymous caller, 403 with the rule's message for a known one.

    The 401's challenge is the principal's own (``challenge()``), if it
    names one. A 403 is recorded (``record_denial``); ``rule`` None is a
    refusal of scopes on a route that checks none (``SCOPES_UNBOUNDED``).
    """
    if not user.is_authenticated:
        challenge = getattr(user, "challenge", None)
        return NotAuthenticated(
            challenge() if challenge is not None else DEFAULT_CHALLENGE
        )
    await record_denial(connection, user, rule)
    return PermissionDenied(SCOPES_UNBOUNDED if rule is None else rule.message)
