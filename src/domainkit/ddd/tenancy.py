"""The tenant being served, and the aggregates that belong to one.

The domain's half of multi-tenancy (``domainkit.tenancy`` has the rest):
``TenantOwned`` aggregates, and the current tenant that repositories
scope them to. See ``docs/tenancy.md``.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from domainkit.ddd._dataclass import EntityMeta
from domainkit.ddd.exceptions import PermissionDenied

__all__ = [
    "TenantMismatch",
    "TenantOwned",
    "TenantRequired",
    "all_tenants",
    "check_tenant_id",
    "current_tenant",
    "row_tenant",
    "scoped_key",
    "shared_keys",
    "tenant",
]

TENANT_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
"""What a tenant id may be: safe in a key, a log line, and a schema name."""
TENANT_ID_FIELD: Final = "tenant_id"

_current: ContextVar[str | None] = ContextVar("tenant", default=None)
_everyone: ContextVar[bool] = ContextVar("all_tenants", default=False)
_shared: ContextVar[bool] = ContextVar("shared_keys", default=False)


class TenantRequired(PermissionDenied):
    """Tenant data was asked for with no tenant current (403)."""

    code = "tenant_required"

    def __init__(self, message: str = "Say which tenant this is for.") -> None:
        """Say so."""
        super().__init__(message)


class TenantMismatch(PermissionDenied):
    """An aggregate of another tenant was handed to this tenant's repository (403)."""

    code = "tenant_mismatch"

    def __init__(self, message: str = "That belongs to another tenant.") -> None:
        """Say so."""
        super().__init__(message)


class TenantOwned(metaclass=EntityMeta):
    """Mixin for aggregates each of which belongs to one tenant.

    Put it before the aggregate base::

        class Invoice(TenantOwned, AggregateRoot[InvoiceId]): ...

    The table needs a ``tenant_id`` column (``String(63)``, not null,
    indexed, and first in the unique constraints a tenant's rows share).
    Repositories scope every read and bulk change to the current tenant,
    and set it on what they add.
    """

    tenant_id: str | None = None


def check_tenant_id(value: object) -> str:
    """``value`` as a tenant id.

    Raises:
        ValueError: It is not one: 1 to 63 letters, digits, ``_``, ``-``,
            starting with a letter or a digit.
    """
    text = str(value)
    if isinstance(value, bool) or not TENANT_ID.fullmatch(text):
        raise ValueError("a tenant id is 1 to 63 letters, digits, '_', '-'")
    return text


def current_tenant() -> str | None:
    """The tenant being served, if any."""
    return _current.get()


@contextmanager
def tenant(tenant_id: object) -> Iterator[str]:
    """Serve ``tenant_id`` inside the block (work outside a request, tests).

    Raises:
        ValueError: It is not a tenant id.
    """
    checked = check_tenant_id(tenant_id)
    token = _current.set(checked)
    try:
        yield checked
    finally:
        _current.reset(token)


@contextmanager
def all_tenants() -> Iterator[None]:
    """Read and change every tenant's rows inside the block, on purpose.

    For maintenance and reports across tenants; nothing else does it by
    default.
    """
    token = _everyone.set(True)
    try:
        yield
    finally:
        _everyone.reset(token)


@contextmanager
def shared_keys() -> Iterator[None]:
    """Cache and rate-limit keys inside the block are every tenant's.

    For what belongs to no tenant though counted inside one: a user's or an
    address's login attempts, what principals rest on. Unlike
    ``all_tenants``, rows stay scoped to the current tenant.
    """
    token = _shared.set(True)
    try:
        yield
    finally:
        _shared.reset(token)


def row_tenant() -> str | None:
    """The tenant ``TenantOwned`` rows are scoped to; None inside ``all_tenants``.

    Raises:
        TenantRequired: No tenant is current, and ``all_tenants`` is not on.
    """
    if _everyone.get():
        return None
    current = _current.get()
    if current is None:
        raise TenantRequired
    return current


def claim(aggregate: object) -> None:
    """Give a new ``TenantOwned`` aggregate the current tenant.

    Raises:
        TenantRequired: No tenant is current (and ``all_tenants`` is not on).
        TenantMismatch: It names another tenant.
    """
    if not isinstance(aggregate, TenantOwned):
        return
    owner = row_tenant()
    if owner is None:
        if aggregate.tenant_id is None:
            raise TenantRequired
        return
    if aggregate.tenant_id is None:
        aggregate.tenant_id = owner
    elif aggregate.tenant_id != owner:
        raise TenantMismatch


def visible(aggregate: object) -> bool:
    """Whether the current tenant may see ``aggregate``.

    Raises:
        TenantRequired: It is ``TenantOwned`` and no tenant is current.
    """
    if not isinstance(aggregate, TenantOwned):
        return True
    owner = row_tenant()
    return owner is None or aggregate.tenant_id == owner


def scoped_key(key: str) -> str:
    """``key``, the current tenant's own: caches and rate limits use it.

    As it is inside ``all_tenants`` and ``shared_keys``.
    """
    current = _current.get()
    if current is None or _everyone.get() or _shared.get():
        return key
    return f"t:{current}:{key}"
