"""The guards approvals brings: large postings, captures, reversals; superusers.

``POST_ENTRY`` guards ``accounting.post_entry`` once ``ENTRY_THRESHOLDS``
names amounts: an entry that moves more than its currency's amount (its
debits, per currency; a currency the thresholds do not name, unless
``"*"`` does, is always more: FIN-89) needs ``ENTRY_APPROVALS`` approvals
by holders of ``ENTRY_APPROVERS``, and ``post_entry`` refuses it
otherwise (``approval_required``), through the ledger's ``on_post_entry``
hook. Its makers need ``accounting.add_entry``, as posting does.
``CAPTURE_HOLD`` guards ``accounting.capture_hold`` the same way: a hold
reserves any amount, unguarded, and its capture is what moves it.
``REVERSE_ENTRY`` guards ``accounting.reverse_entry``: a reversal moves as
much as the entry it undoes, so it needs the approval that entry needed
(FIN-88). The ledger's ``on_stage_entry`` hook checks every entry posted,
captured, or reversed, as it is booked, against the thresholds, so no
path around ``post_entry`` books more unapproved.

The run of any approved request whose policy asked at least
``ENTRY_APPROVALS`` approvals passes the thresholds for the entries it
books: a project's own guarded payout, approved by enough people, posts
its entry without a second request (FIN-92).

``GRANT_SUPERUSER`` guards making a user a superuser once
``SUPERUSER_APPROVALS`` is more than 0: the auth admin API refuses to
(``approval_required``), and an approved ``GrantSuperuser`` does it. Only
a superuser submits one, and the run checks that its maker still is;
the approvers are superusers (or the holders of ``SUPERUSER_APPROVERS``),
never the user it promotes.

``ADMIN_APPROVALS`` holds changes of auth's admin API for approval: making
staff (``SET_STAFF``), adding groups (``SET_GROUPS``) or permissions
(``SET_PERMISSIONS``), setting a password (``SET_PASSWORD``),
impersonating (``IMPERSONATE``); since 2.7, making a user who holds access
active again (``REACTIVATE``), and granting a group permissions
(``SET_GROUP_PERMISSIONS``). The admin API asks before each change
(``on_admin_change``, ``on_admin_group_change``), after the project's
checks (``HOLD_ORDER``); for one the setting names, the change is
submitted as the staff member's request and refused
(``approval_pending``), and the same call, made again by them once the
request is approved, makes it, once (a guard ``by_maker``). The
approvers are superusers (or the holders of ``ADMIN_APPROVERS``), never
the user the change is about, nor a member of the group granted.
"""

import hashlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from domainkit import clock
from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.ddd import Command, PermissionDenied, ValueObject, fingerprint
from domainkit.exceptions import ImproperlyConfigured

from .exceptions import ApprovalRequired, MakerNotAllowed
from .guards import (
    Guard,
    Policy,
    Running,
    Service,
    Threshold,
    check,
    current_run,
    guards,
    on_guard,
    requires_approval,
)
from .models import RequestStatus
from .settings import ApprovalsSettings
from .uow import ApprovalsUnitOfWork

if TYPE_CHECKING:
    from domainkit.contrib.accounting import (
        AccountingUnitOfWork,
        CaptureHold,
        Entry,
        Hold,
        PostEntry,
        ReverseEntry,
    )
    from domainkit.contrib.auth import AbstractBaseUser, AuthUnitOfWork, Group
    from domainkit.contrib.auth.backends import ObjectGrant
    from domainkit.contrib.auth.gates import AdminChange, GroupChange

POST_ENTRY: Final = "accounting.post_entry"
"""The guarded action of postings over their currency's threshold."""
CAPTURE_HOLD: Final = "accounting.capture_hold"
"""The guarded action of a hold's capture over its currency's threshold."""
REVERSE_ENTRY: Final = "accounting.reverse_entry"
"""The guarded action of a reversal over its currency's threshold."""
GRANT_SUPERUSER: Final = "auth.grant_superuser"
"""The guarded action of making a user a superuser."""
SET_STAFF: Final = "auth.set_staff"
"""The guarded action of making a user staff in the admin API."""
SET_GROUPS: Final = "auth.set_groups"
"""The guarded action of adding a user to groups in the admin API."""
SET_PERMISSIONS: Final = "auth.set_permissions"
"""The guarded action of granting a user permissions in the admin API."""
SET_PASSWORD: Final = "auth.set_password"  # noqa: S105 - an action, not a secret.
"""The guarded action of setting a user's password in the admin API."""
IMPERSONATE: Final = "auth.impersonate"
"""The guarded action of acting as a user in the admin API."""
REACTIVATE: Final = "auth.reactivate"
"""The guarded action of making a user who holds access active again (since 2.7)."""
SET_GROUP_PERMISSIONS: Final = "auth.set_group_permissions"
"""The guarded action of granting a group permissions in the admin API (since 2.7)."""
HOLD_ORDER: Final = 100
"""The ``order`` approvals' checks of admin changes register with (since 2.7).

After the checks registered without one, so a change a project refuses
spends no approval (user report A19)."""


@requires_approval(POST_ENTRY)
async def approved_entry(command: "PostEntry", uow: "AccountingUnitOfWork") -> "Entry":
    """Post an approved entry: the service ``POST_ENTRY`` runs (``post_entry``)."""
    from domainkit.contrib.accounting import post_entry

    return await post_entry(command, uow)


@requires_approval(CAPTURE_HOLD)
async def approved_capture(
    command: "CaptureHold", uow: "AccountingUnitOfWork"
) -> "Hold":
    """Capture an approved hold: what ``CAPTURE_HOLD`` runs (``capture_hold``)."""
    from domainkit.contrib.accounting import capture_hold

    return await capture_hold(command, uow)


@requires_approval(REVERSE_ENTRY)
async def approved_reversal(
    command: "ReverseEntry", uow: "AccountingUnitOfWork"
) -> "Entry":
    """Post an approved reversal: what ``REVERSE_ENTRY`` runs (``reverse_entry``)."""
    from domainkit.contrib.accounting import reverse_entry

    return await reverse_entry(command, uow)


_threshold: tuple[ApprovalsSettings, Threshold] | None = None
"""The thresholds last read, with the settings they were read from."""


def current_threshold() -> Threshold:
    """``ENTRY_THRESHOLDS`` of the settings now, read again when they change.

    A test that changes them through ``override_settings`` or the
    ``settings`` fixture is held to the new amounts (TEST-15).
    """
    global _threshold
    config = registry.settings(ApprovalsSettings)
    kept = _threshold
    if kept is None or kept[0] is not config:
        kept = _threshold = (config, Threshold(config.ENTRY_THRESHOLDS))
    return kept[1]


def guard_entries(config: ApprovalsSettings) -> None:
    """Guard postings, captures, and reversals over ``ENTRY_THRESHOLDS``.

    Each refuses what is over them unapproved. The guards' services are
    the ledger's, decorated here (``approved_entry`` and the others), so
    every registered guard is decorated (DSGN-18); the ledger itself is
    held by its hooks.

    Raises:
        ImproperlyConfigured: A threshold is not an amount of its currency.
    """
    from domainkit.contrib.accounting import (
        CAPTURE_SOURCE,
        AccountingUnitOfWork,
        CaptureHold,
        PostEntry,
        ReverseEntry,
        Side,
    )
    from domainkit.contrib.accounting.hooks import on_post_entry, on_stage_entry

    Threshold(config.ENTRY_THRESHOLDS)  # A wrong amount fails setup().

    def large(command: PostEntry) -> bool:
        return current_threshold().exceeded_by(
            line.amount for line in command.lines if line.side is Side.DEBIT
        )

    def policy(name: str, **options: Any) -> Policy:  # noqa: ANN401
        return Policy(
            name,
            approvals=config.ENTRY_APPROVALS,
            permission=config.ENTRY_APPROVERS,
            **options,
        )

    on_guard(
        Guard(
            POST_ENTRY,
            PostEntry,
            approved_entry,
            AccountingUnitOfWork,
            (policy("accounting.large_entry", when=large),),
            permission="accounting.add_entry",
        )
    )
    on_guard(
        Guard(
            CAPTURE_HOLD,
            CaptureHold,
            approved_capture,
            AccountingUnitOfWork,
            (policy("accounting.large_capture"),),
            permission="accounting.capture_hold",
        )
    )
    on_guard(
        Guard(
            REVERSE_ENTRY,
            ReverseEntry,
            approved_reversal,
            AccountingUnitOfWork,
            (policy("accounting.large_reversal"),),
            permission="accounting.reverse_entry",
        )
    )

    async def check_booking(uow: object, entry: "Entry", accounts: object) -> None:
        """Refuse an entry over its threshold that no approved request books.

        Every entry ``post_entry``, ``capture_hold``, and ``reverse_entry``
        book comes here, whatever the command looked like: a reversal
        moves what its entry moved, back (FIN-88).
        """
        moved = (line.amount for line in entry.lines() if line.side is Side.DEBIT)
        if not current_threshold().exceeded_by(moved) or _approved_booking(entry):
            return
        raise ApprovalRequired("This command needs approval: submit it for approval.")

    def _approved_booking(entry: "Entry") -> bool:
        """Whether ``entry`` is what the approved request being run books.

        A run of the ledger's own guards books what it approved: the
        posting of its source and reference, the capture of its hold, the
        reversal of its entry. A run of another guard books what its
        command posts, if its policy asked enough approvals (FIN-92).
        """
        run = current_run()
        if run is None:
            return False
        command = run.command
        if run.action == POST_ENTRY and isinstance(command, PostEntry):
            return (command.source, command.reference) == (
                entry.source,
                entry.reference,
            )
        if run.action == CAPTURE_HOLD and isinstance(command, CaptureHold):
            return (entry.source, entry.reference) == (
                CAPTURE_SOURCE,
                str(command.hold_id),
            )
        if run.action == REVERSE_ENTRY and isinstance(command, ReverseEntry):
            return entry.reverses == command.entry_id
        return run.action not in LEDGER_ACTIONS and approved_run(run)

    async def check_entry(command: PostEntry) -> None:
        """Refuse a posting over its threshold that no approved request runs."""
        run = current_run()
        if run is None or run.action in LEDGER_ACTIONS or not approved_run(run):
            await check(POST_ENTRY, command)

    on_post_entry(check_entry)
    on_stage_entry(check_booking)


LEDGER_ACTIONS: Final = frozenset({POST_ENTRY, CAPTURE_HOLD, REVERSE_ENTRY})
"""The ledger's own guarded actions, whose runs book only what they approved."""


def approved_run(run: Running) -> bool:
    """Whether the approved request ``run`` had as many approvals as entries need.

    Its policy asked at least ``ENTRY_APPROVALS`` approvals (of the settings
    now, as the thresholds are read: TEST-15): what another guard's command
    books passes the thresholds (FIN-92).
    """
    return run.required >= registry.settings(ApprovalsSettings).ENTRY_APPROVALS


class GrantSuperuser(Command):
    """Make a user a superuser: they hold every permission."""

    user_id: UUID


@requires_approval(GRANT_SUPERUSER)
async def grant_superuser(command: GrantSuperuser, uow: "AuthUnitOfWork") -> None:
    """Make the user a superuser, once approved, if its maker still is one.

    Raises:
        MakerNotAllowed: The run's maker is not an active superuser (any
            more), or it is not run from an approved request.
        UserNotFound: No user has that id.
    """
    from domainkit.contrib.auth import UserId

    run = current_run()
    maker_id = None if run is None or run.maker is None else _user_id(run.maker)
    async with uow:
        maker = None if maker_id is None else await uow.users.find(maker_id)
        if maker is None or not (maker.is_active and maker.is_superuser):
            raise MakerNotAllowed("Only a superuser may make a superuser.")
        user = await uow.users.get(UserId(command.user_id))
        user.set_superuser(True)
        await uow.commit()


def _user_id(text: str) -> Any:  # noqa: ANN401 - a user's id, or None.
    """``text`` as a user's id, or None if it is none."""
    from domainkit.contrib.auth import UserId

    try:
        return UserId(UUID(text))
    except ValueError:
        return None


def _promoted(command: GrantSuperuser) -> list[str]:
    """The user a grant promotes: they do not decide it."""
    return [str(command.user_id)]


def guard_superusers(config: ApprovalsSettings) -> None:
    """Guard making superusers: superusers submit, others of them approve.

    The approvers are superusers, or the holders of ``SUPERUSER_APPROVERS``
    where it is set; never the maker, nor the user promoted.
    """
    from domainkit.contrib.auth import AuthUnitOfWork, IsSuperuser

    approvers: Mapping[str, Any] = (
        {"approver_rule": IsSuperuser()}
        if config.SUPERUSER_APPROVERS is None
        else {"permission": config.SUPERUSER_APPROVERS}
    )
    on_guard(
        Guard(
            GRANT_SUPERUSER,
            GrantSuperuser,
            grant_superuser,
            AuthUnitOfWork,
            (
                Policy(
                    "auth.superuser",
                    approvals=config.SUPERUSER_APPROVALS,
                    **approvers,
                ),
            ),
            maker_rule=IsSuperuser(),
            parties=_promoted,
        )
    )


def refuse_ungoverned_superusers() -> None:
    """Refuse making a superuser outside approvals, where a guard holds it.

    Auth's admin API asks before it makes one (``auth.gates.on_make_superuser``).

    Raises:
        ApprovalRequired: A guard is registered as ``GRANT_SUPERUSER``.
    """
    refuse_if_guarded(GRANT_SUPERUSER)


def refuse_if_guarded(action: str) -> None:
    """Refuse ``action`` done outside approvals, if a guard is registered for it.

    For the paths that do a guarded action without its service, such as
    the auth admin API making a superuser.

    Raises:
        ApprovalRequired: A guard is registered as ``action``.
    """
    from .guards import guards

    if action in guards():
        raise ApprovalRequired("This command needs approval: submit it for approval.")


class SetStaff(Command):
    """Make a user staff, in the admin API (``PATCH /users/{id}``).

    Where ``auth.reactivate`` is not held, it is also making a staff member
    active again: their return gives staff back (since 2.7).
    """

    user_id: UUID


class HeldGroup(ValueObject):
    """A group a ``SetGroups`` adds, as it was when the change was asked.

    Its id, its name, the keys of the permissions it grants, sorted, and
    its grants on single objects: how many, and their digest (SHA-256 of
    them, sorted; empty for none). Since 2.7, user report A21.
    """

    id: UUID
    name: str
    permissions: tuple[str, ...]
    object_grants: int = 0
    object_grants_digest: str = ""


class SetGroups(Command):
    """Add a user to groups, and take them out of others, in the admin API.

    ``added`` and ``removed`` are what ``PUT /users/{id}/groups`` changes,
    by name, sorted: the change itself, not the list sent. ``groups``
    holds each group added, by name, as ``HeldGroup`` (since 2.7): its id
    and the permissions it grants. An approval grants the groups it
    held: a group renamed, made again, or granted other permissions since
    makes another command, which waits for an approval of its own.
    """

    user_id: UUID
    added: tuple[str, ...]
    removed: tuple[str, ...] = ()
    groups: tuple[HeldGroup, ...] = ()


class SetPermissions(Command):
    """Grant a user permissions, and take others away, in the admin API.

    ``added`` and ``removed`` are what ``PUT /users/{id}/permissions``
    changes, by key, sorted.
    """

    user_id: UUID
    added: tuple[str, ...]
    removed: tuple[str, ...] = ()


class SetPassword(Command):
    """Set a user's password, or make it unusable, in the admin API.

    It names the user alone: the password is never stored, and approvers
    could not judge it.
    """

    user_id: UUID


class Impersonate(Command):
    """Act as a user, for ``reason``, in the admin API."""

    user_id: UUID
    reason: str


class Reactivate(Command):
    """Make a user who holds access active again, in the admin API (since 2.7).

    ``PATCH /users/{id}`` with ``"is_active": true``, for an inactive user
    who is staff or a superuser, belongs to a group, or holds a
    permission, directly or on an object: they get it all back. It holds
    what they come back with, as the change leaves it: the flags, each
    group (``HeldGroup``), their own permissions, sorted, and their grants
    on single objects (how many, and their digest). Anything granted since
    makes another command, which waits for its own approval.
    """

    user_id: UUID
    is_staff: bool
    is_superuser: bool
    groups: tuple[HeldGroup, ...]
    permissions: tuple[str, ...]
    object_grants: int
    object_grants_digest: str


class SetGroupPermissions(Command):
    """Grant a group permissions, and take others away, in the admin API.

    ``added`` and ``removed`` are what ``PUT /groups/{id}/permissions``
    changes, by key, sorted; ``name`` is the group's. Every member gains
    what it adds, so none of them decides it (since 2.7).
    """

    group_id: UUID
    name: str
    added: tuple[str, ...]
    removed: tuple[str, ...] = ()


ADMIN_COMMANDS: Final[Mapping[str, type[Command]]] = {
    SET_STAFF: SetStaff,
    SET_GROUPS: SetGroups,
    SET_PERMISSIONS: SetPermissions,
    SET_PASSWORD: SetPassword,
    IMPERSONATE: Impersonate,
    REACTIVATE: Reactivate,
    SET_GROUP_PERMISSIONS: SetGroupPermissions,
}
"""The command of each admin action ``ADMIN_APPROVALS`` may name."""
ADMIN_POLICIES: Final[Mapping[str, str]] = {
    SET_STAFF: "auth.staff",
    SET_GROUPS: "auth.groups",
    SET_PERMISSIONS: "auth.permissions",
    SET_PASSWORD: "auth.password",
    IMPERSONATE: "auth.impersonation",
    REACTIVATE: "auth.reactivation",
    SET_GROUP_PERMISSIONS: "auth.group_permissions",
}
"""The policy each admin action's requests wait under."""


def _admin_service(action: str) -> Service:
    """What ``action``'s guard runs: nothing, the admin API's call makes the change.

    The guard is ``by_maker``: its maker's call, made again once the request
    is approved, runs this as the approved request, then makes the change
    itself.
    """

    @requires_approval(action)
    async def approved_change(command: Command, uow: object) -> None:
        """Let the admin API's call make the approved change."""

    return approved_change


ADMIN_SERVICES: Final[Mapping[str, Service]] = {
    action: _admin_service(action) for action in ADMIN_COMMANDS
}
"""Each admin action's decorated service."""


def _about_user(command: Any) -> list[str]:  # noqa: ANN401 - an admin command.
    """The user an admin change is about: they do not decide it."""
    return [str(command.user_id)]


async def _members(command: SetGroupPermissions) -> list[str]:
    """The members of the group granted, as stored now: they do not decide it."""
    from domainkit.contrib.auth import AuthUnitOfWork

    async with AuthUnitOfWork() as uow:
        return [str(each) for each in await uow.groups.member_ids(command.group_id)]


def guard_admin_changes(config: ApprovalsSettings) -> None:
    """Guard the admin changes ``ADMIN_APPROVALS`` names, each with its approvals.

    The approvers are superusers, or the holders of ``ADMIN_APPROVERS``
    where it is set; never the maker, nor the user the change is about.
    Only the admin API submits them (``http=False``), and its maker's call
    runs them (``by_maker``).
    """
    from domainkit.contrib.auth import AuthUnitOfWork, IsSuperuser

    approvers: Mapping[str, Any] = (
        {"approver_rule": IsSuperuser()}
        if config.ADMIN_APPROVERS is None
        else {"permission": config.ADMIN_APPROVERS}
    )
    for action, approvals in config.ADMIN_APPROVALS.items():
        on_guard(
            Guard(
                action,
                ADMIN_COMMANDS[action],
                ADMIN_SERVICES[action],
                AuthUnitOfWork,
                (Policy(ADMIN_POLICIES[action], approvals=approvals, **approvers),),
                parties=_members if action == SET_GROUP_PERMISSIONS else _about_user,
                http=False,
                by_maker=True,
            )
        )


def refuse_staff_creation() -> None:
    """Refuse creating a user who is staff while making staff is held.

    Auth's ``on_create_staff`` calls it with the admin API's own checks of
    a creation, and in its twin, so asking for an authorization of such a
    creation is refused as the creation is.

    Raises:
        ApprovalRequired: ``ADMIN_APPROVALS`` holds ``auth.set_staff``.
    """
    if SET_STAFF in guards():
        raise ApprovalRequired(
            "Making staff needs approval: create the user, then make them staff."
        )


async def check_admin_change(
    actor: Any,  # noqa: ANN401 - the staff member, a user.
    target: "AbstractBaseUser | None",
    change: "AdminChange",
) -> None:
    """Hold a change of the admin API for approval, if a guard holds it.

    Auth's ``on_admin_change`` calls it before each change, after the
    checks registered without an order (``HOLD_ORDER``). A change that
    ``ADMIN_APPROVALS`` names runs the guard's service: the staff member's
    approved request of exactly this change is spent and the change goes
    on, or else the change is submitted, or found waiting, and refused.
    At the change's twin (``change.asking``) nothing is submitted or spent.

    Raises:
        ApprovalRequired: A user is created staff while making staff is
            guarded: create them, then make them staff; or a change both
            makes a user staff and active again while both are guarded.
        PermissionDenied: Staff act as someone else (an impersonation).
        ApprovalPending: The change waits for its approvals.
    """
    if change.kind == "create":
        if change.values.get("is_staff") is True:
            refuse_staff_creation()  # The admin API asked it already.
        return
    held = None if target is None else await _admin_command(target, change)
    if held is not None:
        await _hold(actor, *held, asking=change.asking, last=change.last_check)


async def check_group_change(
    actor: Any,  # noqa: ANN401 - the staff member, a user.
    group: "Group | None",
    change: "GroupChange",
) -> None:
    """Hold granting a group permissions for approval, if a guard holds it.

    Auth's ``on_admin_group_change`` calls it before each change of a
    group, as ``check_admin_change`` is called for a user's. Only a change
    that grants (``auth.set_group_permissions``) is held: what only takes
    away, a new group (it has no members), a name, and a deletion are not.
    """
    if (
        change.kind != "group_permissions"
        or not change.added
        or group is None
        or SET_GROUP_PERMISSIONS not in guards()
    ):
        return
    command = SetGroupPermissions(
        group_id=group.id,
        name=group.name,
        added=tuple(sorted(change.added)),
        removed=tuple(sorted(change.removed)),
    )
    await _hold(actor, SET_GROUP_PERMISSIONS, command, asking=change.asking)


async def _hold(
    actor: Any,  # noqa: ANN401 - the staff member, a user.
    action: str,
    command: Command,
    *,
    asking: bool,
    last: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Spend the staff member's approved request of ``command``, or submit it.

    At a twin (``asking``) nothing is submitted or spent: the change is
    only asked for. ``last`` is the admin API's last check of the change
    (``AdminChange.last_check``): run once an approved request is found,
    before it is spent, so a change it refuses spends no approval; never
    while the change waits.

    Raises:
        PermissionDenied: Staff act as someone else (an impersonation).
        ApprovalPending: The change waits for its approvals.
        Exception: Whatever ``last`` raises.
    """
    if asking:
        return
    context = current_request()
    if context is None or context.actor != str(actor.id):
        raise PermissionDenied("Requests are made and decided in one's own name only.")
    if last is not None and await _approved(action, command, context.actor):
        await last()
    await ADMIN_SERVICES[action](command, None)


async def _approved(action: str, command: Command, maker: str) -> bool:
    """Whether ``maker`` holds an approved request of ``command`` that has not expired.

    What ``run_by_maker`` looks for, to spend: the call goes on with it.
    """
    registered = guards()[action]
    digest = fingerprint(action, registered.dump(command))
    async with ApprovalsUnitOfWork() as uow:
        found = await uow.requests.made_by(maker, action, digest)
    now = clock.now()
    return any(
        request.status is RequestStatus.APPROVED and not request.expired(now)
        for request in found
    )


TWO_HOLDS: Final = (
    "Make the user staff and active again in two changes: each waits for its "
    "own approval."
)
"""Why a change held twice over is refused (``auth.set_staff``, ``auth.reactivate``)."""


async def _admin_command(
    target: "AbstractBaseUser", change: "AdminChange"
) -> tuple[str, Command] | None:
    """The guarded action and command of ``change`` to ``target``, if one holds it.

    Making staff (not unmaking), making a user who holds access active
    again, adding groups or permissions (a change that only takes away is
    not held), setting a password, impersonating.

    Raises:
        ApprovalRequired: The change both makes the user staff and active
            again while both are held: two approvals, never one for both.
        ImproperlyConfigured: A change of groups held does not name each
            group it adds (``AdminChange.groups``).
    """
    held = guards()
    if change.kind == "update":
        return await _updated(target, change, held)
    action = USER_ACTIONS.get(change.kind)
    granting = change.kind not in ("groups", "permissions") or bool(change.added)
    if action is None or action not in held or not granting:
        return None
    user_id = target.id
    added, removed = tuple(sorted(change.added)), tuple(sorted(change.removed))
    if action == SET_GROUPS:
        groups = _held_groups(change)
        return action, SetGroups(
            user_id=user_id, added=added, removed=removed, groups=groups
        )
    if action == SET_PERMISSIONS:
        return action, SetPermissions(user_id=user_id, added=added, removed=removed)
    if action == SET_PASSWORD:
        return action, SetPassword(user_id=user_id)
    reason = str(change.values.get("reason", ""))
    return action, Impersonate(user_id=user_id, reason=reason)


USER_ACTIONS: Final[Mapping[str, str]] = {
    "groups": SET_GROUPS,
    "permissions": SET_PERMISSIONS,
    "password": SET_PASSWORD,
    "impersonate": IMPERSONATE,
}
"""The guarded action of each kind of change to a user but ``update``."""


async def _updated(
    target: "AbstractBaseUser", change: "AdminChange", held: Mapping[str, Guard]
) -> tuple[str, Command] | None:
    """The guarded action of a ``PATCH /users/{id}``, if one holds it.

    Making the user active again is ``REACTIVATE``'s while they will hold
    access; else, where only making staff is held, ``SET_STAFF`` covers a
    staff member made active again, as it covers making staff (user report
    A21).

    Raises:
        ApprovalRequired: The change makes the user staff and active again
            while both are held.
    """
    values = change.values
    staff = getattr(target, "is_staff", False) is True
    making = values.get("is_staff") is True and not staff
    reactivating = values.get("is_active") is True and not target.is_active
    returning = (
        await _returning(target, change)
        if reactivating and REACTIVATE in held
        else None
    )
    if returning is not None:
        if making and SET_STAFF in held:
            raise ApprovalRequired(TWO_HOLDS)
        return REACTIVATE, returning
    staff_after = values.get("is_staff", staff) is True
    if SET_STAFF in held and (making or (reactivating and staff_after)):
        return SET_STAFF, SetStaff(user_id=target.id)
    return None


async def _returning(
    target: "AbstractBaseUser", change: "AdminChange"
) -> Reactivate | None:
    """What ``target`` comes back with once ``change`` is made; None: no access.

    Staff or a superuser (as the change leaves them), a member of a group,
    or granted a permission, directly or on an object (every backend's
    ``object_grants``). What a backend grants only an active user, such as
    a member's keys in a tenant, is not known here: guard it yourself.
    """
    from domainkit.contrib.auth.backends import object_grants

    values = change.values
    flags = {
        flag: values.get(flag, getattr(target, flag, False)) is True
        for flag in ("is_staff", "is_superuser")
    }
    groups = tuple(
        [
            await _held(group)
            for group in sorted(target.groups, key=lambda group: group.name)
        ]
    )
    permissions = tuple(sorted(each.key for each in target.user_permissions))
    count, digest = _grants_digest(await object_grants(target))
    if not (any(flags.values()) or groups or permissions or count):
        return None
    return Reactivate(
        user_id=target.id,
        **flags,
        groups=groups,
        permissions=permissions,
        object_grants=count,
        object_grants_digest=digest,
    )


async def _held(group: "Group") -> HeldGroup:
    """``group`` as a command holds it: its id, name, permissions, and grants."""
    from domainkit.contrib.auth.backends import object_grants

    count, digest = _grants_digest(await object_grants(group))
    keys = tuple(sorted(group.permission_keys()))
    return HeldGroup(group.id, group.name, keys, count, digest)


def _grants_digest(grants: "Iterable[ObjectGrant]") -> tuple[int, str]:
    """How many ``grants`` there are, and the SHA-256 of them, sorted ("" for none).

    A grant on an object made since changes the digest: the command holding
    it is another one.
    """
    lines = sorted(
        f"{grant.permission}\t{grant.content_type}\t{grant.object_id}"
        for grant in grants
    )
    if not lines:
        return 0, ""
    return len(lines), hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _held_groups(change: "AdminChange") -> tuple[HeldGroup, ...]:
    """Each group ``change`` adds, by id, with its permissions, sorted by name.

    Raises:
        ImproperlyConfigured: The change does not hold each group it adds
            (``AdminChange.groups``): it could not be bound.
    """
    found = tuple(
        HeldGroup(
            group.id,
            group.name,
            tuple(sorted(group.permissions)),
            *_grants_digest(group.object_grants),
        )
        for group in sorted(change.groups, key=lambda group: group.name)
    )
    if {group.name for group in found} != set(change.added):
        raise ImproperlyConfigured(
            "A change of groups names the groups it adds in AdminChange.groups, "
            "so their approval binds them."
        )
    return found
