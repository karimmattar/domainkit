"""An organisation's routes, by its id in the path, and its members' roles.

The project keeps memberships in a table of its own, a role on each;
``role_permissions`` is its ``TENANCY.MEMBER_PERMISSIONS``, and
``role_tenants`` its ``MEMBER_TENANTS`` (tenancy tests).
``everything`` mounts them with auth's API, personal tokens, and admin API.
"""

from typing import Any, Final

from fastapi import APIRouter, Depends
from sqlalchemy import Column, MetaData, String, Table, Uuid, select

from domainkit.contrib.auth.admin import router as admin_router
from domainkit.contrib.auth.api import router as auth_router
from domainkit.contrib.auth.personal_tokens import router as tokens_router
from domainkit.db import connections
from domainkit.http.access import HasPermissions, Require
from domainkit.tenancy import PathTenant, current_tenant

MEMBERS: Final = Table(
    "org_members",
    MetaData(),
    Column("user_id", Uuid, primary_key=True),
    Column("org", String(63), primary_key=True),
    Column("role", String(20), nullable=False),
)
"""Who belongs to which organisation, and as what."""
ROLES: Final = {
    "owner": frozenset({"shop.view_product", "shop.change_product"}),
    "viewer": frozenset({"shop.view_product"}),
}
"""The permission keys each role holds."""


async def role_permissions(user: Any, org: str) -> frozenset[str] | None:  # noqa: ANN401
    """The keys of ``user``'s role in ``org``; None if they are not a member."""
    if not user.is_authenticated:
        return None
    async with connections.engine().connect() as connection:
        role = await connection.scalar(
            select(MEMBERS.c.role).where(
                MEMBERS.c.user_id == user.id, MEMBERS.c.org == org
            )
        )
    return None if role is None else ROLES[role]


async def role_tenants(user: Any) -> list[str]:  # noqa: ANN401
    """The organisations ``user`` belongs to: ``TENANCY.MEMBER_TENANTS``."""
    async with connections.engine().connect() as connection:
        found = await connection.scalars(
            select(MEMBERS.c.org).where(MEMBERS.c.user_id == user.id)
        )
    return list(found)


router = APIRouter(prefix="/orgs/{org}", dependencies=[Depends(PathTenant("org"))])


@router.get(
    "/products", dependencies=[Depends(Require(HasPermissions("shop.view_product")))]
)
async def products() -> dict[str, Any]:
    """Read the organisation's products."""
    return {"tenant": current_tenant()}


@router.patch(
    "/products",
    dependencies=[Depends(Require(HasPermissions("shop.change_product")))],
)
async def change() -> dict[str, Any]:
    """Change the organisation's products."""
    return {"tenant": current_tenant(), "changed": True}


everything = APIRouter()
everything.include_router(router)
everything.include_router(auth_router, prefix="/auth")
everything.include_router(tokens_router, prefix="/auth")
everything.include_router(admin_router, prefix="/admin/auth")
