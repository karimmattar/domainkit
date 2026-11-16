"""Multi-tenancy: a tenant per request; its rows, caches, limits, and database.

Turn it on with ``TENANCY`` and the middleware, after authentication::

    TENANCY = {"SUBDOMAIN_OF": "example.com", "MEMBERSHIP": "accounts.tenancy.member"}
    MIDDLEWARE = [..., "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
                  "domainkit.tenancy.TenantMiddleware"]

``TenantMiddleware`` works out each request's tenant (``RESOLVER``, else
the subdomain of ``SUBDOMAIN_OF``, else the ``HEADER``), checks that the
caller belongs to it (``MEMBERSHIP``), and serves the request as that
tenant. A route may instead take its tenant from its path, once routed:
``PathTenant("org_id")`` serves ``/orgs/{org_id}/...`` as that tenant,
and answers ``404`` to a caller who does not belong to it (ADR 0150).
Code reads it with ``current_tenant()``, and work done outside a
request names one with ``tenant(...)``; tasks queued by a request run as
its tenant. While a tenant is current:

- **rows**: repositories of a ``TenantOwned`` aggregate read and change
  only its rows, set it on the ones they add, and refuse another's; with
  no tenant they refuse to read at all (``TenantRequired``), unless
  ``all_tenants()`` says so on purpose;
- **databases**: a unit of work on a shard group picks the tenant's
  database (``PINNED`` in ``SHARDS`` gives a tenant its own), and one on
  a database of ``SCHEMA_DATABASES`` (PostgreSQL) works in the tenant's
  schema (``SCHEMA``, ``tenant_{tenant}``);
- **caches and limits**: cache keys and rate-limit counts are the
  tenant's own;
- **logs**: every record names it (``tenant``);
- **permissions**: with ``MEMBER_PERMISSIONS``, the keys a member holds
  in it (``member_permissions``), which ``domainkit.contrib.auth``'s
  ``MembershipBackend`` grants, and which ``HasMemberPermissions`` counts
  alone, without the grants a user holds everywhere (ADR 0170).
"""

import functools
import inspect
import keyword
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Final
from uuid import UUID

from fastapi import FastAPI, Path
from fastapi.dependencies.models import Dependant
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from domainkit.checks import CheckMessage
from domainkit.conf.global_settings import TenancyConfig
from domainkit.ddd.tenancy import (
    TENANT_ID,
    TenantMismatch,
    TenantOwned,
    TenantRequired,
    all_tenants,
    check_tenant_id,
    current_tenant,
    row_tenant,
    scoped_key,
    tenant,
)
from domainkit.exceptions import ImproperlyConfigured
from domainkit.http.access import (
    DEFAULT_CHALLENGE,
    BasePermission,
    NotAuthenticated,
    Principal,
    checks_callers,
    scopes_of,
    served_routes,
)
from domainkit.http.errors import error_response
from domainkit.http.live import record_check
from domainkit.utils.module_loading import import_string

__all__ = [
    "HasMemberPermissions",
    "InTenant",
    "PathTenant",
    "TenantMiddleware",
    "TenantMismatch",
    "TenantOwned",
    "TenantRequired",
    "all_tenants",
    "current_tenant",
    "member_permissions",
    "row_tenant",
    "scoped_key",
    "tenant",
    "tenant_schema",
    "tenants_beyond",
]

CONNECTION: Final = "domainkit_connection"
"""The name ``PathTenant`` takes the connection by, kept apart from path parameters."""
ID_TYPES: Final = (str, int, UUID)
"""The types a ``PathTenant`` reads its parameter as."""


def tenant_schema(tenant_id: str) -> str:
    """The PostgreSQL schema of ``tenant_id`` (``TENANCY.SCHEMA``)."""
    from domainkit.conf import settings

    config = settings.TENANCY
    template = "tenant_{tenant}" if config is None else config.SCHEMA
    return template.format(tenant=check_tenant_id(tenant_id).lower().replace("-", "_"))


type Resolver = Callable[[HTTPConnection], Awaitable[str | None] | str | None]
type Membership = Callable[[Any, str], Awaitable[bool] | bool]
type MemberPermissions = Callable[
    [Any, str], Awaitable[Iterable[str] | None] | Iterable[str] | None
]
type MemberTenants = Callable[[Any], Awaitable[Iterable[str]] | Iterable[str]]


@dataclass(frozen=True, slots=True)
class _Member:
    """Whom a request was served as a member of a tenant, holding what."""

    user: object
    tenant_id: str
    permissions: frozenset[str]


_member: ContextVar[_Member | None] = ContextVar("tenant_member", default=None)


class _Members:
    """Who belongs to a tenant, holding what: ``TENANCY``'s hooks, imported once."""

    def __init__(self, config: TenancyConfig) -> None:
        """Import ``MEMBER_PERMISSIONS`` or ``MEMBERSHIP``, and ``MEMBER_TENANTS``."""
        self.permissions: MemberPermissions | None = (
            None
            if config.MEMBER_PERMISSIONS is None
            else import_string(config.MEMBER_PERMISSIONS)
        )
        self.membership: Membership | None = (
            None if config.MEMBERSHIP is None else import_string(config.MEMBERSHIP)
        )
        self.tenants: MemberTenants | None = (
            None
            if config.MEMBER_TENANTS is None
            else import_string(config.MEMBER_TENANTS)
        )

    @property
    def checked(self) -> bool:
        """Whether a hook says who belongs (else everyone does)."""
        return self.permissions is not None or self.membership is not None

    async def admit(
        self, user: object, tenant_id: str, *, fresh: bool = False
    ) -> frozenset[str] | None:
        """The keys ``user`` holds as a member of ``tenant_id``; None if not one.

        ``MEMBER_PERMISSIONS`` answers; ``MEMBERSHIP`` lets a member in with
        no keys; with neither, everyone is a member. The answer a request
        was served with is reused for its principal and tenant, unless
        ``fresh``.

        Raises:
            ImproperlyConfigured: ``MEMBER_PERMISSIONS`` returned something
                other than permission keys or None.
        """
        known = _member.get()
        if (
            not fresh
            and known is not None
            and known.user is user
            and known.tenant_id == tenant_id
        ):
            return known.permissions
        if self.permissions is not None:
            return _keys(await _ask(self.permissions, user, tenant_id))
        if self.membership is not None:
            return frozenset() if await _ask(self.membership, user, tenant_id) else None
        return frozenset()


@functools.cache
def _members(config: TenancyConfig) -> _Members:
    """``config``'s hooks, imported once per configuration."""
    return _Members(config)


async def _ask(
    hook: Callable[[Any, str], object], user: object, tenant_id: str
) -> object:
    """What ``hook`` answers for ``user`` and ``tenant_id``, awaited if need be."""
    answer = hook(user, tenant_id)
    return await answer if inspect.isawaitable(answer) else answer


async def _listed(hook: MemberTenants, user: object) -> frozenset[str]:
    """The tenants ``MEMBER_TENANTS`` (``hook``) says ``user`` belongs to.

    Raises:
        ImproperlyConfigured: It answered something other than tenant ids
            (one string, None, a bool).
    """
    answer = hook(user)
    listed = await answer if inspect.isawaitable(answer) else answer
    if isinstance(listed, str | bytes) or not isinstance(listed, Iterable):
        raise ImproperlyConfigured(
            "TENANCY.MEMBER_TENANTS returns the ids of the tenants a user "
            f"belongs to; it returned a {type(listed).__name__}."
        )
    return frozenset(listed)


def _keys(answer: object) -> frozenset[str] | None:
    """``MEMBER_PERMISSIONS``'s ``answer`` as keys; None for one who is not a member.

    Raises:
        ImproperlyConfigured: It is not keys nor None (one string, a bool).
    """
    if answer is None:
        return None
    if isinstance(answer, str | bytes) or not isinstance(answer, Iterable):
        raise ImproperlyConfigured(
            "TENANCY.MEMBER_PERMISSIONS returns the permission keys a member "
            "holds, or None for one who is not a member; it returned a "
            f"{type(answer).__name__}."
        )
    return frozenset(answer)


@contextmanager
def _serving(member: _Member) -> Iterator[None]:
    """Serve ``member``'s tenant inside the block, ``member`` admitted to it."""
    token = _member.set(member)
    try:
        with tenant(member.tenant_id):
            yield
    finally:
        _member.reset(token)


async def member_permissions(user: object) -> frozenset[str]:
    """The permission keys ``user`` holds as a member of the current tenant.

    ``TENANCY.MEMBER_PERMISSIONS`` answers, once per request: the answer
    the request was admitted with is reused for its principal. Empty with
    no tenant current, without that setting, or for one who is not a
    member. ``domainkit.contrib.auth``'s ``MembershipBackend`` grants them.
    """
    from domainkit.conf import settings

    current = current_tenant()
    config = settings.TENANCY
    if current is None or config is None or config.MEMBER_PERMISSIONS is None:
        return frozenset()
    return await _members(config).admit(user, current) or frozenset()


async def tenants_beyond(
    actor: object, user: object, held: Iterable[str]
) -> frozenset[str] | None:
    """The tenants where ``user`` holds what ``actor`` does not; None if unknown.

    ``user`` holds more in a tenant they belong to (``MEMBER_TENANTS``)
    when ``actor`` does not belong to it, or holds keys there that are
    neither ``held`` (what ``actor`` holds everywhere) nor ``actor``'s own
    keys there. Changing a user hands over their memberships, so the admin
    API refuses staff such a user, as administrators may not reset the
    credentials of one who holds more than they do (ADR 0150). None when
    ``MEMBER_PERMISSIONS`` is set without ``MEMBER_TENANTS``: no one can be
    ruled out as a member. Empty without memberships to count.
    """
    from domainkit.conf import settings

    config = settings.TENANCY
    if config is None:
        return frozenset()
    admitting = _members(config)
    listing = admitting.tenants
    if listing is None:
        return None if admitting.permissions is not None else frozenset()
    theirs = await _listed(listing, user)
    if not theirs:
        return frozenset()
    mine = await _listed(listing, actor)
    beyond = set(theirs - mine)
    everywhere = frozenset(held)
    for tenant_id in sorted(theirs & mine):
        keys = await admitting.admit(user, tenant_id, fresh=True)
        own = await admitting.admit(actor, tenant_id, fresh=True)
        if keys is not None and (own is None or not keys <= everywhere | own):
            beyond.add(tenant_id)
    return frozenset(beyond)


class TenantMiddleware:
    """Serve each request as its tenant (see the module).

    A request that names no tenant, where ``REQUIRED`` and its path is not
    ``EXEMPT_PATHS``', gets ``400 tenant_not_named``; a malformed id,
    ``400 invalid_tenant``; a caller ``MEMBERSHIP`` (or
    ``MEMBER_PERMISSIONS``) refuses, ``403 not_a_member``. A WebSocket is
    closed (1008) instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``; read ``TENANCY`` once."""
        from domainkit.conf import settings

        self.app = app
        config = settings.TENANCY
        if config is None:
            raise ImproperlyConfigured("TenantMiddleware needs TENANCY settings.")
        self.config = config
        self.resolver: Resolver | None = (
            None if config.RESOLVER is None else import_string(config.RESOLVER)
        )
        self.members = _members(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Resolve the tenant; serve the request as it, or refuse."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        found = await self._resolve(connection)
        refusal = None
        held = None
        if found is None:
            if self.config.REQUIRED and not connection.url.path.startswith(
                self.config.EXEMPT_PATHS
            ):
                refusal = error_response(
                    400, "Say which tenant this is for.", code="tenant_not_named"
                )
        elif not TENANT_ID.fullmatch(found):
            refusal = error_response(
                400, "That is not a tenant.", code="invalid_tenant"
            )
        elif (held := await self.members.admit(scope.get("user"), found)) is None:
            refusal = error_response(
                403, "You do not belong to that tenant.", code="not_a_member"
            )
        if refusal is not None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await refusal(scope, receive, send)
            return
        if found is None or held is None:
            await self.app(scope, receive, send)
            return
        with _serving(_Member(scope.get("user"), found, held)):
            await self.app(scope, receive, send)

    async def _resolve(self, connection: HTTPConnection) -> str | None:
        """The tenant the request names: resolver, subdomain, or header."""
        config = self.config
        if self.resolver is not None:
            answer = self.resolver(connection)
            return await answer if inspect.isawaitable(answer) else answer
        if config.SUBDOMAIN_OF is not None:
            host = (connection.url.hostname or "").lower()
            suffix = f".{config.SUBDOMAIN_OF.lower()}"
            if host.endswith(suffix) and "." not in host[: -len(suffix)]:
                return host[: -len(suffix)] or None
            return None
        if config.HEADER is not None:
            return connection.headers.get(config.HEADER) or None
        return None


class PathTenant:
    """A FastAPI dependency: serve the route as the tenant its path names.

    The tenant is chosen once the route is matched, from the path
    parameter ``parameter``; put it on a router, before its other
    dependencies, so every route under the prefix is served as it::

        router = APIRouter(
            prefix="/orgs/{org_id}",
            dependencies=[Depends(PathTenant("org_id"))],
        )

    The route then runs as ``TenantMiddleware`` runs one: rows, caches,
    limits, logs, tasks, and ``member_permissions`` are the tenant's. The
    dependency returns the tenant id. ``TENANCY.MEMBERSHIP`` or
    ``MEMBER_PERMISSIONS`` must say who belongs, since a caller may type
    any id. A caller who does not belong gets ``404``, the answer of a
    route that does not exist, so a tenant's existence is not told; so
    does an id that is not a tenant id, and a path naming a tenant other
    than the one the request is already served as. An anonymous caller
    refused gets ``401``, as ``Require`` answers, whatever the id. While a
    WebSocket or an event stream stays open, membership is checked again
    with the route's rules (``LIVE_REAUTH_INTERVAL``).

    ``id_type`` is the parameter's type, as the route reads it: with
    ``UUID`` or ``int``, the tenant id is the value's own text
    (``str(UUID(...))``, lowercase), so one tenant has one id however the
    path writes it, and a value that is not one is refused with ``422``,
    as the route would refuse it. A ``ResourceRouter`` given ``tenant=``
    serves its routes with one of its id's type (ADR 0170).
    """

    def __init__(self, parameter: str, *, id_type: type = str) -> None:
        """Take the tenant from the path parameter ``parameter``, of ``id_type``.

        Raises:
            ImproperlyConfigured: ``parameter`` cannot name a path parameter,
                or ``id_type`` is not ``str``, ``int``, or ``UUID``.
        """
        if (
            not parameter.isidentifier()
            or keyword.iskeyword(parameter)
            or parameter == CONNECTION
        ):
            raise ImproperlyConfigured(
                f"PathTenant: {parameter!r} cannot name a path parameter; give "
                "the name the path has between braces, as in "
                "PathTenant('org_id') for '/orgs/{org_id}'."
            )
        if id_type not in ID_TYPES:
            raise ImproperlyConfigured(
                f"PathTenant: a tenant's id in the path is text, an int, or a "
                f"UUID, not {getattr(id_type, '__name__', id_type)}."
            )
        self.parameter = parameter
        self.id_type = id_type
        # FastAPI reads the parameters from this: the path parameter is
        # declared, documented, and passed by its own name.
        self.__signature__ = inspect.Signature(
            [
                inspect.Parameter(
                    CONNECTION,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=HTTPConnection,
                ),
                inspect.Parameter(
                    parameter,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=Annotated[id_type, Path(description="The tenant.")],
                ),
            ]
        )

    async def __call__(
        self, domainkit_connection: HTTPConnection, **path: object
    ) -> AsyncIterator[str]:
        """Serve the request as the path's tenant until it is answered.

        Raises:
            HTTPException: 404, the caller does not belong to the tenant.
            NotAuthenticated: An anonymous caller does not belong to it.
            ImproperlyConfigured: No ``TENANCY``, or it does not say who
                belongs to a tenant.
        """
        connection = domainkit_connection
        found = str(path[self.parameter])
        user = connection.scope.get("user")
        held = await self._admit(user, found)
        record_check(connection, functools.partial(self._still_member, found))
        with _serving(_Member(user, found, held)):
            yield found

    async def _admit(self, user: object, found: str) -> frozenset[str]:
        """What ``user`` holds in the tenant ``found``, or the refusal."""
        admitting = _path_members()
        served = current_tenant()
        if (served is not None and served != found) or not TENANT_ID.fullmatch(found):
            raise HTTPException(404)
        held = await admitting.admit(user, found)
        if held is not None:
            return held
        if not getattr(user, "is_authenticated", False):
            challenge = getattr(user, "challenge", None)
            raise NotAuthenticated(
                challenge() if challenge is not None else DEFAULT_CHALLENGE
            )
        raise HTTPException(404)

    async def _still_member(self, tenant_id: str, connection: HTTPConnection) -> bool:
        """Whether the connection's principal, as loaded now, still belongs."""
        user = connection.scope.get("user")
        return await _path_members().admit(user, tenant_id, fresh=True) is not None


def _path_members() -> _Members:
    """``TENANCY``'s hooks, which must check who belongs, for ``PathTenant``.

    Raises:
        ImproperlyConfigured: No ``TENANCY``, or neither ``MEMBERSHIP`` nor
            ``MEMBER_PERMISSIONS``.
    """
    from domainkit.conf import settings

    config = settings.TENANCY
    admitting = None if config is None else _members(config)
    if admitting is None or not admitting.checked:
        raise ImproperlyConfigured(
            "PathTenant needs TENANCY with MEMBERSHIP or MEMBER_PERMISSIONS: "
            "a path names whichever tenant the caller types."
        )
    return admitting


class InTenant(BasePermission):
    """Requests served as a tenant: ``TenantMiddleware``'s, or a ``PathTenant``'s.

    ``~InTenant()`` allows those served as none, such as a tenants'
    resource's collection, and the objects reached there.
    """

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """A tenant is being served."""
        return current_tenant() is not None

    async def has_object_permission(
        self, request: HTTPConnection, user: Principal, obj: object
    ) -> bool:
        """As for the request: so ``~InTenant()`` allows objects served as none."""
        return current_tenant() is not None


class HasMemberPermissions(BasePermission):
    """Members holding every permission named, in the tenant being served.

    It counts the keys ``TENANCY.MEMBER_PERMISSIONS`` gives the caller as a
    member of the tenant (``member_permissions``), and nothing else: not
    what a user holds everywhere (``ModelBackend``'s grants, a superuser's
    status), which ``HasPermissions`` counts too. So a member who is also
    staff acts in the tenant with their role alone (ADR 0170). With no
    tenant served it refuses. Credentials limited to scopes must name the
    keys too, as for ``HasPermissions``.
    """

    bounds_scopes: ClassVar[bool] = True

    def __init__(self, *perms: str) -> None:
        """Require ``perms`` among the member's keys."""
        if not perms:
            raise ImproperlyConfigured(
                "HasMemberPermissions needs at least one permission."
            )
        self.perms = frozenset(perms)

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """An active member holds every key in the tenant, within the scopes.

        Raises:
            ImproperlyConfigured: ``TENANCY.MEMBER_PERMISSIONS`` is not set,
                so no member holds any key.
        """
        from domainkit.conf import settings

        config = settings.TENANCY
        if config is None or config.MEMBER_PERMISSIONS is None:
            raise ImproperlyConfigured(
                "HasMemberPermissions counts the keys TENANCY.MEMBER_PERMISSIONS "
                "names: set it to the function that says what a member holds."
            )
        if not (user.is_authenticated and user.is_active):
            return False
        scopes = scopes_of(request)
        if scopes is not None and not self.perms <= scopes:
            return False
        return self.perms <= await member_permissions(user)


def path_tenant_problems(app: FastAPI) -> list[str]:
    """Routes whose ``PathTenant`` cannot serve them as it should.

    ``tenancy.E001``: it names a parameter the route's path does not have,
    so every request would be refused (422). ``tenancy.E002``: a rule (a
    dependency that checks callers, such as ``Require``) comes before it
    among the route's own dependencies, so the rule sees no tenant. A
    ``PathTenant`` inside another dependency is checked against the path
    only.
    """
    problems: list[str] = []
    for route, dependant in served_routes(app.routes):
        if dependant is None:
            continue
        named = {each.parameter for each in _path_tenants(dependant)}
        problems.extend(
            CheckMessage(
                "tenancy.E001",
                f"{route} takes its tenant from the path parameter "
                f"{parameter!r} (PathTenant), which its path does not have: "
                f"add {{{parameter}}} to the path, or name the parameter it has.",
            )
            for parameter in sorted(named)
            if not re.search(rf"\{{{parameter}(:[^}}]*)?\}}", route)
        )
        own = dependant.dependencies
        first = next(
            (n for n, each in enumerate(own) if isinstance(each.call, PathTenant)),
            None,
        )
        if first is not None and any(checks_callers(each) for each in own[:first]):
            problems.append(
                CheckMessage(
                    "tenancy.E002",
                    f"{route} checks who calls before PathTenant serves the "
                    "path's tenant: those rules see no tenant, so members are "
                    "refused, and strangers get 403 rather than 404. Put "
                    "Depends(PathTenant(...)) first.",
                )
            )
    return problems


def _path_tenants(dependant: Dependant) -> list[PathTenant]:
    """Every ``PathTenant`` among ``dependant``'s dependencies, at any depth."""
    found = [dependant.call] if isinstance(dependant.call, PathTenant) else []
    for each in dependant.dependencies:
        found.extend(_path_tenants(each))
    return found
