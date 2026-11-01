"""The guards approvals brings: large postings, captures, reversals; superusers.

``ENTRIES`` guards ``accounting.post_entry`` once ``ENTRY_THRESHOLDS``
names amounts: an entry that moves more than its currency's amount (its
debits, per currency; a currency the thresholds do not name, unless
``"*"`` does, is always more: FIN-89) needs ``ENTRY_APPROVALS`` approvals
by holders of ``ENTRY_APPROVERS``, and ``post_entry`` refuses it
otherwise (``approval_required``), through the ledger's ``on_posting``
hook. Its makers need ``accounting.add_entry``, as posting does.
``CAPTURES`` guards ``accounting.capture_hold`` the same way: a hold
reserves any amount, unguarded, and its capture is what moves it.
``REVERSES`` guards ``accounting.reverse_entry``: a reversal moves as
much as the entry it undoes, so it needs the approval that entry needed
(FIN-88). The ledger's ``on_booking`` hook checks every entry posted,
captured, or reversed, as it is booked, against the thresholds, so no
path around ``post_entry`` books more unapproved.

The run of any approved request whose policy asked at least
``ENTRY_APPROVALS`` approvals passes the thresholds for the entries it
books: a project's own guarded payout, approved by enough people, posts
its entry without a second request (FIN-92).

``SUPERUSERS`` guards making a user a superuser once
``SUPERUSER_APPROVALS`` is more than 0: the auth admin API refuses to
(``approval_required``), and an approved ``GrantSuperuser`` does it. Only
a superuser submits one, and the run checks that its maker still is;
the approvers are superusers (or the holders of ``SUPERUSER_APPROVERS``),
never the user it promotes.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from domainkit.apps import registry
from domainkit.ddd import Command

from .exceptions import ApprovalRequired, MakerNotAllowed
from .guards import (
    Guard,
    Policy,
    Running,
    Threshold,
    check,
    current_run,
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
    from domainkit.contrib.auth import AuthUnitOfWork

ENTRIES: Final = "accounting.post_entry"
"""The guarded action of postings over their currency's threshold."""
CAPTURES: Final = "accounting.capture_hold"
"""The guarded action of a hold's capture over its currency's threshold."""
REVERSES: Final = "accounting.reverse_entry"
"""The guarded action of a reversal over its currency's threshold."""
SUPERUSERS: Final = "auth.grant_superuser"
"""The guarded action of making a user a superuser."""


@requires_approval(ENTRIES)
async def approved_entry(command: "PostEntry", uow: "AccountingUnitOfWork") -> "Entry":
    """Post an approved entry: the service ``ENTRIES`` runs (``post_entry``)."""
    from domainkit.contrib.accounting import post_entry

    return await post_entry(command, uow)


@requires_approval(CAPTURES)
async def approved_capture(
    command: "CaptureHold", uow: "AccountingUnitOfWork"
) -> "Hold":
    """Capture an approved hold: the service ``CAPTURES`` runs (``capture_hold``)."""
    from domainkit.contrib.accounting import capture_hold

    return await capture_hold(command, uow)


@requires_approval(REVERSES)
async def approved_reversal(
    command: "ReverseEntry", uow: "AccountingUnitOfWork"
) -> "Entry":
    """Post an approved reversal: the service ``REVERSES`` runs (``reverse_entry``)."""
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
    from domainkit.contrib.accounting import CAPTURES as CAPTURE_SOURCE
    from domainkit.contrib.accounting import (
        AccountingUnitOfWork,
        CaptureHold,
        PostEntry,
        ReverseEntry,
        Side,
    )
    from domainkit.contrib.accounting.hooks import on_booking, on_posting

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
            ENTRIES,
            PostEntry,
            approved_entry,
            AccountingUnitOfWork,
            (policy("accounting.large_entry", when=large),),
            permission="accounting.add_entry",
        )
    )
    on_guard(
        Guard(
            CAPTURES,
            CaptureHold,
            approved_capture,
            AccountingUnitOfWork,
            (policy("accounting.large_capture"),),
            permission="accounting.capture_hold",
        )
    )
    on_guard(
        Guard(
            REVERSES,
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
        if run.action == ENTRIES and isinstance(command, PostEntry):
            return (command.source, command.reference) == (
                entry.source,
                entry.reference,
            )
        if run.action == CAPTURES and isinstance(command, CaptureHold):
            return (entry.source, entry.reference) == (
                CAPTURE_SOURCE,
                str(command.hold_id),
            )
        if run.action == REVERSES and isinstance(command, ReverseEntry):
            return entry.reverses == command.entry_id
        return run.action not in LEDGER_ACTIONS and approved_run(run)

    async def check_entry(command: PostEntry) -> None:
        """Refuse a posting over its threshold that no approved request runs."""
        run = current_run()
        if run is None or run.action in LEDGER_ACTIONS or not approved_run(run):
            await check(ENTRIES, command)

    on_posting(check_entry)
    on_booking(check_booking)


LEDGER_ACTIONS: Final = frozenset({ENTRIES, CAPTURES, REVERSES})
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


@requires_approval(SUPERUSERS)
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
            SUPERUSERS,
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
