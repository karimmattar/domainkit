"""An organisation's routes, by its id in the path, and its members' roles.

The project keeps memberships in a table of its own, a role on each;
``role_permissions`` is its ``TENANCY.MEMBER_PERMISSIONS`` (tenancy tests).
"""

from typing import Any, Final

from fastapi import APIRouter, Depends
from sqlalchemy import Column, MetaData, String, Table, Uuid, select

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
