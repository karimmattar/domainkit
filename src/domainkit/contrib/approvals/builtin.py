"""The guards approvals brings: large postings, and making superusers.

``ENTRIES`` guards ``accounting.post_entry`` once ``ENTRY_THRESHOLDS``
names amounts: an entry that moves more than its currency's amount (its
debits, per currency) needs ``ENTRY_APPROVALS`` approvals by holders of
``ENTRY_APPROVERS``, and ``post_entry`` refuses it otherwise
(``approval_required``), through the ledger's ``on_posting`` hook. Its
makers need ``accounting.add_entry``, as posting does.

``SUPERUSERS`` guards making a user a superuser once
``SUPERUSER_APPROVALS`` is more than 0: the auth admin API refuses to
(``approval_required``), and an approved ``GrantSuperuser`` does it.
"""

from typing import TYPE_CHECKING, Final
from uuid import UUID

from domainkit.ddd import Command

from .guards import Guard, Policy, Threshold, check, on_guard, requires_approval
from .settings import ApprovalsSettings

if TYPE_CHECKING:
    from domainkit.contrib.accounting import PostEntry
    from domainkit.contrib.auth import AuthUnitOfWork

ENTRIES: Final = "accounting.post_entry"
"""The guarded action of postings over their currency's threshold."""
SUPERUSERS: Final = "auth.grant_superuser"
"""The guarded action of making a user a superuser."""


def guard_entries(config: ApprovalsSettings) -> None:
    """Guard ``post_entry`` over ``ENTRY_THRESHOLDS``, and refuse it unapproved.

    Raises:
        ImproperlyConfigured: A threshold is not an amount of its currency.
    """
    from domainkit.contrib.accounting import (
        AccountingUnitOfWork,
        PostEntry,
        Side,
        post_entry,
    )
    from domainkit.contrib.accounting.hooks import on_posting

    threshold = Threshold(config.ENTRY_THRESHOLDS)

    def large(command: PostEntry) -> bool:
        return threshold.exceeded_by(
            line.amount for line in command.lines if line.side is Side.DEBIT
        )

    on_guard(
        Guard(
            ENTRIES,
            PostEntry,
            post_entry,
            AccountingUnitOfWork,
            (
                Policy(
                    "accounting.large_entry",
                    approvals=config.ENTRY_APPROVALS,
                    permission=config.ENTRY_APPROVERS,
                    when=large,
                ),
            ),
            permission="accounting.add_entry",
        )
    )
    on_posting(check_entry)


async def check_entry(command: "PostEntry") -> None:
    """Refuse a posting over its threshold that no approved request runs."""
    await check(ENTRIES, command)


class GrantSuperuser(Command):
    """Make a user a superuser: they hold every permission."""

    user_id: UUID


@requires_approval(SUPERUSERS)
async def grant_superuser(command: GrantSuperuser, uow: "AuthUnitOfWork") -> None:
    """Make the user a superuser, once approved.

    Raises:
        UserNotFound: No user has that id.
    """
    from domainkit.contrib.auth import UserId

    async with uow:
        user = await uow.users.get(UserId(command.user_id))
        user.set_superuser(True)
        await uow.commit()


def guard_superusers(config: ApprovalsSettings) -> None:
    """Guard making superusers: ``SUPERUSER_APPROVALS`` of ``SUPERUSER_APPROVERS``."""
    from domainkit.contrib.auth import AuthUnitOfWork

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
                    permission=config.SUPERUSER_APPROVERS,
                ),
            ),
            permission=config.SUPERUSER_APPROVERS,
        )
    )


def refuse_if_guarded(action: str) -> None:
    """Refuse ``action`` done outside approvals, if a guard is registered for it.

    For the paths that do a guarded action without its service, such as
    the auth admin API making a superuser.

    Raises:
        ApprovalRequired: A guard is registered as ``action``.
    """
    from .exceptions import ApprovalRequired
    from .guards import guards

    if action in guards():
        raise ApprovalRequired("This command needs approval: submit it for approval.")
