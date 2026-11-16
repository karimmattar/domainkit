"""The admin API's changes, each authorized as one act: ``auth.admin_change``.

With ``AUTH.ADMIN_ACTION`` on (ADR 0163), every route of the admin API
that changes a user or a group asks for an authorization of that change
(``Action-Authorization``, ADR 0144), in place of a step-up window. Staff
ask for one at the route's twin, ``POST <its path>/authorization``
(``POST <its path>/delete/authorization`` for a ``DELETE``), confirm it
with a code, the phone, or a passkey, and send it with the change, which
spends it once, with its commit.

The act's arguments are the change (``AdminAct``): its kind, the user or
group, and what it sets, adds, and takes away, sorted; never a password.
What staff are shown is built here, from the act and the stored user or
group: which one, and what changes.
"""

import json
from collections.abc import Iterable
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.requests import HTTPConnection

from domainkit.http.access import BasePermission, Principal
from domainkit.i18n import gettext, gettext_noop, ngettext, pgettext
from domainkit.utils.decimals import jsonable

from .access import StepUp
from .actions import Action, Shown, spent_in_code
from .exceptions import GroupNotFound, UserNotFound
from .gates import AdminChange
from .models import AbstractBaseUser, GroupId, UserId
from .repositories.authorizations import DETAIL_MAX_LENGTH, MAX_DETAILS
from .settings import auth_settings
from .uow import AuthUnitOfWork

PURPOSE: Final = "auth.admin_change"
"""The action each change of the admin API is, with ``ADMIN_ACTION`` on."""
NEW: Final = "new"
"""The id in the subject of a user or a group being created."""
GROUP_KINDS: Final = frozenset(
    {"group_create", "group_rename", "group_permissions", "group_delete"}
)
"""The kinds of change made to a group, not to a user."""

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
MORE_LABEL: Final = "…"
"""The label of the line that says how many values are not shown."""
CUT: Final = "…"
"""What ends a text cut to fit."""


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
    ) -> "AdminAct":
        """The act of ``change`` to ``user`` (None for one being created)."""
        return cls(
            kind=change.kind,
            user_id=None if user is None else user.id,
            name=name,
            values={key: jsonable(value) for key, value in change.values.items()},
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

    The title says the kind of change; then the user or group by its
    first identifier or name, the name given, the reason of an
    impersonation, what is added and taken away, whether a new user gets a
    password, and the values set, by field. Six values at most: with more,
    the last line says how many are not shown.

    Raises:
        UserNotFound: The user is gone.
        GroupNotFound: The group is gone.
    """
    title = TITLES[act.kind]
    if act.kind == "password" and act.password is False:
        title = UNUSABLE
    leading: list[tuple[str, str]] = []
    if act.user_id is not None:
        leading.append((gettext(USER), await _user_named(act.user_id)))
    if act.group_id is not None:
        leading.append((gettext(GROUP), await _group_named(act.group_id)))
    if act.name is not None:
        label = {"group_create": GROUP, "group_rename": NEW_NAME}.get(
            act.kind, USERNAME
        )
        leading.append((gettext(label), act.name))
    values = dict(act.values)
    if act.kind == "impersonate":
        leading.append((gettext(REASON), _text(values.pop("reason", ""))))
    if act.added:
        leading.append((gettext(ADDS), ", ".join(act.added)))
    if act.removed:
        leading.append((gettext(TAKES_AWAY), ", ".join(act.removed)))
    if act.kind == "create" and act.password is not None:
        state = pgettext("password", PASSWORD_STATES[act.password])
        leading.append((gettext(PASSWORD), state))
    named = [(name, _text(value)) for name, value in sorted(values.items())]
    return Shown(fitted(gettext(title)), fit(leading, named))


async def _user_named(user_id: UUID) -> str:
    """How the screen names the user: their first identifier (``get_username``).

    Raises:
        UserNotFound: There is none.
    """
    async with AuthUnitOfWork() as uow:
        user = await uow.users.find(UserId(user_id))
    if user is None:
        raise UserNotFound("No such user.")
    return user.get_username()


async def _group_named(group_id: UUID) -> str:
    """The group's name.

    Raises:
        GroupNotFound: There is none.
    """
    async with AuthUnitOfWork() as uow:
        group = await uow.groups.find(GroupId(group_id))
    if group is None:
        raise GroupNotFound("No such group.")
    return group.name


def fit(
    leading: list[tuple[str, str]], named: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """``leading``, then ``named``, each fitted: six labelled values at most.

    A named value whose label is taken already is not shown, and counted.
    With more than fit, the last line says how many are not shown.
    """
    details = [(fitted(label), fitted(value)) for label, value in leading]
    labels = {label for label, _ in details} | {MORE_LABEL}
    kept = []
    for name, value in named:
        label = fitted(name)
        if label and label not in labels:
            labels.add(label)
            kept.append((label, fitted(value)))
    room = MAX_DETAILS - len(details)
    if len(kept) == len(named) and len(kept) <= room:
        return [*details, *kept]
    shown = kept[: room - 1]
    hidden = len(named) - len(shown)
    more = ngettext("{count} more value", "{count} more values", hidden)
    return [*details, *shown, (MORE_LABEL, fitted(more.format(count=hidden)))]


def _text(value: object) -> str:
    """``value`` as text: a string as itself, anything else as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fitted(text: str) -> str:
    """``text`` as a screen shows it: printable, at most 64 characters.

    A character that is not printable is shown escaped, as Python writes
    it; a longer text is cut, and ends with ``…``.
    """
    printable = "".join(
        each if each.isprintable() else each.encode("unicode_escape").decode("ascii")
        for each in text
    )
    if len(printable) <= DETAIL_MAX_LENGTH:
        return printable
    return printable[: DETAIL_MAX_LENGTH - len(CUT)] + CUT


ADMIN_CHANGE: Final = spent_in_code(
    Action(PURPOSE, command=AdminAct, subject=subject_of, show=show)
)
"""A change of the admin API, spent with it (``admin._admin_change``)."""


def asked() -> bool:
    """Whether the admin API's changes ask for authorizations (``ADMIN_ACTION``)."""
    return auth_settings().ADMIN_ACTION


class AdminChangeAuthorized(BasePermission):
    """With ``AUTH.ADMIN_ACTION`` on: the change holds an authorization of its own.

    It asks what ``StepUp(for_="auth.admin_change")`` asks (ADR 0144): an
    ``Action-Authorization`` header holding a confirmed authorization of
    the staff member's, of their login. ``401 authorization_required``
    without one, ``403 action_not_authorized`` with one that does not hold;
    staff acting as a user are refused. The route checks that it binds
    the change it makes, when it makes it. Off, it allows everyone.
    """

    def __init__(self) -> None:
        """Ask nothing until the setting is on."""
        self._rule: BasePermission | None = None

    def asking(self) -> BasePermission | None:
        """``StepUp(for_="auth.admin_change")``, while the setting is on."""
        if not asked():
            return None
        if self._rule is None:
            self._rule = StepUp(for_=ADMIN_CHANGE)
        return self._rule

    async def has_permission(self, request: HTTPConnection, user: Principal) -> bool:
        """The setting is off, or the request holds an authorization of a change."""
        rule = self.asking()
        return rule is None or await rule.has_permission(request, user)

    async def refusal(
        self, request: HTTPConnection, user: Principal
    ) -> Exception | None:
        """``401 authorization_required`` without the header; else ``403``."""
        rule = self.asking()
        return None if rule is None else await rule.refusal(request, user)
