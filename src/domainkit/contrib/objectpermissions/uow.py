"""Where grants are stored: a repository, its unit of work, and one in memory."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from domainkit.db import SqlAlchemyRepository, SqlAlchemyUnitOfWork
from domainkit.ddd import InMemoryRepository, Repository
from domainkit.ddd.query import Criteria, F

from .models import ObjectId, ObjectPermission, ObjectPermissionId, object_column


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


class ObjectPermissionRepository(Repository[ObjectPermission, ObjectPermissionId]):
    """Grants: find them with ``about(...)``."""

    model = ObjectPermission


class SqlAlchemyObjectPermissionRepository(
    SqlAlchemyRepository[ObjectPermission, ObjectPermissionId],
    ObjectPermissionRepository,
):
    """Grants in ``domainkit_object_permissions``."""

    model = ObjectPermission


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
        """Whether a grant gives ``permission`` on the object to the user or a group."""
        return any(
            grant.content_type == content_type
            and grant.permission == permission
            and grant.object_id == object_id
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
