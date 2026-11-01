"""The admin API: users, groups, and permissions over REST (Django's admin).

Mount it where staff tools live::

    from domainkit.contrib.auth.admin import router as admin_router

    router.include_router(admin_router, prefix="/admin/auth")

Every endpoint needs a staff user (``is_staff``) or a superuser, and the
model permission of its action: ``view``, ``add``, ``change``, or
``delete`` on the user model, ``auth.group``, or ``auth.permission``.

Staff hand out only what they have. Only superusers make, unmake, or
change superusers. Other staff cannot grant permissions, or groups with
permissions, they lack themselves, and cannot change a user or group that
holds such a permission: setting a password is as good as becoming the
user. Where ``domainkit.contrib.approvals`` guards making superusers
(``APPROVALS.SUPERUSER_APPROVALS``), no one makes one here
(``approval_required``): an approved ``auth.grant_superuser`` request
does. Changes are recorded as events, so the audit trail shows who
changed what.
"""

import dataclasses
from collections.abc import Callable, Iterable
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated, Any, Final, Self, get_type_hints
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from domainkit.apps import registry
from domainkit.ddd import PermissionDenied
from domainkit.ddd.query import F
from domainkit.ddd.query import Selection as Listing
from domainkit.http import Pagination, error_responses
from domainkit.http.access import IsUnscoped, Require
from domainkit.resources import HasModelPermission as HasModelPermission
from domainkit.resources import ModelActions, ResourceRouter
from domainkit.resources.access import ModelLike
from domainkit.utils.decimals import jsonable

from .access import IsAdminUser, IsSuperuser
from .backends import ModelBackend, get_all_permissions, get_backends
from .events import UserLoggedOut
from .exceptions import (
    GroupNotFound,
    InvalidUserField,
    LastSuperuser,
    UnknownUserField,
    UserNotFound,
)
from .models import AbstractBaseUser, AnyUser, Group, GroupId, Permission, UserId
from .repositories import GroupRepository
from .uow import AuthUnitOfWork
from .users import get_user_model
from .validators import validate_password

__all__ = [
    "router",
]

PAGE_SIZE: Final = 50
APPROVALS: Final = "domainkit.contrib.approvals"
MAX_PAGE_SIZE: Final = 200
HIDDEN_FIELDS: Final = frozenset(
    {"id", "version", "password", "groups", "user_permissions", "last_login"}
)
"""Fields ``/users`` never shows among ``fields`` (``last_login`` has its own key)."""

router = APIRouter(tags=["auth admin"])
pages = Pagination(default_limit=PAGE_SIZE, max_limit=MAX_PAGE_SIZE)
_errors = error_responses
"""OpenAPI entries for the error statuses an endpoint returns."""


def staff_rules(model: ModelLike) -> ModelActions:
    """Staff (``is_staff``) or superusers, with the model permission of each action.

    Credentials limited to scopes are refused: what staff grant is bounded
    by their permissions, never by a token's scopes.
    """
    return ModelActions(model, IsUnscoped(), IsAdminUser() | IsSuperuser())


def staff(action: str, model: Callable[[], type]) -> Any:  # noqa: ANN401 - Depends.
    """The dependency of an admin endpoint: staff with ``action`` on the model."""
    return Depends(Require(*staff_rules(model).rules_for(action)))


def _users(action: str) -> Any:  # noqa: ANN401 - Depends.
    """Staff with ``action`` on the user model in use."""
    return staff(action, get_user_model)


def _groups(action: str) -> Any:  # noqa: ANN401 - Depends.
    """Staff with ``action`` on groups."""
    return staff(action, lambda: Group)


FieldValue = str | bool | int | float | None
"""A value a client may give a user field."""


class AdminUserOut(BaseModel):
    """A user as staff see it; ``fields`` holds the model's ``admin_fields()``."""

    id: UUID
    username: str
    is_active: bool
    is_staff: bool
    is_superuser: bool
    last_login: datetime | None
    groups: list[str]
    permissions: list[str]
    """Permissions granted to the user directly (not through groups)."""
    fields: dict[str, JsonValue]

    @classmethod
    def of(cls, user: AbstractBaseUser) -> Self:
        """Describe ``user``."""
        shown = type(user).admin_fields() - HIDDEN_FIELDS
        return cls(
            id=user.id,
            username=user.get_username(),
            is_active=user.is_active,
            is_staff=getattr(user, "is_staff", None) is True,
            is_superuser=user.is_superuser,
            last_login=user.last_login,
            groups=sorted(group.name for group in user.groups),
            permissions=sorted(permission.key for permission in user.user_permissions),
            fields={name: jsonable(getattr(user, name)) for name in sorted(shown)},
        )


class UserPage(BaseModel):
    """Users, and the cursor of the next page."""

    items: list[AdminUserOut]
    next_cursor: str | None


class AdminUserIn(BaseModel):
    """A new user: without a password, it cannot log in with one."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )
    is_active: bool = True
    is_staff: bool = False
    is_superuser: bool = False
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    """The model's other fields (``email``, ``first_name``, ...)."""


class AdminUserPatch(BaseModel):
    """What to change; what is left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    is_staff: bool | None = None
    is_superuser: bool | None = None
    fields: dict[str, FieldValue] = Field(default_factory=dict)


class AdminPasswordIn(BaseModel):
    """A new password; null makes the password unusable."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(min_length=1, max_length=4096, repr=False)


class GroupsIn(BaseModel):
    """Every group the user should be in, by name."""

    model_config = ConfigDict(extra="forbid")

    groups: list[str] = Field(max_length=1000)


class PermissionsIn(BaseModel):
    """Every permission to grant, by key (``<domain>.<codename>``)."""

    model_config = ConfigDict(extra="forbid")

    permissions: list[str] = Field(max_length=1000)


class GroupIn(BaseModel):
    """A new group and its permissions."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    permissions: list[str] = Field(default_factory=list, max_length=1000)


class GroupPatch(BaseModel):
    """A group's new name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)


class GroupOut(BaseModel):
    """A group and its permissions."""

    id: UUID
    name: str
    permissions: list[str]

    @classmethod
    def of(cls, group: Group) -> Self:
        """Describe ``group``."""
        return cls(
            id=group.id, name=group.name, permissions=sorted(group.permission_keys())
        )


class GroupPage(BaseModel):
    """Groups, and the cursor of the next page."""

    items: list[GroupOut]
    next_cursor: str | None


class PermissionOut(BaseModel):
    """A permission: its key, name, and content type."""

    key: str
    name: str
    content_type: str


COMMON: Final = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
ONE: Final = (*COMMON, status.HTTP_404_NOT_FOUND)
CHANGE: Final = (*ONE, status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def user_listing(
    search: Annotated[
        str | None,
        Query(max_length=150, description="Part of the username, in any case."),
    ] = None,
) -> Listing:
    """Users by username; ``search`` keeps those whose name contains it."""
    name = F(get_user_model().USERNAME_FIELD)
    return Listing(name.icontains(search) if search else None, ordering=[name])


async def group_listing() -> Listing:
    """Groups by name (unique)."""
    return GroupRepository.LISTING


users = ResourceRouter(
    "/users",
    model=get_user_model,
    basename="user",
    schema=AdminUserOut,
    present=AdminUserOut.of,
    page_schema=UserPage,
    unit_of_work=AuthUnitOfWork,
    repository=lambda uow: uow.users,
    not_found=lambda _: UserNotFound("No such user."),
    filters=user_listing,
    pagination=pages,
    rule=staff_rules(get_user_model),
)
"""``GET /users`` (by username; ``search`` matches part of it) and ``/users/{id}``."""
groups = ResourceRouter(
    "/groups",
    model=Group,
    schema=GroupOut,
    present=GroupOut.of,
    page_schema=GroupPage,
    unit_of_work=AuthUnitOfWork,
    repository=lambda uow: uow.groups,
    not_found=lambda _: GroupNotFound("No such group."),
    filters=group_listing,
    pagination=pages,
    rule=staff_rules(lambda: Group),
)
"""``GET /groups`` (by name) and ``/groups/{id}``."""
router.include_router(users)
router.include_router(groups)


@router.post("/users", status_code=status.HTTP_201_CREATED, responses=_errors(*CHANGE))
async def create_user(
    body: AdminUserIn, actor: Annotated[AnyUser, _users("add")]
) -> AdminUserOut:
    """Create a user; a password is checked by ``AUTH.PASSWORD_VALIDATORS``.

    With ``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``, the user changes it at the
    first login.

    ``fields`` are tidied by the model's ``clean_field``, as on a change.
    """
    if body.is_superuser:
        _superusers_only(actor, "Only superusers may make superusers.")
        _approval_needed()
    model = get_user_model()
    fields = _checked(model, body.fields)
    flags: dict[str, bool] = {
        "is_active": body.is_active,
        "is_superuser": body.is_superuser,
    }
    if _has_field(model, "is_staff"):
        flags["is_staff"] = body.is_staff
    elif body.is_staff:
        raise UnknownUserField("This user model has no staff flag.")
    if body.password is not None:
        candidate = SimpleNamespace(**{**fields, model.USERNAME_FIELD: body.username})
        validate_password(body.password, candidate)
    async with AuthUnitOfWork() as uow:
        user = await uow.users.create_user(
            body.username, body.password, **fields, **flags
        )
        for name, value in fields.items():
            setattr(user, name, user.clean_field(name, value))
        if body.password is not None:  # The user changes it at the first login.
            await uow.passwords.require_change(user.id)
        await uow.commit()
    return AdminUserOut.of(user)


@router.patch("/users/{user_id}", responses=_errors(*CHANGE))
async def update_user(
    user_id: UUID, body: AdminUserPatch, actor: Annotated[AnyUser, _users("change")]
) -> AdminUserOut:
    """Change a user's status flags and fields."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _may_change(actor, user)
        was_active = user.is_active
        if body.is_superuser is not None and body.is_superuser != user.is_superuser:
            _superusers_only(actor, "Only superusers may make or unmake superusers.")
            if body.is_superuser:
                _approval_needed()
        if (
            user.is_superuser
            and user.is_active
            and False in (body.is_superuser, body.is_active)
        ):
            await _not_the_last_superuser(uow)  # Before anything is flushed.
        if body.is_staff is not None:
            set_staff = getattr(user, "set_staff", None)
            if set_staff is None:
                raise UnknownUserField("This user model has no staff flag.")
            set_staff(body.is_staff)
        if body.is_active is not None:
            user.set_active(body.is_active)
        if body.is_superuser is not None:
            user.set_superuser(body.is_superuser)
        user.update(**_checked(type(user), body.fields))
        await _may_change(actor, user)  # Not into someone who holds more either.
        if was_active and not user.is_active:  # Their sessions end with them.
            await uow.tokens.revoke_user(user.id)
        await uow.commit()
    return AdminUserOut.of(user)


@router.delete(
    "/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors(*ONE)
)
async def delete_user(
    user_id: UUID, actor: Annotated[AnyUser, _users("delete")]
) -> None:
    """Delete a user, every token of theirs, and their second factor."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _may_change(actor, user)
        if user.is_superuser and user.is_active:
            await _not_the_last_superuser(uow)
        user.delete()
        await uow.users.remove(user)  # With its credentials (forget_credentials).
        await uow.commit()


@router.post(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*CHANGE),
)
async def set_password(
    user_id: UUID, body: AdminPasswordIn, actor: Annotated[AnyUser, _users("change")]
) -> None:
    """Set a user's password (null: unusable); the user's sessions end.

    With ``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``, another user's password set
    here logs in only to be changed (PCI DSS v4.0 8.3.5).
    """
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _may_change(actor, user)
        if body.password is not None:
            validate_password(body.password, user)
        await uow.users.set_password(
            user, body.password, priority=True, temporary=user.id != actor.id
        )
        await uow.tokens.revoke_user(user.id)
        await uow.commit()


@router.post(
    "/users/{user_id}/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*ONE),
)
async def log_out_user(
    user_id: UUID, actor: Annotated[AnyUser, _users("change")]
) -> None:
    """End every session of a user."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _may_change(actor, user)
        await uow.tokens.revoke_user(user.id)
        user.record(UserLoggedOut(user_id=user.id, everywhere=True))
        await uow.commit()


@router.put("/users/{user_id}/groups", responses=_errors(*CHANGE))
async def set_user_groups(
    user_id: UUID, body: GroupsIn, actor: Annotated[AnyUser, _users("change")]
) -> AdminUserOut:
    """Make the user a member of exactly ``groups``."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _may_change(actor, user)
        wanted = await uow.groups.get_many_by_name(body.groups)
        added = [group for group in wanted if group not in user.groups]
        await _may_grant(actor, _keys(*added))
        for group in [group for group in user.groups if group not in wanted]:
            user.leave(group)
        for group in added:
            user.join(group)
        await uow.commit()
    return AdminUserOut.of(user)


@router.put("/users/{user_id}/permissions", responses=_errors(*CHANGE))
async def set_user_permissions(
    user_id: UUID, body: PermissionsIn, actor: Annotated[AnyUser, _users("change")]
) -> AdminUserOut:
    """Grant the user exactly ``permissions`` directly (groups add theirs)."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _may_change(actor, user)
        wanted = await uow.permissions.get_many(body.permissions)
        added = [
            permission
            for permission in wanted
            if permission not in user.user_permissions
        ]
        await _may_grant(actor, {permission.key for permission in added})
        user.revoke(*[p for p in user.user_permissions if p not in wanted])
        user.grant(*added)
        await uow.commit()
    return AdminUserOut.of(user)


@router.post("/groups", status_code=status.HTTP_201_CREATED, responses=_errors(*CHANGE))
async def create_group(
    body: GroupIn, actor: Annotated[AnyUser, _groups("add")]
) -> GroupOut:
    """Create a group with ``permissions``."""
    async with AuthUnitOfWork() as uow:
        permissions = await uow.permissions.get_many(body.permissions)
        await _may_grant(actor, {permission.key for permission in permissions})
        group = await uow.groups.create(body.name)
        group.grant(*permissions)
        await uow.commit()
    return GroupOut.of(group)


@router.patch("/groups/{group_id}", responses=_errors(*CHANGE))
async def rename_group(
    group_id: UUID, body: GroupPatch, actor: Annotated[AnyUser, _groups("change")]
) -> GroupOut:
    """Rename a group."""
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
        await uow.groups.rename(group, body.name)
        await uow.commit()
    return GroupOut.of(group)


@router.put("/groups/{group_id}/permissions", responses=_errors(*CHANGE))
async def set_group_permissions(
    group_id: UUID, body: PermissionsIn, actor: Annotated[AnyUser, _groups("change")]
) -> GroupOut:
    """Grant the group exactly ``permissions``; its members gain and lose them."""
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
        wanted = await uow.permissions.get_many(body.permissions)
        added = [
            permission for permission in wanted if permission not in group.permissions
        ]
        await _may_grant(actor, {permission.key for permission in added})
        group.revoke(*[p for p in group.permissions if p not in wanted])
        group.grant(*added)
        await uow.commit()
    return GroupOut.of(group)


@router.delete(
    "/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*ONE),
)
async def delete_group(
    group_id: UUID, actor: Annotated[AnyUser, _groups("delete")]
) -> None:
    """Delete a group; its members lose its permissions."""
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
        group.delete()
        await uow.groups.remove(group)
        await uow.commit()


@router.get("/permissions", responses=_errors(*COMMON))
async def list_permissions(
    actor: Annotated[AnyUser, staff("view", lambda: Permission)],
) -> list[PermissionOut]:
    """Every permission, by domain, model, and codename."""
    async with AuthUnitOfWork() as uow:
        permissions = await uow.permissions.all()
    return [
        PermissionOut(
            key=permission.key,
            name=permission.name,
            content_type=str(permission.content_type),
        )
        for permission in permissions
    ]


async def _user(uow: AuthUnitOfWork, user_id: UUID) -> AbstractBaseUser:
    """The user ``user_id``.

    Raises:
        UserNotFound: There is none.
    """
    user = await uow.users.find(UserId(user_id))
    if user is None:
        raise UserNotFound("No such user.")
    return user


async def _group(uow: AuthUnitOfWork, group_id: UUID) -> Group:
    """The group ``group_id``.

    Raises:
        GroupNotFound: There is none.
    """
    group = await uow.groups.find(GroupId(group_id))
    if group is None:
        raise GroupNotFound("No such group.")
    return group


def _approval_needed() -> None:
    """Refuse making a superuser here where ``contrib.approvals`` guards it.

    With ``APPROVALS.SUPERUSER_APPROVALS``, an approved request makes them.
    """
    if registry.is_installed(APPROVALS):
        from domainkit.contrib.approvals import SUPERUSERS, refuse_if_guarded

        refuse_if_guarded(SUPERUSERS)


def _superusers_only(actor: AnyUser, message: str) -> None:
    """Refuse a step only superusers may take."""
    if not actor.is_superuser:
        raise PermissionDenied(message)


async def _may_change(actor: AnyUser, user: AbstractBaseUser) -> None:
    """Refuse to change a user who holds more than ``actor``.

    Changing a password or logging a user out is as good as becoming them,
    so staff only change users whose every grant (direct or through groups,
    active or not, and from every backend in ``AUTH.BACKENDS``) they have
    themselves; only superusers change superusers. Other backends grant an
    inactive user nothing, so only superusers change inactive users while
    such backends are configured: what the user would hold is unknown.
    """
    if user.is_superuser:
        _superusers_only(actor, "Only superusers may change superusers.")
    granted = {permission.key for permission in user.user_permissions}
    if user.is_active:
        granted |= await get_all_permissions(user)
    elif any(not isinstance(backend, ModelBackend) for backend in get_backends()):
        _superusers_only(
            actor,
            "Only superusers may change inactive users while other backends "
            "grant permissions.",
        )
    await _may_grant(
        actor,
        granted | _keys(*user.groups),
        "You can only change users whose permissions you have",
    )


async def _not_the_last_superuser(uow: AuthUnitOfWork) -> None:
    """Refuse to demote, deactivate, or delete the last active superuser."""
    if await uow.users.active_superusers() <= 1:
        raise LastSuperuser(
            "This is the last active superuser; make another one first."
        )


async def _may_change_group(actor: AnyUser, group: Group) -> None:
    """Refuse to change a group that grants what ``actor`` lacks."""
    await _may_grant(
        actor,
        group.permission_keys(),
        "You can only change groups whose permissions you have",
    )


async def _may_grant(
    actor: AnyUser,
    keys: Iterable[str],
    refusal: str = "You can only grant permissions you have",
) -> None:
    """Refuse to hand out (or take away) permissions ``actor`` does not have."""
    if actor.is_superuser:
        return
    if missing := sorted(set(keys) - await get_all_permissions(actor)):
        raise PermissionDenied(f"{refusal}; you lack {', '.join(missing)}.")


def _keys(*groups: Group) -> set[str]:
    """Every permission the groups grant."""
    return {key for group in groups for key in group.permission_keys()}


def _checked(
    model: type[AbstractBaseUser], fields: dict[str, FieldValue]
) -> dict[str, object]:
    """``fields``, each converted to the type the user model declares for it.

    Raises:
        UnknownUserField: A field is not one of ``model.editable_fields()``.
        InvalidUserField: A value does not fit its field.
    """
    model.check_editable(fields)
    types = get_type_hints(model)
    checked = {}
    for name, value in fields.items():
        try:
            checked[name] = TypeAdapter(types[name]).validate_python(value)
        except ValidationError as error:
            reason = error.errors()[0]["msg"]
            raise InvalidUserField(f"{name}: {reason}.") from None
    return checked


def _has_field(model: type, name: str) -> bool:
    """Whether the user model stores ``name``."""
    return any(each.name == name for each in dataclasses.fields(model))
