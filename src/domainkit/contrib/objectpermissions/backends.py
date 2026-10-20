"""``ObjectPermissionBackend``: what grants allow on one object."""

from domainkit.contrib.auth.backends import BaseBackend
from domainkit.contrib.auth.models import AnyUser

from .services import describe, unit_of_work_for
from .uow import about


class ObjectPermissionBackend(BaseBackend):
    """Permissions granted on an object to a user or to one of their groups.

    Add it to ``AUTH.BACKENDS`` after ``ModelBackend``. It answers only
    about objects (``has_perm(user, perm, obj)``), and only for active
    users; objects of unmapped classes, or with ids of other types, have
    no grants.
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
        groups = [group.id for group in getattr(user, "groups", ())]
        async with unit_of_work_for(content_type) as uow:
            found = await uow.grants.filter(
                about(content_type, object_id, user_id=user.id, group_ids=groups)
            )
        return frozenset(grant.permission for grant in found)
