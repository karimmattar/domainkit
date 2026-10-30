"""What the ledger refuses, each with its code and HTTP status.

Messages are fixed sentences, so the HTTP boundary translates them; what a
refusal is about (an account, a currency) is in the request that made it.
"""

from domainkit.ddd import BusinessRuleViolation, Conflict, DuplicateValue, NotFound


class AccountNotFound(NotFound):
    """No account has that id or code."""

    code = "account_not_found"


class EntryNotFound(NotFound):
    """No journal entry has that id."""

    code = "entry_not_found"


class HoldNotFound(NotFound):
    """No hold has that id."""

    code = "hold_not_found"


class PeriodNotFound(NotFound):
    """No accounting period has that id."""

    code = "period_not_found"


class InvalidAmount(BusinessRuleViolation):
    """An amount that is not one of its currency: unknown, inexact, or not positive."""

    code = "invalid_amount"


class InvalidEntry(BusinessRuleViolation):
    """An entry that cannot be posted as written: its lines, or its accounts."""

    code = "invalid_entry"


class UnbalancedEntry(BusinessRuleViolation):
    """An entry whose debits and credits differ in some currency."""

    code = "unbalanced_entry"


class AccountIsClosed(BusinessRuleViolation):
    """A posting or a hold on an account that is closed."""

    code = "account_closed"


class AccountNotEmpty(BusinessRuleViolation):
    """Closing an account that still has a balance, or holds."""

    code = "account_not_empty"


class InsufficientFunds(BusinessRuleViolation):
    """A change that would take an account that may not go negative below zero."""

    code = "insufficient_funds"


class NoOpenPeriod(BusinessRuleViolation):
    """No accounting period covers the entry's date (``REQUIRE_PERIOD``)."""

    code = "no_open_period"


class PeriodIsClosed(BusinessRuleViolation):
    """An entry dated in a period that is closed."""

    code = "period_closed"


class PeriodsOverlap(BusinessRuleViolation):
    """A new period whose dates another period covers, or that ends before it starts."""

    code = "periods_overlap"


class ReferenceReused(Conflict):
    """A source and reference that name an entry or a hold made otherwise."""

    code = "reference_reused"


class HoldNotActive(Conflict):
    """Capturing or releasing a hold that was captured, released, or has expired."""

    code = "hold_not_active"


class InvalidStatement(BusinessRuleViolation):
    """A statement that cannot be imported: its format, or one of its lines."""

    code = "invalid_statement"


class AccountCodeTaken(DuplicateValue):
    """Another account has that code."""

    code = "account_code_taken"


class PeriodNameTaken(DuplicateValue):
    """Another period has that name."""

    code = "period_name_taken"
