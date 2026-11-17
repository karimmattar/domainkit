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
What staff are shown is built here, from the act and the stored user or
group: which one, and what changes.
"""

from collections.abc import Iterable, Mapping
from typing import Final, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from domainkit.i18n import gettext, gettext_noop, pgettext
from domainkit.utils.decimals import jsonable

from .actions import Action, Shown, spent_in_code
from .exceptions import GroupNotFound, UserNotFound
from .gates import (
    AdminChange,
    AdminKind,
    GroupChange,
    GroupKind,
    on_admin_group_screen,
    on_admin_screen,
)
from .models import AbstractBaseUser, Group, GroupId, UserId
from .screens import Typed, Value, fitted, laid_out, named
from .settings import auth_settings, identifier_fields
from .uow import AuthUnitOfWork
from .users import get_user_model

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
FIELDS: Final = {
    "email": gettext_noop("Email"),
    "mobile_number": gettext_noop("Mobile number"),
    "first_name": gettext_noop("First name"),
    "last_name": gettext_noop("Last name"),
}
"""The labels of the built-in user's fields; another is named after its field."""
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
TAKES_AWAY: Final = gettext_noop("Takes away")
PASSWORD: Final = gettext_noop("Password")
PASSWORD_STATES: Final = {True: "set", False: "none"}
"""Whether a user is created with a password: in the context ``password``."""


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
        out: whether a user created is active.
        """
        given = {**change.values, **(values or {})}
        return cls(
            kind=change.kind,
            user_id=None if user is None else user.id,
            name=name,
            values={key: jsonable(value) for key, value in given.items()},
            added=tuple(sorted(change.added)),
            removed=tuple(sorted(change.removed)),
            password=password,
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


async def show(act: AdminAct) -> Shown:
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
    the first lines kept for what comes first. What staff typed is
    ``Typed`` (since 2.8): the reason, a group's new name, and each text a
    change sets on a user but the fields that identify them, and those
    sent as they are stored (a form saved whole). Then the
    ``on_admin_screen`` checks of a change to a user run, or the
    ``on_admin_group_screen`` checks of a change to a stored group (since
    2.7): approvals refuses there a change it would hold that its
    approver's screen could not show whole.

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
    if act.added:
        details.append((gettext(ADDS), act.added))
    if act.removed:
        details.append((gettext(TAKES_AWAY), act.removed))
    if act.kind == "create" and act.password is not None:
        state = pgettext("password", PASSWORD_STATES[act.password])
        details.append((gettext(PASSWORD), state))
    taken = {label for label, _ in details}
    identifiers = _identifiers()
    for name, value in sorted(values.items()):
        label = field_label(name)
        label = name if label in taken else label  # A field "group" beside "Group".
        kept = user is not None and jsonable(getattr(user, name, None)) == value
        details.append((label, value if name in identifiers or kept else _typed(value)))
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


def _identifiers() -> frozenset[str]:
    """The user's fields that name them, which a screen shows as set, never as typed.

    The username, the email, and each identifier (``IDENTIFIER_FIELDS``,
    ``AUTH.IDENTIFIERS``): an address or a number there is what is set.
    """
    model = get_user_model()
    named = {model.USERNAME_FIELD, model.EMAIL_FIELD}
    named |= {*getattr(model, "IDENTIFIER_FIELDS", ()), *identifier_fields(model)}
    return frozenset(each for each in named if each)


def _typed(value: JsonValue) -> JsonValue:
    """``value`` marked ``Typed`` if it is text: staff wrote it."""
    return Typed(value) if isinstance(value, str) else value


def field_label(name: str) -> str:
    """How a screen names the user's field ``name``: "Email", "Display name".

    The built-in user's fields have labels in the request's language;
    another is its name, its underscores spaces, its first letter a
    capital, as Django names a field without a ``verbose_name``.
    """
    if name in FIELDS:
        return gettext(FIELDS[name])
    spaced = name.replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:] if spaced else name


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
