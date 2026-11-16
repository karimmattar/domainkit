"""Where grants are stored: a repository, its unit of work, and one in memory."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.clock import now
from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.ddd import InMemoryRepository, Repository
from domainkit.ddd.query import Criteria, F

from .models import ObjectId, ObjectPermission, ObjectPermissionId, object_column
from .tables import object_permissions


def about(
    content_type: str,
    object_id: ObjectId,
    *,
    permission: str | None = None,
    user_id: UUID | None = None,
    group_ids: Iterable[UUID] = (),
) -> Criteria:
    """Grants on one object; of one permission, for a user or their groups."""
    column = object_column(type(object_id))
    criteria = (F("content_type") == content_type) & (F(column) == object_id)
    if permission is not None:
        criteria &= F("permission") == permission
    groups = sorted(set(group_ids))
    whom = [
        *([F("user_id") == user_id] if user_id is not None else []),
        *([F("group_id").in_(groups)] if groups else []),
    ]
    if whom:
        either = whom[0] if len(whom) == 1 else whom[0] | whom[1]
        criteria &= either
    return criteria


def in_force(at: datetime) -> Criteria:
    """Grants that count at ``at``: without an expiry, or expiring later."""
    return F("expires_at").isnull() | (F("expires_at") > at)


def of_role(role: str | None) -> Criteria:
    """Grants that came with ``role``; with None, those granted alone."""
    return F("role").isnull() if role is None else F("role") == role


class ObjectPermissionRepository(Repository[ObjectPermission, ObjectPermissionId]):
    """Grants: find them with ``about(...)``."""

    model = ObjectPermission

    async def clear_expired(self, at: datetime | None = None) -> int:
        """Delete the grants that no longer count at ``at`` (default: now); how many."""
        moment = at or now()
        found = await self.filter(
            F("expires_at").isnull(value=False) & (F("expires_at") <= moment)
        )
        for grant in found:
            await self.remove(grant)
        return len(found)


class SqlAlchemyObjectPermissionRepository(
    SqlAlchemyRepository[ObjectPermission, ObjectPermissionId],
    ObjectPermissionRepository,
):
    """Grants in ``domainkit_object_permissions``."""

    model = ObjectPermission

    async def clear_expired(self, at: datetime | None = None) -> int:
        """Delete the grants that no longer count at ``at``, in one statement."""
        result = await self.session.execute(
            delete(object_permissions).where(
                object_permissions.c.expires_at <= (at or now())
            )
        )
        return cast(CursorResult[Any], result).rowcount


class InMemoryObjectPermissions(
    InMemoryRepository[ObjectPermission, ObjectPermissionId],
    ObjectPermissionRepository,
):
    """Grants in a dictionary, for tests; ``VisibleTo(grants=...)`` reads it."""

    def allows(
        self,
        permission: str,
        content_type: str,
        object_id: ObjectId,
        user_id: UUID | None,
        group_ids: frozenset[UUID],
    ) -> bool:
        """Whether a grant in force gives ``permission`` on the object to them."""
        moment = now()
        return any(
            grant.content_type == content_type
            and grant.permission == permission
            and grant.object_id == object_id
            and not grant.expired(moment)
            and (
                (user_id is not None and grant.user_id == user_id)
                or grant.group_id in group_ids
            )
            for grant in self.items.values()
        )


class ObjectPermissionsUnitOfWork(SqlAlchemyUnitOfWork):
    """One transaction on the grants of one database (the objects')."""

    grants: ObjectPermissionRepository

    def create_repositories(self, session: AsyncSession) -> None:
        """Bind the repository."""
        self.grants = SqlAlchemyObjectPermissionRepository(session)
