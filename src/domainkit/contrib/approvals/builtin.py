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
impersonating (``IMPERSONATE``). The admin API asks before each change
(``on_admin_change``); for one the setting names, the change is submitted
as the staff member's request and refused (``approval_pending``), and the
same call, made again by them once the request is approved, makes it,
once (a guard ``by_maker``). The approvers are superusers (or the holders
of ``ADMIN_APPROVERS``), never the user the change is about.

What a person deciding a request about a user is shown names the user,
read when shown (``show_about_user``): "User: alice@example.com", then
the groups or permissions it adds and takes away, each list whole over
as many lines as it takes, and an impersonation's reason (ADR 0173).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from pydantic import TypeAdapter

from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.ddd import Command, PermissionDenied
from domainkit.i18n import gettext, gettext_noop

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
from .settings import ApprovalsSettings

if TYPE_CHECKING:
    from domainkit.contrib.accounting import (
        AccountingUnitOfWork,
        CaptureHold,
        Entry,
        Hold,
        PostEntry,
        ReverseEntry,
    )
    from domainkit.contrib.auth import AbstractBaseUser, AuthUnitOfWork
    from domainkit.contrib.auth.gates import AdminChange

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
USER: Final = gettext_noop("User")
"""The label of the user a request is about, shown by name."""
SHOWN_LABELS: Final = {
    "added": gettext_noop("Adds"),
    "removed": gettext_noop("Takes away"),
    "reason": gettext_noop("Reason"),
}
"""The labels of what a request about a user changes, as the admin API's own."""


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


async def show_about_user(command: Any) -> list[tuple[str, object]]:  # noqa: ANN401 - a command about a user.
    """What a request about a user shows: the user by name, then what changes.

    ``user_id`` is shown as the user's first identifier
    (``get_username()``), read now and never stored with the request: a
    user erased meanwhile shows by id. ``added`` and ``removed`` are lists
    ("Adds", "Takes away"), left out when empty; ``reason`` is "Reason";
    any other field shows by its name.
    """
    from domainkit.contrib.auth import AuthUnitOfWork, UserId

    shown: list[tuple[str, object]] = []
    dumped = TypeAdapter(type(command)).dump_python(command, mode="json")
    for name, value in dumped.items():
        if name == "user_id":
            async with AuthUnitOfWork() as uow:
                user = await uow.users.find(UserId(command.user_id))
            named = str(value) if user is None else user.get_username()
            shown.append((gettext(USER), named))
        elif name in ("added", "removed"):
            if value:
                shown.append((gettext(SHOWN_LABELS[name]), value))
        elif name in SHOWN_LABELS:
            shown.append((gettext(SHOWN_LABELS[name]), value))
        else:
            shown.append((name, value))
    return shown


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
            show=show_about_user,
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
    """Make a user staff, in the admin API (``PATCH /users/{id}``)."""

    user_id: UUID


class SetGroups(Command):
    """Add a user to groups, and take them out of others, in the admin API.

    ``added`` and ``removed`` are what ``PUT /users/{id}/groups`` changes,
    by name, sorted: the change itself, not the list sent.
    """

    user_id: UUID
    added: tuple[str, ...]
    removed: tuple[str, ...] = ()


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


ADMIN_COMMANDS: Final[Mapping[str, type[Command]]] = {
    SET_STAFF: SetStaff,
    SET_GROUPS: SetGroups,
    SET_PERMISSIONS: SetPermissions,
    SET_PASSWORD: SetPassword,
    IMPERSONATE: Impersonate,
}
"""The command of each admin action ``ADMIN_APPROVALS`` may name."""
ADMIN_POLICIES: Final[Mapping[str, str]] = {
    SET_STAFF: "auth.staff",
    SET_GROUPS: "auth.groups",
    SET_PERMISSIONS: "auth.permissions",
    SET_PASSWORD: "auth.password",
    IMPERSONATE: "auth.impersonation",
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
                parties=_about_user,
                http=False,
                by_maker=True,
                show=show_about_user,
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

    Auth's ``on_admin_change`` calls it before each change. A change that
    ``ADMIN_APPROVALS`` names runs the guard's service: the staff member's
    approved request of exactly this change is spent and the change goes
    on, or else the change is submitted, or found waiting, and refused.

    Raises:
        ApprovalRequired: A user is created staff while making staff is
            guarded: create them, then make them staff.
        PermissionDenied: Staff act as someone else (an impersonation).
        ApprovalPending: The change waits for its approvals.
    """
    if change.kind == "create":
        if change.values.get("is_staff") is True:
            refuse_staff_creation()  # The admin API asked it already.
        return
    held = None if target is None else _admin_command(target, change)
    if held is None or held[0] not in guards():
        return
    action, command = held
    context = current_request()
    if context is None or context.actor != str(actor.id):
        raise PermissionDenied("Requests are made and decided in one's own name only.")
    await ADMIN_SERVICES[action](command, None)


def _admin_command(target: "AbstractBaseUser", change: "AdminChange") -> Any:  # noqa: ANN401 - (action, command) or None.
    """The guarded action and command of ``change`` to ``target``, if it has one.

    Making staff (not unmaking), adding groups or permissions (a change that
    only takes away is not held), setting a password, impersonating.
    """
    user_id = target.id
    if change.kind == "update":
        making = change.values.get("is_staff") is True
        if making and not getattr(target, "is_staff", False):
            return SET_STAFF, SetStaff(user_id=user_id)
        return None
    if change.kind in ("groups", "permissions") and change.added:
        added, removed = tuple(sorted(change.added)), tuple(sorted(change.removed))
        if change.kind == "groups":
            return SET_GROUPS, SetGroups(user_id=user_id, added=added, removed=removed)
        return SET_PERMISSIONS, SetPermissions(
            user_id=user_id, added=added, removed=removed
        )
    if change.kind == "password":
        return SET_PASSWORD, SetPassword(user_id=user_id)
    if change.kind == "impersonate":
        reason = str(change.values.get("reason", ""))
        return IMPERSONATE, Impersonate(user_id=user_id, reason=reason)
    return None
