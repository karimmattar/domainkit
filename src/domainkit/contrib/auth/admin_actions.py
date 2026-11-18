"""The admin API's changes, each authorized as one act: ``auth.admin_change``.

With ``AUTH.ADMIN_ACTION`` on (ADR 0163), every route of the admin API
that changes a user or a group asks for an authorization of that change
(``Action-Authorization``, ADR 0144), in place of a step-up window. Staff
ask for one at the route's twin, ``POST <its path>/authorization``
(``<its path>/authorization/patch`` and ``.../delete`` where a ``PATCH``
and a ``DELETE`` share a path), confirm it with a code, the phone, or a
passkey, and send it with the change, which spends it once, with its
commit. The admin API imports this module only while the setting is on.

The act's arguments are the change (``AdminAct``): its kind, the user or
group, and what it sets, adds, and takes away, sorted; never a password.
Each group a change adds is bound by its id and what it grants, and each
it takes away by its id (since 2.8, user report A17). What staff are
shown is built here, from the act and the stored user or group: which
one, and what changes.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from domainkit.i18n import gettext, gettext_noop, pgettext
from domainkit.utils.decimals import jsonable

from ._shown import FIELDS as FIELDS
from ._shown import field_label as field_label
from ._shown import (
    grants_digest,
    group_item,
    identifying_fields,
    named_first,
    named_permissions,
)
from .actions import Action, Shown, spent_in_code
from .exceptions import GroupNotFound, UserNotFound
from .gates import (
    AddedGroup,
    AdminChange,
    AdminKind,
    GroupChange,
    GroupKind,
    on_admin_group_screen,
    on_admin_screen,
)
from .models import AbstractBaseUser, Group, GroupId, UserId
from .screens import Typed, Value, fitted, laid_out, named
from .settings import auth_settings
from .uow import AuthUnitOfWork

PURPOSE: Final = "auth.admin_change"
"""The action each change of the admin API is, with ``ADMIN_ACTION`` on."""
NEW: Final = "new"
"""The id in the subject of a user or a group being created."""
FLAGS: Final = {
    "is_superuser": gettext_noop("Superuser"),
    "is_staff": gettext_noop("Staff"),
    "is_active": gettext_noop("Active"),
}
"""The flags a change sets that grant or take access, and their labels: shown first."""
FLAG_STATES: Final = {True: "yes", False: "no"}
"""What a flag set is shown as: in the context ``flag``."""
GROUP_KINDS: Final = frozenset(
    {"group_create", "group_rename", "group_permissions", "group_delete"}
)
"""The kinds of change made to a group, not to a user."""
NAMING_KINDS: Final = frozenset({"group_create", "group_rename"})
"""The kinds of change whose ``name`` staff type: a group's new name (since 2.8)."""

ActKind = Literal[
    "create",
    "update",
    "delete",
    "password",
    "logout",
    "impersonate",
    "groups",
    "permissions",
    "group_create",
    "group_rename",
    "group_permissions",
    "group_delete",
]
"""A change to a user (``AdminChange.kind``), or to a group."""

TITLES: Final[dict[str, str]] = {
    "create": gettext_noop("Create a user"),
    "update": gettext_noop("Change a user"),
    "delete": gettext_noop("Delete a user"),
    "password": gettext_noop("Set a user's password"),
    "logout": gettext_noop("End a user's sessions"),
    "impersonate": gettext_noop("Act as a user"),
    "groups": gettext_noop("Change a user's groups"),
    "permissions": gettext_noop("Change a user's permissions"),
    "group_create": gettext_noop("Create a group"),
    "group_rename": gettext_noop("Rename a group"),
    "group_permissions": gettext_noop("Change a group's permissions"),
    "group_delete": gettext_noop("Delete a group"),
}
"""What the screen says above each kind of change."""
UNUSABLE: Final = gettext_noop("Make a user's password unusable")
"""The title of a password change that leaves no usable password."""
USER: Final = gettext_noop("User")
GROUP: Final = gettext_noop("Group")
USERNAME: Final = gettext_noop("Username")
NEW_NAME: Final = gettext_noop("New name")
REASON: Final = gettext_noop("Reason")
ADDS: Final = gettext_noop("Adds")
GRANTS: Final = gettext_noop("Grants")
"""The label of the keys ``NAMED_PERMISSIONS`` names among those the groups
a change adds grant (since 2.8)."""
TAKES_AWAY: Final = gettext_noop("Takes away")
PASSWORD: Final = gettext_noop("Password")
PASSWORD_STATES: Final = {True: "set", False: "none"}
"""Whether a user is created with a password: in the context ``password``."""


class ActGroup(BaseModel):
    """A group a change adds a user to, as its act binds it (since 2.8).

    Its id, its name, the keys of the permissions it grants, sorted, and
    its grants on single objects: how many, and their digest (SHA-256 of
    them, sorted; empty for none), as approvals' ``HeldGroup``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    permissions: tuple[str, ...]
    object_grants: int = 0
    object_grants_digest: str = ""

    @classmethod
    def of(cls, group: AddedGroup) -> "ActGroup":
        """``group`` as the admin API read it, bound."""
        count, digest = grants_digest(group.object_grants)
        return cls(
            id=group.id,
            name=group.name,
            permissions=tuple(sorted(group.permissions)),
            object_grants=count,
            object_grants_digest=digest,
        )


BOUND_GROUPS: Final = ("groups", "taken")
"""The fields an act of 2.7 did not have: left out of its arguments when empty."""


class AdminAct(BaseModel):
    """A change of the admin API, as its authorization binds it: what, and to whom.

    ``kind`` is the change's (``AdminChange.kind``), or a group's
    (``group_create``, ``group_rename``, ``group_permissions``,
    ``group_delete``). ``user_id`` or ``group_id`` names what it changes;
    ``name`` is the username a user is created with, or the name a group
    is created or renamed with. ``values`` are what it sets, as JSON
    (``AdminChange.values``: fields, flags, an impersonation's reason);
    ``added`` and ``removed`` the groups or permissions, sorted; and
    ``password`` whether it leaves a usable password (``create`` and
    ``password``), never the password.

    Since 2.8, a change of a user's groups binds each group it adds,
    sorted by name, by id and what it grants (``groups``, ``ActGroup``),
    and the ids of those it takes away, in the order of ``removed``
    (``taken``): a group renamed, made again, or granted more since the
    staff member confirmed it is another act (user report A17). Both are
    left out of the act's arguments when empty, so the authorization of
    any other change asked for under 2.7 still binds it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ActKind
    user_id: UUID | None = None
    group_id: UUID | None = None
    name: str | None = None
    values: dict[str, JsonValue] = Field(default_factory=dict)
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    password: bool | None = None
    groups: tuple[ActGroup, ...] = ()
    taken: tuple[UUID, ...] = ()

    @model_serializer(mode="wrap")
    def _as_bound(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """The act's arguments: ``groups`` and ``taken`` only where they hold some."""
        dumped: dict[str, Any] = handler(self)
        for name in BOUND_GROUPS:
            if not dumped.get(name):
                dumped.pop(name, None)
        return dumped

    @classmethod
    def of(
        cls,
        user: AbstractBaseUser | None,
        change: AdminChange,
        *,
        name: str | None = None,
        password: bool | None = None,
        values: Mapping[str, object] | None = None,
    ) -> "AdminAct":
        """The act of ``change`` to ``user`` (None for one being created).

        ``values`` adds what the change sets that ``change.values`` leaves
        out: whether a user created is active. A change of groups binds
        each group it adds as the admin API read it (``change.groups``),
        and the id of each of the user's groups it takes away.
        """
        given = {**change.values, **(values or {})}
        removed = tuple(sorted(change.removed))
        taken: tuple[UUID, ...] = ()
        if change.kind == "groups" and user is not None:
            ids = {group.name: group.id for group in user.groups}
            taken = tuple(ids[each] for each in removed if each in ids)
        return cls(
            kind=change.kind,
            user_id=None if user is None else user.id,
            name=name,
            values={key: jsonable(value) for key, value in given.items()},
            added=tuple(sorted(change.added)),
            removed=removed,
            password=password,
            groups=tuple(
                ActGroup.of(group)
                for group in sorted(change.groups, key=lambda group: group.name)
            ),
            taken=taken,
        )

    @classmethod
    def of_group(
        cls,
        kind: ActKind,
        group_id: UUID | None = None,
        *,
        name: str | None = None,
        added: Iterable[str] = (),
        removed: Iterable[str] = (),
    ) -> "AdminAct":
        """The act of a change to a group (``group_id`` None for one being created)."""
        return cls(
            kind=kind,
            group_id=group_id,
            name=name,
            added=tuple(sorted(added)),
            removed=tuple(sorted(removed)),
        )


def subject_of(act: AdminAct) -> str:
    """What the change is on: ``auth.user:<id>``, or ``auth.group:<id>``.

    ``new`` stands for the id of a user or a group being created.
    """
    if act.kind in GROUP_KINDS:
        return f"auth.group:{act.group_id or NEW}"
    return f"auth.user:{act.user_id or NEW}"


async def show(act: AdminAct) -> Shown:  # noqa: C901
    """Which user or group, and what changes, read from the stored one.

    The title says the kind of change; then the user or group, by its
    first identifier or name and the end of its id ("ann@example.com
    (…123456789abc)", the name cut past 48 characters: the act binds the
    id), the name given, the
    flags that grant or take access ("Superuser", "Staff", "Active": yes or
    no), the reason of an impersonation, what is added and taken away (a
    list over as many lines as it takes: "Adds", "Adds (2)"), whether a new
    user gets a password, and the other values set, by field ("Email",
    "Display name"). Laid out on a screen (``laid_out``): with
    ``AUTH.AUTHORIZATIONS.WHOLE`` on, what the change sets, adds, or takes
    away that does not fit whole is refused; off, it is cut and counted,
    the first lines kept for what comes first. Each group a change adds
    shows as approvals shows it, in a few words: "readers (…9abc) 3 perms",
    and the keys ``AUTH.AUTHORIZATIONS.NAMED_PERMISSIONS`` names among
    what they grant show by name first ("Grants"); in a list of keys, those
    it names come first (since 2.8). What staff typed is ``Typed`` (since
    2.8): the reason, a group's new name, and each text a change sets on a
    user but the fields that identify them; such a text sent as it is
    stored (a form saved whole) changes nothing: it is not typed, and with
    ``TYPED_LINKS`` refused not shown. Then the ``on_admin_screen`` checks
    of a change to a user run, or the ``on_admin_group_screen`` checks of a
    change to a stored group (since 2.7): approvals refuses there a change
    it would hold that its approver's screen could not show whole.

    Raises:
        UserNotFound: The user is gone.
        GroupNotFound: The group is gone.
        TooMuchToShow: With ``WHOLE`` on, the change does not fit whole.
        LinkNotShown: With ``TYPED_LINKS`` refused, what staff typed holds
            a link, an email address, or a number to call.
    """
    title = TITLES[act.kind]
    if act.kind == "password" and act.password is False:
        title = UNUSABLE
    details: list[tuple[str, Value]] = []
    user = None if act.user_id is None else await _user(act.user_id)
    if user is not None:
        details.append((gettext(USER), named(user.get_username(), user.id)))
    group = None if act.group_id is None else await _group(act.group_id)
    if group is not None:
        details.append((gettext(GROUP), named(group.name, group.id)))
    if act.name is not None:
        label = {"group_create": GROUP, "group_rename": NEW_NAME}.get(
            act.kind, USERNAME
        )
        typed = Typed(act.name) if act.kind in NAMING_KINDS else act.name
        details.append((gettext(label), typed))
    values = dict(act.values)
    details.extend(
        (gettext(label), _flag(values.pop(flag)))
        for flag, label in FLAGS.items()
        if flag in values
    )
    if act.kind == "impersonate":
        details.append((gettext(REASON), _typed(values.pop("reason", ""))))
    details.extend(_added(act))
    if act.removed:
        details.append((gettext(TAKES_AWAY), act.removed))
    if act.kind == "create" and act.password is not None:
        state = pgettext("password", PASSWORD_STATES[act.password])
        details.append((gettext(PASSWORD), state))
    taken = {label for label, _ in details}
    identifiers = identifying_fields()
    refused = auth_settings().AUTHORIZATIONS.TYPED_LINKS == "refused"
    for name, value in sorted(values.items()):
        label = field_label(name)
        label = name if label in taken else label  # A field "group" beside "Group".
        if name not in identifiers and isinstance(value, str):
            if user is None or jsonable(getattr(user, name, None)) != value:
                value = Typed(value)
            elif refused:
                continue  # Sent as stored (a form saved whole): it changes nothing.
        details.append((label, value))
        taken.add(label)
    whole = auth_settings().AUTHORIZATIONS.WHOLE
    shown = Shown(fitted(gettext(title)), laid_out(details, whole=whole))
    if user is not None and act.kind not in GROUP_KINDS:
        change = _change_of(act)
        for hook in on_admin_screen:
            await hook.call(user, change)
    if group is not None:
        group_change = GroupChange(
            cast("GroupKind", act.kind),
            name=act.name,
            added=frozenset(act.added),
            removed=frozenset(act.removed),
        )
        for group_hook in on_admin_group_screen:
            await group_hook.call(group, group_change)
    return shown


def _added(act: AdminAct) -> list[tuple[str, Value]]:
    """What ``act`` adds: the groups in a few words, after the keys named; or keys.

    A change of groups of 2.7's shape, which bound no group, shows the
    names alone. Keys come sorted, those ``NAMED_PERMISSIONS`` names first.
    """
    if act.groups:
        named = named_permissions()
        keys = {key for group in act.groups for key in group.permissions}
        items = [
            group_item(
                group.name, group.id, len(group.permissions), group.object_grants
            )
            for group in act.groups
        ]
        granted = sorted(keys & named)
        return [
            *([(gettext(GRANTS), granted)] if granted else []),
            (gettext(ADDS), items),
        ]
    if not act.added:
        return []
    if act.kind == "groups":
        return [(gettext(ADDS), act.added)]
    return [(gettext(ADDS), named_first(act.added))]


def _change_of(act: AdminAct) -> AdminChange:
    """The ``AdminChange`` ``act`` binds, as the admin API's checks see it."""
    fields = frozenset(act.values) if act.kind in ("create", "update") else frozenset()
    return AdminChange(
        cast("AdminKind", act.kind),
        fields,
        added=frozenset(act.added),
        removed=frozenset(act.removed),
        values=dict(act.values),
    )


def _typed(value: JsonValue) -> JsonValue:
    """``value`` marked ``Typed`` if it is text: staff wrote it."""
    return Typed(value) if isinstance(value, str) else value


def _flag(value: JsonValue) -> JsonValue:
    """A flag's value: "yes" or "no", in the request's language; else as given."""
    if isinstance(value, bool):
        return pgettext("flag", FLAG_STATES[value])
    return value


async def _user(user_id: UUID) -> AbstractBaseUser:
    """The user the change is to, as stored.

    Raises:
        UserNotFound: There is none.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
    if user is None:
        raise UserNotFound("No such user.")
    return user


async def _group(group_id: UUID) -> Group:
    """The group the change is to, as stored.

    Raises:
        GroupNotFound: There is none.
    """
    async with AuthUnitOfWork() as uow:
        group = await uow.groups.find(GroupId(group_id))
    if group is None:
        raise GroupNotFound("No such group.")
    return group


ADMIN_CHANGE: Final = spent_in_code(
    Action(PURPOSE, command=AdminAct, subject=subject_of, show=show)
)
"""A change of the admin API, spent with it (``admin._admin_change``).

Imported only once something asks for it (``AUTH.ADMIN_ACTION`` on): the
admin API loads none of this module while the setting is off."""
