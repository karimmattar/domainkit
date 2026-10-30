"""The ledger over HTTP: accounts, entries, holds, periods, reports, reconciliation.

Mount it where the project's API lives::

    from domainkit.contrib.accounting.api import router as accounting

    router.include_router(accounting, prefix="/api/accounting")

Each route needs a model permission of its action (``ModelActions``),
whatever authenticates requests, so every route denies by default.
Reading needs ``accounting.view_<model>`` (accounts, entries, holds,
periods, and a period's reports); creating, ``add_<model>``. The other
actions have permissions of their own: ``close_account``,
``reconcile_account`` (importing statements and reconciling; reading the
breaks needs ``view_account``), ``reverse_entry``, ``capture_hold``,
``release_hold``, and ``close_period``.

Refusals answer as every domain error does: ``404`` for what is not
there, ``409`` for a reference reused, ``422`` for an entry the books
refuse, each with its code (``unbalanced_entry``, ``period_closed``, ...),
as problem details where the project asks for them. Amounts are
``{"amount": "10.50", "currency": "USD"}``, the amount as text.
"""

import dataclasses
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from domainkit.http import Filter, FilterSet, Pagination, error_responses
from domainkit.http.access import Require
from domainkit.http.middleware import body_limit
from domainkit.resources import HasModelPermission, ModelActions, ResourceRouter
from domainkit.resources.renderers import CSV, JSON, negotiate

from .exceptions import InvalidStatement
from .holds import (
    CaptureHold,
    PlaceHold,
    ReleaseHold,
    capture_hold,
    place_hold,
    release_hold,
)
from .ledger import (
    CloseAccount,
    ClosePeriod,
    OpenAccount,
    OpenPeriod,
    PostEntry,
    ReverseEntry,
    close_account,
    close_period,
    open_account,
    open_period,
    post_entry,
    reverse_entry,
)
from .models import (
    MAX_NAME,
    MAX_PERIOD_NAME,
    MAX_TEXT,
    Account,
    AccountId,
    AccountType,
    Entry,
    Hold,
    Period,
    PeriodId,
)
from .reconciliation import CsvStatement, breaks_of, import_statement, reconcile
from .reports import general_ledger, trial_balance
from .schemas import (
    AccountOut,
    EntryOut,
    HoldOut,
    LineIn,
    MoneyIn,
    MoneyOut,
    PeriodOut,
)
from .uow import AccountingUnitOfWork

__all__ = [
    "accounts",
    "entries",
    "holds",
    "periods",
    "router",
]

PAGES: Final = Pagination(default_limit=50, max_limit=200)
TAGS: Final = ["accounting"]
MAX_STATEMENT_BYTES: Final = 10 * 1024 * 1024
"""The largest statement ``POST /accounts/{id}/statements`` takes."""
Source = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")]
Reference = Annotated[str, Field(min_length=1, max_length=128)]
Text = Annotated[str, Field(max_length=MAX_TEXT)]


def _rule(action: str, model: type) -> Any:  # noqa: ANN401 - a dependency.
    """The dependency that needs ``action``'s permission on ``model``."""
    return Depends(Require(HasModelPermission(action, model)))


class AccountFilters(FilterSet):
    """Accounts by code, type, currency, status, or holder."""

    model = Account
    code = Filter("exact", "in")
    type = Filter("exact", "in")
    currency = Filter("exact")
    status = Filter("exact")
    holder = Filter("exact")
    ordering_fields = ("code",)
    default_ordering = ("code",)


class EntryFilters(FilterSet):
    """Entries by source, reference, date, period, or what they reverse."""

    model = Entry
    source = Filter("exact")
    reference = Filter("exact")
    booked_on = Filter("exact", "gte", "lte")
    period_id = Filter("exact")
    reverses = Filter("exact")
    ordering_fields = ("booked_on", "posted_at")
    default_ordering = ("-posted_at",)


class HoldFilters(FilterSet):
    """Holds by account, status, or reference."""

    model = Hold
    account_id = Filter("exact")
    status = Filter("exact")
    source = Filter("exact")
    reference = Filter("exact")
    ordering_fields = ("placed_at", "expires_at")
    default_ordering = ("-placed_at",)


class PeriodFilters(FilterSet):
    """Periods by status, in date order."""

    model = Period
    status = Filter("exact")
    ordering_fields = ("starts_on",)
    default_ordering = ("starts_on",)


class AccountIn(BaseModel):
    """A new account."""

    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")]
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
    type: AccountType
    currency: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{2,7}$")]
    holder: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)] | None = None
    may_go_negative: bool = True


class EntryIn(BaseModel):
    """An entry to post: once per ``source`` and ``reference``."""

    model_config = ConfigDict(extra="forbid")

    source: Source
    reference: Reference
    booked_on: date
    lines: Annotated[list[LineIn], Field(min_length=2, max_length=1000)]
    description: Text = ""


class ReversalIn(BaseModel):
    """When the reversal is booked, and why."""

    model_config = ConfigDict(extra="forbid")

    booked_on: date | None = None
    description: Text = ""


class HoldIn(BaseModel):
    """An amount to reserve on an account."""

    model_config = ConfigDict(extra="forbid")

    account_id: UUID
    amount: MoneyIn
    source: Source
    reference: Reference
    expires_at: datetime | None = None


class CaptureIn(BaseModel):
    """Where a captured hold goes, how much of it, and when."""

    model_config = ConfigDict(extra="forbid")

    counter_account_id: UUID
    amount: MoneyIn | None = None
    booked_on: date | None = None
    description: Text = ""


class PeriodIn(BaseModel):
    """A new period."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=MAX_PERIOD_NAME)]
    starts_on: date
    ends_on: date


def _money(value: object) -> Any:  # noqa: ANN401 - Money, or None.
    """A body's amount (``MoneyIn``, as a dict) as ``Money``; None stays None."""
    return None if value is None else MoneyIn.model_validate(value).to_money()


async def post_from_body(command: PostEntry, uow: AccountingUnitOfWork) -> Entry:
    """``post_entry``, with the body's lines made ``Line``s."""
    lines = tuple(LineIn.model_validate(line).to_line() for line in command.lines)
    return await post_entry(dataclasses.replace(command, lines=lines), uow)


async def place_from_body(command: PlaceHold, uow: AccountingUnitOfWork) -> Hold:
    """``place_hold``, with the body's amount made ``Money``."""
    return await place_hold(
        dataclasses.replace(command, amount=_money(command.amount)), uow
    )


async def capture_from_body(command: CaptureHold, uow: AccountingUnitOfWork) -> Hold:
    """``capture_hold``, with the body's amount made ``Money``."""
    return await capture_hold(
        dataclasses.replace(command, amount=_money(command.amount)), uow
    )


accounts = ResourceRouter(
    "/accounts",
    model=Account,
    schema=AccountOut,
    present=AccountOut.of,
    unit_of_work=AccountingUnitOfWork,
    repository=lambda uow: uow.accounts,
    filters=AccountFilters,
    pagination=PAGES,
    rule=ModelActions(Account),
    tags=TAGS,
)
"""The chart of accounts: list, read, open, close, and reconcile."""
accounts.command_route(OpenAccount, service=open_account, body=AccountIn)
accounts.command_route(
    CloseAccount, service=close_account, path="/close", action="close"
)

entries = ResourceRouter(
    "/entries",
    model=Entry,
    schema=EntryOut,
    present=EntryOut.of,
    unit_of_work=AccountingUnitOfWork,
    repository=lambda uow: uow.entries,
    filters=EntryFilters,
    pagination=PAGES,
    rule=ModelActions(Entry),
    tags=TAGS,
)
"""Journal entries: list, read, post, and reverse; never change or delete."""
entries.command_route(PostEntry, service=post_from_body, body=EntryIn)
entries.command_route(
    ReverseEntry,
    service=reverse_entry,
    body=ReversalIn,
    path="/reverse",
    action="reverse",
    status_code=status.HTTP_201_CREATED,
)

holds = ResourceRouter(
    "/holds",
    model=Hold,
    schema=HoldOut,
    present=HoldOut.of,
    unit_of_work=AccountingUnitOfWork,
    repository=lambda uow: uow.holds,
    filters=HoldFilters,
    pagination=PAGES,
    rule=ModelActions(Hold),
    tags=TAGS,
)
"""Holds: list, read, place, capture, and release."""
holds.command_route(PlaceHold, service=place_from_body, body=HoldIn)
holds.command_route(
    CaptureHold,
    service=capture_from_body,
    body=CaptureIn,
    path="/capture",
    action="capture",
)
holds.command_route(
    ReleaseHold, service=release_hold, path="/release", action="release"
)

periods = ResourceRouter(
    "/periods",
    model=Period,
    schema=PeriodOut,
    present=PeriodOut.of,
    unit_of_work=AccountingUnitOfWork,
    repository=lambda uow: uow.periods,
    filters=PeriodFilters,
    pagination=PAGES,
    rule=ModelActions(Period),
    tags=TAGS,
)
"""Accounting periods: list, read, open, close, and their reports."""
periods.command_route(OpenPeriod, service=open_period, body=PeriodIn)
periods.command_route(ClosePeriod, service=close_period, path="/close", action="close")

REPORTS: Final = (JSON, CSV)
_reports = error_responses(401, 403, 404, 406)


@periods.get(
    "/{period_id}/trial-balance",
    name="accounting_trial_balance",
    responses={
        **_reports,
        200: {"content": {CSV: {}}, "description": "The trial balance."},
    },
    dependencies=[_rule("view", Period)],
)
async def read_trial_balance(
    period_id: UUID, accept: Annotated[str | None, Header()] = None
) -> Response:
    """The period's trial balance, as stable JSON or CSV (``Accept``).

    Its totals, one per currency, net zero in balanced books.
    """
    media = negotiate(accept, REPORTS)
    report = await trial_balance(PeriodId(period_id))
    if media == CSV:
        return Response(report.to_csv(), media_type=CSV, headers={"Vary": "Accept"})
    return Response(report.to_json(), media_type=JSON, headers={"Vary": "Accept"})


@periods.get(
    "/{period_id}/general-ledger",
    name="accounting_general_ledger",
    responses={
        **_reports,
        200: {"content": {CSV: {}}, "description": "The general ledger."},
    },
    dependencies=[_rule("view", Period)],
)
async def read_general_ledger(
    period_id: UUID, accept: Annotated[str | None, Header()] = None
) -> StreamingResponse:
    """The postings dated in the period, streamed as stable JSON or CSV (``Accept``)."""
    media = negotiate(accept, REPORTS)
    stream = general_ledger(PeriodId(period_id), "csv" if media == CSV else "json")
    first = await anext(stream)  # A missing period is a 404 before the answer starts.

    async def rest() -> Any:  # noqa: ANN401 - the stream's pieces.
        yield first
        async for piece in stream:
            yield piece

    return StreamingResponse(rest(), media_type=media, headers={"Vary": "Accept"})


class ImportedOut(BaseModel):
    """What an import stored: new lines, and lines it had already."""

    imported: int
    skipped: int


class BreakOut(BaseModel):
    """A statement line without a posting, or a posting without a line."""

    kind: Literal["unmatched_line", "unmatched_posting"]
    statement_line_id: UUID | None
    posting_id: UUID | None
    booked_on: date
    amount: MoneyOut
    """Signed as the statement's: positive is a debit to the account."""
    reference: str


class ReconciledOut(BaseModel):
    """What a reconciliation matched, by rule, and the breaks it left."""

    run_id: UUID
    matched: dict[str, int]
    breaks: list[BreakOut]


def _breaks(found: Any) -> list[BreakOut]:  # noqa: ANN401 - breaks.
    """Describe breaks."""
    return [
        BreakOut(
            kind=each.kind,
            statement_line_id=each.statement_line_id,
            posting_id=each.posting_id,
            booked_on=each.booked_on,
            amount=MoneyOut.of(each.amount, each.currency),
            reference=each.reference,
        )
        for each in found
    ]


@accounts.post(
    "/{account_id}/statements",
    name="accounting_import_statement",
    responses=error_responses(401, 403, 404, 413, 422),
    dependencies=[_rule("reconcile", Account)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {CSV: {"schema": {"type": "string"}}},
        }
    },
)
@body_limit(MAX_STATEMENT_BYTES)
async def post_statement(
    account_id: UUID,
    request: Request,
    name: Annotated[str, Query(min_length=1, max_length=128)] = "statement.csv",
) -> ImportedOut:
    """Import a statement's lines (CSV, UTF-8); lines imported before are skipped.

    At most 10 MiB, and ``ACCOUNTING.MAX_STATEMENT_LINES`` lines.
    """
    if request.headers.get("content-type", "").split(";")[0].strip() != CSV:
        raise InvalidStatement("A statement is sent as text/csv.")
    try:
        text = (await request.body()).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidStatement("A statement is UTF-8 text.") from None
    done = await import_statement(AccountId(account_id), CsvStatement(text, name=name))
    return ImportedOut(imported=done.imported, skipped=done.skipped)


@accounts.post(
    "/{account_id}/reconcile",
    name="accounting_reconcile",
    responses=error_responses(401, 403, 404),
    dependencies=[_rule("reconcile", Account)],
)
async def post_reconcile(account_id: UUID) -> ReconciledOut:
    """Match the account's statement lines to its postings; the breaks left."""
    done = await reconcile(AccountId(account_id))
    return ReconciledOut(
        run_id=done.run_id, matched=dict(done.matched), breaks=_breaks(done.breaks)
    )


@accounts.get(
    "/{account_id}/breaks",
    name="accounting_breaks",
    responses=error_responses(401, 403, 404),
    dependencies=[_rule("view", Account)],
)
async def read_breaks(account_id: UUID) -> list[BreakOut]:
    """What the account's last reconciliation left unmatched."""
    return _breaks(await breaks_of(AccountId(account_id)))


router = APIRouter()
"""Every route of the ledger (see the module)."""
for resource in (accounts, entries, holds, periods):
    router.include_router(resource)
