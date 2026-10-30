"""Entries posted from messages, and the expiry of holds (needs messaging).

``defer_entry(uow, command)`` posts an entry once the caller's unit of work
commits, through the outbox: the task ``POST_ENTRY`` runs under the inbox
(``@inbox``), so a message delivered twice commits its entry once, and the
entry's source and reference make a second message about the same entry
return the first one's. The worker releases expired holds every
``ACCOUNTING.EXPIRE_HOLDS_EVERY``.
"""

from datetime import date
from typing import Final

from domainkit.contrib.messaging import inbox, periodic, task
from domainkit.ddd import DomainError, UnitOfWork

from .holds import expire_holds
from .ledger import PostEntry, post_entry
from .schemas import LineIn
from .settings import AccountingSettings
from .uow import AccountingUnitOfWork

POST_ENTRY: Final = "domainkit.accounting.post_entry"
"""The task that posts an entry from a message."""
EXPIRE_HOLDS: Final = "domainkit.accounting.expire_holds"


async def post_entry_message(
    source: str,
    reference: str,
    booked_on: date,
    lines: list[LineIn],
    description: str = "",
) -> None:
    """Post the entry a message carries (the task ``POST_ENTRY``)."""
    await post_entry(
        PostEntry(
            source=source,
            reference=reference,
            booked_on=booked_on,
            lines=tuple(line.to_line() for line in lines),
            description=description,
        ),
        AccountingUnitOfWork(),
    )


def defer_entry(uow: UnitOfWork, command: PostEntry) -> None:
    """Post ``command``'s entry once ``uow`` commits, through the outbox, once."""
    uow.defer(
        post_entry_message,
        source=command.source,
        reference=command.reference,
        booked_on=command.booked_on,
        lines=[LineIn.of(line).model_dump(mode="json") for line in command.lines],
        description=command.description,
    )


async def expire_due_holds() -> None:
    """Release the holds whose time is up (the periodic task ``EXPIRE_HOLDS``)."""
    await expire_holds()


def register(config: AccountingSettings) -> None:
    """Register the posting task, under the inbox, and the expiry of holds.

    A refused entry is dead-lettered at once: running it again would be
    refused again, until someone changes the books (``messages --retry``).
    """
    inbox(post_entry_message)
    task(name=POST_ENTRY, dont_retry_on=(DomainError,))(post_entry_message)
    periodic(config.EXPIRE_HOLDS_EVERY, name=EXPIRE_HOLDS)(expire_due_holds)
