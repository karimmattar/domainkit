"""Multi-tenancy: a tenant per request; its rows, caches, limits, and database.

Turn it on with ``TENANCY`` and the middleware, after authentication::

    TENANCY = {"SUBDOMAIN_OF": "example.com", "MEMBERSHIP": "accounts.tenancy.member"}
    MIDDLEWARE = [..., "domainkit.contrib.auth.middleware.AuthenticationMiddleware",
                  "domainkit.tenancy.TenantMiddleware"]

``TenantMiddleware`` works out each request's tenant (``RESOLVER``, else
the subdomain of ``SUBDOMAIN_OF``, else the ``HEADER``), checks that the
caller belongs to it (``MEMBERSHIP``), and serves the request as that
tenant. Code reads it with ``current_tenant()``, and work done outside a
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
- **logs**: every record names it (``tenant``).
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

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
from domainkit.http.errors import error_response

__all__ = [
    "TenantMiddleware",
    "TenantMismatch",
    "TenantOwned",
    "TenantRequired",
    "all_tenants",
    "current_tenant",
    "row_tenant",
    "scoped_key",
    "tenant",
    "tenant_schema",
]


def tenant_schema(tenant_id: str) -> str:
    """The PostgreSQL schema of ``tenant_id`` (``TENANCY.SCHEMA``)."""
    from domainkit.conf import settings

    config = settings.TENANCY
    template = "tenant_{tenant}" if config is None else config.SCHEMA
    return template.format(tenant=check_tenant_id(tenant_id).lower().replace("-", "_"))


type Resolver = Callable[[HTTPConnection], Awaitable[str | None] | str | None]
type Membership = Callable[[Any, str], Awaitable[bool] | bool]


class TenantMiddleware:
    """Serve each request as its tenant (see the module).

    A request that names no tenant, where ``REQUIRED`` and its path is not
    ``EXEMPT_PATHS``', gets ``400 tenant_not_named``; a malformed id,
    ``400 invalid_tenant``; a caller ``MEMBERSHIP`` refuses,
    ``403 not_a_member``. A WebSocket is closed (1008) instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``; read ``TENANCY`` once."""
        from domainkit.conf import settings
        from domainkit.utils.module_loading import import_string

        self.app = app
        config = settings.TENANCY
        if config is None:
            from domainkit.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured("TenantMiddleware needs TENANCY settings.")
        self.config = config
        self.resolver: Resolver | None = (
            None if config.RESOLVER is None else import_string(config.RESOLVER)
        )
        self.membership: Membership | None = (
            None if config.MEMBERSHIP is None else import_string(config.MEMBERSHIP)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Resolve the tenant; serve the request as it, or refuse."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        found = await self._resolve(connection)
        refusal = None
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
        elif not await self._member(scope, found):
            refusal = error_response(
                403, "You do not belong to that tenant.", code="not_a_member"
            )
        if refusal is not None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await refusal(scope, receive, send)
            return
        if found is None:
            await self.app(scope, receive, send)
            return
        with tenant(found):
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

    async def _member(self, scope: Scope, tenant_id: str) -> bool:
        """Whether ``MEMBERSHIP`` lets the request's user in (yes without one)."""
        if self.membership is None:
            return True
        answer = self.membership(scope.get("user"), tenant_id)
        return bool(await answer if inspect.isawaitable(answer) else answer)
