"""The ledger's postings, counted against limits (with ``contrib.accounting``).

``ready()`` registers ``enforce`` on the ledger's posting path
(``on_booking``), so every entry the ledger books is counted in the
posting's own transaction, after its accounts are locked: refused, the
entry is not booked. A posting that lowers its account's balance moves
money out (``Direction.OUT``), one that raises it, in. Each entry is one
movement per subject, direction, and currency: of each account (by id)
and each account's holder, the sum of the entry's postings for it; of the
request's actor, the entry's size, its debits, out. A reversal counts
nothing, and gives back what the entry it reverses counted; the ledger
reverses no reversal (SEC-15b-3).

A holder or an actor that is not a subject limits take (a control
character, more than 255 characters) is counted under its digest,
``sha256:`` and 64 hex digits (``ledger_subject``): every entry the ledger
takes is counted, and none is refused for its holder's name.
"""

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from typing import Final
from uuid import UUID

from domainkit.context import current_request
from domainkit.contrib.accounting import (
    Account,
    AccountId,
    AccountingUnitOfWork,
    Entry,
    Side,
)
from domainkit.money import Money

from .models import ACCOUNT, ACTOR, HOLDER, SUBJECT, Direction, Movement
from .usage import consume, release

DIGEST: Final = "sha256:"
"""What the subject of a holder or actor limits cannot take begins with."""
KEY: Final = "accounting.entry:"
"""What an entry's movements are counted under, before the entry's id."""


def entry_key(entry_id: UUID) -> str:
    """The key the movements of the entry ``entry_id`` are counted under."""
    return f"{KEY}{entry_id}"


def ledger_subject(name: str) -> str:
    """``name`` as a subject: itself if limits take it, else its digest."""
    if SUBJECT.fullmatch(name):
        return name
    return DIGEST + hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()


def movements(entry: Entry, accounts: Mapping[AccountId, Account]) -> list[Movement]:
    """The entry's movements: one per subject, direction, and currency."""
    context = current_request()
    actor = None if context is None else context.actor
    totals: defaultdict[tuple[str, str, Direction, str], list[Money]] = defaultdict(
        list
    )
    for posting in entry.postings:
        account = accounts[posting.account_id]
        direction = (
            Direction.IN if posting.side is account.normal_balance else Direction.OUT
        )
        named = [(ACCOUNT, str(account.id))]
        if account.holder is not None:
            named.append((HOLDER, ledger_subject(account.holder)))
        for kind, subject in named:
            totals[kind, subject, direction, posting.currency].append(posting.money)
        if actor is not None and posting.side is Side.DEBIT:
            totals[
                ACTOR, ledger_subject(actor), Direction.OUT, posting.currency
            ].append(posting.money)
    return [
        Movement(sum(amounts[1:], amounts[0]), {kind: subject}, direction)
        for (kind, subject, direction, _), amounts in sorted(totals.items())
    ]


async def enforce(
    uow: AccountingUnitOfWork, entry: Entry, accounts: Mapping[AccountId, Account]
) -> None:
    """Count the entry's movements, or release those of the entry it reverses.

    Raises:
        LimitExceeded, TransactionLimitExceeded: A movement is over a limit.
    """
    if entry.reverses is not None:
        await release(uow, entry_key(entry.reverses))
        return
    await consume(uow, *movements(entry, accounts), key=entry_key(entry.id))
