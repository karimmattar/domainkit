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
  ``MembershipBackend`` grants.
"""

import functools
import inspect
import keyword
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import Path
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

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
from domainkit.http.access import DEFAULT_CHALLENGE, NotAuthenticated
from domainkit.http.errors import error_response
from domainkit.http.live import record_check
from domainkit.utils.module_loading import import_string

__all__ = [
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
]

CONNECTION: Final = "domainkit_connection"
"""The name ``PathTenant`` takes the connection by, kept apart from path parameters."""


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
        """Import ``MEMBER_PERMISSIONS`` or ``MEMBERSHIP``, if set."""
        self.permissions: MemberPermissions | None = (
            None
            if config.MEMBER_PERMISSIONS is None
            else import_string(config.MEMBER_PERMISSIONS)
        )
        self.membership: Membership | None = (
            None if config.MEMBERSHIP is None else import_string(config.MEMBERSHIP)
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
    """

    def __init__(self, parameter: str) -> None:
        """Take the tenant from the path parameter ``parameter``.

        Raises:
            ImproperlyConfigured: ``parameter`` cannot name a path parameter.
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
        self.parameter = parameter
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
                    annotation=Annotated[str, Path(description="The tenant.")],
                ),
            ]
        )

    async def __call__(
        self, domainkit_connection: HTTPConnection, **path: str
    ) -> AsyncIterator[str]:
        """Serve the request as the path's tenant until it is answered.

        Raises:
            HTTPException: 404, the caller does not belong to the tenant.
            NotAuthenticated: An anonymous caller does not belong to it.
            ImproperlyConfigured: No ``TENANCY``, or it does not say who
                belongs to a tenant.
        """
        connection = domainkit_connection
        found = path[self.parameter]
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
