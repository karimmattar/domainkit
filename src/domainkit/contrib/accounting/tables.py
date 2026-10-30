"""The ledger's tables: the chart, entries and postings, holds, periods, statements.

Amounts are ``ExactDecimal(18, 4)`` with the currency's code beside them:
exact on PostgreSQL and SQLite, for currencies of up to four places.
Entries and postings are append-only: on PostgreSQL, triggers that
``protection`` makes after each ``migrate`` refuse every update, delete,
and truncation. No table is in a fixture (a loaded posting would not move
its account's balance), and ``flush`` keeps them all: they are the books.
"""

from typing import Final

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from domainkit.db import EnumValue, ExactDecimal, UTCDateTime, metadata
from domainkit.db.orm import children, map_imperatively

from .exceptions import AccountCodeTaken, PeriodNameTaken
from .models import (
    PRECISION,
    SCALE,
    Account,
    AccountStatus,
    AccountType,
    Entry,
    Hold,
    HoldStatus,
    Period,
    PeriodStatus,
    Posting,
    Side,
)

INFO: Final = {"domain": "accounting", "fixtures": False, "flush": False}
APPEND_ONLY: Final = {**INFO, "append_only": True}
MATCHED: Final = "matched"
UNMATCHED: Final = "unmatched"
LINE_STATUSES: Final = (UNMATCHED, MATCHED)


def amount(name: str, *, nullable: bool = False) -> Column[object]:
    """An amount column: exact, 18 digits, four of them decimal."""
    return Column(name, ExactDecimal(PRECISION, SCALE), nullable=nullable)


accounts = Table(
    "domainkit_accounting_accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "code",
        String(32),
        nullable=False,
        unique=True,
        info={"duplicate": AccountCodeTaken},
    ),
    Column("name", String(200), nullable=False),
    Column("type", EnumValue(AccountType), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("status", EnumValue(AccountStatus), nullable=False),
    Column("holder", String(255), index=True),
    Column("may_go_negative", Boolean, nullable=False),
    amount("balance"),
    amount("held"),
    Column("opened_at", UTCDateTime, nullable=False),
    Column("closed_at", UTCDateTime),
    Column("version", Integer, nullable=False),
    info=INFO,
)
"""The chart of accounts, with each account's balance in its normal sign."""

periods = Table(
    "domainkit_accounting_periods",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "name",
        String(64),
        nullable=False,
        unique=True,
        info={"duplicate": PeriodNameTaken},
    ),
    Column("starts_on", Date, nullable=False),
    Column("ends_on", Date, nullable=False),
    Column("status", EnumValue(PeriodStatus), nullable=False),
    Column("closed_at", UTCDateTime),
    Column("version", Integer, nullable=False),
    Index("ix_domainkit_accounting_periods_dates", "starts_on", "ends_on"),
    info=INFO,
)

entries = Table(
    "domainkit_accounting_entries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("source", String(64), nullable=False),
    Column("reference", String(128), nullable=False),
    Column("booked_on", Date, nullable=False),
    Column("description", String(255), nullable=False),
    Column("period_id", Uuid, ForeignKey(periods.c.id), index=True),
    Column(
        "reverses", Uuid, ForeignKey("domainkit_accounting_entries.id"), unique=True
    ),
    Column("fingerprint", String(64), nullable=False),
    Column("posted_at", UTCDateTime, nullable=False),
    UniqueConstraint("source", "reference"),
    Index("ix_domainkit_accounting_entries_booked_on", "booked_on", "posted_at"),
    info=APPEND_ONLY,
)
"""Journal entries: one per ``(source, reference)``, each reversed once at most."""

postings = Table(
    "domainkit_accounting_postings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("entry_id", Uuid, ForeignKey(entries.c.id), nullable=False, index=True),
    Column("line", Integer, nullable=False),
    Column("account_id", Uuid, ForeignKey(accounts.c.id), nullable=False),
    Column("side", EnumValue(Side), nullable=False),
    amount("amount"),
    Column("currency", String(8), nullable=False),
    Column("booked_on", Date, nullable=False),
    Column("memo", String(255), nullable=False),
    UniqueConstraint("entry_id", "line"),
    Index("ix_domainkit_accounting_postings_account", "account_id", "booked_on"),
    Index("ix_domainkit_accounting_postings_booked_on", "booked_on"),
    info=APPEND_ONLY,
)
"""The lines of the entries: an amount on one side of one account."""

holds = Table(
    "domainkit_accounting_holds",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey(accounts.c.id), nullable=False, index=True),
    amount("amount"),
    Column("currency", String(8), nullable=False),
    Column("source", String(64), nullable=False),
    Column("reference", String(128), nullable=False),
    Column("status", EnumValue(HoldStatus), nullable=False),
    Column("expires_at", UTCDateTime, nullable=False),
    Column("placed_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("entry_id", Uuid, ForeignKey(entries.c.id)),
    Column("version", Integer, nullable=False),
    UniqueConstraint("source", "reference"),
    Index("ix_domainkit_accounting_holds_expiry", "status", "expires_at"),
    info=INFO,
)
"""Amounts reserved on accounts; each account's ``held`` is its active ones'."""

statement_lines = Table(
    "domainkit_accounting_statement_lines",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey(accounts.c.id), nullable=False),
    Column("external_id", String(128), nullable=False),
    Column("statement", String(128), nullable=False),
    Column("booked_on", Date, nullable=False),
    amount("amount"),
    Column("currency", String(8), nullable=False),
    Column("reference", String(128), nullable=False),
    Column("description", String(255), nullable=False),
    Column("status", String(16), nullable=False),
    Column("imported_at", UTCDateTime, nullable=False),
    UniqueConstraint("account_id", "external_id"),
    Index("ix_domainkit_accounting_statement_lines_open", "account_id", "status"),
    info=INFO,
)
"""Lines of external statements (a bank's), imported once each by their id.

``amount`` is signed as the statement has it: money in is positive. It
matches a posting on the account's debit side when positive, its credit
side when negative.
"""

matches = Table(
    "domainkit_accounting_matches",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "statement_line_id",
        Uuid,
        ForeignKey(statement_lines.c.id),
        nullable=False,
        unique=True,
    ),
    Column("posting_id", Uuid, ForeignKey(postings.c.id), nullable=False, unique=True),
    Column("rule", String(64), nullable=False),
    Column("matched_at", UTCDateTime, nullable=False),
    info=INFO,
)
"""Which posting each matched statement line is, and the rule that said so."""

breaks = Table(
    "domainkit_accounting_breaks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey(accounts.c.id), nullable=False),
    Column("run_id", Uuid, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("statement_line_id", Uuid, ForeignKey(statement_lines.c.id)),
    Column("posting_id", Uuid, ForeignKey(postings.c.id)),
    Column("booked_on", Date, nullable=False),
    amount("amount"),
    Column("currency", String(8), nullable=False),
    Column("reference", String(128), nullable=False),
    Column("found_at", UTCDateTime, nullable=False),
    Index("ix_domainkit_accounting_breaks_run", "account_id", "run_id"),
    info=INFO,
)
"""What a reconciliation left unmatched: lines without a posting, and postings
without a line, as each run found them (its ``run_id``)."""


def start_mappers() -> None:
    """Map the aggregates (idempotent); entries never change, so carry no version."""
    map_imperatively(Account, accounts)
    map_imperatively(Period, periods)
    map_imperatively(Hold, holds)
    map_imperatively(Posting, postings)
    map_imperatively(
        Entry,
        entries,
        versioned=False,
        properties={"postings": children(Posting, order_by=postings.c.line)},
    )
