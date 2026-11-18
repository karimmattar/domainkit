"""``ObjectPermissionBackend``: what grants allow on one object."""

from collections.abc import Mapping
from datetime import datetime
from typing import Final, cast

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
        return await self.object_grants_beyond(holder, frozenset())

    async def object_grants_beyond(
        self, holder: AbstractBaseUser | Group, permissions: frozenset[str]
    ) -> frozenset[ObjectGrant]:
        """``object_grants(holder)``, less the grants of ``permissions``, in the query.

        Only the columns a grant needs are read: nothing is loaded or
        tracked, so a holder of many grants costs the event loop less.
        """
        whom: Criteria
        if isinstance(holder, Group):
            whom = F("group_id") == holder.id
        else:
            groups = sorted(group.id for group in holder.groups)
            whom = F("user_id") == holder.id
            if groups:
                whom |= F("group_id").in_(groups)
        current = whom & in_force(now())
        if permissions:
            current &= ~F("permission").in_(sorted(permissions))
        found: list[ObjectGrant] = []
        for database in router.hosting_targets():
            async with ObjectPermissionsUnitOfWork(database=database) as uow:
                rows = await uow.grants.values(current, *_COLUMNS)
            found.extend(
                ObjectGrant(
                    cast("str", row["permission"]),
                    cast("str", row["content_type"]),
                    _object_id(row),
                    cast("datetime | None", row["expires_at"]),
                )
                for row in rows
            )
        return ObjectGrant.lasting(found)  # A grant held twice: its last end.


_COLUMNS: Final = (
    "permission",
    "content_type",
    "object_uuid",
    "object_int",
    "object_text",
    "expires_at",
)
"""What ``object_grants`` reads of each grant."""


def _object_id(row: Mapping[str, object]) -> object:
    """The object's id, whichever column holds it (``ObjectPermission.object_id``)."""
    if row["object_uuid"] is not None:
        return row["object_uuid"]
    if row["object_int"] is not None:
        return row["object_int"]
    return row["object_text"] or ""
