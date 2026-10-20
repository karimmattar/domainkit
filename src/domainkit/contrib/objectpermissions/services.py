"""Granting and revoking permissions on objects, within the granter's own.

Staff hand out only what they hold: granting or revoking ``permission`` on
an object needs that permission (on the model, or on the object) and the
model permission to manage grants (``objectpermissions.add_objectpermission``
to grant, ``delete_`` to revoke); superusers have both. No one grants
anything to themselves. Each grant and revocation is an event, so the
audit trail shows who gave what to whom.
"""

from collections.abc import Sequence
from uuid import UUID

from domainkit.contrib.auth.backends import has_perm
from domainkit.contrib.auth.models import AnyUser
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.db.routing import router
from domainkit.ddd import BusinessRuleViolation, PermissionDenied

from .models import ObjectId, ObjectPermission, object_column
from .uow import ObjectPermissionsUnitOfWork, about


class InvalidGrant(BusinessRuleViolation):
    """A grant names a permission of another model, or no one or two at once."""


def describe(obj: object) -> tuple[str, ObjectId]:
    """``(content type, id)`` of a stored object: ``("catalog.product", id)``.

    Raises:
        LookupError: Its class is not mapped in an installed domain.
        TypeError: Its id is not a UUID, an integer, or text.
    """
    from domainkit.contrib.contenttypes import content_type_key

    content_type = ".".join(content_type_key(type(obj)))
    object_id = getattr(obj, "id", None)
    object_column(type(object_id))
    return content_type, object_id  # type: ignore[return-value]


def unit_of_work_for(content_type: str) -> ObjectPermissionsUnitOfWork:
    """A unit of work on the database that holds objects of ``content_type``."""
    return ObjectPermissionsUnitOfWork(
        database=router.target_for(content_type.partition(".")[0])
    )


async def grant_permission(
    actor: AnyUser,
    permission: str,
    obj: object,
    *,
    user: UUID | None = None,
    group: UUID | None = None,
    uow: ObjectPermissionsUnitOfWork | None = None,
) -> ObjectPermission:
    """Let ``user`` (or the members of ``group``) have ``permission`` on ``obj``.

    Granting what is granted already changes nothing and returns that grant.

    Raises:
        PermissionDenied: The actor may not manage grants, lacks the
            permission, or names themselves.
        InvalidGrant: Not exactly one of ``user`` and ``group``, or the
            permission is not one of ``obj``'s model.
        UnknownPermission: No permission has that key.
    """
    content_type, object_id = describe(obj)
    user_id, group_ids = _whom(user, group)
    await _check_ceiling(actor, permission, obj, "add")
    if user is not None and user == actor.id:
        raise PermissionDenied("You cannot grant permissions to yourself.")
    await _check_permission(permission, content_type)
    async with uow or unit_of_work_for(content_type) as work:
        existing = await work.grants.first(
            about(
                content_type,
                object_id,
                permission=permission,
                user_id=user_id,
                group_ids=group_ids,
            )
        )
        if existing is not None:
            return existing
        grant = ObjectPermission.create(
            content_type=content_type,
            permission=permission,
            object_id=object_id,
            user_id=user,
            group_id=group,
            by=actor.id,
        )
        await work.grants.add(grant)
        await work.commit()
    return grant


async def revoke_permission(
    actor: AnyUser,
    permission: str,
    obj: object,
    *,
    user: UUID | None = None,
    group: UUID | None = None,
    uow: ObjectPermissionsUnitOfWork | None = None,
) -> int:
    """Take back ``permission`` on ``obj`` from ``user`` or ``group``; how many.

    Raises:
        PermissionDenied: The actor may not manage grants, or lacks the
            permission.
        InvalidGrant: Not exactly one of ``user`` and ``group``.
    """
    content_type, object_id = describe(obj)
    user_id, group_ids = _whom(user, group)
    await _check_ceiling(actor, permission, obj, "delete")
    async with uow or unit_of_work_for(content_type) as work:
        found = await work.grants.filter(
            about(
                content_type,
                object_id,
                permission=permission,
                user_id=user_id,
                group_ids=group_ids,
            )
        )
        for grant in found:
            grant.revoke(by=actor.id)
            await work.grants.remove(grant)
        await work.commit()
    return len(found)


async def grants_on(
    obj: object, *, uow: ObjectPermissionsUnitOfWork | None = None
) -> list[ObjectPermission]:
    """Every grant on ``obj``; who may see them is the caller's to check."""
    content_type, object_id = describe(obj)
    async with uow or unit_of_work_for(content_type) as work:
        return await work.grants.filter(about(content_type, object_id))


def _whom(user: UUID | None, group: UUID | None) -> tuple[UUID | None, Sequence[UUID]]:
    """``about``'s user and groups, for one user or one group.

    Raises:
        InvalidGrant: Not exactly one of them.
    """
    if (user is None) == (group is None):
        raise InvalidGrant("Name a user or a group, not both or neither.")
    return user, () if group is None else (group,)


async def _check_ceiling(
    actor: AnyUser, permission: str, obj: object, manage: str
) -> None:
    """Refuse an actor who may not manage grants, or lacks ``permission``.

    Raises:
        PermissionDenied: They may not.
    """
    if not await has_perm(actor, f"objectpermissions.{manage}_objectpermission"):
        raise PermissionDenied("You may not manage permissions on objects.")
    if not (
        await has_perm(actor, permission) or await has_perm(actor, permission, obj)
    ):
        raise PermissionDenied(
            "You may grant or revoke only permissions you hold on the object."
        )


async def _check_permission(permission: str, content_type: str) -> None:
    """Refuse a permission of another model.

    Raises:
        UnknownPermission: No permission has that key.
        InvalidGrant: It is another model's.
    """
    async with AuthUnitOfWork() as auth:
        (found,) = await auth.permissions.get_many([permission])
    if str(found.content_type) != content_type:
        raise InvalidGrant("That permission is not one of the object's model.")
