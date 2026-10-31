"""The guards approvals brings: large postings and captures, and making superusers.

``ENTRIES`` guards ``accounting.post_entry`` once ``ENTRY_THRESHOLDS``
names amounts: an entry that moves more than its currency's amount (its
debits, per currency) needs ``ENTRY_APPROVALS`` approvals by holders of
``ENTRY_APPROVERS``, and ``post_entry`` refuses it otherwise
(``approval_required``), through the ledger's ``on_posting`` hook. Its
makers need ``accounting.add_entry``, as posting does. ``CAPTURES``
guards ``accounting.capture_hold`` the same way: a hold reserves any
amount, unguarded, and its capture is what moves it. The ledger's
``on_booking`` hook checks every entry posted or captured, as it is
booked, against the thresholds, so no path around ``post_entry`` (a
capture, a command built so ``on_posting`` cannot read it) books more
unapproved.

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

from domainkit.ddd import Command

from .exceptions import ApprovalRequired, MakerNotAllowed
from .guards import (
    Guard,
    Policy,
    Threshold,
    check,
    current_run,
    on_guard,
    requires_approval,
)
from .settings import ApprovalsSettings

if TYPE_CHECKING:
    from domainkit.contrib.accounting import Entry, PostEntry
    from domainkit.contrib.auth import AuthUnitOfWork

ENTRIES: Final = "accounting.post_entry"
"""The guarded action of postings over their currency's threshold."""
CAPTURES: Final = "accounting.capture_hold"
"""The guarded action of a hold's capture over its currency's threshold."""
SUPERUSERS: Final = "auth.grant_superuser"
"""The guarded action of making a user a superuser."""


def guard_entries(config: ApprovalsSettings) -> None:
    """Guard postings and captures over ``ENTRY_THRESHOLDS``; refuse them unapproved.

    Raises:
        ImproperlyConfigured: A threshold is not an amount of its currency.
    """
    from domainkit.contrib.accounting import (
        CAPTURES as CAPTURED,
    )
    from domainkit.contrib.accounting import (
        AccountingUnitOfWork,
        CaptureHold,
        PostEntry,
        Side,
        capture_hold,
        post_entry,
    )
    from domainkit.contrib.accounting.hooks import on_booking, on_posting

    threshold = Threshold(config.ENTRY_THRESHOLDS)

    def large(command: PostEntry) -> bool:
        return threshold.exceeded_by(
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
            post_entry,
            AccountingUnitOfWork,
            (policy("accounting.large_entry", when=large),),
            permission="accounting.add_entry",
        )
    )
    on_guard(
        Guard(
            CAPTURES,
            CaptureHold,
            capture_hold,
            AccountingUnitOfWork,
            (policy("accounting.large_capture"),),
            permission="accounting.capture_hold",
        )
    )

    async def check_booking(uow: object, entry: "Entry", accounts: object) -> None:
        """Refuse an entry over its threshold that no approved request books.

        Every entry ``post_entry`` and ``capture_hold`` book comes here,
        whatever the command looked like; reversals post what an entry
        posted before, and are left to the limits.
        """
        if entry.source != CAPTURED and entry.source.startswith("accounting."):
            return
        moved = (line.amount for line in entry.lines() if line.side is Side.DEBIT)
        if not threshold.exceeded_by(moved) or _approved_booking(entry):
            return
        raise ApprovalRequired("This command needs approval: submit it for approval.")

    def _approved_booking(entry: "Entry") -> bool:
        """Whether ``entry`` is what the approved request being run books."""
        run = current_run()
        if run is None:
            return False
        command = run.command
        if isinstance(command, PostEntry):
            return run.action == ENTRIES and (command.source, command.reference) == (
                entry.source,
                entry.reference,
            )
        return (
            isinstance(command, CaptureHold)
            and run.action == CAPTURES
            and entry.source == CAPTURED
            and entry.reference == str(command.hold_id)
        )

    on_posting(check_entry)
    on_booking(check_booking)


async def check_entry(command: "PostEntry") -> None:
    """Refuse a posting over its threshold that no approved request runs."""
    await check(ENTRIES, command)


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
