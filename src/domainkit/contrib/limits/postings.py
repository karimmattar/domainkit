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
reverses no reversal (SEC-15b-3). A reversal booked while an approved
request runs (one that needed approval, as a reversal over
``APPROVALS.ENTRY_THRESHOLDS`` does) is counted as a movement instead,
and gives back nothing: undoing a payout two people approved does not
free the payer's limits to draw it again (FIN-88).

Only the subjects a window bounds keep their movements (PERF-26). When a
limit with a window is set, changed, or enabled for accounts or holders,
``count_earlier`` counts what their postings moved since the window
began, so a limit set at 11:00 sees the 900 moved at 10:00 (SEC-15b-5).
An actor's movements are not in the ledger: they count from when a limit
bounds them.

A holder or an actor that is not a subject limits take (a control
character, more than 255 characters) is counted under its digest,
``sha256:`` and 64 hex digits (``ledger_subject``): every entry the ledger
takes is counted, and none is refused for its holder's name.
"""

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final, cast
from uuid import UUID, uuid4

from sqlalchemy import FromClause, exists, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import class_mapper

from domainkit.apps import registry
from domainkit.context import current_request
from domainkit.contrib.accounting import (
    Account,
    AccountId,
    AccountingUnitOfWork,
    AccountType,
    Entry,
    Posting,
    Side,
)
from domainkit.db import dbclock
from domainkit.money import Money

from .models import ACCOUNT, ACTOR, HOLDER, SUBJECT, Direction, Limit, Movement
from .tables import subjects, usage
from .usage import SubjectKey, _ensure, consume, release

APPROVALS: Final = "domainkit.contrib.approvals"
DIGEST: Final = "sha256:"
"""What the subject of a holder or actor limits cannot take begins with."""
KEY: Final = "accounting.entry:"
"""What an entry's movements are counted under, before the entry's id."""


LEDGER_KINDS: Final = frozenset({ACCOUNT, HOLDER})
"""The kinds of subject whose earlier movements the ledger's postings show."""


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
    if entry.reverses is not None and not approved():
        await release(uow, entry_key(entry.reverses))
        return
    await consume(uow, *movements(entry, accounts), key=entry_key(entry.id))


def approved() -> bool:
    """Whether an approved request of ``domainkit.contrib.approvals`` runs here."""
    if not registry.is_installed(APPROVALS):
        return False
    from domainkit.contrib.approvals import current_run

    return current_run() is not None


async def count_earlier(session: AsyncSession, limit: Limit) -> int:
    """Count what the ledger moved for ``limit``'s subjects since its window began.

    For a limit with a window, enabled, of accounts or holders: every entry
    posted in the window and not reversed, in its currency and direction,
    is counted against its subjects as ``enforce`` would have counted it,
    unless it was counted already. Run in the transaction that sets the
    limit, on the ledger's database (``limits.E001``).

    Returns:
        How many movements it counted.
    """
    if limit.window is None or not limit.enabled or limit.kind not in LEDGER_KINDS:
        return 0
    moment = dbclock.now(session.get_bind().dialect.name)
    now = (
        moment
        if isinstance(moment, datetime)
        else cast(datetime, await session.scalar(select(moment)))
    )
    start = limit.window.start(now, limit.time_zone)
    # The ledger's tables, as its mapped classes name them (its public names).
    posting_rows, entry_rows, account_rows = map(_table, (Posting, Entry, Account))
    postings, entries, accounts = posting_rows.c, entry_rows.c, account_rows.c
    reversal = entry_rows.alias("reversal").c
    after = (
        entries.posted_at > start
        if limit.window.rolling
        else entries.posted_at >= start
    )
    statement = (
        select(
            postings.entry_id,
            entries.posted_at,
            postings.account_id,
            postings.side,
            postings.amount,
            accounts.type,
            accounts.holder,
        )
        .join_from(posting_rows, entry_rows, entries.id == postings.entry_id)
        .join(account_rows, accounts.id == postings.account_id)
        .where(
            after,
            postings.currency == limit.currency,
            entries.reverses.is_(None),
            ~exists().where(reversal.reverses == entries.id),
        )
    )
    moved: defaultdict[tuple[UUID, str], Decimal] = defaultdict(Decimal)
    when: dict[UUID, datetime] = {}
    for row in await session.execute(statement):
        normal = AccountType(row.type).normal_balance
        direction = Direction.IN if row.side is normal else Direction.OUT
        name = (
            str(row.account_id)
            if limit.kind == ACCOUNT
            else None
            if row.holder is None
            else ledger_subject(row.holder)
        )
        if direction is not limit.direction or name is None:
            continue
        if limit.subject not in ("", name):
            continue
        moved[row.entry_id, name] += row.amount
        when[row.entry_id] = row.posted_at
    return await _count(session, limit, moved, when)


def _table(kind: type) -> FromClause:
    """The table the ledger maps ``kind`` to."""
    return class_mapper(kind).local_table


async def _count(
    session: AsyncSession,
    limit: Limit,
    moved: Mapping[tuple[UUID, str], Decimal],
    when: Mapping[UUID, datetime],
) -> int:
    """Write the usage of ``moved`` not written yet, one row per entry and subject."""
    keys: list[SubjectKey] = sorted(
        {(limit.kind, limit.direction, limit.currency, name) for _, name in moved}
    )
    if not keys:
        return 0
    await _ensure(session, keys)
    ids = {
        row.subject: row.id
        for row in await session.execute(
            select(subjects.c.id, subjects.c.subject).where(
                subjects.c.kind == limit.kind,
                subjects.c.direction == limit.direction,
                subjects.c.currency == limit.currency,
                subjects.c.subject.in_([name for *_, name in keys]),
            )
        )
    }
    counted = {
        (row.subject_id, row.key)
        for row in await session.execute(
            select(usage.c.subject_id, usage.c.key).where(
                usage.c.subject_id.in_(ids.values()),
                usage.c.at >= min(when.values()),
            )
        )
    }
    rows = [
        {
            "id": uuid4(),
            "subject_id": ids[name],
            "amount": amount,
            "at": when[entry_id],
            "key": entry_key(entry_id),
        }
        for (entry_id, name), amount in sorted(moved.items())
        if (ids[name], entry_key(entry_id)) not in counted
    ]
    if rows:
        await session.execute(insert(usage), rows)
    return len(rows)
