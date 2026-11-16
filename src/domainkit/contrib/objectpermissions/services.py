"""Granting and revoking permissions on objects, within the granter's own.

Staff hand out only what they hold: granting or revoking ``permission`` on
an object needs that permission (on the model, or on the object) and the
model permission to manage grants (``objectpermissions.add_objectpermission``
to grant, ``delete_`` to revoke); superusers have both. No one grants
anything to themselves. Each grant and revocation is an event, so the
audit trail shows who gave what to whom.

A grant may expire (``expires_at``); a role (``ObjectRole``) grants several
permissions together; and a permission of another model may be granted on
objects of a model the project names with ``grantable_on``.
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from domainkit.apps.hooks import HookPoint
from domainkit.clock import now
from domainkit.contrib.auth.backends import has_perm
from domainkit.contrib.auth.models import AnyUser
from domainkit.contrib.auth.uow import AuthUnitOfWork
from domainkit.db.routing import router
from domainkit.ddd import BusinessRuleViolation, PermissionDenied

from .models import (
    PERMISSION_KEY,
    ObjectId,
    ObjectPermission,
    ObjectRole,
    object_column,
)
from .uow import ObjectPermissionsUnitOfWork, about, in_force, of_role

type Grantable = Callable[[str], Iterable[str]]
"""What ``on_grantable`` calls: a content type, to the permissions of other
models that may be granted on its objects."""

on_grantable: Final[HookPoint[Grantable]] = HookPoint(
    "objectpermissions.grantable", arguments=1, awaited=False
)
"""The permissions of other models grantable on a content type (``grantable_on``)."""


class InvalidGrant(BusinessRuleViolation):
    """A grant names a permission of another model, or no one or two at once."""

    code = "invalid_grant"


def describe(obj: object) -> tuple[str, ObjectId]:
    """``(content type, id)`` of a stored object: ``("catalog.product", id)``.

    Raises:
        LookupError: Its class is not mapped in an installed domain.
        TypeError: Its id is not a UUID, an integer, or text.
    """
    content_type = content_type_of(type(obj))
    object_id = getattr(obj, "id", None)
    object_column(type(object_id))
    return content_type, object_id  # type: ignore[return-value]


def content_type_of(model: type) -> str:
    """The content type of a mapped class: ``catalog.product``.

    Raises:
        LookupError: It is not mapped in an installed domain.
    """
    from domainkit.db.models import model_key as content_type_key

    return ".".join(content_type_key(model))


def grantable_on(model: type, *permissions: str, domain: str | None = None) -> None:
    """Let ``permissions`` of other models be granted on objects of ``model``.

    Call it in ``DomainConfig.ready()``::

        grantable_on(Company, "memberships.add_membership")

    Then ``grant_permission(actor, "memberships.add_membership", company,
    ...)`` is a grant, and ``visible_to(user, "memberships.add_membership",
    Company)`` the companies a user may add members to. Without it, such a
    grant is refused (``InvalidGrant``). ``domain`` registers for an
    installed domain from outside its ``ready()`` (a test).

    Raises:
        ValueError: No permission, or one that is not ``domain.codename``.
    """
    if not permissions:
        raise ValueError("Name the permissions grantable on the model.")
    for key in permissions:
        if not PERMISSION_KEY.fullmatch(key):
            raise ValueError(f"{key!r} is not a permission key (domain.codename).")
    keys = frozenset(permissions)

    def grantable(content_type: str) -> Iterable[str]:
        try:
            own = content_type_of(model)
        except LookupError:  # Not in an installed domain: it has no objects.
            return ()
        return keys if content_type == own else ()

    grantable.__qualname__ = f"grantable_on({model.__qualname__})"
    on_grantable(grantable, domain=domain)


def grantable_permissions(content_type: str) -> frozenset[str]:
    """The permissions of other models grantable on objects of ``content_type``."""
    return frozenset(key for hook in on_grantable for key in hook.call(content_type))


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
    expires_at: datetime | None = None,
    uow: ObjectPermissionsUnitOfWork | None = None,
) -> ObjectPermission:
    """Let ``user`` (or the members of ``group``) have ``permission`` on ``obj``.

    ``expires_at`` ends the grant at that moment; None (the default) never
    does. Granting what is granted already returns that grant, given the
    expiry asked for: the same one changes nothing.

    Raises:
        PermissionDenied: The actor may not manage grants, lacks the
            permission, or names themselves.
        InvalidGrant: Not exactly one of ``user`` and ``group``, an expiry
            that is not in the future, or a permission that is neither of
            ``obj``'s model nor ``grantable_on`` it.
        UnknownPermission: No permission has that key.
    """
    content_type, object_id = describe(obj)
    _whom(user, group)
    _check_expiry(expires_at)
    await _check_ceiling(actor, (permission,), obj, "add")
    _check_not_self(actor, user)
    await _check_permissions((permission,), content_type)
    async with uow or unit_of_work_for(content_type) as work:
        grant = await _grant(
            work,
            actor,
            permission,
            (content_type, object_id),
            user=user,
            group=group,
            expires_at=expires_at,
            role=None,
        )
        await work.commit()
    return grant


async def grant_role(
    actor: AnyUser,
    role: ObjectRole,
    obj: object,
    *,
    user: UUID | None = None,
    group: UUID | None = None,
    expires_at: datetime | None = None,
    uow: ObjectPermissionsUnitOfWork | None = None,
) -> list[ObjectPermission]:
    """Grant every permission of ``role`` on ``obj`` together; the grants.

    One grant per permission, named after the role, in one transaction:
    all or none. The actor must hold each permission, as for
    ``grant_permission``. Granting the role again gives its grants the
    expiry asked for, and adds those it lacks.

    Raises:
        PermissionDenied: As ``grant_permission``, for any permission.
        InvalidGrant: As ``grant_permission``, for any permission.
        UnknownPermission: No permission has one of the keys.
    """
    content_type, object_id = describe(obj)
    _whom(user, group)
    _check_expiry(expires_at)
    await _check_ceiling(actor, role.permissions, obj, "add")
    _check_not_self(actor, user)
    await _check_permissions(role.permissions, content_type)
    async with uow or unit_of_work_for(content_type) as work:
        grants = [
            await _grant(
                work,
                actor,
                permission,
                (content_type, object_id),
                user=user,
                group=group,
                expires_at=expires_at,
                role=role.name,
            )
            for permission in role.permissions
        ]
        await work.commit()
    return grants


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

    Every grant of it to them goes, granted alone or with a role.

    Raises:
        PermissionDenied: The actor may not manage grants, or lacks the
            permission.
        InvalidGrant: Not exactly one of ``user`` and ``group``.
    """
    content_type, object_id = describe(obj)
    user_id, group_ids = _whom(user, group)
    await _check_ceiling(actor, (permission,), obj, "delete")
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


async def revoke_role(
    actor: AnyUser,
    role: ObjectRole,
    obj: object,
    *,
    user: UUID | None = None,
    group: UUID | None = None,
    uow: ObjectPermissionsUnitOfWork | None = None,
) -> int:
    """Take back what ``role`` granted on ``obj`` to ``user`` or ``group``; how many.

    The grants named after the role go, whatever its permissions are now;
    the same permissions granted alone, or with another role, stay.

    Raises:
        PermissionDenied: The actor may not manage grants, or lacks a
            permission of the role, or of a grant it made.
        InvalidGrant: Not exactly one of ``user`` and ``group``.
    """
    content_type, object_id = describe(obj)
    user_id, group_ids = _whom(user, group)
    await _check_ceiling(actor, role.permissions, obj, "delete")
    async with uow or unit_of_work_for(content_type) as work:
        found = await work.grants.filter(
            about(content_type, object_id, user_id=user_id, group_ids=group_ids)
            & of_role(role.name)
        )
        await _check_holds(
            actor, {grant.permission for grant in found} - set(role.permissions), obj
        )
        for grant in found:
            grant.revoke(by=actor.id)
            await work.grants.remove(grant)
        await work.commit()
    return len(found)


async def grants_on(
    obj: object, *, uow: ObjectPermissionsUnitOfWork | None = None
) -> list[ObjectPermission]:
    """Every grant in force on ``obj``; who may see them is the caller's to check."""
    content_type, object_id = describe(obj)
    async with uow or unit_of_work_for(content_type) as work:
        return await work.grants.filter(
            about(content_type, object_id) & in_force(now())
        )


async def clear_expired_grants(*, at: datetime | None = None) -> int:
    """Delete the grants past their expiry, in every database; how many.

    Expired grants never count, so this only frees their rows
    (``cleargrants`` runs it). ``at`` defaults to now.
    """
    moment = at or now()
    count = 0
    for database in router.hosting_targets():
        async with ObjectPermissionsUnitOfWork(database=database) as uow:
            count += await uow.grants.clear_expired(moment)
            await uow.commit()
    return count


async def _grant(
    work: ObjectPermissionsUnitOfWork,
    actor: AnyUser,
    permission: str,
    on: tuple[str, ObjectId],
    *,
    user: UUID | None,
    group: UUID | None,
    expires_at: datetime | None,
    role: str | None,
) -> ObjectPermission:
    """``permission``'s grant with ``role``: added, or renewed to ``expires_at``."""
    content_type, object_id = on
    existing = await work.grants.first(
        about(
            content_type,
            object_id,
            permission=permission,
            user_id=user,
            group_ids=() if group is None else (group,),
        )
        & of_role(role)
    )
    if existing is None:
        grant = ObjectPermission.create(
            content_type=content_type,
            permission=permission,
            object_id=object_id,
            user_id=user,
            group_id=group,
            by=actor.id,
            expires_at=expires_at,
            role=role,
        )
        await work.grants.add(grant)
        return grant
    if existing.expires_at != expires_at:
        existing.renew(expires_at, by=actor.id)
    return existing


def _whom(user: UUID | None, group: UUID | None) -> tuple[UUID | None, Sequence[UUID]]:
    """``about``'s user and groups, for one user or one group.

    Raises:
        InvalidGrant: Not exactly one of them.
    """
    if (user is None) == (group is None):
        raise InvalidGrant("Name a user or a group, not both or neither.")
    return user, () if group is None else (group,)


def _check_expiry(expires_at: datetime | None) -> None:
    """Refuse an expiry without a time zone, or not in the future.

    Raises:
        InvalidGrant: It is one of those.
    """
    if expires_at is None:
        return
    if expires_at.tzinfo is None:
        raise InvalidGrant("A grant's expiry needs a time zone.")
    if expires_at <= now():
        raise InvalidGrant("A grant's expiry must be in the future.")


def _check_not_self(actor: AnyUser, user: UUID | None) -> None:
    """Refuse a grant to the actor themselves.

    Raises:
        PermissionDenied: ``user`` is the actor.
    """
    if user is not None and user == actor.id:
        raise PermissionDenied("You cannot grant permissions to yourself.")


async def _check_ceiling(
    actor: AnyUser, permissions: Iterable[str], obj: object, manage: str
) -> None:
    """Refuse an actor who may not manage grants, or lacks one of ``permissions``.

    Raises:
        PermissionDenied: They may not.
    """
    if not await has_perm(actor, f"objectpermissions.{manage}_objectpermission"):
        raise PermissionDenied("You may not manage permissions on objects.")
    await _check_holds(actor, permissions, obj)


async def _check_holds(actor: AnyUser, permissions: Iterable[str], obj: object) -> None:
    """Refuse an actor who lacks one of ``permissions``, on the model and on ``obj``.

    Raises:
        PermissionDenied: They lack one.
    """
    for permission in sorted(permissions):
        if not (
            await has_perm(actor, permission) or await has_perm(actor, permission, obj)
        ):
            raise PermissionDenied(
                "You may grant or revoke only permissions you hold on the object."
            )


async def _check_permissions(permissions: Iterable[str], content_type: str) -> None:
    """Refuse a permission of another model, unless it is ``grantable_on`` this one.

    Raises:
        UnknownPermission: No permission has one of the keys.
        InvalidGrant: One is another model's, and not grantable here.
    """
    keys = list(permissions)
    async with AuthUnitOfWork() as auth:
        found = await auth.permissions.get_many(keys)
    foreign = {each.key for each in found if str(each.content_type) != content_type}
    if foreign - grantable_permissions(content_type):
        raise InvalidGrant("That permission is not one of the object's model.")
