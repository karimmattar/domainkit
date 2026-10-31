"""A double-entry ledger: accounts, journal entries, holds, periods, reconciliation.

Install it, then ``makemigrations`` and ``migrate``::

    INSTALLED_DOMAINS = ["domainkit.contrib.accounting", ...]

    cash = await open_account(
        OpenAccount(code="1000", name="Cash", type=AccountType.ASSET, currency="EGP"),
        AccountingUnitOfWork(),
    )
    await post_entry(
        PostEntry(
            source="payments",
            reference=str(payment.id),        # once per source and reference
            booked_on=today,
            lines=(Line.debit(cash.id, amount), Line.credit(sales.id, amount)),
        ),
        AccountingUnitOfWork(),
    )

An entry balances per currency or is refused, and is never changed: a
correction is ``reverse_entry``. Posting locks its accounts in the order
of their ids, and moves their balances in the same transaction. Holds
reserve an amount until captured, released, or expired; periods close.
``trial_balance`` and ``general_ledger`` report a period, as CSV or
stable JSON; ``reconciliation`` matches a bank's statement to the
postings. ``api.router`` serves it all over HTTP, by model permission.
See ``docs/contrib/accounting.md`` and ADRs 0116 and 0117.
"""

from typing import TYPE_CHECKING

from domainkit.utils.module_loading import lazy_names

from .exceptions import (
    AccountCodeTaken,
    AccountIsClosed,
    AccountNotEmpty,
    AccountNotFound,
    EntryNotFound,
    HoldNotActive,
    HoldNotFound,
    InsufficientFunds,
    InvalidAmount,
    InvalidEntry,
    InvalidStatement,
    NoOpenPeriod,
    PeriodIsClosed,
    PeriodNameTaken,
    PeriodNotFound,
    PeriodsOverlap,
    ReferenceReused,
    UnbalancedEntry,
)
from .holds import (
    CAPTURES,
    CaptureHold,
    PlaceHold,
    ReleaseHold,
    capture_hold,
    expire_holds,
    place_hold,
    release_hold,
)
from .hooks import on_posting
from .ledger import (
    REVERSALS,
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
    Account,
    AccountClosed,
    AccountId,
    AccountOpened,
    AccountStatus,
    AccountType,
    Entry,
    EntryId,
    EntryPosted,
    Hold,
    HoldFinished,
    HoldId,
    HoldPlaced,
    HoldStatus,
    Line,
    Period,
    PeriodClosed,
    PeriodId,
    PeriodOpened,
    PeriodStatus,
    Posting,
    Side,
)
from .settings import AccountingSettings
from .uow import AccountingUnitOfWork

if TYPE_CHECKING:
    from .reports import TrialBalance, general_ledger, trial_balance, verify_ledger
    from .tasks import defer_entry

__all__ = [
    "CAPTURES",
    "REVERSALS",
    "Account",
    "AccountClosed",
    "AccountCodeTaken",
    "AccountId",
    "AccountIsClosed",
    "AccountNotEmpty",
    "AccountNotFound",
    "AccountOpened",
    "AccountStatus",
    "AccountType",
    "AccountingSettings",
    "AccountingUnitOfWork",
    "CaptureHold",
    "CloseAccount",
    "ClosePeriod",
    "Entry",
    "EntryId",
    "EntryNotFound",
    "EntryPosted",
    "Hold",
    "HoldFinished",
    "HoldId",
    "HoldNotActive",
    "HoldNotFound",
    "HoldPlaced",
    "HoldStatus",
    "InsufficientFunds",
    "InvalidAmount",
    "InvalidEntry",
    "InvalidStatement",
    "Line",
    "NoOpenPeriod",
    "OpenAccount",
    "OpenPeriod",
    "Period",
    "PeriodClosed",
    "PeriodId",
    "PeriodIsClosed",
    "PeriodNameTaken",
    "PeriodNotFound",
    "PeriodOpened",
    "PeriodStatus",
    "PeriodsOverlap",
    "PlaceHold",
    "PostEntry",
    "Posting",
    "ReferenceReused",
    "ReleaseHold",
    "ReverseEntry",
    "Side",
    "TrialBalance",
    "UnbalancedEntry",
    "capture_hold",
    "close_account",
    "close_period",
    "defer_entry",
    "expire_holds",
    "general_ledger",
    "on_posting",
    "open_account",
    "open_period",
    "place_hold",
    "post_entry",
    "release_hold",
    "reverse_entry",
    "trial_balance",
    "verify_ledger",
]

# Reports and messages are imported on first use: posting needs neither.
__getattr__ = lazy_names(
    __name__,
    {
        "TrialBalance": f"{__name__}.reports",
        "defer_entry": f"{__name__}.tasks",
        "general_ledger": f"{__name__}.reports",
        "trial_balance": f"{__name__}.reports",
        "verify_ledger": f"{__name__}.reports",
    },
)
