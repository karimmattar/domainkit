"""Reports of a period: the trial balance and the general ledger, as CSV and JSON.

The trial balance is read in one statement, so one snapshot: what was
committed when it began. The general ledger is read in pieces of
``CHUNK`` postings, each in a short transaction of its own, so a slow
reader holds no connection between them (PERF-28); a closed period's
postings never change (FIN-87), so its general ledger is the same
whatever the pieces saw. Each sums to zero per currency, as balanced entries must: the
trial balance's debits equal its credits and its closing balances add up
to zero; the general ledger's ``net`` column (debits positive, credits
negative) adds up to zero. ``verify_ledger`` checks the books themselves.

The JSON is stable: keys sorted, no spaces, amounts as fixed-point text
at their currency's places, rows in a fixed order (accounts by code,
postings by date, posting time, entry, and line), so the same books give
the same bytes. CSV cells a spreadsheet would run as a formula start with
an apostrophe (``domainkit.resources``' exports do the same).
"""

import json
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import RowMapping, case, func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from domainkit.db import connections
from domainkit.resources.renderers import csv_rows

from .exceptions import PeriodNotFound
from .models import (
    ZERO,
    AccountId,
    AccountType,
    HoldStatus,
    PeriodId,
    PeriodStatus,
    Side,
    amount_text,
)
from .tables import accounts, entries, holds, periods, postings
from .uow import accounting_alias

__all__ = [
    "GENERAL_LEDGER_FORMAT",
    "TRIAL_BALANCE_FORMAT",
    "CurrencyTotal",
    "LedgerProblem",
    "PeriodSummary",
    "TrialBalance",
    "TrialBalanceRow",
    "general_ledger",
    "trial_balance",
    "verify_ledger",
]

NOTHING: Final = Decimal("0.0000")
"""What an account with no postings or holds sums to, at the columns' scale."""
DEBIT_NORMAL: Final = tuple(
    kind for kind in AccountType if kind.normal_balance is Side.DEBIT
)
"""The types whose balance is debits less credits: assets and expenses."""
TRIAL_BALANCE_FORMAT: Final = "domainkit.accounting.trial_balance"
GENERAL_LEDGER_FORMAT: Final = "domainkit.accounting.general_ledger"
VERSION: Final = 1
"""The version of both JSON formats."""
CHUNK: Final = 500
"""Postings per piece of a streamed general ledger."""
type Format = Literal["csv", "json"]
TRIAL_BALANCE_COLUMNS: Final = (
    "code",
    "name",
    "type",
    "currency",
    "opening",
    "debits",
    "credits",
    "closing",
)
GENERAL_LEDGER_COLUMNS: Final = (
    "booked_on",
    "entry_id",
    "source",
    "reference",
    "description",
    "line",
    "account_code",
    "account_id",
    "side",
    "amount",
    "currency",
    "net",
    "memo",
)


def _json(document: object) -> str:
    """``document`` as stable JSON: keys sorted, no spaces."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


@dataclass(frozen=True, slots=True)
class PeriodSummary:
    """The period a report is of."""

    id: PeriodId
    name: str
    starts_on: date
    ends_on: date
    status: PeriodStatus

    def document(self) -> dict[str, str]:
        """The period, as the JSON reports write it."""
        return {
            "id": str(self.id),
            "name": self.name,
            "starts_on": self.starts_on.isoformat(),
            "ends_on": self.ends_on.isoformat(),
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    """One account's line: its balance before the period, its movements, and after.

    ``opening`` and ``closing`` are signed as debits less credits, so a
    credit balance is negative; ``debits`` and ``credits`` are the period's.
    """

    account_id: AccountId
    code: str
    name: str
    type: AccountType
    currency: str
    opening: Decimal
    debits: Decimal
    credits: Decimal

    @property
    def closing(self) -> Decimal:
        """The balance at the period's end: opening, plus debits, less credits."""
        return self.opening + self.debits - self.credits

    def document(self) -> dict[str, str]:
        """The row, as the JSON report writes it."""
        code = self.currency
        return {
            "account_id": str(self.account_id),
            "code": self.code,
            "name": self.name,
            "type": self.type.value,
            "currency": code,
            "opening": amount_text(self.opening, code),
            "debits": amount_text(self.debits, code),
            "credits": amount_text(self.credits, code),
            "closing": amount_text(self.closing, code),
        }


@dataclass(slots=True)
class CurrencyTotal:
    """What one currency's rows add up to: zero ``net``, in balanced books."""

    currency: str
    opening: Decimal = ZERO
    debits: Decimal = ZERO
    credits: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        """Debits less credits since the beginning: zero when the books balance."""
        return self.opening + self.debits - self.credits

    def document(self) -> dict[str, str]:
        """The total, as the JSON reports write it."""
        code = self.currency
        return {
            "currency": code,
            "opening": amount_text(self.opening, code),
            "debits": amount_text(self.debits, code),
            "credits": amount_text(self.credits, code),
            "net": amount_text(self.net, code),
        }


def _totals(rows: Iterable[TrialBalanceRow]) -> list[CurrencyTotal]:
    """The rows' totals, one per currency, by code."""
    found: dict[str, CurrencyTotal] = {}
    for row in rows:
        total = found.setdefault(row.currency, CurrencyTotal(row.currency))
        total.opening += row.opening
        total.debits += row.debits
        total.credits += row.credits
    return [found[code] for code in sorted(found)]


@dataclass(frozen=True, slots=True)
class TrialBalance:
    """Every account with a balance or movements in a period, and the totals."""

    period: PeriodSummary
    rows: Sequence[TrialBalanceRow]
    totals: Sequence[CurrencyTotal] = field(default=())

    @property
    def balanced(self) -> bool:
        """Whether every currency's debits equal its credits, from the beginning."""
        return all(total.net == ZERO for total in self.totals)

    def to_json(self) -> str:
        """The stable JSON document, ending with a newline."""
        return (
            _json(
                {
                    "format": TRIAL_BALANCE_FORMAT,
                    "version": VERSION,
                    "period": self.period.document(),
                    "accounts": [row.document() for row in self.rows],
                    "totals": [total.document() for total in self.totals],
                }
            )
            + "\n"
        )

    def to_csv(self) -> str:
        """One line per account, then a ``total`` line per currency."""
        lines: list[Mapping[str, str]] = [row.document() for row in self.rows]
        for total in self.totals:
            each = total.document()
            lines.append({**each, "name": "total", "closing": each["net"]})
        return csv_rows(
            [
                TRIAL_BALANCE_COLUMNS,
                *(
                    [line.get(name, "") for name in TRIAL_BALANCE_COLUMNS]
                    for line in lines
                ),
            ]
        )


async def _period(connection: AsyncConnection, period_id: PeriodId) -> PeriodSummary:
    """The period ``period_id``, or ``PeriodNotFound``."""
    row = (
        await connection.execute(select(periods).where(periods.c.id == period_id))
    ).first()
    if row is None:
        raise PeriodNotFound("No accounting period has that id.")
    return PeriodSummary(row.id, row.name, row.starts_on, row.ends_on, row.status)


async def trial_balance(period_id: PeriodId) -> TrialBalance:
    """The trial balance of a period: every account with a balance or movements.

    Raises:
        PeriodNotFound: No period has that id.
    """
    async with connections.engine(accounting_alias()).connect() as connection:
        period = await _period(connection, period_id)
        before = postings.c.booked_on < period.starts_on
        moved = (
            select(
                postings.c.account_id,
                postings.c.side,
                before.label("before"),
                func.sum(postings.c.amount).label("total"),
            )
            .where(postings.c.booked_on <= period.ends_on)
            .group_by(postings.c.account_id, postings.c.side, before)
            .subquery()
        )
        # The chart is joined in the same statement: no parameter per account,
        # however many moved (PERF-23).
        statement = (
            select(
                accounts.c.id,
                accounts.c.code,
                accounts.c.name,
                accounts.c.type,
                accounts.c.currency,
                moved.c.side,
                moved.c.before,
                moved.c.total,
            )
            .join(moved, moved.c.account_id == accounts.c.id)
            .order_by(accounts.c.code)
        )
        sums: defaultdict[tuple[AccountId, Side, bool], Decimal] = defaultdict(Decimal)
        chart: dict[AccountId, RowMapping] = {}
        for row in (await connection.execute(statement)).mappings():
            chart.setdefault(row["id"], row)
            sums[row["id"], row["side"], bool(row["before"])] += row["total"]
    rows = [
        TrialBalanceRow(
            account_id=account_id,
            code=account["code"],
            name=account["name"],
            type=account["type"],
            currency=account["currency"],
            opening=sums[account_id, Side.DEBIT, True]
            - sums[account_id, Side.CREDIT, True],
            debits=sums[account_id, Side.DEBIT, False],
            credits=sums[account_id, Side.CREDIT, False],
        )
        for account_id, account in chart.items()
    ]
    return TrialBalance(period, rows, _totals(rows))


def _posting_document(row: RowMapping) -> dict[str, object]:
    """One posting of the general ledger, as text."""
    code: str = row["currency"]
    amount: Decimal = row["amount"]
    side: Side = row["side"]
    return {
        "booked_on": row["booked_on"].isoformat(),
        "entry_id": str(row["entry_id"]),
        "source": row["source"],
        "reference": row["reference"],
        "description": row["description"],
        "line": row["line"],
        "account_code": row["account_code"],
        "account_id": str(row["account_id"]),
        "side": side.value,
        "amount": amount_text(amount, code),
        "currency": code,
        "net": amount_text(amount if side is Side.DEBIT else -amount, code),
        "memo": row["memo"],
    }


ORDER: Final = (
    postings.c.booked_on,
    entries.c.posted_at,
    postings.c.entry_id,
    postings.c.line,
)
"""The general ledger's order, and the key its pieces are read after."""
GENERAL_LEDGER: Final = (
    select(
        postings.c.booked_on,
        postings.c.entry_id,
        entries.c.source,
        entries.c.reference,
        entries.c.description,
        postings.c.line,
        accounts.c.code.label("account_code"),
        postings.c.account_id,
        postings.c.side,
        postings.c.amount,
        postings.c.currency,
        postings.c.memo,
        entries.c.posted_at,
    )
    .join(entries, entries.c.id == postings.c.entry_id)
    .join(accounts, accounts.c.id == postings.c.account_id)
    .order_by(*ORDER)
)
"""Every posting, in the general ledger's order."""


async def general_ledger(period_id: PeriodId, format: Format) -> AsyncIterator[str]:
    """The postings dated in a period, streamed as CSV or JSON, with totals.

    Postings come by date, then the time their entry was posted, its id,
    and their line. The CSV ends with one ``total`` line per currency, its
    ``net`` zero; the JSON's ``totals`` are those lines. Each piece of
    ``CHUNK`` postings is read in a transaction of its own, after the last
    posting of the one before (a keyset), so no connection is held while
    the caller writes what it was given.

    Raises:
        PeriodNotFound: No period has that id (before anything is yielded).
    """
    csv = format == "csv"
    totals: dict[str, CurrencyTotal] = {}
    engine = connections.engine(accounting_alias())
    async with engine.connect() as connection:
        period = await _period(connection, period_id)
    if csv:
        yield csv_rows([GENERAL_LEDGER_COLUMNS])
    else:
        yield (
            f'{{"format":{_json(GENERAL_LEDGER_FORMAT)},'
            f'"period":{_json(period.document())},"postings":['
        )
    dated = GENERAL_LEDGER.where(
        postings.c.booked_on >= period.starts_on,
        postings.c.booked_on <= period.ends_on,
    ).limit(CHUNK)
    separator = ""
    after: tuple[object, ...] | None = None
    while True:
        statement = dated
        if after is not None:
            statement = statement.where(
                tuple_(*ORDER)
                > tuple_(
                    *(
                        literal(value, column.type)
                        for value, column in zip(after, ORDER, strict=True)
                    )
                )
            )
        async with engine.connect() as connection:
            chunk = (await connection.execute(statement)).mappings().all()
        if chunk:
            last = chunk[-1]
            after = tuple(last[column.name] for column in ORDER)
            documents = []
            for row in chunk:
                total = totals.setdefault(
                    row["currency"], CurrencyTotal(row["currency"])
                )
                if row["side"] is Side.DEBIT:
                    total.debits += row["amount"]
                else:
                    total.credits += row["amount"]
                documents.append(_posting_document(row))
            if csv:
                yield csv_rows(
                    [each[name] for name in GENERAL_LEDGER_COLUMNS]
                    for each in documents
                )
            else:
                yield separator + ",".join(map(_json, documents))
                separator = ","
        if len(chunk) < CHUNK:
            break
    ordered = [total.document() for _, total in sorted(totals.items())]
    if not csv:
        yield f'],"totals":{_json(ordered)},"version":{VERSION}}}\n'
        return
    yield csv_rows(
        [
            {**total, "description": "total"}.get(name, "")
            for name in GENERAL_LEDGER_COLUMNS
        ]
        for total in ordered
    )


@dataclass(frozen=True, slots=True)
class LedgerProblem:
    """Something ``verify_ledger`` found wrong with the books."""

    kind: Literal["unbalanced_entry", "balance_differs", "held_differs"]
    """An entry whose debits and credits differ in a currency, an account
    whose stored balance is not the sum of its postings, or one whose held
    amount is not the sum of its active holds."""
    id: str
    """The entry's id, or the account's."""
    detail: str


def _shown(value: Decimal) -> str:
    """``value`` at the columns' scale, as both databases' sums give it alike."""
    return str(value.quantize(NOTHING))


async def verify_ledger() -> list[LedgerProblem]:
    """Check the books: entries balance, balances and held amounts add up.

    Each entry's debits equal its credits per currency; each account's
    balance is the sum of its postings, and its held amount the sum of its
    active holds. Each is read in one statement; run it while nothing posts, or
    accept that an entry committed between them shows as a difference. The
    database compares, and only what differs comes back, streamed, so the
    check holds no more than its problems however long the books (PERF-28).
    """
    signed = case(
        (postings.c.side == Side.DEBIT, postings.c.amount), else_=-postings.c.amount
    )
    problems: list[LedgerProblem] = []
    async with connections.engine(accounting_alias()).connect() as connection:
        debits = func.coalesce(
            func.sum(postings.c.amount).filter(postings.c.side == Side.DEBIT),
            NOTHING,
            type_=postings.c.amount.type,
        )
        credits = func.coalesce(
            func.sum(postings.c.amount).filter(postings.c.side == Side.CREDIT),
            NOTHING,
            type_=postings.c.amount.type,
        )
        unbalanced = await connection.stream(
            select(
                postings.c.entry_id,
                postings.c.currency,
                debits.label("debits"),
                credits.label("credits"),
            )
            .group_by(postings.c.entry_id, postings.c.currency)
            .having(debits != credits)
            .order_by(postings.c.entry_id, postings.c.currency)
        )
        problems.extend(
            [
                LedgerProblem(
                    "unbalanced_entry",
                    str(row.entry_id),
                    f"{row.currency}: debits {_shown(row.debits)}, "
                    f"credits {_shown(row.credits)}",
                )
                async for row in unbalanced
            ]
        )
        net = (
            select(postings.c.account_id, func.sum(signed).label("net"))
            .group_by(postings.c.account_id)
            .subquery()
        )
        moved = func.coalesce(net.c.net, NOTHING, type_=postings.c.amount.type)
        expected = case((accounts.c.type.in_(DEBIT_NORMAL), moved), else_=-moved).label(
            "expected"
        )
        balances = await connection.stream(
            select(accounts.c.id, accounts.c.code, accounts.c.balance, expected)
            .select_from(accounts.outerjoin(net, net.c.account_id == accounts.c.id))
            .where(accounts.c.balance != expected)
            .order_by(accounts.c.code)
        )
        problems.extend(
            [
                LedgerProblem(
                    "balance_differs",
                    str(each.id),
                    f"{each.code}: balance {_shown(each.balance)}, "
                    f"postings {_shown(each.expected)}",
                )
                async for each in balances
            ]
        )
        active = (
            select(holds.c.account_id, func.sum(holds.c.amount).label("total"))
            .where(holds.c.status == HoldStatus.ACTIVE)
            .group_by(holds.c.account_id)
            .subquery()
        )
        total = func.coalesce(active.c.total, NOTHING, type_=holds.c.amount.type).label(
            "total"
        )
        held = await connection.stream(
            select(accounts.c.id, accounts.c.code, accounts.c.held, total)
            .select_from(
                accounts.outerjoin(active, active.c.account_id == accounts.c.id)
            )
            .where(accounts.c.held != total)
            .order_by(accounts.c.code)
        )
        problems.extend(
            [
                LedgerProblem(
                    "held_differs",
                    str(each.id),
                    f"{each.code}: held {_shown(each.held)}, "
                    f"active holds {_shown(each.total)}",
                )
                async for each in held
            ]
        )
    return problems
