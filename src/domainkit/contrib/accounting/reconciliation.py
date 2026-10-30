"""Reconciliation: external statements matched to the ledger's postings.

Lines come in through a port, ``StatementSource``; ``CsvStatement`` reads
a bank's CSV export. ``import_statement`` stores them once each, by their
id on the statement, so importing a file twice imports nothing more.
``reconcile`` matches the account's unmatched lines to its unmatched
postings with the rules of ``ACCOUNTING.MATCHING_RULES``, in order, records
each match, and records what is left as breaks: lines without a posting,
and postings without a line, dated within the statements. ``breaks`` lists
them; ``python manage.py accounting unmatched`` prints them.

A statement's amount is signed as the account sees it: positive is a
debit (money in, for a bank account kept as an asset), negative a credit.
A rule is a class with a ``name`` and ``match(lines, postings, window)``
that pairs some of them; projects add their own (``MatchingRule``).
"""

import csv
import io
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from typing import Final, Literal, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import Executable, delete, func, insert, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit import clock
from domainkit.apps import registry
from domainkit.db import connections
from domainkit.exceptions import ImproperlyConfigured
from domainkit.money import Money
from domainkit.utils.module_loading import import_string

from .exceptions import AccountNotFound, InvalidAmount, InvalidStatement
from .models import AccountId, Side, ledger_currency
from .settings import AccountingSettings
from .tables import (
    MATCHED,
    UNMATCHED,
    accounts,
    breaks,
    entries,
    matches,
    postings,
    statement_lines,
)
from .uow import AccountingUnitOfWork, accounting_alias

__all__ = [
    "Break",
    "ByAmountAndDate",
    "ByReference",
    "CsvStatement",
    "Imported",
    "MatchingRule",
    "PostingItem",
    "Reconciled",
    "StatementItem",
    "StatementLine",
    "StatementSource",
    "breaks_of",
    "import_statement",
    "reconcile",
]

AMOUNT: Final = re.compile(r"[+-]?\d{1,14}(\.\d{1,4})?")
"""A statement's amount: digits, a point, and up to four places; no exponent."""
EXTERNAL_ID: Final = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,128}")
CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
"""What a line's reference and description may not have: control characters."""
CHECK_BATCH: Final = 500
"""Stored lines ``import_statement`` reads back at a time."""
UNMATCHED_LINE: Final = "unmatched_line"
UNMATCHED_POSTING: Final = "unmatched_posting"
DEFAULT_COLUMNS: Final = {
    "id": "id",
    "date": "date",
    "amount": "amount",
    "currency": "currency",
    "reference": "reference",
    "description": "description",
}
"""The columns ``CsvStatement`` reads, by default named as they are here."""


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One line of an external statement, as a source reads it."""

    external_id: str
    """Its id on the statement: a line is imported once per account by it."""
    booked_on: date
    amount: Money
    """Signed as the account sees it: positive is a debit."""
    reference: str = ""
    description: str = ""


@runtime_checkable
class StatementSource(Protocol):
    """Where statement lines come from: a file, a bank's API."""

    name: str
    """What the lines came in, kept with each (a file's name, a statement's id)."""

    def lines(self) -> Iterable[StatementLine]:
        """The statement's lines, in its order.

        Raises:
            InvalidStatement: The statement cannot be read.
        """
        ...


class CsvStatement:
    """A statement as CSV text, with a header row.

    The columns are ``id``, ``date`` (ISO, ``2026-10-30``), ``amount``
    (``-12.50``: no thousands separators, no exponent), ``currency``,
    and optionally ``reference`` and ``description``. ``columns`` maps
    those names to a bank's own (``{"id": "Transaction ID"}``).
    """

    def __init__(
        self,
        text: str,
        *,
        name: str = "statement.csv",
        columns: Mapping[str, str] | None = None,
        delimiter: str = ",",
    ) -> None:
        """Read ``text`` (decoded already), with ``columns`` renamed."""
        self.text = text
        self.name = name
        self.columns = {**DEFAULT_COLUMNS, **(columns or {})}
        self.delimiter = delimiter

    def lines(self) -> Iterator[StatementLine]:
        """Each row as a line, checked.

        Raises:
            InvalidStatement: The text is not CSV (a field over
                ``csv.field_size_limit()``), a required column is missing,
                or a row's date, amount, or currency is not one.
        """
        reader = csv.DictReader(io.StringIO(self.text), delimiter=self.delimiter)
        try:
            names = set(reader.fieldnames or ())
            required = [
                self.columns[key] for key in ("id", "date", "amount", "currency")
            ]
            if not set(required) <= names:
                raise InvalidStatement(
                    "A statement needs the columns id, date, amount, and currency."
                )
            for row in reader:
                yield self._line(row)
        except csv.Error:
            raise InvalidStatement("A statement cannot be read as CSV.") from None

    def _line(self, row: Mapping[str, str | None]) -> StatementLine:
        """One row as a line."""
        columns = self.columns

        def value(key: str) -> str:
            return (row.get(columns[key]) or "").strip()

        external_id, amount = value("id"), value("amount")
        if not EXTERNAL_ID.fullmatch(external_id):
            raise InvalidStatement("A statement line's id has 1 to 128 characters.")
        if not AMOUNT.fullmatch(amount):
            raise InvalidStatement("A statement line's amount is not an amount.")
        try:
            booked_on = date.fromisoformat(value("date"))
        except ValueError:
            raise InvalidStatement("A statement line's date is not a date.") from None
        try:
            unit = ledger_currency(value("currency"))
            money = Money.of(amount.removeprefix("+"), unit)
        except (InvalidAmount, ValueError):
            raise InvalidStatement(
                "A statement line's amount does not fit its currency."
            ) from None
        return StatementLine(
            external_id,
            booked_on,
            money,
            value("reference")[:128],
            value("description")[:255],
        )


@dataclass(frozen=True, slots=True)
class Imported:
    """What ``import_statement`` did: lines stored, and lines it had already."""

    imported: int
    skipped: int


def _config() -> AccountingSettings:
    """``ACCOUNTING``'s settings."""
    return registry.settings(AccountingSettings)


def _insert_new(connection: AsyncConnection) -> Executable:
    """An insert of statement lines that skips those stored already."""
    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    statement: Executable = dialect.insert(statement_lines).on_conflict_do_nothing(
        index_elements=[statement_lines.c.account_id, statement_lines.c.external_id]
    )
    return statement


async def import_statement(account_id: AccountId, source: StatementSource) -> Imported:
    """Store ``source``'s lines for an account; a line stored before is skipped.

    Every line is read and checked before any is stored, in one
    transaction. A line stored before must be the same (its date, amount,
    reference, and description); one that changed is refused, not skipped.

    Raises:
        AccountNotFound: No account has that id.
        InvalidStatement: A line cannot be read, has control characters, is
            in another currency than the account's, appears twice, differs
            from the line stored under its id, or there are more than
            ``ACCOUNTING.MAX_STATEMENT_LINES``.
    """
    limit = _config().MAX_STATEMENT_LINES
    async with AccountingUnitOfWork(readonly=True) as uow:
        account = await uow.accounts.get(account_id)
    rows: dict[str, dict[str, object]] = {}
    now = clock.now()
    for line in source.lines():
        if len(rows) == limit:
            raise InvalidStatement(
                "A statement has at most ACCOUNTING.MAX_STATEMENT_LINES lines."
            )
        if line.amount.currency.code != account.currency:
            raise InvalidStatement("A statement line is not in the account's currency.")
        _check(line)
        if line.external_id in rows:
            raise InvalidStatement("Two statement lines have the same id.")
        rows[line.external_id] = {
            "id": uuid4(),
            "account_id": account_id,
            "external_id": line.external_id,
            "statement": source.name[:128],
            "booked_on": line.booked_on,
            "amount": line.amount.amount,
            "currency": account.currency,
            "reference": line.reference,
            "description": line.description,
            "status": UNMATCHED,
            "imported_at": now,
        }
    if not rows:
        return Imported(0, 0)
    async with connections.engine(accounting_alias()).begin() as connection:
        before = await _count_lines(connection, account_id)
        await connection.execute(_insert_new(connection), list(rows.values()))
        stored = await _count_lines(connection, account_id) - before
        # Read back after the insert: a line another import stored meanwhile
        # is checked too. Raising rolls the whole statement back.
        if await _changed(connection, account_id, rows):
            raise InvalidStatement(
                "A statement line differs from the line imported before under its id."
            )
    return Imported(stored, len(rows) - stored)


def _check(line: StatementLine) -> None:
    """Refuse a line too long, or with control characters a terminal would run."""
    if not EXTERNAL_ID.fullmatch(line.external_id):
        raise InvalidStatement("A statement line's id has 1 to 128 characters.")
    if len(line.reference) > 128 or len(line.description) > 255:
        raise InvalidStatement(
            "A statement line's reference has up to 128 characters, "
            "and its description up to 255."
        )
    if CONTROL.search(line.reference) or CONTROL.search(line.description):
        raise InvalidStatement(
            "A statement line's reference and description have no control characters."
        )


async def _changed(
    connection: AsyncConnection,
    account_id: AccountId,
    rows: Mapping[str, Mapping[str, object]],
) -> bool:
    """Whether a line stored under one of ``rows``' ids differs from its row."""
    fields = ("booked_on", "amount", "reference", "description")
    ids = list(rows)
    for start in range(0, len(ids), CHECK_BATCH):
        stored = await connection.execute(
            select(
                statement_lines.c.external_id,
                *(statement_lines.c[name] for name in fields),
            ).where(
                statement_lines.c.account_id == account_id,
                statement_lines.c.external_id.in_(ids[start : start + CHECK_BATCH]),
            )
        )
        for found in stored.mappings():
            row = rows[found["external_id"]]
            if any(found[name] != row[name] for name in fields):
                return True
    return False


async def _count_lines(connection: AsyncConnection, account_id: AccountId) -> int:
    """How many statement lines the account has."""
    return int(
        await connection.scalar(
            select(func.count())
            .select_from(statement_lines)
            .where(statement_lines.c.account_id == account_id)
        )
        or 0
    )


@dataclass(frozen=True, slots=True)
class StatementItem:
    """An unmatched statement line, as rules see it."""

    id: UUID
    booked_on: date
    amount: Decimal
    """Signed: positive is a debit to the account."""
    currency: str
    reference: str
    description: str


@dataclass(frozen=True, slots=True)
class PostingItem:
    """An unmatched posting on the account, as rules see it."""

    id: UUID
    entry_id: UUID
    booked_on: date
    amount: Decimal
    """Signed: positive is a debit, as the statement's."""
    currency: str
    reference: str
    """Its entry's reference."""
    memo: str


@runtime_checkable
class MatchingRule(Protocol):
    """A way to pair statement lines with postings (``ACCOUNTING.MATCHING_RULES``)."""

    name: str
    """Recorded with each match it makes (up to 64 characters)."""

    def match(
        self,
        lines: Sequence[StatementItem],
        postings: Sequence[PostingItem],
        window: timedelta,
    ) -> Iterable[tuple[StatementItem, PostingItem]]:
        """Pairs of a line and a posting that are the same movement.

        Each line and each posting in at most one pair; those left go to
        the next rule. ``window`` is how far apart their dates may be.
        """
        ...


def _near(line: StatementItem, posting: PostingItem, window: timedelta) -> bool:
    """Same amount and currency, dated within ``window`` of each other."""
    return (
        line.amount == posting.amount
        and line.currency == posting.currency
        and abs(line.booked_on - posting.booked_on) <= window
    )


class ByReference:
    """A line and a posting with the same reference, amount, and currency, near in date.

    The posting's reference is its entry's. Among several postings, the
    nearest in date, then the first posted.
    """

    name = "reference"

    def match(
        self,
        lines: Sequence[StatementItem],
        postings: Sequence[PostingItem],
        window: timedelta,
    ) -> Iterator[tuple[StatementItem, PostingItem]]:
        """Pair each line with a reference to the nearest posting of that reference."""
        by_reference: defaultdict[str, list[PostingItem]] = defaultdict(list)
        for posting in postings:
            by_reference[posting.reference].append(posting)
        taken: set[UUID] = set()
        for line in lines:
            candidates = [
                posting
                for posting in by_reference.get(line.reference, ())
                if line.reference
                and posting.id not in taken
                and _near(line, posting, window)
            ]
            if candidates:
                best = min(
                    candidates,
                    key=lambda posting: abs(posting.booked_on - line.booked_on),
                )
                taken.add(best.id)
                yield line, best


class ByAmountAndDate:
    """A line and a posting of the same amount and currency, near in date, alone.

    Only when each is the other's one candidate: two payments of the same
    amount on the same days are left for a person to tell apart.
    """

    name = "amount_and_date"

    def match(
        self,
        lines: Sequence[StatementItem],
        postings: Sequence[PostingItem],
        window: timedelta,
    ) -> Iterator[tuple[StatementItem, PostingItem]]:
        """Pair the lines and postings that have exactly one candidate each."""
        for_line = {
            line.id: [posting for posting in postings if _near(line, posting, window)]
            for line in lines
        }
        for_posting: defaultdict[UUID, int] = defaultdict(int)
        for candidates in for_line.values():
            for posting in candidates:
                for_posting[posting.id] += 1
        for line in lines:
            candidates = for_line[line.id]
            if len(candidates) == 1 and for_posting[candidates[0].id] == 1:
                yield line, candidates[0]


@cache
def _rule(path: str) -> MatchingRule:
    """The rule class at ``path``, made once."""
    try:
        made = import_string(path)()
    except (ImportError, TypeError) as exc:
        raise ImproperlyConfigured(
            f"ACCOUNTING.MATCHING_RULES names {path!r}, which is not a rule "
            f"class taking no arguments ({exc})."
        ) from None
    if not isinstance(made, MatchingRule):
        raise ImproperlyConfigured(
            f"ACCOUNTING.MATCHING_RULES names {path!r}, which has no name or match()."
        )
    return made


def configured_rules() -> list[MatchingRule]:
    """The rules of ``ACCOUNTING.MATCHING_RULES``, in order.

    Raises:
        ImproperlyConfigured: A path names no rule.
    """
    return [_rule(path) for path in _config().MATCHING_RULES]


@dataclass(frozen=True, slots=True)
class Break:
    """Something a reconciliation left unmatched."""

    kind: Literal["unmatched_line", "unmatched_posting"]
    statement_line_id: UUID | None
    posting_id: UUID | None
    booked_on: date
    amount: Decimal
    """Signed: positive is a debit to the account."""
    currency: str
    reference: str


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What ``reconcile`` did: matches by rule, and the breaks left."""

    run_id: UUID
    matched: Mapping[str, int]
    breaks: Sequence[Break]


async def _unmatched(
    connection: AsyncConnection, account_id: AccountId
) -> tuple[list[StatementItem], list[PostingItem], tuple[date, date] | None]:
    """The account's unmatched lines and postings, and the days its statements cover."""
    span = (
        await connection.execute(
            select(
                func.min(statement_lines.c.booked_on),
                func.max(statement_lines.c.booked_on),
            ).where(statement_lines.c.account_id == account_id)
        )
    ).one()
    if span[0] is None:
        return [], [], None
    lines = [
        StatementItem(
            row.id,
            row.booked_on,
            row.amount,
            row.currency,
            row.reference,
            row.description,
        )
        for row in await connection.execute(
            select(statement_lines)
            .where(
                statement_lines.c.account_id == account_id,
                statement_lines.c.status == UNMATCHED,
            )
            .order_by(statement_lines.c.booked_on, statement_lines.c.external_id)
        )
    ]
    window = timedelta(days=_config().MATCH_WINDOW_DAYS)
    found = await connection.execute(
        select(
            postings.c.id,
            postings.c.entry_id,
            postings.c.booked_on,
            postings.c.side,
            postings.c.amount,
            postings.c.currency,
            entries.c.reference,
            postings.c.memo,
        )
        .join(entries, entries.c.id == postings.c.entry_id)
        .outerjoin(matches, matches.c.posting_id == postings.c.id)
        .where(
            postings.c.account_id == account_id,
            matches.c.id.is_(None),
            postings.c.booked_on >= span[0] - window,
            postings.c.booked_on <= span[1] + window,
        )
        .order_by(postings.c.booked_on, entries.c.posted_at, postings.c.line)
    )
    items = [
        PostingItem(
            row.id,
            row.entry_id,
            row.booked_on,
            row.amount if row.side is Side.DEBIT else -row.amount,
            row.currency,
            row.reference,
            row.memo,
        )
        for row in found
    ]
    return lines, items, (span[0], span[1])


async def reconcile(account_id: AccountId) -> Reconciled:
    """Match an account's statement lines to its postings; record matches and breaks.

    One reconciliation of an account at a time (a lock of it). The
    breaks it records replace the account's earlier ones: postings without
    a line count only within the dates its statements cover.

    Raises:
        AccountNotFound: No account has that id.
        ImproperlyConfigured: A rule paired a line or a posting twice, or
            one it was not given.
    """
    rules = configured_rules()
    window = timedelta(days=_config().MATCH_WINDOW_DAYS)
    run_id = uuid4()
    async with AccountingUnitOfWork() as uow:
        await uow.lock(f"accounting.reconcile:{account_id}")
        await uow.accounts.get(account_id)
        connection = await uow.session.connection()
        lines, items, span = await _unmatched(connection, account_id)
        now = clock.now()
        counts: dict[str, int] = {}
        for rule in rules:
            paired = list(rule.match(lines, items, window))
            if not paired:
                continue
            used_lines = {line.id for line, _ in paired}
            used_postings = {posting.id for _, posting in paired}
            if len(used_lines) != len(paired) or len(used_postings) != len(paired):
                raise ImproperlyConfigured(
                    f"Matching rule {rule.name!r} paired a line or a posting twice."
                )
            if not used_lines <= {line.id for line in lines} or not (
                used_postings <= {item.id for item in items}
            ):
                raise ImproperlyConfigured(
                    f"Matching rule {rule.name!r} paired a line or a posting it "
                    "was not given (another account's, or one matched already)."
                )
            await connection.execute(
                insert(matches),
                [
                    {
                        "id": uuid4(),
                        "statement_line_id": line.id,
                        "posting_id": posting.id,
                        "rule": rule.name[:64],
                        "matched_at": now,
                    }
                    for line, posting in paired
                ],
            )
            await connection.execute(
                update(statement_lines)
                .where(statement_lines.c.id.in_(used_lines))
                .values(status=MATCHED)
            )
            counts[rule.name] = len(paired)
            lines = [line for line in lines if line.id not in used_lines]
            items = [item for item in items if item.id not in used_postings]
        left = [
            Break(
                UNMATCHED_LINE,
                line.id,
                None,
                line.booked_on,
                line.amount,
                line.currency,
                line.reference,
            )
            for line in lines
        ] + [
            Break(
                UNMATCHED_POSTING,
                None,
                item.id,
                item.booked_on,
                item.amount,
                item.currency,
                item.reference,
            )
            for item in items
            if span is not None and span[0] <= item.booked_on <= span[1]
        ]
        await connection.execute(
            delete(breaks).where(breaks.c.account_id == account_id)
        )
        if left:
            await connection.execute(
                insert(breaks),
                [
                    {
                        "id": uuid4(),
                        "account_id": account_id,
                        "run_id": run_id,
                        "kind": each.kind,
                        "statement_line_id": each.statement_line_id,
                        "posting_id": each.posting_id,
                        "booked_on": each.booked_on,
                        "amount": each.amount,
                        "currency": each.currency,
                        "reference": each.reference,
                        "found_at": now,
                    }
                    for each in left
                ],
            )
        await uow.commit()
    return Reconciled(run_id, counts, left)


async def breaks_of(account_id: AccountId) -> list[Break]:
    """The breaks the account's last reconciliation left, by date.

    Raises:
        AccountNotFound: No account has that id.
    """
    async with connections.engine(accounting_alias()).connect() as connection:
        found = await connection.scalar(
            select(accounts.c.id).where(accounts.c.id == account_id)
        )
        if found is None:
            raise AccountNotFound("No account has that id.")
        rows = await connection.execute(
            select(breaks)
            .where(breaks.c.account_id == account_id)
            .order_by(
                breaks.c.booked_on, breaks.c.kind, breaks.c.reference, breaks.c.id
            )
        )
        return [
            Break(
                row.kind,
                row.statement_line_id,
                row.posting_id,
                row.booked_on,
                row.amount,
                row.currency,
                row.reference,
            )
            for row in rows
        ]
