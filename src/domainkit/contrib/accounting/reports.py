"""Reports of a period: the trial balance and the general ledger, as CSV and JSON.

Both are read in one statement, so one snapshot: what was committed when
it began. Each sums to zero per currency, as balanced entries must: the
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

from sqlalchemy import RowMapping, case, func, select
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
        statement = (
            select(
                postings.c.account_id,
                postings.c.side,
                before.label("before"),
                func.sum(postings.c.amount).label("total"),
            )
            .where(postings.c.booked_on <= period.ends_on)
            .group_by(postings.c.account_id, postings.c.side, before)
        )
        sums: defaultdict[tuple[AccountId, Side, bool], Decimal] = defaultdict(Decimal)
        for row in await connection.execute(statement):
            sums[row.account_id, row.side, bool(row.before)] += row.total
        ids = sorted({account_id for account_id, _, _ in sums})
        chart = (
            await connection.execute(
                select(
                    accounts.c.id,
                    accounts.c.code,
                    accounts.c.name,
                    accounts.c.type,
                    accounts.c.currency,
                )
                .where(accounts.c.id.in_(ids))
                .order_by(accounts.c.code)
            )
        ).all()
    rows = [
        TrialBalanceRow(
            account_id=account.id,
            code=account.code,
            name=account.name,
            type=account.type,
            currency=account.currency,
            opening=sums[account.id, Side.DEBIT, True]
            - sums[account.id, Side.CREDIT, True],
            debits=sums[account.id, Side.DEBIT, False],
            credits=sums[account.id, Side.CREDIT, False],
        )
        for account in chart
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
    )
    .join(entries, entries.c.id == postings.c.entry_id)
    .join(accounts, accounts.c.id == postings.c.account_id)
    .order_by(
        postings.c.booked_on, entries.c.posted_at, postings.c.entry_id, postings.c.line
    )
)
"""Every posting, in the general ledger's order."""


async def general_ledger(period_id: PeriodId, format: Format) -> AsyncIterator[str]:
    """The postings dated in a period, streamed as CSV or JSON, with totals.

    Postings come by date, then the time their entry was posted, its id,
    and their line. The CSV ends with one ``total`` line per currency, its
    ``net`` zero; the JSON's ``totals`` are those lines.

    Raises:
        PeriodNotFound: No period has that id (before anything is yielded).
    """
    csv = format == "csv"
    totals: dict[str, CurrencyTotal] = {}
    async with connections.engine(accounting_alias()).connect() as connection:
        period = await _period(connection, period_id)
        if csv:
            yield csv_rows([GENERAL_LEDGER_COLUMNS])
        else:
            yield (
                f'{{"format":{_json(GENERAL_LEDGER_FORMAT)},'
                f'"period":{_json(period.document())},"postings":['
            )
        result = await connection.stream(
            GENERAL_LEDGER.where(
                postings.c.booked_on >= period.starts_on,
                postings.c.booked_on <= period.ends_on,
            )
        )
        separator = ""
        async for chunk in result.mappings().partitions(CHUNK):
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


async def verify_ledger() -> list[LedgerProblem]:
    """Check the books: entries balance, balances and held amounts add up.

    Each entry's debits equal its credits per currency; each account's
    balance is the sum of its postings, and its held amount the sum of its
    active holds. Each is read in one statement; run it while nothing posts, or
    accept that an entry committed between them shows as a difference.
    """
    signed = case(
        (postings.c.side == Side.DEBIT, postings.c.amount), else_=-postings.c.amount
    )
    problems: list[LedgerProblem] = []
    async with connections.engine(accounting_alias()).connect() as connection:
        unbalanced = await connection.execute(
            select(
                postings.c.entry_id,
                postings.c.currency,
                func.sum(postings.c.amount)
                .filter(postings.c.side == Side.DEBIT)
                .label("debits"),
                func.sum(postings.c.amount)
                .filter(postings.c.side == Side.CREDIT)
                .label("credits"),
            ).group_by(postings.c.entry_id, postings.c.currency)
        )
        problems.extend(
            LedgerProblem(
                "unbalanced_entry",
                str(row.entry_id),
                f"{row.currency}: debits {row.debits or ZERO}, "
                f"credits {row.credits or ZERO}",
            )
            for row in unbalanced
            if (row.debits or ZERO) != (row.credits or ZERO)
        )
        balances = await connection.execute(
            select(
                accounts.c.id,
                accounts.c.code,
                accounts.c.type,
                accounts.c.balance,
                func.sum(signed).label("net"),
            )
            .select_from(
                accounts.outerjoin(postings, postings.c.account_id == accounts.c.id)
            )
            .group_by(
                accounts.c.id, accounts.c.code, accounts.c.type, accounts.c.balance
            )
            .order_by(accounts.c.code)
        )
        for each in balances:
            net = each.net or ZERO
            expected = net if each.type.normal_balance is Side.DEBIT else -net
            if expected != each.balance:
                problems.append(
                    LedgerProblem(
                        "balance_differs",
                        str(each.id),
                        f"{each.code}: balance {each.balance}, postings {expected}",
                    )
                )
        active = (
            select(holds.c.account_id, func.sum(holds.c.amount).label("total"))
            .where(holds.c.status == HoldStatus.ACTIVE)
            .group_by(holds.c.account_id)
            .subquery()
        )
        held = await connection.execute(
            select(accounts.c.id, accounts.c.code, accounts.c.held, active.c.total)
            .select_from(
                accounts.outerjoin(active, active.c.account_id == accounts.c.id)
            )
            .order_by(accounts.c.code)
        )
        problems.extend(
            LedgerProblem(
                "held_differs",
                str(each.id),
                f"{each.code}: held {each.held}, active holds {each.total or ZERO}",
            )
            for each in held
            if (each.total or ZERO) != each.held
        )
    return problems
