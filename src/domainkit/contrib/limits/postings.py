"""The ledger's postings, counted against limits (with ``contrib.accounting``).

``ready()`` registers ``enforce`` on the ledger's posting path
(``on_stage_entry``), which works out each entry's movements as it is
booked, and ``count_staged`` on the commit (``on_stage``), which counts
them in the posting's own transaction, after its accounts are locked and
the rest of its work is done: refused, the entry is not booked. Counted
at the commit, under a shared lock of each ledger kind (``counting``)
that ``count_earlier`` takes alone, a posting in flight while a limit is
set either commits before the limit counts what moved earlier, or sees
the limit when it counts (SEC-16b-2). A posting that lowers its account's balance moves
money out (``Direction.OUT``), one that raises it, in. Each entry is one
movement per subject, direction, and currency: of each account (by id)
and each account's holder, the sum of the entry's postings for it; of the
request's actor, the entry's size, its debits, out. A reversal counts
nothing, and gives back what the entry it reverses counted; the ledger
reverses no reversal (SEC-15b-3). A reversal booked while an approved
request runs (one that needed approval, as a reversal over
``APPROVALS.ENTRY_THRESHOLDS`` does) is counted as a movement instead,
and gives back nothing: undoing a payout two people approved does not
free the payer's limits to draw it again (FIN-88). Such a reversal is
marked (``kept``), so a limit set later counts it, and the entry it
reverses, as ``enforce`` did (SEC-16b-9).

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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import FromClause, and_, exists, insert, or_, select, text
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
from domainkit.db import dbclock, lock_key
from domainkit.money import Money

from .models import ACCOUNT, ACTOR, HOLDER, SUBJECT, Direction, Movement, Window, fits
from .tables import kept, subjects, usage
from .usage import SubjectKey, _ensure, consume, release

if TYPE_CHECKING:
    from domainkit.db import SqlAlchemyUnitOfWork
    from domainkit.ddd import DeferredTask, DomainEvent

APPROVALS: Final = "domainkit.contrib.approvals"
DIGEST: Final = "sha256:"
"""What the subject of a holder or actor limits cannot take begins with."""
KEY: Final = "accounting.entry:"
"""What an entry's movements are counted under, before the entry's id."""


LEDGER_KINDS: Final = frozenset({ACCOUNT, HOLDER})
"""The kinds of subject whose earlier movements the ledger's postings show."""
STAGED: Final = "domainkit.limits.staged"
"""Where ``enforce`` leaves an entry's count for the commit: ``session.info``."""
CHUNK: Final = 1000
"""Subjects one statement of ``count_earlier`` names at most (SEC-16b-8).

asyncpg binds at most 32,767 parameters a statement; a subject's row
takes five."""
_SHARE: Final = text(
    "SELECT pg_advisory_xact_lock_shared(key) "
    "FROM unnest(CAST(:keys AS BIGINT[])) AS locks(key)"
)
_TAKE: Final = text("SELECT pg_advisory_xact_lock(:key)")


class Scope(Protocol):
    """What ``count_earlier`` counts for: a limit, or a rule as enforced."""

    @property
    def kind(self) -> str: ...  # noqa: D102 - as named.
    @property
    def direction(self) -> Direction: ...  # noqa: D102 - as named.
    @property
    def currency(self) -> str: ...  # noqa: D102 - as named.
    @property
    def subject(self) -> str: ...  # noqa: D102 - as named.
    @property
    def window(self) -> Window | None: ...  # noqa: D102 - as named.
    @property
    def time_zone(self) -> str: ...  # noqa: D102 - as named.
    @property
    def enabled(self) -> bool: ...  # noqa: D102 - as named.


@dataclass(frozen=True, slots=True)
class _Staged:
    """What ``enforce`` found an entry moves, to count when it commits."""

    key: str
    """The key the entry's movements are counted under."""
    movements: Sequence[Movement]
    """None for a reversal that gives back what ``releases`` counted."""
    releases: str | None
    kept: UUID | None
    """An approved reversal's id: it is marked, and counted as a movement."""


def counting(kind: str) -> int:
    """The lock a count of ``kind``'s postings takes: shared, or alone to count back."""
    return lock_key(f"domainkit.limits.counting:{kind}")


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
    """Stage the entry's movements, or the release of the entry it reverses.

    ``count_staged`` counts them when the posting commits; whether a
    reversal frees what it undid is decided here, while the request that
    books it runs.

    Raises:
        MovementOutOfRange: A movement is larger than usage keeps.
    """
    key = entry_key(entry.id)
    if entry.reverses is not None and not approved():
        staged = _Staged(key, (), entry_key(entry.reverses), None)
    else:
        marked = None if entry.reverses is None else entry.id
        staged = _Staged(key, movements(entry, accounts), None, marked)
    _staged(uow.session, create=True).append(staged)


def _staged(session: AsyncSession, *, create: bool = False) -> list[_Staged]:
    """What ``enforce`` staged in the session's transaction; kept if ``create``."""
    transaction = session.sync_session.get_transaction()
    found: tuple[object, list[_Staged]] | None = session.info.get(STAGED)
    if found is not None and found[0] is transaction:
        if not create:
            del session.info[STAGED]
        return found[1]
    if not create:
        return []
    staged: list[_Staged] = []
    session.info[STAGED] = (transaction, staged)
    return staged


async def count_staged(
    uow: "SqlAlchemyUnitOfWork",
    events: Sequence["DomainEvent"],
    tasks: Sequence["DeferredTask"],
) -> None:
    """Count what ``enforce`` staged in ``uow``'s transaction, as it commits.

    Under a shared lock of each ledger kind (``counting``), taken last:
    a limit that ``count_earlier`` counts back for waits for the commit,
    and one it already counted back for is read here (SEC-16b-2).

    Raises:
        LimitExceeded, TransactionLimitExceeded: A movement is over a limit.
    """
    session = uow.session
    staged = _staged(session)
    if not staged:
        return
    if session.get_bind().dialect.name == "postgresql":
        keys = sorted(counting(kind) for kind in LEDGER_KINDS)
        await session.execute(_SHARE, {"keys": keys})
    for each in staged:
        if each.releases is not None:
            await release(uow, each.releases)
            continue
        if each.kept is not None:
            now = dbclock.now(session.get_bind().dialect.name)
            await session.execute(insert(kept).values(entry_id=each.kept, at=now))
        await consume(uow, *each.movements, key=each.key)


def approved() -> bool:
    """Whether an approved request of ``domainkit.contrib.approvals`` runs here."""
    if not registry.is_installed(APPROVALS):
        return False
    from domainkit.contrib.approvals import current_run

    return current_run() is not None


async def count_earlier(session: AsyncSession, limit: Scope) -> int:
    """Count what the ledger moved for ``limit``'s subjects since its window began.

    For a limit with a window, enabled, of accounts or holders: every entry
    posted in the window and not reversed, in its currency and direction,
    is counted against its subjects as ``enforce`` would have counted it,
    unless it was counted already; so is a reversal ``enforce`` counted as
    a movement (``kept``), and the entry it reverses (SEC-16b-9). Run in
    the transaction that sets the limit, on the ledger's database
    (``limits.E001``): on PostgreSQL it first takes the kind's
    ``counting`` lock alone, so the postings in flight commit first and
    those that follow see the limit (SEC-16b-2).

    Returns:
        How many movements it counted.

    Raises:
        MovementOutOfRange: A movement is past the columns usage keeps it in.
    """
    if limit.window is None or not limit.enabled or limit.kind not in LEDGER_KINDS:
        return 0
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(_TAKE, {"key": counting(limit.kind)})
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
    marked = select(kept.c.entry_id)
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
            or_(
                and_(
                    entries.reverses.is_(None),
                    ~exists().where(
                        reversal.reverses == entries.id, reversal.id.not_in(marked)
                    ),
                ),
                entries.id.in_(marked),
            ),
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
    for amount in moved.values():
        fits(amount)
    return await _count(session, limit, moved, when)


def _table(kind: type) -> FromClause:
    """The table the ledger maps ``kind`` to."""
    return class_mapper(kind).local_table


def _chunks[T](items: Sequence[T]) -> Iterator[Sequence[T]]:
    """``items``, ``CHUNK`` at a time."""
    for start in range(0, len(items), CHUNK):
        yield items[start : start + CHUNK]


async def _count(
    session: AsyncSession,
    limit: Scope,
    moved: Mapping[tuple[UUID, str], Decimal],
    when: Mapping[UUID, datetime],
) -> int:
    """Write the usage of ``moved`` not written yet, one row per entry and subject."""
    keys: list[SubjectKey] = sorted(
        {(limit.kind, limit.direction, limit.currency, name) for _, name in moved}
    )
    if not keys:
        return 0
    ids: dict[str, UUID] = {}
    counted: set[tuple[UUID, str | None]] = set()
    for chunk in _chunks(keys):
        await _ensure(session, list(chunk))
        found = await session.execute(
            select(subjects.c.id, subjects.c.subject).where(
                subjects.c.kind == limit.kind,
                subjects.c.direction == limit.direction,
                subjects.c.currency == limit.currency,
                subjects.c.subject.in_([name for *_, name in chunk]),
            )
        )
        named = {row.subject: row.id for row in found}
        ids.update(named)
        counted.update(
            (row.subject_id, row.key)
            for row in await session.execute(
                select(usage.c.subject_id, usage.c.key).where(
                    usage.c.subject_id.in_(list(named.values())),
                    usage.c.at >= min(when.values()),
                )
            )
        )
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
