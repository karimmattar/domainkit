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
user. Grants on single objects count as model permissions do: staff
change no user, and join no one to a group, holding a grant on an object
they lack, unless they hold its permission on the whole model (AUTH-14).
Where ``domainkit.contrib.approvals`` guards making superusers
(``APPROVALS.SUPERUSER_APPROVALS``), no one makes one here
(``approval_required``): an approved ``auth.grant_superuser`` request
does. A change passes the admin API's own checks, then the
``on_admin_change`` checks, which may hold it for approval
(``APPROVALS.ADMIN_APPROVALS``: ``approval_pending``). Changes are
recorded as events, so the audit trail shows who changed what.

With ``AUTH.ADMIN_ACTION`` on, each change is an act (ADR 0163): its
route asks for an authorization of ``auth.admin_change`` bound to that
change, which its twin asks for (``POST <its path>/authorization``, or
``<its path>/authorization/patch`` and ``.../delete`` where a ``PATCH``
and a ``DELETE`` share a path), and the change spends with its commit
(``admin_actions``, imported only while the setting is on).
"""

import dataclasses
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any, Final, Self, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
)

from domainkit.db.encrypted import EncryptedType
from domainkit.ddd import PermissionDenied
from domainkit.ddd.query import And, F
from domainkit.ddd.query import Selection as Listing
from domainkit.http import PageRequest, Pagination, error_responses
from domainkit.http.access import BasePermission, IsUnscoped, Principal, Require
from domainkit.http.errors import APIError
from domainkit.resources import HasModelPermission as HasModelPermission
from domainkit.resources import ModelActions, ResourceRouter
from domainkit.resources.access import ModelLike
from domainkit.tenancy import tenants_beyond
from domainkit.utils.decimals import jsonable

from ._http import no_store
from .access import IsAdminUser, IsSuperuser, StepUp, impersonated
from .actions import (
    ActionAuthorizationOut,
    check_authorization,
    request_action_authorization,
    spend_authorization,
)
from .authentication import IMPERSONATION
from .backends import (
    ModelBackend,
    ObjectGrant,
    backends_without_object_grants,
    get_all_permissions,
    get_backends,
    object_grants,
)
from .events import UserImpersonated, UserLoggedOut
from .exceptions import (
    GroupNotFound,
    LastSuperuser,
    UnknownUserField,
    UserNotFound,
)
from .gates import (
    AdminChange,
    AdminKind,
    on_admin_change,
    on_create_staff,
    on_make_superuser,
)
from .models import AbstractBaseUser, AnyUser, Group, GroupId, Permission, UserId
from .repositories import GroupRepository, user_search
from .settings import auth_settings, identifier_fields
from .tokens import LoginMethod, LoginOrigin, logging_in
from .uow import AuthUnitOfWork
from .users import FieldValue, get_user_model, get_user_table, typed_fields
from .validators import avalidate_password

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

    from .actions import Action
    from .admin_actions import ActKind, AdminAct

__all__ = [
    "may_change",
    "may_grant",
    "may_hold",
    "router",
]

PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 200
HIDDEN_FIELDS: Final = frozenset(
    {"id", "version", "password", "groups", "user_permissions", "last_login"}
)
"""Fields ``/users`` never shows among ``fields`` (``last_login`` has its own key)."""
IDENTIFIED: Final = ("email", "mobile_number")
"""Fields a user changes by proving the new value first (ADR 0153)."""

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


def _change(action: str, model: Callable[[], type]) -> Any:  # noqa: ANN401 - Depends.
    """The dependency of a change: staff with ``action``, and its authorization.

    With ``AUTH.ADMIN_ACTION`` on, the request holds an authorization of an
    admin change (``ChangeAuthorized``, ADR 0163); the route spends it on
    the change it makes, which must be the one authorized.
    """
    rules = staff_rules(model).rules_for(action)
    return Depends(Require(*rules, ChangeAuthorized()))


def _asked() -> bool:
    """Whether the admin API's changes ask for authorizations (``ADMIN_ACTION``)."""
    return auth_settings().ADMIN_ACTION


class ChangeAuthorized(BasePermission):
    """With ``AUTH.ADMIN_ACTION`` on: the change holds an authorization of its own.

    It asks what ``StepUp(for_="auth.admin_change")`` asks (ADR 0144): an
    ``Action-Authorization`` header holding a confirmed authorization of
    the staff member's, of their login. ``401 authorization_required``
    without one, ``403 action_not_authorized`` with one that does not hold;
    staff acting as a user are refused. The route checks that it binds the
    change it makes, when it makes it. Off, it allows everyone, and the
    action's module is not loaded.
    """

    def __init__(self) -> None:
        """Ask nothing until the setting is on."""
        self._rule: BasePermission | None = None

    def _asking(self) -> BasePermission | None:
        """``StepUp(for_="auth.admin_change")``, while the setting is on."""
        if not _asked():
            return None
        if self._rule is None:
            self._rule = StepUp(for_=_admin_change_action())
        return self._rule

    async def has_permission(self, request: "HTTPConnection", user: Principal) -> bool:
        """The setting is off, or the request holds an authorization of a change."""
        rule = self._asking()
        return rule is None or await rule.has_permission(request, user)

    async def refusal(
        self, request: "HTTPConnection", user: Principal
    ) -> Exception | None:
        """``401 authorization_required`` without the header; else ``403``."""
        rule = self._asking()
        return None if rule is None else await rule.refusal(request, user)


def _admin_change_action() -> "Action":
    """``auth.admin_change``, loaded on first use (``admin_actions``)."""
    from .admin_actions import ADMIN_CHANGE

    return ADMIN_CHANGE


TWIN: Final = "domainkit_twin"
"""The attribute that marks a twin's endpoint: it asks for an authorization,
and makes no change (``domainkit.resources.asking_authorization``)."""


def _twin[F: Callable[..., Any]](endpoint: F) -> F:
    """Mark ``endpoint`` as a twin (``TWIN``)."""
    setattr(endpoint, TWIN, True)
    return endpoint


def _user_act(
    user: AbstractBaseUser | None,
    change: AdminChange,
    *,
    name: str | None = None,
    password: bool | None = None,
    values: dict[str, object] | None = None,
) -> "AdminAct":
    """The act of ``change`` to ``user``, as its authorization binds it."""
    from .admin_actions import AdminAct

    return AdminAct.of(user, change, name=name, password=password, values=values)


def _group_act(
    kind: "ActKind",
    group_id: UUID | None = None,
    *,
    name: str | None = None,
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> "AdminAct":
    """The act of a change to a group, as its authorization binds it."""
    from .admin_actions import AdminAct

    return AdminAct.of_group(kind, group_id, name=name, added=added, removed=removed)


def _user_change(action: str) -> Any:  # noqa: ANN401 - Depends.
    """Staff with ``action`` on the user model, changing a user as authorized."""
    return _change(action, get_user_model)


def _group_change(action: str) -> Any:  # noqa: ANN401 - Depends.
    """Staff with ``action`` on groups, changing a group as authorized."""
    return _change(action, lambda: Group)


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


class UserNameOut(BaseModel):
    """A user as a page names them: who they are, not what they hold."""

    id: UUID
    username: str
    name: str | None
    """The full name, where the user model has one (``get_full_name``)."""
    identifiers: dict[str, JsonValue]
    """The fields of ``AUTH.IDENTIFIERS`` among those ``/users`` shows, by name."""

    @classmethod
    def of(cls, user: AbstractBaseUser) -> Self:
        """Name ``user``."""
        model = type(user)
        shown = model.admin_fields() - HIDDEN_FIELDS
        full_name = getattr(user, "get_full_name", None)
        return cls(
            id=user.id,
            username=user.get_username(),
            name=(full_name() or None) if callable(full_name) else None,
            identifiers={
                name: jsonable(getattr(user, name))
                for name in identifier_fields(model)
                if name in shown
            },
        )


class UserNamePage(BaseModel):
    """Users by name, and the cursor of the next page."""

    items: list[UserNameOut]
    next_cursor: str | None


class AdminUserIn(BaseModel):
    """A new user: without a password, it cannot log in with one."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=255)
    """Optional where the username does not identify users (``AUTH.IDENTIFIERS``):
    ``fields`` then gives the email address or the mobile number."""
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


class ImpersonateIn(BaseModel):
    """Why staff act as the user: recorded in the audit trail."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class ImpersonationOut(BaseModel):
    """A token that acts as the user until it expires; it is never refreshed."""

    access_token: str = Field(repr=False)
    token_type: str = "Bearer"  # noqa: S105 - a scheme, not a secret.
    expires_in: int


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
ASK: Final = (status.HTTP_400_BAD_REQUEST, *CHANGE)
"""What a twin answers besides ``201``: what its change answers, and ``400
step_up_unsupported`` for credentials that cannot ask."""


async def user_listing(
    search: Annotated[
        str | None,
        Query(
            max_length=150,
            description=(
                "Part of the username, the email address, or the mobile number "
                "(those of AUTH.IDENTIFIERS), in any case."
            ),
        ),
    ] = None,
    ids: Annotated[
        list[UUID] | None,
        Query(
            alias="id",
            max_length=MAX_PAGE_SIZE,
            description=(
                f"Only these users, by id: repeat it, up to {MAX_PAGE_SIZE} "
                "(as many as one page holds)."
            ),
        ),
    ] = None,
) -> Listing:
    """Users by their first identifier; ``search`` and ``id`` keep those they name.

    ``user_search`` finds them by the username and the fields of
    ``AUTH.IDENTIFIERS``. ``id`` names users by id, so a page of rows that
    refer to users names them in one request (user report D2). They are
    ordered by the first of those fields that is not encrypted
    (``AUTH.IDENTIFIERS[0]``, else the username), and by id among equal
    values.
    """
    model = get_user_model()
    found = [user_search(model, search)] if search else []
    if ids:
        found.append(F("id").in_(dict.fromkeys(ids)))
    return Listing(And.of(*found) if found else None, ordering=[F(_ordered_by(model))])


def _ordered_by(model: type[AbstractBaseUser]) -> str:
    """The first identifier that is not encrypted, else the username, as before."""
    columns = get_user_table().c
    for name in identifier_fields(model):
        if not isinstance(columns[name].type, EncryptedType):
            return name
    return model.USERNAME_FIELD


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
"""``GET /users`` (``search`` matches part of an identifier; ``id`` names
users) and ``/users/{id}``."""


@router.get("/users/names", responses=_errors(*COMMON))
async def list_user_names(
    actor: Annotated[AnyUser, _users("view")],
    listing: Annotated[Listing, Depends(user_listing)],
    page: Annotated[PageRequest, Depends(pages)],
) -> UserNamePage:
    """Users by name: ``GET /users``' page, without what each user holds.

    ``search`` and ``id`` keep the users they name, in the same order, so a
    cursor of either list reads the next page of both. Each user is their
    id, ``username``, full name, and identifiers: a page of rows that refer
    to users names them without their groups and permissions (user report
    D2).
    """
    async with AuthUnitOfWork() as uow:
        found = await page.fetch(uow.users, listing)
    out = page.respond(found, [UserNameOut.of(user) for user in found.items], listing)
    return UserNamePage(items=out.items, next_cursor=out.next_cursor)


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
    body: AdminUserIn, actor: Annotated[AnyUser, _user_change("add")]
) -> AdminUserOut:
    """Create a user; a password is checked by ``AUTH.PASSWORD_VALIDATORS``.

    With ``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``, the user changes it at the
    first login.

    ``fields`` are tidied by the model's ``clean_field``, as on a change.
    """
    fields, flags, change = await _creating(actor, body)
    async with AuthUnitOfWork() as uow:
        await _admin_change(actor, None, change, uow, lambda: _created(body, change))
        user = await uow.users.create_user(
            body.username, body.password, **fields, **flags
        )
        for name, value in fields.items():
            setattr(user, name, user.clean_field(name, value))
        if body.password is not None:  # The user changes it at the first login.
            await uow.passwords.require_change(user.id)
        await uow.commit()
    return AdminUserOut.of(user)


@router.post(
    "/users/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_create_user(
    body: AdminUserIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("add")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of creating this user (``AUTH.ADMIN_ACTION``)."""
    _asking()
    _, _, change = await _creating(actor, body)
    return await _ask(request, response, _created(body, change))


async def _creating(
    actor: AnyUser, body: AdminUserIn
) -> tuple[dict[str, object], dict[str, bool], AdminChange]:
    """The fields and flags creating ``body`` sets, checked; the change it is."""
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
        await avalidate_password(body.password, candidate)
    if body.is_staff:
        for hook in on_create_staff:  # Approvals, while making staff is held.
            hook.call()
    flags_set = {name for name in ("is_staff", "is_superuser") if getattr(body, name)}
    change = AdminChange(
        "create",
        frozenset({*fields, *flags_set}),
        values={**fields, **dict.fromkeys(flags_set, True)},
    )
    return fields, flags, change


def _created(body: AdminUserIn, change: AdminChange) -> "AdminAct":
    """The act of creating ``body``'s user: its name, activity, and password.

    Whether the user is active is bound, though ``change`` names it only
    when set; whether there is a password, never which.
    """
    return _user_act(
        None,
        change,
        name=body.username,
        password=body.password is not None,
        values={"is_active": body.is_active},
    )


@router.patch("/users/{user_id}", responses=_errors(*CHANGE))
async def update_user(
    user_id: UUID,
    body: AdminUserPatch,
    actor: Annotated[AnyUser, _user_change("change")],
) -> AdminUserOut:
    """Change a user's status flags and fields.

    What it checks comes first, then the ``on_admin_change`` checks, then
    the change.
    """
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        fields, change = await _updating(actor, user, body)
        await _admin_change(actor, user, change, uow, lambda: _user_act(user, change))
        was_active = user.is_active
        if _demoting(user, body):
            await _not_the_last_superuser(uow)  # Before anything is flushed.
        set_staff = getattr(user, "set_staff", None)
        if body.is_staff is not None and set_staff is not None:
            set_staff(body.is_staff)
        if body.is_active is not None:
            user.set_active(body.is_active)
        if body.is_superuser is not None:
            user.set_superuser(body.is_superuser)
        before = {name: getattr(user, name, None) for name in IDENTIFIED}
        user.update(**fields)
        await _replaced(uow, user, before)
        await may_change(actor, user)  # Not into someone who holds more either.
        if was_active and not user.is_active:  # Their sessions end with them.
            await uow.tokens.revoke_user(user.id)
        await uow.commit()
    return AdminUserOut.of(user)


@router.post(
    "/users/{user_id}/authorization/patch",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_update_user(
    user_id: UUID,
    body: AdminUserPatch,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of this change of a user (``PATCH``)."""
    _asking()
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        _, change = await _updating(actor, user, body)
        if _demoting(user, body):
            await _not_the_last_superuser(uow)
    return await _ask(request, response, _user_act(user, change))


def _demoting(user: AbstractBaseUser, body: AdminUserPatch) -> bool:
    """Whether ``body`` takes an active superuser's status or activity away."""
    return (
        user.is_superuser
        and user.is_active
        and False in (body.is_superuser, body.is_active)
    )


async def _updating(
    actor: AnyUser, user: AbstractBaseUser, body: AdminUserPatch
) -> tuple[dict[str, object], AdminChange]:
    """The fields ``body`` sets on ``user``, checked; the change it is."""
    await may_change(actor, user)
    flags = {
        name: value
        for name in ("is_staff", "is_active", "is_superuser")
        if (value := getattr(body, name)) is not None
    }
    fields = _checked(type(user), body.fields)
    if body.is_staff is not None and getattr(user, "set_staff", None) is None:
        raise UnknownUserField("This user model has no staff flag.")
    if body.is_superuser is not None and body.is_superuser != user.is_superuser:
        _superusers_only(actor, "Only superusers may make or unmake superusers.")
        if body.is_superuser:
            _approval_needed()
    change = AdminChange(
        "update", frozenset({*fields, *flags}), values={**fields, **flags}
    )
    return fields, change


@router.delete(
    "/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_errors(*ONE)
)
async def delete_user(
    user_id: UUID, actor: Annotated[AnyUser, _user_change("delete")]
) -> None:
    """Delete a user, every token of theirs, and their second factor."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _changing(actor, user, "delete")
        await _admin_change(actor, user, change, uow, lambda: _user_act(user, change))
        if user.is_superuser and user.is_active:
            await _not_the_last_superuser(uow)
        user.delete()
        await uow.users.remove(user)  # With its credentials (forget_credentials).
        await uow.commit()


@router.post(
    "/users/{user_id}/authorization/delete",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_delete_user(
    user_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("delete")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of deleting a user (``DELETE /users/{id}``)."""
    _asking()
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _changing(actor, user, "delete")
        if user.is_superuser and user.is_active:
            await _not_the_last_superuser(uow)
    return await _ask(request, response, _user_act(user, change))


async def _changing(
    actor: AnyUser, user: AbstractBaseUser, kind: AdminKind
) -> AdminChange:
    """The change ``kind`` to ``user``, which sets nothing, if ``actor`` may make it."""
    await may_change(actor, user)
    return AdminChange(kind)


@router.post(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*CHANGE),
)
async def set_password(
    user_id: UUID,
    body: AdminPasswordIn,
    actor: Annotated[AnyUser, _user_change("change")],
) -> None:
    """Set a user's password (null: unusable); the user's sessions end.

    With ``AUTH.PASSWORD_CHANGE_ON_FIRST_USE``, another user's password set
    here logs in only to be changed (PCI DSS v4.0 8.3.5).
    """
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _setting_password(actor, user, body)
        await _admin_change(actor, user, change, uow, lambda: _password_act(user, body))
        await uow.users.set_password(
            user, body.password, priority=True, temporary=user.id != actor.id
        )
        await uow.tokens.revoke_user(user.id)
        await uow.commit()


@router.post(
    "/users/{user_id}/password/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_set_password(
    user_id: UUID,
    body: AdminPasswordIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of setting a user's password; it is not kept."""
    _asking()
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        await _setting_password(actor, user, body)
    return await _ask(request, response, _password_act(user, body))


async def _setting_password(
    actor: AnyUser, user: AbstractBaseUser, body: AdminPasswordIn
) -> AdminChange:
    """The change of ``user``'s password, if ``actor`` may make it and it is valid."""
    await may_change(actor, user)
    if body.password is not None:
        await avalidate_password(body.password, user)
    return AdminChange("password")


def _password_act(user: AbstractBaseUser, body: AdminPasswordIn) -> "AdminAct":
    """The act of setting ``user``'s password: usable or not, never which."""
    usable = body.password is not None
    return _user_act(user, AdminChange("password"), password=usable)


@router.post(
    "/users/{user_id}/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*ONE),
)
async def log_out_user(
    user_id: UUID, actor: Annotated[AnyUser, _user_change("change")]
) -> None:
    """End every session of a user."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _changing(actor, user, "logout")
        await _admin_change(actor, user, change, uow, lambda: _user_act(user, change))
        await uow.tokens.revoke_user(user.id)
        user.record(UserLoggedOut(user_id=user.id, everywhere=True))
        await uow.commit()


@router.post(
    "/users/{user_id}/logout/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_log_out_user(
    user_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of ending every session of a user."""
    _asking()
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _changing(actor, user, "logout")
    return await _ask(request, response, _user_act(user, change))


@router.post(
    "/users/{user_id}/impersonate",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*CHANGE),
)
async def impersonate(
    user_id: UUID,
    body: ImpersonateIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _user_change("change")],
) -> ImpersonationOut:
    """Act as an active user for ``AUTH.IMPERSONATION_TTL``, for ``reason``.

    Only staff who may change the user (who hold every permission the user
    holds; superusers for superusers), never as themselves. The token's
    requests are recorded as the staff member's, it reaches no step-up,
    and it is never refreshed; logging out ends it.

    Raises:
        PermissionDenied: Impersonation is off, the request is itself an
            impersonation, the user is the staff member, or holds more
            than them.
    """
    response.headers["Cache-Control"] = "no-store"  # The answer is a token.
    ttl = _impersonation_ttl(request, actor, user_id)
    staff_id = cast("UUID", actor.id)  # Staff: always a user with an id.
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _impersonating(actor, user, body)
        await _admin_change(actor, user, change, uow, lambda: _user_act(user, change))
        with logging_in(LoginOrigin(LoginMethod.IMPERSONATION)):
            raw = await uow.tokens.issue(
                user.id, ttl=ttl, name=IMPERSONATION, family=staff_id
            )
        user.record(
            UserImpersonated(
                user_id=user.id, impersonator_id=staff_id, reason=body.reason
            )
        )
        await uow.commit()
    return ImpersonationOut(access_token=raw, expires_in=int(ttl.total_seconds()))


@router.post(
    "/users/{user_id}/impersonate/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_impersonate(
    user_id: UUID,
    body: ImpersonateIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of acting as a user, for ``reason``."""
    _asking()
    _impersonation_ttl(request, actor, user_id)
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        change = await _impersonating(actor, user, body)
    return await _ask(request, response, _user_act(user, change))


def _impersonation_ttl(request: Request, actor: AnyUser, user_id: UUID) -> timedelta:
    """How long an impersonation works, if one may start here.

    Raises:
        PermissionDenied: Impersonation is off, the request is itself an
            impersonation, or the user is the staff member.
    """
    ttl = auth_settings().IMPERSONATION_TTL
    if ttl is None:
        raise PermissionDenied("Impersonation is off (AUTH.IMPERSONATION_TTL).")
    if impersonated(request):  # Its token would name the impersonated staff.
        raise PermissionDenied("An impersonation cannot impersonate.")
    if user_id == actor.id:
        raise PermissionDenied("Staff cannot impersonate themselves.")
    return ttl


async def _impersonating(
    actor: AnyUser, user: AbstractBaseUser, body: ImpersonateIn
) -> AdminChange:
    """The change of acting as ``user``, if ``actor`` may.

    Raises:
        PermissionDenied: The user is inactive, or holds more than ``actor``.
    """
    if not user.is_active:
        raise PermissionDenied("Only active users can be impersonated.")
    await may_change(actor, user)
    return AdminChange("impersonate", values={"reason": body.reason})


@router.put("/users/{user_id}/groups", responses=_errors(*CHANGE))
async def set_user_groups(
    user_id: UUID, body: GroupsIn, actor: Annotated[AnyUser, _user_change("change")]
) -> AdminUserOut:
    """Make the user a member of exactly ``groups``."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        wanted, added, change = await _grouping(uow, actor, user, body)
        await _admin_change(actor, user, change, uow, lambda: _user_act(user, change))
        for group in [group for group in user.groups if group not in wanted]:
            user.leave(group)
        for group in added:
            user.join(group)
        await uow.commit()
    return AdminUserOut.of(user)


@router.post(
    "/users/{user_id}/groups/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_set_user_groups(
    user_id: UUID,
    body: GroupsIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of putting a user in exactly ``groups``."""
    _asking()
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        _, _, change = await _grouping(uow, actor, user, body)
    return await _ask(request, response, _user_act(user, change))


async def _grouping(
    uow: AuthUnitOfWork, actor: AnyUser, user: AbstractBaseUser, body: GroupsIn
) -> tuple[list[Group], list[Group], AdminChange]:
    """The groups wanted and added, checked; the change it is."""
    await may_change(actor, user)
    wanted = await uow.groups.get_many_by_name(body.groups)
    added = [group for group in wanted if group not in user.groups]
    removed = [group for group in user.groups if group not in wanted]
    await may_grant(actor, _keys(*added))
    await may_hold(actor, await _grants_of(*added))
    change = AdminChange(
        "groups",
        added=frozenset(group.name for group in added),
        removed=frozenset(group.name for group in removed),
    )
    return wanted, added, change


@router.put("/users/{user_id}/permissions", responses=_errors(*CHANGE))
async def set_user_permissions(
    user_id: UUID,
    body: PermissionsIn,
    actor: Annotated[AnyUser, _user_change("change")],
) -> AdminUserOut:
    """Grant the user exactly ``permissions`` directly (groups add theirs)."""
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        wanted, added, change = await _granting(uow, actor, user, body)
        await _admin_change(actor, user, change, uow, lambda: _user_act(user, change))
        user.revoke(*[p for p in user.user_permissions if p not in wanted])
        user.grant(*added)
        await uow.commit()
    return AdminUserOut.of(user)


@router.post(
    "/users/{user_id}/permissions/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_set_user_permissions(
    user_id: UUID,
    body: PermissionsIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _users("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of granting a user exactly ``permissions``."""
    _asking()
    async with AuthUnitOfWork() as uow:
        user = await _user(uow, user_id)
        _, _, change = await _granting(uow, actor, user, body)
    return await _ask(request, response, _user_act(user, change))


async def _granting(
    uow: AuthUnitOfWork, actor: AnyUser, user: AbstractBaseUser, body: PermissionsIn
) -> tuple[list[Permission], list[Permission], AdminChange]:
    """The permissions wanted and added, checked; the change it is."""
    await may_change(actor, user)
    wanted = await uow.permissions.get_many(body.permissions)
    added = [
        permission for permission in wanted if permission not in user.user_permissions
    ]
    removed = [p for p in user.user_permissions if p not in wanted]
    await may_grant(actor, {permission.key for permission in added})
    change = AdminChange(
        "permissions",
        added=frozenset(permission.key for permission in added),
        removed=frozenset(permission.key for permission in removed),
    )
    return wanted, added, change


@router.post("/groups", status_code=status.HTTP_201_CREATED, responses=_errors(*CHANGE))
async def create_group(
    body: GroupIn, actor: Annotated[AnyUser, _group_change("add")]
) -> GroupOut:
    """Create a group with ``permissions``."""
    async with AuthUnitOfWork() as uow:
        permissions = await _creating_group(uow, actor, body)
        keys = {permission.key for permission in permissions}
        await _spend(
            uow, lambda: _group_act("group_create", name=body.name, added=keys)
        )
        group = await uow.groups.create(body.name)
        group.grant(*permissions)
        await uow.commit()
    return GroupOut.of(group)


@router.post(
    "/groups/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_create_group(
    body: GroupIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _groups("add")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of creating this group (``AUTH.ADMIN_ACTION``)."""
    _asking()
    async with AuthUnitOfWork() as uow:
        permissions = await _creating_group(uow, actor, body)
    keys = {permission.key for permission in permissions}
    act = _group_act("group_create", name=body.name, added=keys)
    return await _ask(request, response, act)


async def _creating_group(
    uow: AuthUnitOfWork, actor: AnyUser, body: GroupIn
) -> list[Permission]:
    """The permissions a new group gets, if ``actor`` may grant them."""
    permissions = await uow.permissions.get_many(body.permissions)
    await may_grant(actor, {permission.key for permission in permissions})
    return permissions


@router.patch("/groups/{group_id}", responses=_errors(*CHANGE))
async def rename_group(
    group_id: UUID, body: GroupPatch, actor: Annotated[AnyUser, _group_change("change")]
) -> GroupOut:
    """Rename a group."""
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
        await _spend(uow, lambda: _group_act("group_rename", group.id, name=body.name))
        await uow.groups.rename(group, body.name)
        await uow.commit()
    return GroupOut.of(group)


@router.post(
    "/groups/{group_id}/authorization/patch",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_rename_group(
    group_id: UUID,
    body: GroupPatch,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _groups("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of renaming a group (``PATCH``)."""
    _asking()
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
    act = _group_act("group_rename", group.id, name=body.name)
    return await _ask(request, response, act)


@router.put("/groups/{group_id}/permissions", responses=_errors(*CHANGE))
async def set_group_permissions(
    group_id: UUID,
    body: PermissionsIn,
    actor: Annotated[AnyUser, _group_change("change")],
) -> GroupOut:
    """Grant the group exactly ``permissions``; its members gain and lose them."""
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        wanted, added, removed = await _granting_group(uow, actor, group, body)
        await _spend(uow, lambda: _granted_to(group, added, removed))
        group.revoke(*[p for p in group.permissions if p not in wanted])
        group.grant(*added)
        await uow.commit()
    return GroupOut.of(group)


@router.post(
    "/groups/{group_id}/permissions/authorization",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_set_group_permissions(
    group_id: UUID,
    body: PermissionsIn,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _groups("change")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of granting a group exactly ``permissions``."""
    _asking()
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        _, added, removed = await _granting_group(uow, actor, group, body)
    return await _ask(request, response, _granted_to(group, added, removed))


async def _granting_group(
    uow: AuthUnitOfWork, actor: AnyUser, group: Group, body: PermissionsIn
) -> tuple[list[Permission], list[Permission], list[Permission]]:
    """The permissions wanted, added, and taken away, checked."""
    await _may_change_group(actor, group)
    wanted = await uow.permissions.get_many(body.permissions)
    added = [permission for permission in wanted if permission not in group.permissions]
    removed = [p for p in group.permissions if p not in wanted]
    await may_grant(actor, {permission.key for permission in added})
    return wanted, added, removed


def _granted_to(
    group: Group, added: list[Permission], removed: list[Permission]
) -> "AdminAct":
    """The act of granting ``group`` ``added`` and taking ``removed`` away."""
    return _group_act(
        "group_permissions",
        group.id,
        added=(permission.key for permission in added),
        removed=(permission.key for permission in removed),
    )


@router.delete(
    "/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors(*ONE),
)
async def delete_group(
    group_id: UUID, actor: Annotated[AnyUser, _group_change("delete")]
) -> None:
    """Delete a group; its members lose its permissions."""
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
        await _spend(uow, lambda: _group_act("group_delete", group.id))
        group.delete()
        await uow.groups.remove(group)
        await uow.commit()


@router.post(
    "/groups/{group_id}/authorization/delete",
    status_code=status.HTTP_201_CREATED,
    responses=_errors(*ASK),
)
@_twin
async def ask_to_delete_group(
    group_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[AnyUser, _groups("delete")],
) -> ActionAuthorizationOut:
    """Ask for an authorization of deleting a group (``DELETE /groups/{id}``)."""
    _asking()
    async with AuthUnitOfWork() as uow:
        group = await _group(uow, group_id)
        await _may_change_group(actor, group)
    return await _ask(request, response, _group_act("group_delete", group.id))


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


def _asking() -> None:
    """Refuse to ask for an authorization of a change while none is asked.

    Raises:
        APIError: 404 ``not_found``, while ``AUTH.ADMIN_ACTION`` is off: a
            twin answers as a route that is not there.
    """
    if not _asked():
        raise APIError(status.HTTP_404_NOT_FOUND, "Not Found", code="not_found")


async def _ask(
    request: Request, response: Response, act: "AdminAct"
) -> ActionAuthorizationOut:
    """Store an authorization of ``act`` for the staff member; what they are told.

    It is refused as ``request_action_authorization`` refuses: credentials
    that cannot ask, no way to confirm, staff acting as a user.
    """
    no_store(response)
    action = _admin_change_action()
    asked_for = await request_action_authorization(request, action, act)
    return ActionAuthorizationOut.of(asked_for)


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


async def _admin_change(
    actor: AnyUser,
    user: AbstractBaseUser | None,
    change: AdminChange,
    uow: AuthUnitOfWork,
    act: Callable[[], "AdminAct"],
) -> None:
    """Run the domains' ``on_admin_change`` checks; any may refuse ``change``.

    With ``AUTH.ADMIN_ACTION`` on, the request's authorization must bind
    ``act()``, the change as staff authorized it: checked before the checks
    (one may spend an approval), and spent after them, in ``uow``'s
    transaction, so it is spent if and only if the change commits. Off, the
    act is never built.

    Raises:
        ActionNotAuthorized: With ``ADMIN_ACTION``, the authorization is of
            another change (it is voided), or was spent meanwhile.
    """
    made = act() if _asked() else None
    if made is not None:
        await check_authorization(_admin_change_action(), made)
    for hook in on_admin_change:
        await hook.call(actor, user, change)
    if made is not None:
        await spend_authorization(uow, _admin_change_action(), made)


async def _spend(uow: AuthUnitOfWork, act: Callable[[], "AdminAct"]) -> None:
    """With ``AUTH.ADMIN_ACTION`` on, spend the request's authorization of ``act()``.

    In ``uow``'s transaction: it is spent if and only if the change commits.

    Raises:
        ActionNotAuthorized: It is of another change (it is voided), or was
            spent meanwhile.
    """
    if _asked():
        await spend_authorization(uow, _admin_change_action(), act())


def _approval_needed() -> None:
    """Refuse making a superuser here where an app holds it (``on_make_superuser``).

    With ``APPROVALS.SUPERUSER_APPROVALS``, an approved request makes them.
    """
    for hook in on_make_superuser:
        hook.call()


def _superusers_only(actor: AnyUser, message: str) -> None:
    """Refuse a step only superusers may take."""
    if not actor.is_superuser:
        raise PermissionDenied(message)


async def may_change(actor: AnyUser, user: AbstractBaseUser) -> None:
    """Refuse to change a user who holds more than ``actor``.

    Changing a password or logging a user out is as good as becoming them,
    so staff only change users whose every grant (direct or through groups,
    active or not, and from every backend in ``AUTH.BACKENDS``) they have
    themselves; only superusers change superusers. Other backends grant an
    inactive user nothing, so only superusers change inactive users while
    such backends are configured: what the user would hold is unknown.
    Becoming a user also hands over their memberships: staff change no one
    who belongs to a tenant they do not, or holds keys there they lack
    (``tenants_beyond``); while ``TENANCY.MEMBER_PERMISSIONS`` is set
    without ``MEMBER_TENANTS``, only superusers change users (ADR 0150).
    Staff who hold ``TENANCY.ALL_TENANTS_PERMISSION`` everywhere change a
    tenant's member they do not share it with, when they hold everywhere
    what the member holds there (ADR 0177).
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
    refusal = "You can only change users whose permissions you have"
    await may_grant(actor, granted | _keys(*user.groups), refusal)
    await may_hold(actor, await object_grants(user), refusal)
    await _may_change_member(actor, user, refusal)


async def _may_change_member(
    actor: AnyUser, user: AbstractBaseUser, refusal: str
) -> None:
    """Refuse to change a user who holds, in some tenant, what ``actor`` does not."""
    if actor.is_superuser:
        return
    beyond = await tenants_beyond(actor, user, await get_all_permissions(actor))
    if beyond is None:
        _superusers_only(
            actor,
            "Only superusers may change users while TENANCY.MEMBER_TENANTS does "
            "not say which tenants a user belongs to.",
        )
    elif beyond:
        # How many, not which: staff need not learn where the user belongs.
        raise PermissionDenied(
            f"{refusal}; you lack what they hold in {len(beyond)} of their tenants."
        )


async def _replaced(
    uow: AuthUnitOfWork, user: AbstractBaseUser, before: dict[str, object]
) -> None:
    """End what a staff change replaced: a value held, codes sent to the old one.

    Staff set an address or a number at once, unproved (ADR 0153). A new
    one the user held for that field, waiting for its code, is dropped,
    and the codes sent to the replaced one no longer open the account.
    """
    from .codes import targets_of  # Loaded once codes are used (AuthUnitOfWork).

    replaced = {
        name: old for name, old in before.items() if getattr(user, name, None) != old
    }
    if not replaced:
        return
    await uow.pending_identifiers.drop(user.id, *replaced)
    if targets := [
        target
        for old in replaced.values()
        if old
        for target in targets_of(str(old), user.id)
    ]:
        await uow.verification_codes.forget(user.id, targets)


async def _not_the_last_superuser(uow: AuthUnitOfWork) -> None:
    """Refuse to demote, deactivate, or delete the last active superuser."""
    if await uow.users.active_superusers() <= 1:
        raise LastSuperuser(
            "This is the last active superuser; make another one first."
        )


async def _may_change_group(actor: AnyUser, group: Group) -> None:
    """Refuse to change a group that grants what ``actor`` lacks, on objects too."""
    refusal = "You can only change groups whose permissions you have"
    await may_grant(actor, group.permission_keys(), refusal)
    await may_hold(actor, await object_grants(group), refusal)


async def may_grant(
    actor: AnyUser,
    keys: Iterable[str],
    refusal: str = "You can only grant permissions you have",
) -> None:
    """Refuse to hand out (or take away) permissions ``actor`` does not have."""
    if actor.is_superuser:
        return
    if missing := sorted(set(keys) - await get_all_permissions(actor)):
        raise PermissionDenied(f"{refusal}; you lack {', '.join(missing)}.")


async def may_hold(
    actor: AnyUser,
    grants: Iterable[ObjectGrant],
    refusal: str = "You can only grant permissions you have",
) -> None:
    """Refuse to hand out (or touch) grants on objects ``actor`` does not hold.

    A grant is held through its permission on the whole model, or the same
    grant on the object (``object_grants``). Superusers hold every one.
    While a backend does not list its grants (``object_grants`` left as
    ``BaseBackend``'s), only superusers pass (SEC-16a-5). A refusal names
    how many grants are lacked, not the objects.
    """
    if actor.is_superuser:
        return
    if unlisted := backends_without_object_grants():
        raise PermissionDenied(
            "Only superusers may change users and groups while a backend does "
            f"not list its grants on objects (object_grants): {', '.join(unlisted)}."
        )
    wanted = set(grants)
    if not wanted:
        return
    model = await get_all_permissions(actor)
    # Staff are stored users: the routes' rules refuse anyone else.
    held = await object_grants(cast(AbstractBaseUser, actor))
    lacked = {grant for grant in wanted if grant.permission not in model} - held
    if lacked:
        # How many, not which: staff need not learn where the target holds grants.
        raise PermissionDenied(
            f"{refusal}; you lack {len(lacked)} of its grants on single objects."
        )


async def _grants_of(*groups: Group) -> set[ObjectGrant]:
    """Every grant on objects the groups hold."""
    return {grant for group in groups for grant in await object_grants(group)}


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
    return typed_fields(model, fields)


def _has_field(model: type, name: str) -> bool:
    """Whether the user model stores ``name``."""
    return any(each.name == name for each in dataclasses.fields(model))
