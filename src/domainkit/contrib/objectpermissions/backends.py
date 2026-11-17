"""``ObjectPermissionBackend``: what grants allow on one object."""

from domainkit.clock import now
from domainkit.contrib.auth.backends import BaseBackend, ObjectGrant, memoised
from domainkit.contrib.auth.models import AbstractBaseUser, AnyUser, Group
from domainkit.db.routing import router
from domainkit.ddd.query import Criteria, F

from .services import describe, unit_of_work_for
from .uow import ObjectPermissionsUnitOfWork, about, in_force


class ObjectPermissionBackend(BaseBackend):
    """Permissions granted on an object to a user or to one of their groups.

    Add it to ``AUTH.BACKENDS`` after ``ModelBackend``. It answers only
    about objects (``has_perm(user, perm, obj)``), and only for active
    users; objects of unmapped classes, or with ids of other types, have
    no grants. It also lists a user's or a group's grants
    (``object_grants``), for the admin API's bounds on staff. Expired
    grants count for neither.

    Within a request, one query answers every permission of a user on one
    object (``memoised``), as django-guardian's ``ObjectPermissionChecker``
    does for its lifetime.
    """

    async def get_all_permissions(
        self, user: AnyUser, obj: object | None = None
    ) -> frozenset[str]:
        """Keys of the permissions granted on ``obj`` to the user or their groups."""
        if obj is None or not (user.is_authenticated and user.is_active):
            return frozenset()
        try:
            content_type, object_id = describe(obj)
        except (LookupError, TypeError):
            return frozenset()
        groups = frozenset(group.id for group in getattr(user, "groups", ()))

        async def granted() -> frozenset[str]:
            async with unit_of_work_for(content_type) as uow:
                found = await uow.grants.filter(
                    about(content_type, object_id, user_id=user.id, group_ids=groups)
                    & in_force(now())
                )
            return frozenset(grant.permission for grant in found)

        key = (ObjectPermissionBackend, user.id, groups, content_type, object_id)
        return await memoised(key, granted)

    async def object_grants(
        self, holder: AbstractBaseUser | Group
    ) -> frozenset[ObjectGrant]:
        """The grants in force to ``holder``, and to a user's groups, in every database.

        Grants live beside their objects, so each database hosting a domain
        is read. A user's grants count whether the user is active or not.
        """
        whom: Criteria
        if isinstance(holder, Group):
            whom = F("group_id") == holder.id
        else:
            groups = sorted(group.id for group in holder.groups)
            whom = F("user_id") == holder.id
            if groups:
                whom |= F("group_id").in_(groups)
        found: set[ObjectGrant] = set()
        current = whom & in_force(now())
        for database in router.hosting_targets():
            async with ObjectPermissionsUnitOfWork(database=database) as uow:
                for grant in await uow.grants.filter(current):
                    found.add(
                        ObjectGrant(
                            grant.permission,
                            grant.content_type,
                            grant.object_id,
                            grant.expires_at,
                        )
                    )
        return frozenset(found)
